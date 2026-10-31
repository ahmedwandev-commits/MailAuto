"""Planned-vs-actual analytics: daily evaluation over a range, employee summaries, scores, Today's Workforce Overview."""
from collections import defaultdict
from datetime import date, timedelta

from ..domain import daily, scoring
from ..domain.textnorm import norm
from . import access, settings as settings_svc
from .schedule_service import shifts_by_code


def _daterange(d1, d2):
    d = date.fromisoformat(d1)
    end = date.fromisoformat(d2)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def _plus(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def hr_leave_type(st, text):
    """Leave type for the free text of the HR file (see settings.leave_text_map); anything unknown is the configured default."""
    t = norm(text)
    lmap = {norm(k): v for k, v in st["leave_text_map"].items()}
    return next((v for k, v in lmap.items() if k in t), st["default_leave_type_for_hr_text"])


def leave_lookup(repo, st, att_rows, employee_ids, d1, d2):
    """{(emp, date): {'type', 'source'}} from approved leave requests (preferred) and the HR file's leave text."""
    out = {}
    for lr in repo.leave_requests(employee_ids, d1, d2, status="approved"):
        for d in _daterange(max(lr["date_from"], d1), min(lr["date_to"], d2)):
            out[(lr["employee_id"], d)] = {"type": lr["leave_type"], "source": "request"}
    lmap = {norm(k): v for k, v in st["leave_text_map"].items()}
    comp = [norm(x) for x in st.get("comp_day_labels", [])]
    for r in att_rows:
        p = norm(r.get("permit_text"))
        if p and (r["employee_id"], r["work_date"]) not in out and any(c in p for c in comp):
            out[(r["employee_id"], r["work_date"])] = {"type": "comp", "source": "hr_file"}
        t = norm(r.get("leave_text"))
        if t and (r["employee_id"], r["work_date"]) not in out:
            typ = next((v for k, v in lmap.items() if k in t), st["default_leave_type_for_hr_text"])
            out[(r["employee_id"], r["work_date"])] = {"type": typ, "source": "hr_file"}
    return out


def apply_corrections(att_rows, corrections):
    """Overlay corrections (latest wins) on HR rows; a correction may create a row that was missing."""
    by = {(r["employee_id"], r["work_date"]): dict(r) for r in att_rows}
    for c in corrections:
        k = (c["employee_id"], c["work_date"])
        rec = by.setdefault(k, {"employee_id": k[0], "work_date": k[1], "day_type": "", "in_time": None, "out_time": None,
                                "leave_text": "", "permit_text": "", "mission_text": ""})
        rec[c["field"]] = c["new_value"]
        rec["corrected"] = True
    return by


class DefaultShift:
    """The default-shift rule, prepared once: a working day on which nobody assigned Morning / Evening / Night (or a rest day, or
    stand-by) gets the configured default shift (Normal Day 08:00-16:00 unless changed in the settings)."""

    def __init__(self, repo, st, shifts):
        self.code = st.get("default_shift_code")
        self.on = bool(st.get("default_shift_enabled")) and shifts.get(self.code, {}).get("kind") == "work"
        self.hol = {h["date"] for h in repo.list_holidays()}
        self.work_l = {norm(x) for x in st["hr_workday_labels"]}
        self.rest_l = {norm(x) for x in st["hr_weekly_rest_labels"]}
        self.rest_days = {int(x) for x in st.get("weekly_rest_weekdays") or []}

    def holiday(self, d, day_type_norm):
        return d in self.hol or bool(day_type_norm and day_type_norm not in self.work_l and day_type_norm not in self.rest_l)

    def applies(self, d, day_type_norm):
        return self.on and not self.holiday(d, day_type_norm) and is_workday(d, day_type_norm, self.work_l, self.rest_l, self.rest_days)


def evaluate_range(repo, employee_ids, d1, d2, now, st=None):
    """{employee_id: [evaluate_day() dict per date]} for the given employees (team scoping is done by the caller)."""
    st = st or settings_svc.load(repo)
    shifts = shifts_by_code(repo)
    entries = repo.schedule_entries(d1, d2, employee_ids)
    plan = defaultdict(list)
    for e in entries:
        plan[(e["employee_id"], e["work_date"])].append(e)
    att_rows = repo.attendance_records(d1, _plus(d2, 1), employee_ids)
    att = apply_corrections(att_rows, repo.corrections(d1, _plus(d2, 1), employee_ids))
    loaded = repo.dates_with_attendance(d1, _plus(d2, 1))
    leaves = leave_lookup(repo, st, att_rows, employee_ids, d1, d2)
    reviews = repo.absence_reviews(d1, d2, employee_ids)
    permit_rv = defaultdict(dict)
    for (emp, d, kind), decision in repo.exception_reviews(d1, d2, employee_ids).items():
        permit_rv[(emp, d)][kind] = decision
    rule = DefaultShift(repo, st, shifts)
    rest_l, dcode = rule.rest_l, rule.code
    out = {}
    for emp in employee_ids:
        days = []
        for d in _daterange(d1, d2):
            rec = att.get((emp, d))
            dt = norm(rec["day_type"]) if rec and rec.get("day_type") else ""
            holiday = rule.holiday(d, dt)
            planned = plan.get((emp, d), [])
            if not planned and rule.applies(d, dt):
                # nothing explicit (no Morning / Evening / Night, no rest day, no stand-by) on a working day -> the default shift
                planned = [{"employee_id": emp, "work_date": d, "shift_code": dcode, "source": "default"}]
            days.append(daily.evaluate_day({
                "employee_id": emp, "date": d, "planned": planned, "shifts": shifts, "att": rec,
                "att_next": att.get((emp, _plus(d, 1))), "data_loaded": d in loaded, "data_loaded_next": _plus(d, 1) in loaded,
                "leave": leaves.get((emp, d)), "holiday": holiday, "hr_rest": bool(dt and dt in rest_l),
                "review": reviews.get((emp, d)), "permit_reviews": permit_rv.get((emp, d)),
                "settings": st, "now": now}))
        out[emp] = days
    return out


def is_workday(day, day_type_norm, work_labels, rest_labels, rest_weekdays):
    """Is ``day`` a working day for the default-shift rule? The HR calendar wins when it has a row for the day;
    otherwise the configured weekly rest weekdays decide (holidays are excluded by the caller)."""
    if day_type_norm in rest_labels:
        return False
    if day_type_norm in work_labels:
        return True
    return date.fromisoformat(day).weekday() not in rest_weekdays


def rules_for(repo, period_end):
    best = None
    for v in repo.scoring_rule_versions():
        if v["effective_from"] <= period_end:
            best = v
    if best:
        return scoring.normalize(best["rules"]), best["version"]
    return scoring.normalize({}), None


def employee_report(repo, user, employee_id, d1, d2, now):
    emp = access.require_employee(repo, user, employee_id)
    st = settings_svc.load(repo)
    days = evaluate_range(repo, [employee_id], d1, d2, now, st)[employee_id]
    summary = daily.summarize(days, st["repeat_late_threshold"])
    rules, version = rules_for(repo, d2)
    complete = summary["no_data_days"] == 0 and d2 < now.date().isoformat() and not any(
        d["status"] == daily.SCHEDULED for d in days)
    score = scoring.compute_score(days, rules, summary, complete)
    return {"employee": emp, "days": days, "summary": summary, "score": score, "rules_version": version, "settings": st}


def team_rows(repo, user, d1, d2, now, team=None, days_out=None):
    """One summary row per visible employee (used by the reports, rankings and comparisons). ``days_out``: a dict that
    receives the evaluated days {employee_id: [...]}, for a caller that needs them too (no second evaluation)."""
    st = settings_svc.load(repo)
    emps = access.visible_employees(repo, user)
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    ids = [e["id"] for e in emps]
    ev = evaluate_range(repo, ids, d1, d2, now, st)
    if days_out is not None:
        days_out.update(ev)
    rules, version = rules_for(repo, d2)
    rows = []
    for e in emps:
        days = ev[e["id"]]
        s = daily.summarize(days, st["repeat_late_threshold"])
        complete = s["no_data_days"] == 0 and d2 < now.date().isoformat() and not any(d["status"] == daily.SCHEDULED for d in days)
        sc = scoring.compute_score(days, rules, s, complete)
        rows.append({"employee": e, "summary": s, "score": sc})
    return rows, rules, version


def compare_groups(rows, key):
    """Aggregate team_rows() by 'team' or 'department'."""
    g = defaultdict(lambda: defaultdict(float))
    for r in rows:
        k = r["employee"][key] or "(none)"
        s = r["summary"]
        a = g[k]
        a["employees"] += 1
        for f in ("scheduled_days", "present_days", "absent_days", "late_count", "late_minutes", "early_count",
                  "missing_in", "missing_out", "missing_both", "leave_days", "scheduled_hours", "actual_hours"):
            a[f] += s[f]
        a["score_sum"] += r["score"]["score"]
    out = []
    for k, a in sorted(g.items()):
        judged = a["scheduled_days"]
        out.append({"group": k, **{f: (round(v, 1) if isinstance(v, float) else v) for f, v in a.items()},
                    "attendance_pct": round(100 * a["present_days"] / judged, 1) if judged else None,
                    "avg_score": round(a["score_sum"] / a["employees"], 1)})
    return out


# ------------------------------------------------------------------ Today's Workforce Overview
def managers_by_team(repo):
    m = defaultdict(list)
    for u in repo.list_users():
        if (u.get("hub_role") or u["role"]) == "manager" and u["active"]:
            for t in u["teams"]:
                m[t.strip().lower()].append(u["name"])
    return m


def chosen_managers(repo):
    """{team (lower case): [manager names]} exactly as the administrator picked them in Settings -> Team managers."""
    st = settings_svc.load(repo)
    out = {}
    for team, ids in (st.get("team_managers") or {}).items():
        names = []
        for uid in ids:
            u = repo.get_user(uid)
            if u and u["active"]:
                names.append(u["name"])
        if names:
            out[str(team).strip().lower()] = names
    return out


def chosen_manager_ids(repo):
    """{team (lower case): [account ids]} of the active managers the administrator picked in Settings -> Team managers."""
    st = settings_svc.load(repo)
    out = {}
    for team, ids in (st.get("team_managers") or {}).items():
        keep = [i for i in ids if (repo.get_user(i) or {}).get("active")]
        if keep:
            out[str(team).strip().lower()] = keep
    return out


def staffing(rows, st, shifts):
    """Short-staffed detection per team and shift.

    Staffing % = expected available / required x 100, where *required* is the number of employees PLANNED for that team and shift
    (the schedule) and *expected available* is planned minus those on approved leave / a compensatory day taken. Actual absence is
    not subtracted: it is shown separately. No threshold configured for a team + shift = never flagged."""
    work_codes = {c for c, s in shifts.items() if s["kind"] == "work"}
    groups = defaultdict(list)
    for r in rows:
        for code in dict.fromkeys([r["planned_primary"], *r.get("planned_codes", [])]):      # every work shift of the day (a double shift is in both)
            if code in work_codes:
                for t in access.shift_teams(r["teams"], (r.get("planned_marks") or {}).get(code)):
                    groups[(t, code)].append(r)
    out = []
    for (team, code), rs in sorted(groups.items()):
        planned = len(rs)
        leave = sum(1 for r in rs if r["status"] == daily.LEAVE)
        available = planned - leave
        pct = round(100.0 * available / planned, 1)
        thr = settings_svc.threshold_for(st, team, code)
        out.append({"team": team, "shift": code, "planned": planned, "on_leave": leave, "available": available, "pct": pct,
                    "threshold": thr, "short": thr is not None and pct < thr,
                    "present": sum(1 for r in rs if r["status"] in (daily.PRESENT, daily.LATE, daily.WORKING, daily.MISSION)),
                    "not_arrived": sum(1 for r in rs if r["status"] == daily.NOT_ARRIVED)})
    return out


def staffing_configured(st):
    return any(v not in (None, "") for v in [st.get("min_staffing_pct")] + list((st.get("min_staffing_pct_team") or {}).values())
               + list((st.get("min_staffing_pct_shift") or {}).values()))


LOGIN_ARRIVAL_FROM = (daily.NOT_ARRIVED, daily.SCHEDULED, daily.NO_DATA)       # statuses a portal sign-in turns into "arrived"


def todays_logins(repo, day, now):
    """{user_id: first sign-in time} when ``day`` is today, else nothing: past days are the HR file's."""
    directory = getattr(repo, "directory", None)
    if day != now.date().isoformat() or not hasattr(directory, "first_logins"):
        return {}
    return directory.first_logins(day)


def overview(repo, user, day, now, filters=None):
    filters = filters or {}
    st = settings_svc.load(repo)
    shifts = shifts_by_code(repo)
    emps = access.visible_employees(repo, user)
    ids = [e["id"] for e in emps]
    ev = evaluate_range(repo, ids, day, day, now, st)
    mgr = managers_by_team(repo)
    logins = todays_logins(repo, day, now)
    rows = []
    for e in emps:
        d = ev[e["id"]][0]
        tms = access.emp_teams(e)
        # "arrived" from the portal sign-in: a person who is expected today, has no HR check-in yet and signed in today. It is only a mark
        # (lateness, permits and scores are never computed from it) and the HR file of the day takes over as soon as it is imported.
        arrived = logins.get(str(e["id"])) if d["status"] in LOGIN_ARRIVAL_FROM and not d.get("actual_in") else None
        rows.append({**d, "arrived_login": arrived[:5] if arrived else None, "name": e["name"], "department": e["department"], "sector": e["sector"], "team": tms[0] if tms else "", "teams": tms,
                     "manager": ", ".join(dict.fromkeys(m for t in tms for m in mgr.get(t.strip().lower(), [])))})

    def keep(r):
        f = filters
        return ((not f.get("department") or r["department"] == f["department"])
                and (not f.get("team") or f["team"].lower() in [x.lower() for x in r["teams"]])
                and (not f.get("manager") or f["manager"] in r["manager"])
                and (not f.get("shift") or r["planned_primary"] == f["shift"])
                and (not f.get("status") or r["status"] == f["status"])
                and (not f.get("flag") or f["flag"] in r["flags"])
                and (not f.get("q") or f["q"].lower() in f"{r['employee_id']} {r['name']}".lower()))
    all_rows = rows
    rows = [r for r in rows if keep(r)]
    S = daily
    cnt = lambda p: sum(1 for r in all_rows if p(r))  # noqa: E731
    on_shift = lambda c: cnt(lambda r: r["planned_primary"] == c)  # noqa: E731
    stf = staffing(all_rows, st, shifts)
    short_rows = [s for s in stf if s["short"]]
    short_teams = {s["team"] for s in short_rows}
    teams = {t for r in all_rows for t in r["teams"]}
    kpi = {
        "total_employees": len(all_rows),
        "present_today": cnt(lambda r: r["status"] in (S.PRESENT, S.LATE, S.WORKING, S.MISSION, S.UNSCHEDULED) or
                             (r["status"] == S.MISSING_PUNCH and r["actual_in"]) or bool(r["arrived_login"])),
        "arrived_by_login": cnt(lambda r: bool(r["arrived_login"])),
        "currently_working": cnt(lambda r: r["status"] == S.WORKING),
        "not_arrived": cnt(lambda r: r["status"] == S.NOT_ARRIVED and not r["arrived_login"]),
        "annual_leave": cnt(lambda r: r["status"] == S.LEAVE and r["leave_type"] == "annual"),
        "other_leave": cnt(lambda r: r["status"] == S.LEAVE and r["leave_type"] != "annual"),
        "rest_day": cnt(lambda r: r["status"] in (S.REST, S.REST_WORKED)),
        "absent": cnt(lambda r: r["status"] == S.ABSENT),
        "late": cnt(lambda r: r["late_minutes"] > 0),
        "late_permit": cnt(lambda r: r["late_permit"] == "required"),
        "early_permit": cnt(lambda r: r["early_permit"] == "required"),
        "morning": on_shift("M"), "evening": on_shift("E"), "night": on_shift("N"), "normal_day": on_shift("D"),
        "missing_records": cnt(lambda r: any(f in r["flags"] for f in ("missing_in", "missing_out", "missing_both"))),
        "needs_replacement": cnt(lambda r: r["needs_replacement"]),
        "teams_short": len(short_teams),
        "no_data": cnt(lambda r: r["status"] == S.NO_DATA),
        "standby": cnt(lambda r: r["status"] == S.STANDBY),
        "not_scheduled": cnt(lambda r: r["status"] == S.NO_SCHEDULE),
    }
    status_counts = defaultdict(int)
    for r in all_rows:
        status_counts[r["status"]] += 1
    # per-shift picture for the dashboard: planned (= required), expected available, present, late, on leave, absent
    unavailable = (S.LEAVE, S.ABSENT, S.EXCUSED)
    shift_stats = {}
    for c in ("M", "E", "N", "D"):
        planned = [r for r in all_rows if r["planned_primary"] == c or (c != "D" and c in r.get("planned_codes", []))]       # a double shift counts in both
        avail = len(planned) - sum(1 for r in planned if r["status"] == S.LEAVE)
        thr = settings_svc.threshold_for(st, None, c)
        shift_stats[c] = {"planned": len(planned), "available": avail, "expected_available": avail,
                          "available_actual": sum(1 for r in planned if r["status"] not in unavailable),
                          "present": sum(1 for r in planned if r["status"] in (S.PRESENT, S.LATE, S.WORKING, S.MISSION) or r["arrived_login"]),
                          "late": sum(1 for r in planned if r["late_minutes"] > 0),
                          "leave": sum(1 for r in planned if r["status"] == S.LEAVE),
                          "absent": sum(1 for r in planned if r["status"] == S.ABSENT),
                          "pct": round(100.0 * avail / len(planned), 1) if planned else None, "threshold": thr,
                          "short": bool(planned) and thr is not None and 100.0 * avail / len(planned) < thr}
    # "needs attention", most severe first
    attention = []

    def add(tone, icon, text, n, status="", flag=""):
        if n:
            attention.append({"tone": tone, "icon": icon, "text": text.format(n=n), "n": n, "status": status, "flag": flag})
    add("t-danger", "user-x", "{n} planned employee(s) have no punches, leave or mission (unverified absence)", kpi["absent"], "absent")
    add("t-danger", "repeat", "{n} shift(s) need a replacement (leave or absence)", kpi["needs_replacement"])
    for s in short_rows:
        attention.append({"tone": "t-danger", "icon": "alert-triangle", "n": s["planned"], "status": "", "flag": "",
                          "text": f"Short staffed: {s['team']} · {shifts[s['shift']]['name']} {s['available']}/{s['planned']} ({s['pct']:g}% < {s['threshold']:g}%)"})
    add("t-warn", "clock", "{n} employee(s) not arrived yet (shift started, no check-in)", kpi["not_arrived"], "not_arrived")
    add("t-warn", "clock", "{n} late arrival(s) need an attendance permit (اذن حضور)", kpi["late_permit"], flag="needs_late_permit")
    add("t-warn", "arrow-left", "{n} early departure(s) need a departure permit (اذن انصراف)", kpi["early_permit"], flag="needs_early_permit")
    add("t-warn", "alert-circle", "{n} missing punch(es) (IN or OUT)", status_counts.get(S.MISSING_PUNCH, 0), "missing_punch")
    add("t-warn", "alert-triangle", "{n} employee(s) worked on a planned rest day", status_counts.get(S.REST_WORKED, 0), "rest_day_worked")
    add("t-info", "info", "{n} planned shift(s) without attendance data for this date", kpi["no_data"], "no_data")
    return {"day": day, "rows": rows, "all_rows": all_rows, "kpi": kpi, "status_counts": dict(status_counts),
            "shift_stats": shift_stats, "attention": attention, "short_teams": sorted(short_teams), "staffing": stf,
            "short_staffed": short_rows, "staffing_configured": staffing_configured(st), "shifts": shifts,
            "departments": sorted({r["department"] for r in all_rows if r["department"]}),
            "teams": sorted(teams), "data_loaded": bool(repo.dates_with_attendance(day, day)),
            "has_schedule": any(r["planned_primary"] for r in all_rows)}
