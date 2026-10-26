# -*- coding: utf-8 -*-
"""The daily summary e-mail: today's Calls, E-mails, Tickets and Change requests as one dashboard.

The collector's runner sends it at each ``summary.send_times`` checkpoint (e.g. 10:00, 13:00, 15:00) that falls on a
work day, outside the holidays and inside the summary's active hours (System › Settings › Daily summary); "Run now"
sends it at once. At a checkpoint the summary of every team goes to ``to`` / ``cc``, and each team listed in
``team_recipients`` gets its OWN summary with only its own numbers (its people's calls, its e-mails, its tickets, its
ITSM roles). The state ``daily_summary`` remembers the checkpoints - and, within one, the mails - already sent today,
so a retry or a second collector never sends one twice (``force`` sends again).

Change requests: the implementations COMPLETED count today only (the scoreboard's completed columns add up every
earlier day too); the implementation requests PENDING count all of them, whatever day they came.

The numbers are the ones the pages show for every team:
  * Calls     - the Call Log's own day files (logged calls by status, busiest hour, active employees, per team);
  * E-mails   - the e-mail scan with every override applied (business mails, replied, pending, past SLA ...);
  * Tickets   - the SLA: the live Re-Assign queue, and the portal's status history (closed today with the exact
                assignment -> confirmation clock, open / awaiting the reviewer right now). Third Party is left out, like
                on the SLA pages (Settings › Ticketing › Left out of the SLA);
  * CRs       - the ITSM scoreboard by conclusion (pending / completed / rollback) and the CRs completed today.
Counts and team names only: never a person, a subject or a customer. Never raises: a module that cannot be read says so
in its card and the mail still goes.
"""
import time
from datetime import datetime, timedelta

from .. import itsm_records, overlay, performance, schemas, sla_history, teams
from ..settings import DEFAULTS, deep_merge
from .common import FONT_STACK, NBE_GOLD, NBE_GREEN, JobResult, OutlookMailer, esc, outlook_busy, outlook_error_text
from . import email_report as er

STATE_NAME = "daily_summary"                 # {"day": "YYYY-MM-DD", "sent": {slot: iso time}}
STALE_FACTOR = 3                             # a module's data older than 3 scan intervals is shown as stale
# Outlook too busy for a summary («Out of memory or system resources», busy): it is tried again after these pauses
SEND_RETRY_SECONDS = (20, 60)
_sleep = time.sleep


# ------------------------------------------------------------------------------------------------ helpers
def _age_minutes(stamp, now):
    try:
        then = datetime.fromisoformat(str(stamp).replace(" ", "T")[:19])
    except (TypeError, ValueError):
        return None
    return max(0, int((now - then).total_seconds() // 60))


def _stale(hub, module, stamp, now):
    interval = int((hub.get(module) or {}).get("interval_minutes") or 0)
    age = _age_minutes(stamp, now)
    return bool(interval and age is not None and age > STALE_FACTOR * interval + 2)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def _users(data_dir):
    try:
        from ...storage import Store
        return Store(data_dir).users() or {}
    except Exception:                                       # noqa: BLE001 - a summary never fails on the accounts
        return {}


def _team_of_user(users):
    def team_of(uid):
        u = users.get(str(uid or "")) or {}
        return [t for t in overlay.user_teams(u) if t != schemas.ALL_TEAMS_MARK]
    return team_of


# ------------------------------------------------------------------------------------------------ the four blocks
def calls_block(data_dir, users, day, team=None):
    """The day's logged calls: every employee's, or (``team``) those of the team's employees only."""
    from ... import stats as call_stats
    from ...storage import Store
    cs = Store(data_dir)                                    # read only: never init() (it would write default files)
    reasons = {r["key"]: r for r in cs.reasons()}
    records = cs.day_all(day)
    team_of = _team_of_user(users)
    if team:
        records = [r for r in records if any(overlay.team_in((team,), t) for t in team_of(r.get("user_id")))]
    roll = call_stats.rollup_day(records, reasons)
    t = roll.get("totals") or {}
    hours = [sum((h or {}).values()) for h in roll.get("hours") or []]
    busiest = max(range(len(hours)), key=lambda i: hours[i]) if any(hours) else None
    by_team = {}
    for r in records:
        for team in team_of(r.get("user_id")) or ["(no team)"]:
            by_team[team] = by_team.get(team, 0) + 1
    return {"available": True, "total": int(t.get("total") or 0), "resolved": int(t.get("resolved") or 0),
            "transferred": int(t.get("transferred") or 0), "ticket_inquiry": int(t.get("ticket_inquiry") or 0),
            "other": int(t.get("other") or 0),
            "active_employees": sum(1 for u in (roll.get("users") or {}).values() if (u or {}).get("total")),
            "busiest_hour": busiest, "busiest_hour_calls": hours[busiest] if busiest is not None else 0,
            "by_team": by_team}


def email_block(store, hub, day, now, team=None):
    """Today's e-mails with every override applied: every team's, or (``team``) only that team's."""
    snap = store.read_snapshot("email")
    if not snap:
        return {"available": False, "reason": "no e-mail scan yet"}
    if str(snap.get("date") or "")[:10] != day:
        return {"available": False, "reason": "no e-mail scan today yet", "as_of": snap.get("generated_at", "")}
    ovr = {name: store.overrides(name) for name in schemas.OVERRIDES}
    view = overlay.view_email_data(snap.get("data") or {}, ovr, team, now)
    s = view.get("stats") or {}
    by_team = {}
    for r in view.get("rows") or []:
        if r.get("is_carryover"):
            continue
        team = r.get("team") or overlay.UNCLASSIFIED
        b = by_team.setdefault(team, {"received": 0, "replied": 0, "pending": 0})
        b["received"] += 1
        b["replied"] += r.get("status") == schemas.EMAIL_REPLIED
        b["pending"] += r.get("status") == schemas.EMAIL_PENDING
    keys = ("business_emails", "replied", "pending", "no_action_needed", "reply_rate", "sla_breached",
            "sla_compliance_rate", "escalated_count", "carryover_count", "average_response")
    return {"available": True, "as_of": snap.get("generated_at", ""),
            "stale": _stale(hub, "email", snap.get("generated_at"), now),
            **{k: s.get(k, 0) for k in keys}, "by_team": by_team}


def _resolver(store, hub, users):
    apps = []
    try:
        apps = (store.read_auto_assign() or {}).get("applications") or []
    except Exception:                                       # noqa: BLE001
        pass
    return teams.Resolver(hub, users, apps)


def tickets_block(store, hub, users, day, now, team=None):
    """The security requests of ``day``, counted like Insights › Analysis › SLA (``sla_history.status_numbers`` over
    ``performance.sla_scope``): every team's, or (``team``) only that team's - a request's team is the team of the person
    who executed or holds it (their account), else its application's; Third Party always left out. The Live queue
    (Re-Assign page) is counted from its own scan."""
    scope = (team,) if team else None
    sc = performance.sla_scope(store, hub, users, day, day, scope)
    out = {"available": False}
    snap = store.read_snapshot("sla")
    if snap:
        totals = {k: 0 for k in ("open", "within", "reminder", "breached")}
        for r in sc["live"]:
            totals["open"] += 1
            if r.get("status") in totals:
                totals[r["status"]] += 1
        out.update(available=True, as_of=snap.get("generated_at", ""), live=totals,
                   stale=_stale(hub, "sla", snap.get("generated_at"), now))
    if sc["available"]:
        resolver = sc["resolver"]
        nums = sla_history.status_numbers(sc["grid"], sc["records"], sc["cfg"], day, day, live=sc["live"],
                                          live_team=lambda r: resolver.sla_team(teams.sla_system(r), r.get("security_user")))
        whole = nums["times"]["whole"]
        out.update(available=True, history=True, **{k: nums[k] for k in (
                       "came", "executed", "not_executed", "rejected", "waiting", "confirmed", "confirmed_executed",
                       "confirmed_not", "open", "now", "pre", "times", "by_team", "by_person")},
                   sla_pct=whole["within_pct"] if whole["within_pct"] is not None else 0.0, measured=whole["n"],
                   median_minutes=whole["median_minutes"], sla_minutes=sc["cfg"]["sla_minutes"],
                   history_as_of=sc["last_history"])
    if not out.get("available"):
        out["reason"] = "no ticketing scan yet"
    return out


def crs_block(store, hub, day, now, team=None, users=None):
    """The change requests (ITSM): every role's, or (``team``) only the roles of that team. ``pending`` = every
    implementation request waiting on the board, whatever day it came; ``completed_today`` = the implementations
    completed TODAY (the board's completed columns count earlier days too, so they are not used); ``rollback`` = the
    rollback requests waiting."""
    snap = store.read_snapshot("itsm")
    if not snap:
        return {"available": False, "reason": "no ITSM scan yet"}
    data = snap.get("data") or {}
    roles = data.get("roles") or []
    if team:
        resolver = _resolver(store, hub, users or {})
        roles = [r for r in roles if teams.in_scope((team,), resolver.itsm_team(r.get("role")))]
        board = {}
        for r in roles:
            for label, cat in (r.get("categories") or {}).items():
                board[str(label)] = board.get(str(label), 0) + int((cat or {}).get("count") or 0)
    else:
        board = {str(k): int(v or 0) for k, v in (data.get("totals") or {}).items()}
    buckets = {"pending": 0, "completed": 0, "rollback": 0}
    for label, n in board.items():
        buckets[itsm_records.conclusion_for(label)] += n
    names = {str(r.get("role") or "") for r in roles}
    every = (store.read_itsm_records() or {}).get("records") or {}
    started = itsm_records.recording_start(every)
    records = [r for r in every.values() if not team or str(r.get("role") or "") in names]
    completed_today = sum(1 for r in records                 # never the ones already completed at the first scan
                          if r.get("conclusion") == "completed" and itsm_records.completed_day(r, started) == day)
    new_today = sum(1 for r in records if str(r.get("first_seen") or "")[:10] == day)
    failed = sum(1 for r in roles if r.get("error"))
    waiting = {label: n for label, n in board.items() if itsm_records.conclusion_for(label) != "completed"}
    return {"available": True, "as_of": snap.get("generated_at", ""), "stale": _stale(hub, "itsm", snap.get("generated_at"), now),
            "board": waiting, "total": buckets["pending"] + buckets["rollback"], "pending": buckets["pending"],
            "rollback": buckets["rollback"], "completed_today": completed_today, "new_today": new_today,
            "roles_failed": failed, "roles": len(roles)}


def collect(ctx, now, team=None):
    """The four blocks of today - every team's, or (``team``) only that team's (each block on its own: one that fails
    says why, the others still go)."""
    cfg = deep_merge(DEFAULTS["summary"], ctx.settings or {})
    hub = ctx.hub or {}
    want = cfg.get("modules") or {}
    day = now.date().isoformat()
    users = _users(ctx.data_dir)
    jobs = (("calls", lambda: calls_block(ctx.data_dir, users, day, team)),
            ("email", lambda: email_block(ctx.store, hub, day, now, team)),
            ("tickets", lambda: tickets_block(ctx.store, hub, users, day, now, team)),
            ("crs", lambda: crs_block(ctx.store, hub, day, now, team, users)))
    module_of = {"calls": None, "email": "email", "tickets": "sla", "crs": "itsm"}
    out = {"day": day, "at": now.strftime("%Y-%m-%dT%H:%M:%S"), "team": team or ""}
    for key, build in jobs:
        if not want.get(key, True):
            continue
        section = module_of[key]
        if section and not (hub.get(section) or {}).get("enabled", True):
            out[key] = {"available": False, "reason": "switched off in Settings"}
            continue
        try:
            out[key] = build()
        except Exception as exc:                            # noqa: BLE001 - one module never stops the summary
            ctx.log(f"daily summary: the {key} numbers could not be read ({type(exc).__name__}: {exc})")
            out[key] = {"available": False, "reason": "could not be read"}
    return out


# ------------------------------------------------------------------------------------------------ the mail
def _n(block, key):
    return int((block or {}).get(key) or 0)


def build_subject(cfg, now, slot, data):
    parts = []
    for key, label, field in (("calls", "Calls", "total"), ("email", "Mails", "business_emails"),
                              ("tickets", "Tickets", "came"), ("crs", "CRs pending", "pending")):
        block = data.get(key)
        if block is not None:
            parts.append(f"{label} {_n(block, field) if block.get('available') else '-'}")
    team = f" - {data['team']}" if data.get("team") else ""
    head = (f"{cfg.get('subject_prefix') or 'Daily Operations Summary'}{team} - {now:%d/%m/%Y} "
            f"{slot if slot != 'manual' else now.strftime('%H:%M')}")
    return " ".join((head + (" | " + " · ".join(parts) if parts else "")).split())[:240]


def _sub(text, color=er.MUTED):
    return f'<div style="font-size:11.5px;color:{color};margin-top:4px;">{text}</div>'


def _hero(label, value, sub_html, accent):
    return (f'<td width="25%" valign="top" style="padding:5px;">'
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="background:#ffffff;"><tr>'
            f'<td align="left" bgcolor="{er.CARD_BG}" style="background:{er.CARD_BG};border-top:4px solid {accent};'
            f'padding:14px 14px 13px 14px;font-family:{FONT_STACK};">'
            f'<div style="font-size:11px;color:{er.MUTED};text-transform:uppercase;letter-spacing:0.5px;font-weight:700;">'
            f'{esc(label)}</div>'
            f'<div style="font-size:30px;font-weight:700;color:{NBE_GREEN};margin-top:4px;line-height:1.1;">{esc(value)}</div>'
            f'{sub_html}</td></tr></table></td>')


def _facts(rows):
    body = "".join(er._tr(er._td(k), er._td(v, bold=True, align="right")) for k, v in rows)
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
            + body + "</table>")


def _card(title, inner, accent):
    return (f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;"><tr>'
            f'<td bgcolor="#ffffff" style="background:#ffffff;border:1px solid {er.LINE};border-top:3px solid {accent};'
            f'padding:12px 14px;font-family:{FONT_STACK};">'
            f'<div style="font-size:13.5px;font-weight:700;color:{NBE_GREEN};margin-bottom:6px;">{esc(title)}</div>'
            f'{inner}</td></tr></table>')


def _unavailable(block):
    return f'<div style="font-size:12.5px;color:{er.GREY};">{esc((block or {}).get("reason") or "not available")}</div>'


def _clock(stamp):
    s = str(stamp or "").replace("T", " ")
    return s[11:16] if len(s) >= 16 else s


def _minutes_text(m):
    """45 min / 1.5 h / 3 h (a text such as "1h 20m" is shown as it is)."""
    if m is None or m == "":
        return "—"
    try:
        m = float(m)
    except (TypeError, ValueError):
        return str(m)
    return f"{_hours(m)} h" if m >= 90 else f"{round(m)} min"


def _hours(minutes):
    h = round(float(minutes) / 60, 1)
    return int(h) if h == int(h) else h


def build_html(data, cfg, slot, now, next_slot=None):
    calls, mail, tix, crs = data.get("calls"), data.get("email"), data.get("tickets"), data.get("crs")
    heroes, cards, attention = [], [], []

    if calls is not None:
        if calls.get("available"):
            heroes.append(_hero("Calls", calls["total"], _sub(
                f'{calls["resolved"]} resolved &middot; {calls["transferred"]} transferred &middot; '
                f'{calls["ticket_inquiry"]} inquiry'), er.BLUE))
            hour = calls.get("busiest_hour")
            cards.append(_card("Calls", _facts([
                ("Logged today", calls["total"]), ("Resolved", calls["resolved"]), ("Transferred", calls["transferred"]),
                ("Ticket inquiry", calls["ticket_inquiry"]), ("Other", calls["other"]),
                ("Employees logging calls", calls["active_employees"]),
                ("Busiest hour", f"{hour:02d}:00 ({calls['busiest_hour_calls']})" if hour is not None else "—")]), er.BLUE))
        else:
            heroes.append(_hero("Calls", "—", _sub(esc(calls.get("reason", ""))), er.BLUE))
    if mail is not None:
        if mail.get("available"):
            heroes.append(_hero("E-mails", mail["business_emails"], _sub(
                f'{mail["replied"]} replied &middot; {mail["pending"]} pending &middot; {mail["reply_rate"]}% reply rate'),
                er.GREEN))
            cards.append(_card("E-mails", _facts([
                ("Received today", mail["business_emails"]), ("Replied", mail["replied"]), ("Pending", mail["pending"]),
                ("No reply needed", mail["no_action_needed"]), ("Past the SLA", mail["sla_breached"]),
                ("Within SLA", f'{mail["sla_compliance_rate"]}%'), ("Escalated", mail["escalated_count"]),
                ("Carried over from earlier days", mail["carryover_count"]),
                ("Average first reply", _minutes_text(mail.get("average_response")))])
                + er._bar(mail["replied"], max(1, mail["business_emails"]), er.GREEN), er.GREEN))
            if mail["sla_breached"]:
                attention.append(f'{mail["sla_breached"]} e-mail(s) waiting past the SLA')
            if mail["escalated_count"]:
                attention.append(f'{mail["escalated_count"]} e-mail(s) escalated')
            if mail.get("stale"):
                attention.append(f'the e-mail numbers are from {_clock(mail.get("as_of"))} (the scan is late)')
        else:
            heroes.append(_hero("E-mails", "—", _sub(esc(mail.get("reason", ""))), er.GREEN))
    if tix is not None:
        if tix.get("available"):
            live = tix.get("live") or {}
            if tix.get("history"):
                value = tix.get("came", 0)
                within = (f' &middot; {tix["sla_pct"]}% within {_hours(tix["sla_minutes"])} h'
                          if tix.get("measured") else "")
                sub = (f'requests of today &middot; {tix.get("executed", 0)} executed{within} &middot; '
                       f'{live.get("open", 0)} with us now')
            else:
                value, sub = live.get("open", 0), f'open now &middot; {live.get("breached", 0)} past the SLA'
            heroes.append(_hero("Tickets", value, _sub(sub), er.AMBER))
            rows = []
            if tix.get("history"):
                rows += [("Requests of today with the security team («تاريخ الطلب»)", tix.get("came", 0)),
                         ("Executed (تم التنفيذ / التعزيز على التنفيذ)", tix.get("executed", 0)),
                         ("Not executed (لم يتم التنفيذ / التعزيز على عدم التنفيذ)", tix.get("not_executed", 0)),
                         ("Rejected by the assigner (رفض)", tix.get("rejected", 0)),
                         ("Still with a security user", tix.get("open", 0)),
                         ("Waiting for the reviewer's confirmation", tix.get("waiting", 0)),
                         ("Confirmed (تم التعزيز)", tix.get("confirmed", 0)),
                         ("Within the SLA", f'{tix.get("sla_pct", 0)}%' if tix.get("measured") else "—"),
                         ("Median assignment → confirmation", _minutes_text(tix.get("median_minutes"))),
                         ("Requests of today still with the branch (not counted)", tix.get("pre", 0))]
            if live:
                rows += [("Live queue (Re-Assign)", live.get("open", 0)), ("Reminder", live.get("reminder", 0)),
                         ("Past the SLA", live.get("breached", 0))]
            inner = _facts(rows)
            if tix.get("history"):
                inner += er._bar(tix.get("sla_pct", 0), 100, er.GREEN if tix.get("sla_pct", 0) >= 80 else er.AMBER)
                inner += _sub("Counted as the portal's «الاستفسار عن الطلبات» lists the requests of today: "
                              "came = executed + not executed + rejected + still with a security user.")
            cards.append(_card("Tickets (security requests)", inner, er.AMBER))
            if live.get("breached"):
                attention.append(f'{live["breached"]} ticket(s) open past the SLA')
        else:
            heroes.append(_hero("Tickets", "—", _sub(esc(tix.get("reason", ""))), er.AMBER))
    if crs is not None:
        if crs.get("available"):
            heroes.append(_hero("CRs pending", crs["pending"], _sub(
                f'{crs["completed_today"]} implemented today &middot; {crs["rollback"]} rollback waiting'), er.PURPLE))
            board = [(label, n, er.PURPLE) for label, n in sorted(crs["board"].items(), key=lambda kv: -kv[1])]
            cards.append(_card("Change requests (ITSM)", _facts([
                ("Implementation requests pending (all)", crs["pending"]),
                ("Implementations completed today", crs["completed_today"]),
                ("Rollback requests waiting", crs["rollback"]), ("New today", crs["new_today"])])
                + er._bar_table(board, "No change request waiting on the board."), er.PURPLE))
            if crs["rollback"]:
                attention.append(f'{crs["rollback"]} rollback request(s) waiting')
            if crs["roles_failed"]:
                attention.append(f'{crs["roles_failed"]} ITSM role(s) could not be read')
        else:
            heroes.append(_hero("CRs", "—", _sub(esc(crs.get("reason", ""))), er.PURPLE))

    # two cards a row
    grid = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2] + ['&nbsp;'] * (2 - len(cards[i:i + 2]))
        grid.append("<tr>" + "".join(f'<td width="50%" valign="top" style="padding:6px;">{c}</td>' for c in pair) + "</tr>")
    inner = er._kpi_grid(heroes) if heroes else ""
    if attention:
        items = "".join(f'<div style="margin:3px 0;">&#9888; {esc(a)}</div>' for a in attention)
        inner += er._note(f'<div style="font-weight:700;margin-bottom:4px;">Needs attention</div>{items}', color=er.RED,
                          background="#fdf1ef")
    inner += er._heading("By module")
    inner += ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation">' + "".join(grid)
              + "</table>")
    if cfg.get("per_team", True) and not data.get("team"):
        team_rows = _team_rows(data)
        if team_rows:
            inner += er._heading("By team")
            inner += er._table(["Team", "Calls", "E-mails received", "Replied", "Pending"], team_rows)
    as_of = [f"{label} {_clock(b.get('as_of'))}" for label, b in (("e-mail", mail), ("tickets", tix), ("ITSM", crs))
             if b and b.get("as_of")]
    foot = f"Checkpoint {slot if slot != 'manual' else now.strftime('%H:%M')} of {now:%A %d/%m/%Y}."
    if as_of:
        foot += " Data as of: " + ", ".join(as_of) + "."
    if next_slot:
        foot += f" Next checkpoint: {next_slot}."
    foot += " Counts only - open the IAS Portal for the details."
    inner += er._footer(foot)
    subtitle = (f"National Bank of Egypt &nbsp;&bull;&nbsp; {esc(now.strftime('%A %d/%m/%Y'))} &nbsp;&bull;&nbsp; "
                f"{esc(slot if slot != 'manual' else now.strftime('%H:%M'))} checkpoint"
                + (f" &nbsp;&bull;&nbsp; {esc(data['team'])} only" if data.get("team") else ""))
    title = cfg.get("subject_prefix") or "Daily Operations Summary"
    return er._page(f"{title} - {data['team']}" if data.get("team") else title, subtitle, inner)


def _team_rows(data):
    calls = (data.get("calls") or {}).get("by_team") or {}
    mail = (data.get("email") or {}).get("by_team") or {}
    names = sorted(set(calls) | set(mail), key=lambda t: (t in ("(no team)", overlay.UNCLASSIFIED), t.casefold()))
    rows = []
    for t in names:
        m = mail.get(t) or {}
        rows.append(er._tr(er._td(t, bold=True), er._td(calls.get(t, 0), align="right"),
                           er._td(m.get("received", 0), align="right"), er._td(m.get("replied", 0), align="right"),
                           er._td(m.get("pending", 0), align="right")))
    return rows


# ------------------------------------------------------------------------------------------------ the job
def _next_slot_text(cfg, slot, now):
    times = sorted(cfg.get("send_times") or [])
    later = [t for t in times if t > (slot if slot != "manual" else now.strftime("%H:%M"))]
    return later[0] if later else None


def run(ctx):
    """One checkpoint (the runner passes ``options["slot"]``; "Run now" has none and always sends). Never raises."""
    try:
        return _run(ctx)
    except Exception as exc:                                # noqa: BLE001 - the runner must never crash on a summary
        ctx.log(f"daily summary failed: {type(exc).__name__}: {exc}")
        return JobResult(ok=False, error=f"The daily summary could not be built: {type(exc).__name__}: {exc}")


def _parts(cfg):
    """The mails of one checkpoint: [(key, team or None, to, cc)] - every team's summary to ``to`` / ``cc``, then one
    summary of its own for each team of ``team_recipients`` that has an address."""
    out = []
    if cfg.get("to") or cfg.get("cc"):
        out.append(("all", None, list(cfg.get("to") or []), list(cfg.get("cc") or [])))
    for team, rec in sorted((cfg.get("team_recipients") or {}).items(), key=lambda kv: str(kv[0]).casefold()):
        rec = rec or {}
        if str(team).strip() and (rec.get("to") or rec.get("cc")):
            out.append((f"team:{team}", str(team), list(rec.get("to") or []), list(rec.get("cc") or [])))
    return out


def _counts(data):
    return ", ".join(f"{k} {v}" for k, v in (
        ("calls", _n(data.get("calls"), "total")), ("mails", _n(data.get("email"), "business_emails")),
        ("tickets", _n(data.get("tickets"), "came") or _n((data.get("tickets") or {}).get("live"), "open")),
        ("CRs pending", _n(data.get("crs"), "pending"))))


def send_retrying(mailer, log, label, retry, **mail):
    """``mailer.send(**mail)``, tried again after each pause of ``SEND_RETRY_SECONDS`` while Outlook answers that it is
    too busy. ``retry["on"]`` goes off once a mail used up its tries: Outlook is stuck, the next mails try once."""
    pauses = list(SEND_RETRY_SECONDS) if retry["on"] else []
    while True:
        try:
            return mailer.send(**mail)
        except Exception as exc:                            # noqa: BLE001 - raised again when it is not worth a try
            if not outlook_busy(exc):
                raise
            if not pauses:
                if retry["on"]:
                    retry["on"] = False
                    raise RuntimeError(f"{outlook_error_text(exc)} (Outlook said so at each of "
                                       f"{len(SEND_RETRY_SECONDS) + 1} tries over {sum(SEND_RETRY_SECONDS)} s; "
                                       "restarting Outlook on the collector PC frees its memory)") from exc
                raise
            log(f"{label}: Outlook is busy ({outlook_error_text(exc)}) - trying again in {pauses[0]} s")
            _sleep(pauses.pop(0))


def _run(ctx):
    cfg = deep_merge(DEFAULTS["summary"], ctx.settings or {})
    now = ctx.now()
    day = now.date().isoformat()
    options = ctx.options or {}
    slot = str(options.get("slot") or "manual")
    force = bool(options.get("force")) or options.get("trigger") in ("run-now", "cli")
    state = {}
    try:
        state = ctx.store.read_state(STATE_NAME) or {}
    except Exception:                                       # noqa: BLE001
        state = {}
    today = state.get("day") == day
    sent = dict((state.get("sent") if today else {}) or {})
    done = {k: list(v or []) for k, v in ((state.get("parts") if today else {}) or {}).items()}
    if slot != "manual" and slot in sent and not force:
        return JobResult(ok=True, skipped=True, summary=f"the {slot} summary was already sent today")
    parts = _parts(cfg)
    if not parts:
        return JobResult(ok=True, skipped=True,
                         summary="no recipient - add one (To / Cc, or a team) in System › Settings › Daily summary")
    already = set() if (force or slot == "manual") else set(done.get(slot) or [])
    mailer = None
    built, mailed, errors, retry = [], 0, [], {"on": True}

    def remember():
        try:
            ctx.store.write_state(STATE_NAME, {"day": day, "sent": sent, "parts": done})
        except Exception as exc:                            # noqa: BLE001 - the mail went; the runner also marks the slot
            ctx.log(f"daily summary: could not remember what was sent ({exc})")
    for key, team, to, cc in parts:
        if key in already:
            continue
        label = team or "every team"
        data = collect(ctx, now, team)
        html = build_html(data, cfg, slot, now, _next_slot_text(cfg, slot, now))
        subject = build_subject(cfg, now, slot, data)
        built.append(f"{label}: {_counts(data)}")
        if not ctx.can_email():
            continue
        mailer = mailer or ctx.mailer or OutlookMailer()
        try:
            send_retrying(mailer, ctx.log, f"daily summary ({label})", retry, to=to, cc=cc, subject=subject,
                          html_body=html, inline_images=er.mail_images() or None,
                          display_only=bool(cfg.get("display_only")))
        except Exception as exc:                            # noqa: BLE001 - built, not sent: say which of the two
            ctx.log(f"daily summary ({label}) built ({slot}: {_counts(data)}) but Outlook could not send it: "
                    f"{outlook_error_text(exc)}")
            errors.append(f"{label}: {outlook_error_text(exc)}")
            continue
        mailed += 1
        done.setdefault(slot, []).append(key)
        remember()
    if not ctx.can_email():
        return JobResult(ok=True, summary=f"summary built, e-mail off ({slot}): " + "; ".join(built))
    if errors:
        return JobResult(ok=False, emails_sent=mailed,
                         error=(f"The daily summary was built but Outlook could not send {len(errors)} of "
                                f"{len(errors) + mailed} mail(s): " + "; ".join(errors)))
    sent[slot if slot != "manual" else f"manual {now:%H:%M:%S}"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    remember()
    teams_sent = sum(1 for key, *_ in parts if key.startswith("team:"))
    return JobResult(ok=True, emails_sent=mailed,
                     summary=(f"summary sent ({slot}): " + "; ".join(built)
                              + (f" - {teams_sent} team summar{'y' if teams_sent == 1 else 'ies'}" if teams_sent else "")))
