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
import os
import tempfile
import time
from datetime import datetime, timedelta

from .. import itsm_records, overlay, performance, schemas, sla_history, teams
from ..settings import DEFAULTS, deep_merge
from .common import (FONT_STACK, NBE_GOLD, NBE_GREEN, JobResult, OutlookMailer, esc, outlook_busy, outlook_error_text,
                     outlook_memory)
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
    ``performance.sla_scope``): every team's, or (``team``) only that team's - a request's team is its application's,
    never the person's; Third Party always left out of the SLA, only counted (``left_out``: how many of its requests,
    by status, and its tickets on the Re-Assign page - no SLA status, no time). The Live queue (Re-Assign page) is
    counted from its own scan."""
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
                       "confirmed_not", "open", "now", "pre", "times", "by_team", "by_person", "untimed")},
                   sla_pct=whole["within_pct"] if whole["within_pct"] is not None else 0.0, measured=whole["n"],
                   median_minutes=whole["median_minutes"], sla_minutes=sc["cfg"]["sla_minutes"],
                   history_as_of=sc["last_history"])
    if sc["left_grid"] or sc["left_now"]:
        left = sla_history.status_numbers(sc["left_grid"], {}, sc["cfg"], day, day)
        out["left_out"] = {**sla_history.counts_only(left, sc["left_now"]), "teams": sc["left_teams"]}
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


# One look for both mails (owner: "everything the same"): white boxes with a thin border and a gold top line, the four
# headline tiles always the same size, the numbers in NBE green, and the same status colours in the rings and the bars.
GOOD, WARN, BAD, INFO, NEUTRAL = "#1f9d55", "#e5a00d", "#d64545", "#2a78d6", "#9aa5b1"
BORDER, TRACK, HEAD_BG = "#e3e8ef", "#edf1f5", "#f6f8fa"
RING_PX = 100


def _num(value):
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def _share(part, whole):
    return round(part / whole * 100) if whole else 0


def _gap(px):
    return (f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            f'<td height="{px}" style="font-size:1px;line-height:1px;">&nbsp;</td></tr></table>')


def _box(inner, accent=NBE_GOLD, background="#ffffff", padding="14px 16px"):
    # the borders sit on the cell: Outlook's Word engine ignores borders on a <table>
    return (f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            f'<td valign="top" bgcolor="{background}" style="background:{background};border:1px solid {BORDER};'
            f'border-top:3px solid {accent};padding:{padding};font-family:{FONT_STACK};">{inner}</td></tr></table>')


def _row(cells, columns=None):
    """Boxes side by side in ONE table row - so they are always the same height (also in Outlook) - with a 10 px gutter.
    ``columns``: the width is shared as for that many boxes (the missing ones stay empty)."""
    columns = max(columns or len(cells), len(cells))
    width = (100 - 2 * (columns - 1)) // columns
    tds = []
    for i in range(columns):
        if i:
            tds.append('<td width="2%" style="font-size:1px;">&nbsp;</td>')
        if i < len(cells):
            tds.append(f'<td width="{width}%" valign="top" bgcolor="#ffffff" style="background:#ffffff;'
                       f'border:1px solid {BORDER};border-top:3px solid {NBE_GOLD};padding:14px 16px;'
                       f'font-family:{FONT_STACK};">{cells[i]}</td>')
        else:
            tds.append(f'<td width="{width}%">&nbsp;</td>')
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            + "".join(tds) + "</tr></table>")


def _label(text):
    return (f'<div style="font-size:11px;font-weight:700;color:{er.MUTED};text-transform:uppercase;letter-spacing:0.5px;'
            f'white-space:nowrap;">{esc(text)}</div>')


def _big(value, size=28):
    return (f'<div style="font-size:{size}px;font-weight:700;color:{NBE_GREEN};line-height:1.15;margin-top:4px;">'
            f'{esc(_num(value))}</div>')


def _sub(text, color=er.MUTED):
    return f'<div style="font-size:11.5px;line-height:1.45;color:{color};margin-top:5px;">{text}</div>'


def _tile(label, value, sub=""):
    return _label(label) + _big(value) + (_sub(sub) if sub else "")


def _tiles(data):
    """The four headline tiles - the same in both mails."""
    calls, mail, tix, crs = data.get("calls"), data.get("email"), data.get("tickets"), data.get("crs")
    out = []
    if calls is not None:
        # how each total splits is the ring below it (owner): the tile says only what the number is
        out.append(_tile("Calls", calls["total"], "logged today")
                   if calls.get("available") else _tile("Calls", "—", esc(calls.get("reason", ""))))
    if mail is not None:
        out.append(_tile("E-mails", mail["business_emails"], "received today")
                   if mail.get("available") else _tile("E-mails", "—", esc(mail.get("reason", ""))))
    if tix is not None:
        if not tix.get("available"):
            out.append(_tile("Tickets", "—", esc(tix.get("reason", ""))))
        elif tix.get("history"):
            out.append(_tile("Tickets", tix.get("came", 0), "requests of today"))
        else:
            live = tix.get("live") or {}
            out.append(_tile("Tickets open", live.get("open", 0), f'{_num(live.get("breached", 0))} past the SLA'))
    if crs is not None:
        out.append(_tile("CRs pending", crs["pending"], f'{_num(crs["completed_today"])} implemented today')
                   if crs.get("available") else _tile("CRs pending", "—", esc(crs.get("reason", ""))))
    return _row(out) if out else ""


def _cell(text, bold=False, align="left", line=True, width=None, color=None):
    rule = f"border-bottom:1px solid {er.LINE};" if line else ""
    wide = f' width="{width}"' if width else ""
    return (f'<td{wide} align="{align}" valign="middle" dir="auto" style="padding:8px 0;{rule}font-family:{FONT_STACK};'
            f'font-size:{13 if bold else 12.5}px;color:{color or (NBE_GREEN if bold else er.INK)};'
            f'{"font-weight:700;" if bold else ""}">{text}</td>')


def _facts(rows):
    """[(label, value)] as two columns of label / value pairs, every row the same height."""
    out = []
    for i in range(0, len(rows), 2):
        pair = list(rows[i:i + 2]) + [None] * (2 - len(rows[i:i + 2]))
        tds = []
        for j, item in enumerate(pair):
            if item is None:
                tds.append(_cell("&nbsp;", line=False, width="36%") + _cell("&nbsp;", line=False, width="12%"))
            else:
                tds.append(_cell(esc(item[0]), width="36%") + _cell(esc(_num(item[1])), bold=True, align="right",
                                                                    width="12%"))
            if j == 0:
                tds.append('<td width="4%" style="font-size:1px;">&nbsp;</td>')
        out.append("<tr>" + "".join(tds) + "</tr>")
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="border-collapse:collapse;">' + "".join(out) + "</table>")


def _bar(percent, color):
    """A thin bar on a grey track, made of table cells (Outlook ignores CSS widths on divs)."""
    percent = max(0, min(100, int(percent)))
    thin = "font-size:1px;line-height:8px;height:8px;"
    fill = f'<td width="{percent}%" bgcolor="{color}" style="background:{color};{thin}">&nbsp;</td>'
    rest = f'<td bgcolor="{TRACK}" style="background:{TRACK};{thin}">&nbsp;</td>'
    inner = (fill if percent == 100 else rest if percent == 0 else fill + rest)
    return f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>{inner}</tr></table>'


def _meter(caption, percent, color):
    return (_gap(12) + '<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            + _cell(esc(caption), line=False) + _cell(f"{percent}%", bold=True, align="right", line=False)
            + "</tr></table>" + _bar(percent, color))


def _bars(items, empty_text):
    """[(label, value, colour)] as a list of labelled bars."""
    top = max((v for _, v, _ in items), default=0)
    rows = "".join("<tr>" + _cell(esc(label), width="40%")
                   + f'<td width="48%" valign="middle" style="padding:8px 12px;border-bottom:1px solid {er.LINE};">'
                     f'{_bar(max(2, _share(value, top)) if value else 0, color)}</td>'
                   + _cell(esc(_num(value)), bold=True, align="right", width="12%") + "</tr>"
                   for label, value, color in items)
    if not rows:
        return _sub(esc(empty_text), er.GREY)
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="border-collapse:collapse;">' + rows + "</table>")


def _section(title, body, note=""):
    rule = f"padding:0 0 7px 0;border-bottom:2px solid {NBE_GOLD};font-family:{FONT_STACK};"
    head = ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            f'<td style="{rule}font-size:14.5px;font-weight:700;color:{NBE_GREEN};">{esc(title)}</td>'
            f'<td align="right" style="{rule}font-size:11.5px;color:{er.GREY};white-space:nowrap;">{esc(note)}</td>'
            "</tr></table>")
    return _gap(24) + head + body


def _unavailable(block):
    return _gap(8) + _sub(esc((block or {}).get("reason") or "not available"), er.GREY)


def _clock(stamp):
    s = str(stamp or "").replace("T", " ")
    return s[11:16] if len(s) >= 16 else s


def _as_of(block):
    return f'as of {_clock(block.get("as_of"))}' if (block or {}).get("as_of") else ""


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


def _foot(slot, now, next_slot, tail="", as_of=()):
    text = f"Checkpoint {slot if slot != 'manual' else now.strftime('%H:%M')} of {now:%A %d/%m/%Y}."
    if as_of:
        text += " Data as of: " + ", ".join(as_of) + "."
    if next_slot:
        text += f" Next checkpoint: {next_slot}."
    return er._footer(f"{text} {tail}".strip())


def _subtitle(data, slot, now):
    return (f"National Bank of Egypt &nbsp;&bull;&nbsp; {esc(now.strftime('%A %d/%m/%Y'))} &nbsp;&bull;&nbsp; "
            f"{esc(slot if slot != 'manual' else now.strftime('%H:%M'))} checkpoint"
            + (f" &nbsp;&bull;&nbsp; {esc(data['team'])} only" if data.get("team") else ""))


# the portal's status names behind the English words of the Tickets section (a key: each language in its own cell, so
# the Arabic never runs into the English)
TICKET_WORDS = (("Requests of today", "تاريخ الطلب = اليوم"),
                ("Executed", "تم التنفيذ / التعزيز على التنفيذ"),
                ("Rejected", "لم يتم التنفيذ / التعزيز على عدم التنفيذ / رفض موظف الاسناد"),
                ("Confirmed", "تم التعزيز"))


def _ticket_key():
    small = f"padding:3px 0;font-size:11.5px;color:{er.MUTED};font-family:{FONT_STACK};"
    rows = "".join(f'<tr><td width="30%" style="{small}">{esc(en)}</td>'
                   f'<td dir="rtl" align="right" style="{small}">{esc(ar)}</td></tr>' for en, ar in TICKET_WORDS)
    return (_gap(10) + _label("The portal's status names") + _gap(4)
            + f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation">{rows}</table>'
            + _sub("Requests of today = executed + rejected + still with a security user."))


def build_html(data, cfg, slot, now, next_slot=None, images=None):
    mail, tix, crs = data.get("email"), data.get("tickets"), data.get("crs")
    sections = []
    # owner: no "Needs attention" box and no Calls section - the calls are the tile and the ring
    if mail is not None:
        if mail.get("available"):
            body = _gap(4) + _facts([
                ("Received today", mail["business_emails"]), ("Replied", mail["replied"]),
                ("Pending", mail["pending"]), ("No reply needed", mail["no_action_needed"]),
                ("Past the SLA", mail["sla_breached"]), ("Within the SLA", f'{mail["sla_compliance_rate"]}%'),
                ("Escalated", mail["escalated_count"]), ("Carried over from earlier days", mail["carryover_count"]),
                ("Average first reply", _minutes_text(mail.get("average_response")))])
            body += _meter(f'Replied - {_num(mail["replied"])} of {_num(mail["business_emails"])}',
                           _share(mail["replied"], mail["business_emails"]), GOOD)
        else:
            body = _unavailable(mail)
        sections.append(_section("E-mails", body, _as_of(mail)))
    if tix is not None:
        if tix.get("available"):
            live = tix.get("live") or {}
            rows = []
            if tix.get("history"):
                rows += [("Requests of today", tix.get("came", 0)), ("Executed", tix.get("executed", 0)),
                         ("Rejected", tix.get("rejected", 0)), ("Still with a security user", tix.get("open", 0)),
                         ("Waiting for the reviewer", tix.get("waiting", 0)), ("Confirmed", tix.get("confirmed", 0)),
                         ("Within the SLA", f'{tix.get("sla_pct", 0)}%' if tix.get("measured") else "—"),
                         ("Median assignment → confirmation", _minutes_text(tix.get("median_minutes"))),
                         # note 25: the times come only from the requests whose status history is read
                         ("Status history not read yet", (tix.get("untimed") or {}).get("came", 0)),
                         ("Still with the branch (not counted)", tix.get("pre", 0))]
            if live:
                rows += [("Live Re-Assign queue", live.get("open", 0)), ("Reminder", live.get("reminder", 0)),
                         ("Live past the SLA", live.get("breached", 0))]
            body = _gap(4) + _facts(rows)
            if tix.get("history") and tix.get("measured"):
                pct = tix.get("sla_pct", 0)
                body += _meter(f'Within the SLA of {_hours(tix.get("sla_minutes") or 0)} h', pct,
                               GOOD if pct >= 80 else WARN)
            if tix.get("history"):
                body += _ticket_key()
        else:
            body = _unavailable(tix)
        sections.append(_section("Tickets (security requests)", body, _as_of(tix)))
        left = (tix.get("left_out") or {}) if tix.get("available") else {}
        if left.get("came") or left.get("now"):               # counted only: never in the SLA, its times or alerts
            names = ", ".join(left.get("teams") or []) or "Left out of the SLA"
            body = _gap(4) + _facts([
                ("Requests of today", left.get("came", 0)), ("Executed", left.get("executed", 0)),
                ("Rejected", left.get("rejected", 0)), ("Still with a security user", left.get("open", 0)),
                ("Waiting for the reviewer", left.get("waiting", 0)), ("Confirmed", left.get("confirmed", 0)),
                ("On the Re-Assign page now", left.get("now", 0))])
            body += _gap(6) + _sub("Only how many: these requests are not in the SLA, its times or its alerts.")
            sections.append(_section(f"{names} - counted only", body))
    if crs is not None:
        if crs.get("available"):
            board = [(label, n, INFO) for label, n in sorted(crs["board"].items(), key=lambda kv: -kv[1])]
            body = _gap(4) + _facts([
                ("Implementation requests pending (all)", crs["pending"]),
                ("Implementations completed today", crs["completed_today"]),
                ("Rollback requests waiting", crs["rollback"]), ("New today", crs["new_today"])])
            body += _gap(12) + _label("Waiting on the board") + _bars(board, "No change request waiting on the board.")
        else:
            body = _unavailable(crs)
        sections.append(_section("Change requests (ITSM)", body, _as_of(crs)))

    inner = _tiles(data) + _rings(data, images) + "".join(sections)
    if cfg.get("per_team", True) and not data.get("team"):
        team_rows = _team_rows(data)
        if team_rows:
            inner += _section("By team", _gap(4) + _team_table(team_rows))
    as_of = [f"{label} {_clock(b.get('as_of'))}" for label, b in (("e-mail", mail), ("tickets", tix), ("ITSM", crs))
             if b and b.get("as_of")]
    inner += _foot(slot, now, next_slot, "Counts only - open the IAS Portal for the details.", as_of)
    title = cfg.get("subject_prefix") or "Daily Operations Summary"
    return er._page(f"{title} - {data['team']}" if data.get("team") else title, _subtitle(data, slot, now), inner)


def pie_charts(data, folder, slot):
    """The PNG rings of a summary mail: {content_id: path} - calls, e-mails, tickets and the tickets' SLA (see
    ``_ring_segments``)."""
    import os
    from . import charts
    os.makedirs(folder, exist_ok=True)
    tag = "".join(ch for ch in f"{slot}-{data.get('team') or 'all'}" if ch.isalnum() or ch == "-") or "manual"
    return {cid: charts.ring_png(os.path.join(folder, f"summary-{cid[4:]}-{tag}.png"),
                                 [(value, colour) for _, value, colour in rows])
            for cid, _, rows in _ring_segments(data)}


def _legend(rows):
    whole = sum(v for _, v, _ in rows)
    return "".join(
        f'<tr><td width="14" valign="middle" style="padding:5px 0;"><table cellpadding="0" cellspacing="0" border="0" '
        f'role="presentation"><tr><td width="10" height="10" bgcolor="{colour}" style="background:{colour};'
        f'font-size:1px;line-height:10px;">&nbsp;</td></tr></table></td>'
        + _cell(esc(label), line=False)
        + _cell(esc(_num(value)), bold=True, align="right", line=False, width="40")
        + _cell(f"{_share(value, whole)}%", align="right", line=False, width="40", color=er.MUTED) + "</tr>"
        for label, value, colour in rows)


def _ring(cid, title, rows, images):
    """One ring card: the ring and its legend (the total is on the tile above: the card says only how it splits)."""
    img = (f'<img src="cid:{cid}" width="{RING_PX}" height="{RING_PX}" alt="{esc(title)}" '
           f'style="display:block;border:0;">' if cid in (images or {}) else "")
    chart = f'<td width="{RING_PX + 12}" valign="middle">{img}</td>' if img else ""
    return (_label(title) + _gap(10)
            + '<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            + chart + '<td valign="middle"><table width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'role="presentation">{_legend(rows)}</table></td></tr></table>')


def _sla_split(tix):
    """(within, past) of the timed requests of today (``measured`` of them, ``sla_pct`` within the SLA)."""
    n = int(tix.get("measured") or 0)
    within = round(n * float(tix.get("sla_pct") or 0) / 100)
    return within, n - within


def _ring_segments(data):
    """[(content_id, title, [(label, value, colour)])] of the rings of today - the same in both mails (owner: the line
    under each headline number is a ring)."""
    calls, mail, tix = data.get("calls") or {}, data.get("email") or {}, data.get("tickets") or {}
    out = []
    if calls.get("available"):
        out.append(("pie_calls", "Calls", [
            ("Resolved", calls.get("resolved", 0), GOOD), ("Transferred", calls.get("transferred", 0), INFO),
            ("Ticket inquiry", calls.get("ticket_inquiry", 0), WARN), ("Other", calls.get("other", 0), NEUTRAL)]))
    if mail.get("available"):
        out.append(("pie_email", "E-mails", [
            ("Replied", mail.get("replied", 0), GOOD), ("Pending", mail.get("pending", 0), WARN),
            ("No reply needed", mail.get("no_action_needed", 0), NEUTRAL)]))
    if tix.get("available") and tix.get("history"):
        out.append(("pie_tickets", "Tickets", [
            ("Executed", tix.get("executed", 0), GOOD), ("Rejected", tix.get("rejected", 0), BAD),
            ("With a security user", tix.get("open", 0), INFO)]))
        if tix.get("measured"):
            within, past = _sla_split(tix)
            out.append(("pie_sla", "Tickets - SLA", [
                ("Within the SLA", within, GOOD), ("Past the SLA", past, BAD)]))
    return out


def _rings(data, images):
    """The ring cards, two a row."""
    cards = [_ring(cid, title, rows, images) for cid, title, rows in _ring_segments(data)]
    return "".join(_gap(12) + _row(cards[i:i + 2], columns=2) for i in range(0, len(cards), 2))


def build_compact_html(data, cfg, slot, now, images, next_slot=None):
    """The checkpoint mail of EVERY team (owner's rule): the four headline tiles and the rings only - the details are in
    each team's own summary."""
    inner = _tiles(data) + _rings(data, images)
    inner += _foot(slot, now, next_slot)
    return er._page(cfg.get("subject_prefix") or "Daily Operations Summary", _subtitle(data, slot, now), inner)


def _team_rows(data):
    calls = (data.get("calls") or {}).get("by_team") or {}
    mail = (data.get("email") or {}).get("by_team") or {}
    names = sorted(set(calls) | set(mail), key=lambda t: (t in ("(no team)", overlay.UNCLASSIFIED), t.casefold()))
    return [(t, calls.get(t, 0), (mail.get(t) or {}).get("received", 0), (mail.get(t) or {}).get("replied", 0),
             (mail.get(t) or {}).get("pending", 0)) for t in names]


def _team_table(rows):
    heads = ["Team", "Calls", "E-mails received", "Replied", "Pending"]
    th = "".join(f'<th align="{"left" if i == 0 else "right"}" bgcolor="{HEAD_BG}" style="background:{HEAD_BG};'
                 f'padding:8px 10px;font-size:11px;font-weight:700;color:{er.MUTED};text-transform:uppercase;'
                 f'letter-spacing:0.4px;text-align:{"left" if i == 0 else "right"};border-bottom:1px solid {BORDER};'
                 f'font-family:{FONT_STACK};">{esc(h)}</th>' for i, h in enumerate(heads))
    body = "".join("<tr>" + "".join(
        f'<td align="{"left" if i == 0 else "right"}" dir="auto" style="padding:8px 10px;border-bottom:1px solid '
        f'{er.LINE};font-size:12.5px;color:{NBE_GREEN if i else er.INK};{"font-weight:700;" if i == 0 else ""}'
        f'font-family:{FONT_STACK};">{esc(_num(v))}</td>' for i, v in enumerate(r)) + "</tr>" for r in rows)
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" '
            f'style="border-collapse:collapse;"><tr>{th}</tr>{body}</table>')


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
    """``mailer.send(**mail)`` - while Outlook answers that it is too busy (out of memory or resources), tried again
    after each pause of ``SEND_RETRY_SECONDS``, then once more without the inline logo (less for Outlook to do). The
    log and the error say how much memory Outlook uses. ``retry``: shared by the mails of one run - "on" goes off once a
    mail used up its tries (Outlook is stuck: the next mails try once), "light" once Outlook took a mail only without
    the logo (the next ones go without it)."""
    # without the logo only: the charts of the body stay (they are what the mail says)
    charts_only = {k: v for k, v in (mail.get("inline_images") or {}).items() if k != er.LOGO_CID} or None
    light = (dict(mail, inline_images=charts_only, html_body=er.without_inline_logo(mail.get("html_body") or ""))
             if mail.get("inline_images") else None)
    if light and retry.get("light"):
        mail = light
    pauses = list(SEND_RETRY_SECONDS) if retry["on"] else []
    tries, note = 0, None
    while True:
        tries += 1
        try:
            result = mailer.send(**mail)
            if mail is light and not retry.get("light"):
                retry["light"] = True
                log(f"{label}: sent without the logo - Outlook could not take it with the logo attached")
            return result
        except Exception as exc:                            # noqa: BLE001 - raised again when it is not worth a try
            if not outlook_busy(exc):
                raise
            if note is None:
                note = outlook_memory()
            if pauses:
                log(f"{label}: Outlook is busy ({outlook_error_text(exc)}{'; ' + note if note else ''}) - trying again "
                    f"in {pauses[0]} s")
                _sleep(pauses.pop(0))
                continue
            if light and mail is not light and retry["on"]:
                log(f"{label}: Outlook is still busy - trying once more without the logo")
                mail = light
                continue
            if retry["on"]:
                retry["on"] = False
                raise RuntimeError(
                    f"{outlook_error_text(exc)} (Outlook said so at each of {tries} tries over {sum(SEND_RETRY_SECONDS)} s"
                    + (", the last one without the logo" if light else "") + (f"; {note}" if note else "")
                    + ". Close Outlook completely - in Task Manager no OUTLOOK.EXE may be left - open it again, then "
                      "press Run now)") from exc
            raise


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
    built, mailed, errors, retry = [], 0, [], {"on": True, "light": False}

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
        try:                                               # the rings: in both mails
            extra = pie_charts(data, os.path.join(tempfile.gettempdir(), "ias-portal-charts"), slot)
        except Exception as exc:                            # noqa: BLE001 - a chart never stops the summary
            ctx.log(f"daily summary ({label}): the charts could not be drawn ({exc}); sent without them")
            extra = {}
        if team:                                           # a team's own summary: every detail of its numbers
            html = build_html(data, cfg, slot, now, _next_slot_text(cfg, slot, now), extra)
        else:                                              # every team together: the tiles and the rings only
            html = build_compact_html(data, cfg, slot, now, extra, _next_slot_text(cfg, slot, now))
        subject = build_subject(cfg, now, slot, data)
        built.append(f"{label}: {_counts(data)}")
        if not ctx.can_email():
            continue
        mailer = mailer or ctx.mailer or OutlookMailer()
        try:
            send_retrying(mailer, ctx.log, f"daily summary ({label})", retry, to=to, cc=cc, subject=subject,
                          html_body=html, inline_images=er.mail_images(extra) or None,
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
