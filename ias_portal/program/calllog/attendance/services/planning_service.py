"""Workforce planning screens: weekly plan, day view, team availability (manager dashboard), leave & compensatory-day calendar.

Schedule changes by an administrator or a manager are direct (no approval step): every change is applied at once and written to the
audit trail with the previous value, the new value, the reason, the user and the time."""
from collections import defaultdict
from datetime import date, timedelta

from ..domain import daily
from . import access, audit, settings as settings_svc
from .schedule_service import ImportError_, edit_day, shifts_by_code

WORK = ("M", "E", "N")


def week_start_for(day_iso, start_wd):
    d = date.fromisoformat(day_iso)
    return (d - timedelta(days=(d.weekday() - start_wd) % 7)).isoformat()


def _dates(start, n=7):
    d = date.fromisoformat(start)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


def leave_cells(repo, ids, d1, d2):
    """{(emp, date): {'kind', 'state'}} from leave records and compensatory days (planned / taken)."""
    out = {}
    for r in repo.leave_requests(ids, d1, d2):
        if r["status"] not in ("approved", "pending"):
            continue
        d = max(r["date_from"], d1)
        while d <= min(r["date_to"], d2):
            out[(r["employee_id"], d)] = {"kind": r["leave_type"], "state": r["status"], "ref": r["id"]}
            d = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    for c in repo.comp_days(ids):
        if c["taken_date"] and d1 <= c["taken_date"] <= d2:
            out[(c["employee_id"], c["taken_date"])] = {"kind": "comp", "state": "taken", "ref": c["id"]}
        elif c["planned_take_date"] and not c["taken_date"] and d1 <= c["planned_take_date"] <= d2:
            out.setdefault((c["employee_id"], c["planned_take_date"]), {"kind": "comp", "state": "planned", "ref": c["id"]})
    return out


def week_view(repo, user, start, team=None):
    st = settings_svc.load(repo)
    dates = _dates(start)
    emps = access.visible_employees(repo, user)
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    ids = [e["id"] for e in emps]
    entries = repo.schedule_entries(dates[0], dates[-1], ids)
    shifts = shifts_by_code(repo)
    cells = defaultdict(list)
    for e in entries:
        cells[(e["employee_id"], e["work_date"])].append(e)
    on_roster = {e["employee_id"] for e in entries}
    # a manager planning a week sees the whole team (so people can be added); an all-teams viewer sees only the roster
    rows = [e for e in emps if e["id"] in on_roster or team]
    row_ids = {r["id"] for r in rows}
    leave = leave_cells(repo, list(row_ids), dates[0], dates[-1])
    # the default shift (Normal Day) that applies on working days nobody planned: shown as "auto", never written as an explicit entry
    from . import analytics_service as an
    rule = an.DefaultShift(repo, st, shifts)
    defaults = {}
    if rule.on and row_ids:
        day_types = {(r["employee_id"], r["work_date"]): an.norm(r["day_type"]) for r in repo.attendance_records(dates[0], dates[-1], list(row_ids)) if r["day_type"]}
        for emp in row_ids:
            for d in dates:
                if (emp, d) not in cells and rule.applies(d, day_types.get((emp, d), "")):
                    defaults[(emp, d)] = rule.code
    codes = list(WORK) + ([rule.code] if rule.on and rule.code not in WORK else [])
    avail = {}
    for d in dates:
        avail[d] = {}
        for c in codes:
            planned = [e["employee_id"] for e in entries if e["work_date"] == d and e["shift_code"] == c and e["employee_id"] in row_ids]
            planned += [emp for (emp, dd), code in defaults.items() if dd == d and code == c]
            away = sum(1 for emp in set(planned) if (emp, d) in leave and leave[(emp, d)]["state"] != "pending")
            n = len(set(planned))
            pct = round(100.0 * (n - away) / n, 1) if n else None
            thr = settings_svc.threshold_for(st, team, c)
            avail[d][c] = {"planned": n, "on_leave": away, "available": n - away, "pct": pct, "threshold": thr,
                           "short": bool(n) and thr is not None and pct < thr}
    teams = sorted({t for e in access.visible_employees(repo, user) for t in e["teams"]})
    months = sorted({(date.fromisoformat(d).year, date.fromisoformat(d).month) for d in dates})
    return {"start": start, "end": dates[-1], "dates": dates, "rows": rows, "cells": cells, "leave": leave, "avail": avail,
            "defaults": defaults, "avail_codes": codes, "default_code": rule.code if rule.on else None,
            "shifts": shifts, "teams": teams, "team": team or "",
            "versions": {m: repo.active_version(*m) for m in months}, "settings": st}


def _codes(cells, emp, d):
    return sorted(x["shift_code"] for x in cells.get((emp, d), []))


def save_week(repo, user, start, team, wanted, reason):
    """``wanted``: {(employee_id, date): code or ''}. Only cells that differ from the current plan are written; every written cell
    is a normal audited schedule change with the one shared reason. Returns {'changed': n, 'warnings': [...]}."""
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    reason = (reason or "").strip()
    if not reason:
        raise ImportError_("A reason is required for every schedule change.")
    if team and not access.in_scope(user, team):
        raise access.Forbidden("Not your team.")
    dates = _dates(start)
    emps = {e["id"]: e for e in access.visible_employees(repo, user)}
    entries = repo.schedule_entries(dates[0], dates[-1], list(emps))
    cells = defaultdict(list)
    for e in entries:
        cells[(e["employee_id"], e["work_date"])].append(e)
    todo = []
    for (emp, d), code in wanted.items():
        if emp not in emps or d not in dates:
            raise access.Forbidden("That employee or date is outside this week or your scope.")
        if code and _codes(cells, emp, d) != [code]:
            todo.append((emp, d, code))
        elif not code and _codes(cells, emp, d):
            raise ImportError_("A planned day cannot be cleared - choose Rest day (OFF) instead.")
    warnings, changed = [], 0
    with repo.transaction():
        for ym in sorted({(date.fromisoformat(d).year, date.fromisoformat(d).month) for _, d, _ in todo}):
            if not repo.active_version(*ym):
                repo.commit_schedule_version_atomic(ym[0], ym[1], None, [], user["user_id"], replace=False)
                audit(repo, user, "schedule_created_manually", "schedule", f"{ym[0]}-{ym[1]:02d}", {})
        for emp, d, code in todo:
            res = edit_day(repo, user, emp, d, [code], reason)
            warnings += [f"{emps[emp]['name']} {d}: {w}" for w in res["warnings"]]
            changed += 1
    return {"changed": changed, "warnings": warnings}


# ---------------------------------------------------------------- leave & compensatory-day calendar
def leave_calendar(repo, user, year, month, team=None):
    from .schedule_service import month_dates
    from . import analytics_service as an
    st = settings_svc.load(repo)
    dates = month_dates(year, month)
    emps = access.visible_employees(repo, user)
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    ids = [e["id"] for e in emps]
    cells = leave_cells(repo, ids, dates[0], dates[-1])
    # leave recorded only in the HR file (past days) is shown too, as 'hr'
    for r in repo.attendance_records(dates[0], dates[-1], ids):
        if r["leave_text"] and (r["employee_id"], r["work_date"]) not in cells:
            cells[(r["employee_id"], r["work_date"])] = {"kind": an.hr_leave_type(st, r["leave_text"]), "state": "hr", "text": r["leave_text"]}
    rows = [e for e in emps if any((e["id"], d) in cells for d in dates)]
    per_day = {d: sum(1 for e in rows if (e["id"], d) in cells and cells[(e["id"], d)]["state"] != "pending") for d in dates}
    return {"dates": dates, "rows": rows, "cells": cells, "per_day": per_day}


def day_view(repo, user, day, now, team=None):
    """Who works which shift on a day, with availability and short-staffed detection."""
    from . import analytics_service as an
    ov = an.overview(repo, user, day, now, {"team": team} if team else {})
    groups = {c: [] for c in (*WORK, "D", "SB", "OFF")}
    for r in ov["rows"]:
        if r["planned_primary"] in groups:
            groups[r["planned_primary"]].append(r)
    summary = {c: {**ov["shift_stats"][c]} for c in (*WORK, "D")}
    if team:                                   # «name (MFA)» counts for MFA only: not in the evening of Open System
        for c in groups:
            groups[c] = [r for r in groups[c] if team.lower() in {t.lower() for t in access.shift_teams(r.get("teams") or [r["team"]], (r.get("planned_marks") or {}).get(c))}]
    for r in ov["rows"]:                       # a double shift (Morning + Evening) is in both shifts, not only in the first one
        for c in r["planned_codes"][1:]:
            if c in WORK and c != r["planned_primary"] and c in groups:
                groups[c].append({**r, "double": True})
    if team:      # a team filter: staffing for that team only
        for c in summary:
            rs = [s for s in ov["staffing"] if s["team"].lower() == team.lower() and s["shift"] == c]
            if rs:
                summary[c].update(planned=sum(s["planned"] for s in rs), available=sum(s["available"] for s in rs),
                                  pct=rs[0]["pct"], threshold=rs[0]["threshold"], short=any(s["short"] for s in rs))
    unscheduled = [r for r in ov["rows"] if not r["planned_primary"]]
    # everyone who is away for the day - rest day, annual / other leave, holiday - wherever they were planned; then the others without a plan
    away, seen = [], set()
    for r in [*groups["OFF"], *(r for r in ov["rows"] if r["status"] in ("on_leave", "rest_day", "holiday")), *unscheduled]:
        if r["employee_id"] not in seen:
            seen.add(r["employee_id"])
            away.append(r)
    return {"ov": ov, "groups": groups, "summary": summary, "unscheduled": unscheduled, "away": away}


def modify_view(repo, user, team, day, now):
    """Data of the Modification screen: the employees of ``team`` (only those the user may see) with their shift on ``day`` and their
    leave around it. Changes go through the normal audited services (``edit_day``, ``request_leave``, ``cancel_leave``)."""
    from . import analytics_service as an
    ov = an.overview(repo, user, day, now, {"team": team} if team else {})
    td = team_dashboard(repo, user, team or "", day, now, ov=ov)
    rows = sorted(td["rows"], key=lambda r: r["name"])
    d0 = (date.fromisoformat(day) - timedelta(days=14)).isoformat()
    d1 = (date.fromisoformat(day) + timedelta(days=120)).isoformat()
    leaves = defaultdict(list)
    for q in sorted(repo.leave_requests([r["employee_id"] for r in rows], d0, d1), key=lambda q: q["date_from"]):
        if q["status"] in ("approved", "pending"):
            leaves[q["employee_id"]].append(q)
    shifts = shifts_by_code(repo)
    return {"rows": rows, "leaves": leaves, "shifts": [{"code": c, **s} for c, s in sorted(shifts.items(), key=lambda kv: kv[1].get("sort_order", 9))],
            "leave_types": repo.list_leave_types(), "counts": td["counts"]}


def team_summary(repo, rows, shifts, team=None, day=None, now=None):
    """One line per team for Team availability: total employees, how many are present now, how many are scheduled for each working
    shift, how many are on leave (and not arrived / resting), plus the team's managers as NUMBERS (how many the administrator chose in
    Settings and how many of them have arrived; their names are kept in ``managers`` for the team's own page).
    ``rows`` are ``team_dashboard`` rows."""
    from . import analytics_service as an
    managers = an.chosen_managers(repo)
    manager_ids = an.chosen_manager_ids(repo)
    here = {str(r["employee_id"]) for r in rows if r["presence"]["key"] == "present"}       # arrived by HR or by the portal sign-in
    logins = an.todays_logins(repo, day, now) if day and now else {}
    codes = [c for c, s in sorted(shifts.items(), key=lambda kv: kv[1].get("sort_order", 9)) if s["kind"] == "work"]
    by = {}
    for r in rows:
        for t in (r.get("teams") or [r["team"] or "No team"]):
            if team and t.lower() != team.lower():
                continue
            ids = manager_ids.get(t.strip().lower(), [])
            a = by.setdefault(t, {"team": t, "managers": managers.get(t.strip().lower(), []), "manager_count": len(ids),
                                  "manager_arrived": sum(1 for i in ids if i in here or i in logins), "total": 0, "present": 0, "leave": 0,
                                  "not_arrived": 0, "rest": 0, "shifts": {c: 0 for c in codes}})
            a["total"] += 1
            key = r["presence"]["key"]
            a["present"] += key == "present"
            a["not_arrived"] += key == "missing"
            a["leave"] += r["status"] == daily.LEAVE
            a["rest"] += key == "rest"
            for c in dict.fromkeys([r["planned_primary"], *r.get("planned_codes", [])]):
                if c in a["shifts"] and t in access.shift_teams(r.get("teams") or [t], (r.get("planned_marks") or {}).get(c)):
                    a["shifts"][c] += 1
    return {"codes": codes, "rows": [by[k] for k in sorted(by, key=str.lower)],
            "totals": {k: sum(x[k] for x in by.values()) for k in ("total", "present", "leave", "not_arrived", "rest", "manager_count", "manager_arrived")} |
                      {"shifts": {c: sum(x["shifts"][c] for x in by.values()) for c in codes}}}


# ---------------------------------------------------------------- manager team dashboard
PRESENCE = {  # status -> (key, label, tone, icon)
    daily.PRESENT: ("present", "Checked in", "t-ok", "check-circle"), daily.LATE: ("present", "Checked in (late)", "t-warn", "clock"),
    daily.WORKING: ("present", "Working now", "t-ok", "user-check"), daily.MISSION: ("present", "On mission", "t-ok", "user-check"),
    daily.MISSING_PUNCH: ("present", "Missing punch", "t-warn", "alert-circle"), daily.UNSCHEDULED: ("present", "Checked in", "t-ok", "check-circle"),
    daily.NOT_ARRIVED: ("missing", "Not arrived", "t-danger", "user-x"), daily.ABSENT: ("missing", "No check-in", "t-danger", "user-x"),
    daily.EXCUSED: ("excused", "Excused absence", "t-na", "check"), daily.SCHEDULED: ("expected", "Expected", "t-info", "clock"),
    daily.NO_DATA: ("nodata", "No attendance data", "t-na", "info"), daily.LEAVE: ("leave", "On leave", "t-info", "plane"),
    daily.REST: ("rest", "Rest day", "t-na", "coffee"), daily.REST_WORKED: ("present", "Worked on rest day", "t-warn", "alert-triangle"),
    daily.HOLIDAY: ("rest", "Holiday", "t-na", "calendar"), daily.STANDBY: ("rest", "Stand-by", "t-na", "shield"),
    daily.NO_SCHEDULE: ("rest", "Not scheduled", "t-na", "calendar"),
}
PRESENCE_ORDER = {"missing": 0, "expected": 1, "present": 2, "nodata": 3, "excused": 4, "leave": 5, "rest": 6}


def team_dashboard(repo, user, team, day, now, ov=None):
    """Every employee of ``team`` (and only of the teams the user may see) with the approved plan for ``day``: shift, expected hours,
    expected status, actual punches, a presence indicator, exceptions. Leave and rest days are never shown as absence."""
    from . import analytics_service as an
    ov = ov or an.overview(repo, user, day, now, {"team": team} if team else {})
    shifts = ov["shifts"]
    rows = []
    for r in ov["all_rows"]:
        if team and not any(t.lower() == team.lower() for t in r.get("teams", [r["team"]])):
            continue
        key, label, tone, icon = PRESENCE.get(r["status"], ("rest", r["status"], "t-na", "info"))
        if r.get("arrived_login"):                      # signed in to the portal today; the HR file of the day has no check-in yet
            key, label, tone, icon = "present", f"Arrived {r['arrived_login']} (portal sign-in)", "t-ok", "user-check"
        sh = shifts.get(r["planned_primary"]) if r["planned_primary"] else None
        if r["expected_start"]:
            expected = f"{shifts[r['planned_primary']]['name']} {r['expected_start']}–{r['expected_end']} · {r['scheduled_hours']:g} h"
        elif r["status"] == daily.LEAVE:
            expected = "On leave" + (f" ({r['leave_type']})" if r["leave_type"] else "")
        elif r["status"] in (daily.REST, daily.HOLIDAY):
            expected = "Rest day" if r["status"] == daily.REST else "Holiday"
        elif r["status"] == daily.STANDBY:
            expected = "Stand-by (on call)"
        else:
            expected = "Not scheduled"
        rows.append({**r, "presence": {"key": key, "label": label, "tone": tone, "icon": icon}, "expected_text": expected,
                     "shift_name": sh["name"] if sh else ""})
    rows.sort(key=lambda r: (PRESENCE_ORDER[r["presence"]["key"]], r["expected_start"] or "99:99", r["name"]))
    expected_rows = [r for r in rows if r["expected_present"] or r["status"] in (daily.SCHEDULED,)]
    counts = {"employees": len(rows),
              "expected": sum(1 for r in rows if r["planned_primary"] in {c for c, s in shifts.items() if s["kind"] == "work"}
                              and r["status"] not in (daily.LEAVE, daily.HOLIDAY)),
              "present": sum(1 for r in rows if r["presence"]["key"] == "present"),
              "not_arrived": sum(1 for r in rows if r["presence"]["key"] == "missing"),
              "leave": sum(1 for r in rows if r["status"] == daily.LEAVE),
              "rest": sum(1 for r in rows if r["presence"]["key"] == "rest"),
              "exceptions": sum(1 for r in rows if r["late_permit"] == "required" or r["early_permit"] == "required"
                                or r["status"] == daily.MISSING_PUNCH or "worked_on_rest_day" in r["flags"]),
              "late_permit": sum(1 for r in rows if r["late_permit"] == "required"),
              "early_permit": sum(1 for r in rows if r["early_permit"] == "required")}
    stf = [s for s in ov["staffing"] if not team or s["team"].lower() == team.lower()]
    return {"day": day, "team": team, "rows": rows, "counts": counts, "staffing": stf, "short": [s for s in stf if s["short"]],
            "staffing_configured": ov["staffing_configured"], "shifts": shifts, "data_loaded": ov["data_loaded"],
            "expected_rows": expected_rows}
