"""Tabular reports (one generic shape so the page, the Excel export and the print view share the same data)."""
import io
from collections import defaultdict
from datetime import date, timedelta

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from . import access, analytics_service as an, leave_service, settings as settings_svc

KINDS = {
    "review": "Month-end checklist: what needs a decision",
    "approvals": "Monthly shifts: approval status by team",
    "summary": "Employee attendance summary",
    "scores": "Score summary: each person's score and why points were lost",
    "score_details": "Score details: every problem that cost points",
    "ranking": "Punctuality & attendance score ranking",
    "teams": "Team comparison",
    "departments": "Department comparison",
    "lateness": "Repeated lateness",
    "hours": "Working-hours compliance",
    "punches": "Missing IN / OUT and forgotten-punch reports",
    "leave": "Leave balances and upcoming leave",
    "comp": "Compensatory days",
    "coverage": "Weekly shift coverage & availability",
    "rest": "Weekly rest-day compliance",
    "permits": "Attendance permits (اذن حضور / اذن انصراف)",
    "planned_actual": "Planned schedule vs actual attendance",
    "trends": "Monthly trends",
    "daily": "Daily attendance detail",
    "changes": "Schedule changes: who changed which shift",
}

# one sentence under each report: what it is for
DESC = {
    "review": "Everything a manager still has to decide before the month is closed: absences to review, permits to review, missing punches.",
    "approvals": "Which team has approved the shifts of which month, who approved, and whether anything changed after the approval.",
    "summary": "One line per employee: days, attendance, lateness, leave, hours, permits needed and the score.",
    "ranking": "The same figures ordered by score - the best first; people with too little data go last.",
    "scores": "One line per employee, the lowest score first: the score, the points lost per problem (late, early, absence, missing punch, rejected permit) and the reason in words.",
    "score_details": "Every late arrival, early departure, absence, missing punch or rejected permit that cost points: the date, the detail and the points - to answer “why is my score lower than 100?”.",
    "teams": "Teams side by side: attendance %, lateness, absences, missing punches, permits to review and the average score.",
    "departments": "Departments side by side (same figures as the team comparison).",
    "lateness": "Who is late and how often; “Repeated” = at least the number of late arrivals set in Settings.",
    "hours": "Scheduled hours against the hours really worked (days with both punches).",
    "punches": "Days without a check-in or a check-out, and the forgotten-punch reports.",
    "leave": "Leave taken, balances left and upcoming approved leave.",
    "comp": "Compensatory days earned, taken and still owed.",
    "coverage": "For every day: how many are planned on each shift and how many are really available.",
    "rest": "Planned days off against days off really rested.",
    "permits": "Every late arrival / early departure that needs (or has) a permit, and its review.",
    "planned_actual": "What was planned for each employee against what happened.",
    "trends": "Month by month: attendance %, lateness, absences, leave and score.",
    "daily": "Every employee, every day: the planned shift, the punches and the status - to check a single day.",
    "changes": "Every shift a manager changed in the portal, with the reason - these shifts decide the shift allowance.",
}
TOTAL_KINDS = {"score_details", "summary", "ranking", "planned_actual", "teams", "departments", "lateness", "hours", "punches", "leave", "comp", "permits"}


def _months(d1, d2):
    d = date.fromisoformat(d1).replace(day=1)
    end = date.fromisoformat(d2)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield d.isoformat(), min((nxt - timedelta(days=1)), end).isoformat()
        d = nxt


def _totals(rep, kind):
    """A total line for the numeric columns (not for percentages, averages and scores)."""
    if kind not in TOTAL_KINDS or not rep["rows"]:
        return None
    first = 3 if rep["emp_col"] is not None else 1
    out = ["Total"] + [""] * (len(rep["columns"]) - 1)
    for i, name in enumerate(rep["columns"]):
        if i < first or any(w in name for w in ("%", "Avg", "Score", "Category", "notes", "Repeated", "Compliance", "Date", "Status")):
            continue
        vals = [r[i] for r in rep["rows"]]
        if vals and all(isinstance(v, (int, float)) or v is None for v in vals) and any(isinstance(v, (int, float)) for v in vals):
            out[i] = round(sum(v for v in vals if isinstance(v, (int, float))), 1)
    return out


def build(repo, user, kind, d1, d2, now, team=None):
    """-> {'title','columns','rows','emp_col','notes','description','totals'}"""
    rep = _build(repo, user, kind, d1, d2, now, team)
    rep["description"] = DESC.get(kind, "")
    rep["totals"] = _totals(rep, kind)
    return rep


def _scope(repo, user, team):
    emps = access.visible_employees(repo, user)
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    return emps


def _build_new(repo, user, kind, d1, d2, now, team):
    title, notes = KINDS[kind], []
    emps = _scope(repo, user, team)
    by = {e["id"]: e for e in emps}
    if kind == "review":
        ev = an.evaluate_range(repo, list(by), d1, d2, now)
        body = []
        for emp, days in ev.items():
            e = by[emp]
            for d in days:
                if d["status"] == "absent" and "absence_verified" not in d["flags"]:
                    body.append([emp, e["name"], e["team"], d["date"], "Absence to review", "No punch, leave or mission: a manager decides excused / unexcused"])
                if d.get("late_permit") == "required":
                    body.append([emp, e["name"], e["team"], d["date"], "Late arrival: permit to review", f"{d['late_raw_minutes']} min after the shift start (اذن حضور)"])
                if d.get("early_permit") == "required":
                    body.append([emp, e["name"], e["team"], d["date"], "Early departure: permit to review", f"{d['early_raw_minutes']} min before the shift end (اذن انصراف)"])
                if d["status"] == "missing_punch":
                    body.append([emp, e["name"], e["team"], d["date"], "Missing punch", "check-in not recorded" if "missing_in" in d["flags"] else "check-out not recorded"])
        body.sort(key=lambda r: (r[3], r[0]))
        notes.append("Open the employee (the ID) to review an absence or a permit. A missing permit or an unreviewed absence never lowers a score by itself.")
        return {"title": title, "columns": ["ID", "Employee", "Team", "Date", "To decide", "Detail"], "rows": body, "emp_col": 0, "notes": notes}
    if kind == "daily":
        ev = an.evaluate_range(repo, list(by), d1, d2, now)
        body = []
        for emp in sorted(ev, key=lambda i: (by[i]["name"], i)):
            e = by[emp]
            for d in ev[emp]:
                if d["status"] in ("no_schedule", "scheduled") and not d.get("actual_in"):
                    continue
                body.append([d["date"], date.fromisoformat(d["date"]).strftime("%a"), emp, e["name"], e["team"], "+".join(d["planned_codes"]) or "—",
                             (d.get("actual_in") or "")[:5], (d.get("actual_out") or "")[:5], d["status"].replace("_", " "), d["late_minutes"] or "",
                             d["early_minutes"] or "", ", ".join(f.replace("_", " ") for f in d["flags"])])
        body.sort(key=lambda r: (r[0], r[3]))
        if len(body) > 6000:
            notes.append(f"Only the first 6000 of {len(body)} rows are shown; choose a shorter period or a team.")
            body = body[:6000]
        return {"title": title, "columns": ["Date", "Day", "ID", "Employee", "Team", "Shift", "Check-in", "Check-out", "Status", "Late min", "Early min", "Flags"],
                "rows": body, "emp_col": 2, "notes": notes}
    if kind == "changes":
        body = []
        for a in repo.list_audit(action="schedule_changed", limit=5000):
            day = str(a["ts"])[:10]
            try:
                emp = int(a["entity_id"])
            except (TypeError, ValueError):
                continue
            if not (d1 <= day <= d2) or emp not in by:
                continue
            det = a.get("detail") or {}
            fmt = lambda v: ", ".join(v) if isinstance(v, (list, tuple)) else str(v or "—")
            body.append([str(a["ts"])[:16], a.get("username") or a.get("user_id"), emp, by[emp]["name"], by[emp]["team"], det.get("date", ""),
                         fmt(det.get("previous")), fmt(det.get("new")), det.get("reason", "")])
        notes.append("A shift changed in the portal (Modification, weekly plan) is the shift counted in the allowance sheet; the reason is mandatory.")
        return {"title": title, "columns": ["When", "Changed by", "ID", "Employee", "Team", "Day changed", "From", "To", "Reason"], "rows": body, "emp_col": 2, "notes": notes}
    if kind in ("scores", "score_details"):
        rows, rules, ver = an.team_rows(repo, user, d1, d2, now, team)
        rows.sort(key=lambda r: (r["score"]["insufficient_data"], r["score"]["score"], r["employee"]["id"]))     # the lowest scores first: they need the explanation
        sc0 = rows[0]["score"] if rows else None
        if sc0 and not sc0["configured"]:
            notes.append("Scoring rules are not set by management (Administration → Scoring rules): nobody loses points and nobody has a category.")
        elif sc0:
            notes.append(f"How to read it: Score = {sc0['start']:g} − the points lost. Points are lost only for the problems below, each by the rules saved in Scoring rules "
                         "(a late arrival by its minutes band, plus extra for repeats; a verified unexcused absence; a missing punch; a rejected permit), "
                         "never above the limit of each problem or the overall limit.")
            for t in sc0["rules_text"]:
                if t["text"] or t["repeat"]:
                    notes.append(f"{t['label']}: " + "; ".join(x for x in (t["text"], t["repeat"], t["cap"]) if x))
        if kind == "score_details":
            body = []
            for r in rows:
                e = r["employee"]
                for d in r["score"]["deductions"]:
                    for it in d["items"]:
                        body.append([e["id"], e["name"], e["team"], it["date"], d["label"], it["detail"], round(it["base"], 2), round(it["points"] - it["base"], 2), round(it["points"], 2)])
            body.sort(key=lambda x: (x[3], x[0]))
            notes.append("One line per lost point. Penalty = base + extra for repeats. A problem that reached its limit shows its lines in full, but the person loses only up to the limit (see Score summary).")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Date", "Problem", "Detail", "Base %", "Repeat extra %", "Points lost"], "rows": body, "emp_col": 0, "notes": notes}
        body = []
        for r in rows:
            s, sc, e = r["summary"], r["score"], r["employee"]
            lost = {d["kind"]: round(d["total"], 2) for d in sc["deductions"]}
            why = "; ".join(f"{d['label']} ×{d['count']} = −{d['total']:g}" for d in sc["deductions"] if d["total"]) or ("Nothing lost" if sc["configured"] else "Rules not set")
            if sc["caps_applied"]:
                why += " | limit: " + "; ".join(sc["caps_applied"])
            body.append([e["id"], e["name"], e["team"], sc["score"], sc["start"], round(sc["total_deduction"], 2), sc["category"] or ("Insufficient data" if sc["insufficient_data"] else "—"),
                         lost.get("late", 0), lost.get("early", 0), lost.get("absence", 0), lost.get("missing_punch", 0), lost.get("rejected_permit", 0),
                         s["judged_days"], why, "; ".join(sc["notes"])])
        return {"title": title, "columns": ["ID", "Employee", "Team", "Score", "Start score", "Points lost", "Category", "Lost: late", "Lost: early", "Lost: absence",
                                            "Lost: missing punch", "Lost: rejected permit", "Days judged", "Why points were lost", "Data notes"], "rows": body, "emp_col": 0, "notes": notes}
    # approvals
    from . import allowance_service as asv, shifts_service
    teams = sorted({t for e in emps for t in e["teams"]}, key=str.lower)
    body = []
    approvals = repo.shift_approvals()
    for r in shifts_service.statuses(repo, asv.months_between(d1, d2), teams):
        rec = approvals.get(f"{r['month']}|{r['team']}") or {}
        label = {"approved": "Approved", "changed": "Changed after approval", "pending": "Waiting for the manager"}[r["status"]]
        body.append([r["month"], r["team"], label, r["by"] or "", r["at"], rec.get("employees", ""), rec.get("days", "")])
    notes.append("The shift-allowance sheet of the administrator contains a team's month only once the manager approved it (Monthly shifts).")
    return {"title": title, "columns": ["Month", "Team", "Status", "Approved by", "When", "People", "Counted days"], "rows": body, "emp_col": None, "notes": notes}


def _build(repo, user, kind, d1, d2, now, team=None):
    if kind in ("review", "daily", "changes", "approvals", "scores", "score_details"):
        return _build_new(repo, user, kind, d1, d2, now, team)
    title = KINDS.get(kind, kind)
    notes = []
    if kind in ("summary", "ranking", "lateness", "hours", "punches", "planned_actual", "teams", "departments", "rest", "permits"):
        rows, rules, ver = an.team_rows(repo, user, d1, d2, now, team)
        if kind in ("summary", "ranking", "planned_actual"):
            if not (rows and rows[0]["score"]["configured"]) and kind != "planned_actual":
                notes.append("Scoring rules are not configured by management: scores are shown without penalties and categories.")
            if kind == "ranking":      # people with too little data to judge go last instead of topping the list with 100
                rows.sort(key=lambda r: (r["score"]["insufficient_data"], -r["score"]["score"], r["employee"]["id"]))
            cols = ["ID", "Employee", "Team", "Scheduled days", "Present", "Absent (unverified)", "Leave", "Rest days",
                    "Attendance %", "Late #", "Late min", "Early #", "Missing IN", "Missing OUT", "Missing both",
                    "Sched. hours", "Actual hours", "Diff hours", "Permit needed: late (اذن حضور)", "Permit needed: early (اذن انصراف)",
                    "Score", "Category", "Data notes"]
            body = []
            for r in rows:
                s, sc, e = r["summary"], r["score"], r["employee"]
                flag = "; ".join(sc["notes"][:1])
                body.append([e["id"], e["name"], e["team"], s["scheduled_days"], s["present_days"], s["absent_days"], s["leave_days"],
                             s["rest_days"], s["attendance_pct"], s["late_count"], s["late_minutes"], s["early_count"],
                             s["missing_in"], s["missing_out"], s["missing_both"], s["scheduled_hours"], s["actual_hours"],
                             s["hours_diff"], s["late_permit_required"], s["early_permit_required"], sc["score"], sc["category"] or ("Insufficient data" if sc["insufficient_data"] else "—"), flag])
            if kind == "planned_actual":
                cols = cols[:3] + ["Planned shifts (days)", "Attended", "Absent (unverified)", "Late #", "Early #", "Sched. hours",
                                   "Actual hours", "Diff hours", "Exceptions"]
                body = [[r["employee"]["id"], r["employee"]["name"], r["employee"]["team"], r["summary"]["scheduled_days"],
                         r["summary"]["present_days"], r["summary"]["absent_days"], r["summary"]["late_count"],
                         r["summary"]["early_count"], r["summary"]["scheduled_hours"], r["summary"]["actual_hours"],
                         r["summary"]["hours_diff"], r["summary"]["exceptions"]] for r in rows]
            return {"title": title, "columns": cols, "rows": body, "emp_col": 0, "notes": notes}
        if kind == "permits":
            ids = [r["employee"]["id"] for r in rows]
            names = {r["employee"]["id"]: r["employee"] for r in rows}
            ev = an.evaluate_range(repo, ids, d1, d2, now)
            labels = {"required": "Required - not reviewed", "provided": "Permit on file", "excused": "Excused by review",
                      "rejected": "Rejected by review"}
            body = []
            for emp, days in ev.items():
                for d in days:
                    if d.get("late_permit"):
                        body.append([emp, names[emp]["name"], names[emp]["team"], d["date"], "اذن حضور (late arrival)", d["expected_start"],
                                     (d["actual_in"] or "")[:5], d["late_raw_minutes"], labels[d["late_permit"]]])
                    if d.get("early_permit"):
                        body.append([emp, names[emp]["name"], names[emp]["team"], d["date"], "اذن انصراف (early departure)", d["expected_end"],
                                     (d["actual_out"] or "")[:5], d["early_raw_minutes"], labels[d["early_permit"]]])
            body.sort(key=lambda x: (x[8] != labels["required"], x[3], x[0]))
            notes.append("Thresholds come from Settings (default 15 minutes, separately for arrival and departure). A missing permit never makes anyone "
                         "absent and never lowers a score by itself; the exception stays Required until a manager reviews it.")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Date", "Permit", "Shift time", "Actual", "Minutes", "Status"],
                    "rows": body, "emp_col": 0, "notes": notes}
        if kind == "rest":
            comp = defaultdict(lambda: {"earned": 0.0, "taken": 0.0})
            for c in repo.comp_days([r["employee"]["id"] for r in rows]):
                if d1 <= c["earned_date"] <= d2:
                    comp[c["employee_id"]]["earned"] += c["days"]
                if c["taken_date"] and d1 <= c["taken_date"] <= d2:
                    comp[c["employee_id"]]["taken"] += c["days"]
            body = []
            for r in rows:
                s, e = r["summary"], r["employee"]
                off, worked = s["planned_off"], s["worked_on_rest_day"]
                if not (off or worked or comp[e["id"]]["earned"] or comp[e["id"]]["taken"]):
                    continue
                body.append([e["id"], e["name"], e["team"], off, max(off - worked, 0), worked,
                             round(100 * max(off - worked, 0) / off, 1) if off else None, comp[e["id"]]["earned"], comp[e["id"]]["taken"]])
            notes.append("Planned days off come from the schedule (OFF). Days worked on a planned day off are exceptions; replacement days are the compensatory days recorded as taken.")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Planned days off", "Rested as planned", "Worked on a day off", "Compliance %",
                                                "Comp. days earned", "Comp. days taken"], "rows": body, "emp_col": 0, "notes": notes}
        if kind == "lateness":
            body = [[r["employee"]["id"], r["employee"]["name"], r["employee"]["team"], r["summary"]["late_count"],
                     r["summary"]["late_minutes"], r["summary"]["avg_late_minutes"], "Yes" if r["summary"]["repeated_lateness"] else "",
                     r["summary"]["absent_days"], r["summary"]["verified_unexcused_days"]]
                    for r in rows if r["summary"]["late_count"] or r["summary"]["absent_days"]]
            body.sort(key=lambda x: (-x[3], -x[4]))
            notes.append("'Repeated' = at least the configured number of late arrivals in the period (Settings).")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Late arrivals", "Late minutes", "Avg late min", "Repeated",
                                                "Absent (unverified)", "Verified unexcused"], "rows": body, "emp_col": 0, "notes": notes}
        if kind == "hours":
            body = [[r["employee"]["id"], r["employee"]["name"], r["employee"]["team"], r["summary"]["scheduled_hours"],
                     r["summary"]["actual_hours"], r["summary"]["hours_diff"],
                     round(100 * r["summary"]["actual_hours"] / r["summary"]["scheduled_hours"], 1) if r["summary"]["scheduled_hours"] else None]
                    for r in rows if r["summary"]["scheduled_hours"]]
            notes.append("Hours are counted only for days with attendance data; actual hours need both IN and OUT.")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Scheduled h", "Actual h", "Difference h", "Compliance %"],
                    "rows": body, "emp_col": 0, "notes": notes}
        if kind == "punches":
            reports = defaultdict(lambda: defaultdict(int))
            for pr in repo.punch_reports(d1, d2, [r["employee"]["id"] for r in rows]):
                reports[pr["employee_id"]][pr["status"]] += 1
            body = [[r["employee"]["id"], r["employee"]["name"], r["employee"]["team"], r["summary"]["missing_in"], r["summary"]["missing_out"],
                     r["summary"]["missing_both"], sum(reports[r["employee"]["id"]].values()),
                     reports[r["employee"]["id"]]["verified"], reports[r["employee"]["id"]]["pending"]]
                    for r in rows if r["summary"]["missing_in"] or r["summary"]["missing_out"] or r["summary"]["missing_both"]
                    or reports[r["employee"]["id"]]]
            notes.append("Missing = no punch in the HR file. 'Reported' = the employee reported forgetting; the two are kept separate.")
            return {"title": title, "columns": ["ID", "Employee", "Team", "Missing IN", "Missing OUT", "Missing both", "Reported forgotten",
                                                "Verified", "Pending"], "rows": body, "emp_col": 0, "notes": notes}
        key = "team" if kind == "teams" else "department"
        comp = an.compare_groups(rows, key)
        return {"title": title, "columns": [key.title(), "Employees", "Scheduled days", "Present", "Absent (unverified)", "Attendance %",
                                            "Late #", "Late min", "Early #", "Missing punches", "Leave days", "Avg score"],
                "rows": [[c["group"], c["employees"], c["scheduled_days"], c["present_days"], c["absent_days"], c["attendance_pct"],
                          c["late_count"], c["late_minutes"], c["early_count"], c["missing_in"] + c["missing_out"] + c["missing_both"], c["leave_days"],
                          c["avg_score"]] for c in comp],
                "emp_col": None, "notes": notes}
    if kind == "leave":
        year = int(d1[:4])
        emps = access.visible_employees(repo, user)
        if team:
            emps = [e for e in emps if access.has_team(e, team)]
        bal = leave_service.balances(repo, user, year, [e["id"] for e in emps])
        types = [t["code"] for t in repo.list_leave_types()]
        cols = ["ID", "Employee", "Team"] + [f"{t.title()} taken" for t in types] + ["Annual remaining", "Casual remaining", "Pending days"]
        body = []
        for e in emps:
            b = bal[e["id"]]
            body.append([e["id"], e["name"], e["team"]] + [b[t]["taken"] for t in types] +
                        [b["annual"]["remaining"], b["casual"]["remaining"], sum(b[t]["pending"] for t in types)])
        up = [r for r in repo.leave_requests([e["id"] for e in emps], now.date().isoformat(), None, "approved")]
        if up:
            names = {e["id"]: e["name"] for e in emps}
            notes.append("Upcoming approved leave: " + "; ".join(f"{names.get(r['employee_id'])} {r['leave_type']} {r['date_from']}→{r['date_to']}"
                                                                for r in up[:30]))
        notes.append("A remaining balance is shown only where an entitlement was entered (otherwise —).")
        return {"title": title, "columns": cols, "rows": body, "emp_col": 0, "notes": notes}
    if kind == "comp":
        emps = access.visible_employees(repo, user)
        if team:
            emps = [e for e in emps if access.has_team(e, team)]
        cb = leave_service.comp_balances(repo, user, [e["id"] for e in emps])
        body = [[e["id"], e["name"], e["team"], cb[e["id"]]["earned"], cb[e["id"]]["taken"], cb[e["id"]]["remaining"],
                 cb[e["id"]]["planned_open"]] for e in emps if cb[e["id"]]["earned"] or cb[e["id"]]["taken"]]
        return {"title": title, "columns": ["ID", "Employee", "Team", "Earned", "Taken", "Remaining", "Planned, not yet taken"],
                "rows": body, "emp_col": 0, "notes": notes}
    if kind == "coverage":
        st = settings_svc.load(repo)
        ids = access.visible_ids(repo, user)
        if team:
            ids = [e["id"] for e in access.visible_employees(repo, user) if access.has_team(e, team)]
        entries = repo.schedule_entries(d1, d2, ids)
        ev = an.evaluate_range(repo, ids, d1, d2, now, st)
        unavailable = defaultdict(int)
        for emp, days in ev.items():
            for d in days:
                if d["status"] in ("on_leave", "absent", "excused_absence"):
                    for c in set(d["planned_codes"]) & {"M", "E", "N"}:      # a double shift is away from both of its shifts
                        unavailable[(d["date"], c)] += 1
        counts = defaultdict(int)
        for e in entries:
            if e["shift_code"] in ("M", "E", "N"):
                counts[(e["work_date"], e["shift_code"])] += 1
        body = []
        d = date.fromisoformat(d1)
        while d <= date.fromisoformat(d2):
            iso = d.isoformat()
            row = [iso, d.strftime("%a")]
            for c in ("M", "E", "N"):
                planned = counts[(iso, c)]
                row += [planned, planned - unavailable[(iso, c)]]
            body.append(row)
            d += timedelta(days=1)
        if not an.staffing_configured(st):
            notes.append("No minimum staffing percentage is configured (Settings), so shortages are not flagged.")
        return {"title": title, "columns": ["Date", "Day", "Morning planned", "Morning available", "Evening planned",
                                            "Evening available", "Night planned", "Night available"], "rows": body, "emp_col": None, "notes": notes}
    if kind == "trends":
        out = []
        for m1, m2 in _months(d1, d2):
            rows, _, _ = an.team_rows(repo, user, m1, m2, now, team)
            sch = sum(r["summary"]["scheduled_days"] for r in rows)
            judged = sum(r["summary"]["judged_days"] for r in rows)        # attendance % of the evaluated days, as everywhere else
            pres = sum(r["summary"]["present_days"] for r in rows)
            out.append([m1[:7], len(rows), sch, pres, round(100 * pres / judged, 1) if judged else None,
                        sum(r["summary"]["late_count"] for r in rows), sum(r["summary"]["late_minutes"] for r in rows),
                        sum(r["summary"]["absent_days"] for r in rows), sum(r["summary"]["leave_days"] for r in rows),
                        round(sum(r["score"]["score"] for r in rows) / len(rows), 1) if rows else None])
        return {"title": title, "columns": ["Month", "Employees", "Scheduled days", "Present", "Attendance %", "Late #", "Late min",
                                            "Absent (unverified)", "Leave days", "Avg score"], "rows": out, "emp_col": None,
                "notes": ["Quarterly and custom periods: choose the From / To dates."]}
    raise KeyError(kind)


def to_xlsx(report, d1, d2):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append([report["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Period: {d1} to {d2}"])
    ws.append([])
    ws.append(report["columns"])
    for c in ws[4]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="006A4E")
    for r in report["rows"]:
        ws.append(r)
    if report.get("totals"):
        ws.append(report["totals"])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
    for n in report["notes"]:
        ws.append([])
        ws.append([n])
    for i, col in enumerate(report["columns"], start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, min(40, len(str(col)) + 4))
    ws.freeze_panes = "A5"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
