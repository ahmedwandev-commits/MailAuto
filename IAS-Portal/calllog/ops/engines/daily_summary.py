# -*- coding: utf-8 -*-
"""The daily summary e-mail: today's Calls, E-mails, Tickets and Change requests as one dashboard.

The collector's runner sends it at each ``summary.send_times`` checkpoint (e.g. 10:00, 13:00, 15:00) that falls on a
work day, outside the holidays and inside the summary's active hours (System › Settings › Daily summary); "Run now"
sends it at once. One mail per checkpoint: the state ``daily_summary`` remembers the checkpoints already sent today,
so a retry or a second collector never sends it twice (``force`` sends again).

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
from datetime import datetime, timedelta

from .. import itsm_records, overlay, schemas, sla_history, sla_records, teams
from ..settings import DEFAULTS, deep_merge
from .common import FONT_STACK, NBE_GOLD, NBE_GREEN, JobResult, OutlookMailer, esc
from . import email_report as er

STATE_NAME = "daily_summary"                 # {"day": "YYYY-MM-DD", "sent": {slot: iso time}}
STALE_FACTOR = 3                             # a module's data older than 3 scan intervals is shown as stale


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
def calls_block(data_dir, users, day):
    from ... import stats as call_stats
    from ...storage import Store
    cs = Store(data_dir)                                    # read only: never init() (it would write default files)
    reasons = {r["key"]: r for r in cs.reasons()}
    records = cs.day_all(day)
    roll = call_stats.rollup_day(records, reasons)
    t = roll.get("totals") or {}
    hours = [sum((h or {}).values()) for h in roll.get("hours") or []]
    busiest = max(range(len(hours)), key=lambda i: hours[i]) if any(hours) else None
    by_team = {}
    team_of = _team_of_user(users)
    for r in records:
        for team in team_of(r.get("user_id")) or ["(no team)"]:
            by_team[team] = by_team.get(team, 0) + 1
    return {"available": True, "total": int(t.get("total") or 0), "resolved": int(t.get("resolved") or 0),
            "transferred": int(t.get("transferred") or 0), "ticket_inquiry": int(t.get("ticket_inquiry") or 0),
            "other": int(t.get("other") or 0),
            "active_employees": sum(1 for u in (roll.get("users") or {}).values() if (u or {}).get("total")),
            "busiest_hour": busiest, "busiest_hour_calls": hours[busiest] if busiest is not None else 0,
            "by_team": by_team}


def email_block(store, hub, day, now):
    snap = store.read_snapshot("email")
    if not snap:
        return {"available": False, "reason": "no e-mail scan yet"}
    if str(snap.get("date") or "")[:10] != day:
        return {"available": False, "reason": "no e-mail scan today yet", "as_of": snap.get("generated_at", "")}
    ovr = {name: store.overrides(name) for name in schemas.OVERRIDES}
    view = overlay.view_email_data(snap.get("data") or {}, ovr, None, now)
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


def tickets_block(store, hub, users, day, now):
    sla_cfg = hub.get("sla") or {}
    apps = []
    try:
        apps = (store.read_auto_assign() or {}).get("applications") or []
    except Exception:                                       # noqa: BLE001
        pass
    resolver = teams.Resolver(hub, users, apps)
    excluded = teams.sla_exclusion(sla_cfg, resolver) or (lambda system: False)
    out = {"available": False}
    snap = store.read_snapshot("sla")
    if snap:
        data = snap.get("data") or {}
        live = [r for r in data.get("requests") or [] if not excluded(teams.sla_system(r))]
        totals = {k: 0 for k in ("open", "within", "reminder", "breached")}
        for r in live:
            totals["open"] += 1
            if r.get("status") in totals:
                totals[r["status"]] += 1
        out.update(available=True, as_of=snap.get("generated_at", ""), live=totals,
                   stale=_stale(hub, "sla", snap.get("generated_at"), now))
    cfg = sla_records.clean_config((store.read_sla_records() or {}).get("config"))
    state = store.read_sla_history_state() or {}
    closed = {k: r for k, r in (store.read_sla_closed_day(day) or {}).items() if not excluded(r.get("system"))}
    if state.get("last_history") or closed:
        a = sla_history.analyze(closed, cfg)["overall"]
        grid = {k: g for k, g in (state.get("grid") or {}).items() if not excluded(g.get("system"))}
        nowb = sla_history.right_now(grid, state.get("open") or {}, state.get("index") or {}, cfg,
                                     now.strftime("%Y-%m-%dT%H:%M:%S"))["now"]
        received = sum(1 for g in grid.values() if str(g.get("request_date") or "")[:10] == day)
        within = a.get("sla_pct") or 0.0
        out.update(available=True, history=True, closed_today=a.get("n", 0), executed=a.get("executed", 0),
                   not_executed=a.get("not_executed", 0), sla_pct=within, median_minutes=a.get("median_minutes"),
                   sla_minutes=cfg["sla_minutes"], received_today=received, now=nowb,
                   history_as_of=state.get("last_history", ""))
    if not out.get("available"):
        out["reason"] = "no ticketing scan yet"
    return out


def crs_block(store, hub, day, now):
    snap = store.read_snapshot("itsm")
    if not snap:
        return {"available": False, "reason": "no ITSM scan yet"}
    data = snap.get("data") or {}
    board = {str(k): int(v or 0) for k, v in (data.get("totals") or {}).items()}
    buckets = {"pending": 0, "completed": 0, "rollback": 0}
    for label, n in board.items():
        buckets[itsm_records.conclusion_for(label)] += n
    records = (store.read_itsm_records() or {}).get("records") or {}
    completed_today = sum(1 for r in records.values()
                          if r.get("conclusion") == "completed" and str(r.get("completed_at") or "")[:10] == day)
    new_today = sum(1 for r in records.values() if str(r.get("first_seen") or "")[:10] == day)
    failed = sum(1 for r in data.get("roles") or [] if r.get("error"))
    return {"available": True, "as_of": snap.get("generated_at", ""), "stale": _stale(hub, "itsm", snap.get("generated_at"), now),
            "board": board, "total": sum(board.values()), **buckets, "completed_today": completed_today,
            "new_today": new_today, "roles_failed": failed}


def collect(ctx, now):
    """The four blocks of today (each one on its own: one that fails says why, the others still go)."""
    cfg = deep_merge(DEFAULTS["summary"], ctx.settings or {})
    hub = ctx.hub or {}
    want = cfg.get("modules") or {}
    day = now.date().isoformat()
    users = _users(ctx.data_dir)
    jobs = (("calls", lambda: calls_block(ctx.data_dir, users, day)),
            ("email", lambda: email_block(ctx.store, hub, day, now)),
            ("tickets", lambda: tickets_block(ctx.store, hub, users, day, now)),
            ("crs", lambda: crs_block(ctx.store, hub, day, now)))
    module_of = {"calls": None, "email": "email", "tickets": "sla", "crs": "itsm"}
    out = {"day": day, "at": now.strftime("%Y-%m-%dT%H:%M:%S")}
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
                              ("tickets", "Tickets", "closed_today"), ("crs", "CRs", "total")):
        block = data.get(key)
        if block is not None:
            parts.append(f"{label} {_n(block, field) if block.get('available') else '-'}")
    head = f"{cfg.get('subject_prefix') or 'Daily Operations Summary'} - {now:%d/%m/%Y} {slot if slot != 'manual' else now.strftime('%H:%M')}"
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
            nowb = tix.get("now") or {}
            if tix.get("history"):
                value = tix.get("closed_today", 0)
                sub = (f'closed today &middot; {tix["sla_pct"]}% within {_hours(tix["sla_minutes"])} h'
                       f' &middot; {nowb.get("open", live.get("open", 0))} open now')
            else:
                value, sub = live.get("open", 0), f'open now &middot; {live.get("breached", 0)} past the SLA'
            heroes.append(_hero("Tickets", value, _sub(sub), er.AMBER))
            rows = []
            if tix.get("history"):
                rows += [("Arrived today", tix.get("received_today", 0)), ("Closed today", tix.get("closed_today", 0)),
                         ("Executed", tix.get("executed", 0)), ("Not executed", tix.get("not_executed", 0)),
                         ("Within the SLA", f'{tix.get("sla_pct", 0)}%'),
                         ("Median assignment → confirmation", _minutes_text(tix.get("median_minutes"))),
                         ("Open with a security user now", nowb.get("open", 0)),
                         ("Awaiting the reviewer now", nowb.get("awaiting", 0)), ("On hold now", nowb.get("on_hold", 0))]
            if live:
                rows += [("Live queue (Re-Assign)", live.get("open", 0)), ("Reminder", live.get("reminder", 0)),
                         ("Past the SLA", live.get("breached", 0))]
            inner = _facts(rows)
            if tix.get("history"):
                inner += er._bar(tix.get("sla_pct", 0), 100, er.GREEN if tix.get("sla_pct", 0) >= 80 else er.AMBER)
            cards.append(_card("Tickets (security requests)", inner, er.AMBER))
            if live.get("breached"):
                attention.append(f'{live["breached"]} ticket(s) open past the SLA')
            if nowb.get("breached"):
                attention.append(f'{nowb["breached"]} request(s) with a security user longer than the SLA target')
        else:
            heroes.append(_hero("Tickets", "—", _sub(esc(tix.get("reason", ""))), er.AMBER))
    if crs is not None:
        if crs.get("available"):
            heroes.append(_hero("CRs", crs["total"], _sub(
                f'{crs["completed"]} completed &middot; {crs["rollback"]} rollback &middot; {crs["pending"]} pending'),
                er.PURPLE))
            board = [(label, n, er.PURPLE) for label, n in sorted(crs["board"].items(), key=lambda kv: -kv[1])]
            cards.append(_card("Change requests (ITSM)", _facts([
                ("On the board", crs["total"]), ("Completed today", crs["completed_today"]),
                ("New today", crs["new_today"])]) + er._bar_table(board, "No change requests on the board."), er.PURPLE))
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
    if cfg.get("per_team", True):
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
                f"{esc(slot if slot != 'manual' else now.strftime('%H:%M'))} checkpoint")
    return er._page(cfg.get("subject_prefix") or "Daily Operations Summary", subtitle, inner)


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
    sent = state.get("sent") if state.get("day") == day else {}
    sent = dict(sent or {})
    if slot != "manual" and slot in sent and not force:
        return JobResult(ok=True, skipped=True, summary=f"the {slot} summary was already sent today")
    if not (cfg.get("to") or cfg.get("cc")):
        return JobResult(ok=True, skipped=True, summary="no recipient - add one in System › Settings › Daily summary")
    data = collect(ctx, now)
    html = build_html(data, cfg, slot, now, _next_slot_text(cfg, slot, now))
    subject = build_subject(cfg, now, slot, data)
    counts = ", ".join(f"{k} {v}" for k, v in (
        ("calls", _n(data.get("calls"), "total")), ("mails", _n(data.get("email"), "business_emails")),
        ("tickets", _n(data.get("tickets"), "closed_today") or _n((data.get("tickets") or {}).get("live"), "open")),
        ("CRs", _n(data.get("crs"), "total"))))
    if not ctx.can_email():
        return JobResult(ok=True, summary=f"summary built, e-mail off ({slot}): {counts}")
    mailer = ctx.mailer or OutlookMailer()
    mailer.send(cfg.get("to") or [], cc=cfg.get("cc") or [], subject=subject, html_body=html,
                inline_images=er.mail_images() or None, display_only=bool(cfg.get("display_only")))
    sent[slot if slot != "manual" else f"manual {now:%H:%M:%S}"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        ctx.store.write_state(STATE_NAME, {"day": day, "sent": sent})
    except Exception as exc:                                # noqa: BLE001 - the mail went; the runner also marks the slot
        ctx.log(f"daily summary: could not remember the sent checkpoint ({exc})")
    return JobResult(ok=True, emails_sent=1, summary=f"summary sent ({slot}): {counts}")
