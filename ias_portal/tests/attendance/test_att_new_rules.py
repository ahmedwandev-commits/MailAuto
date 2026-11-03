"""Default shift, attendance-permit thresholds, staffing %, manager team dashboard."""
from .support import add_employees
import datetime as dt

import pytest

from calllog.attendance.domain import daily, scoring
from calllog.attendance.services import analytics_service as an, settings as settings_svc
from .test_att_domain import SHIFTS, ST, rec

SH = {**SHIFTS, "D": {"code": "D", "kind": "work", "start_time": "08:00", "end_time": "16:00"}}
NOW = dt.datetime(2026, 10, 20, 12, 0)
PERMIT = {**ST, "late_permit_minutes": 15, "early_permit_minutes": 15}


def day(planned, att=None, date="2026-10-14", st=None, now=NOW, reviews=None, att_next=None, loaded_next=True, source=None):
    return daily.evaluate_day({
        "employee_id": 1, "date": date, "planned": [{"shift_code": c, **({"source": source} if source else {})} for c in planned], "shifts": SH,
        "att": att, "att_next": att_next, "data_loaded": True, "data_loaded_next": loaded_next, "leave": None, "holiday": False,
        "review": None, "settings": {**PERMIT, **(st or {})}, "now": now, "hr_rest": False, "permit_reviews": reviews})


# ---------------------------------------------------------------- attendance permits: exactly 15 vs more than 15
@pytest.mark.parametrize("shift,start,inn,flag", [
    ("D", "08:00", "08:15:00", None),            # exactly 15 minutes: no permit needed
    ("D", "08:00", "08:15:01", "required"),      # one second more than 15 minutes
    ("D", "08:00", "08:16:00", "required"),
    ("D", "08:00", "08:14:59", None),
    ("M", "07:00", "07:15:00", None), ("M", "07:00", "07:16:30", "required"),
    ("E", "14:00", "14:15:00", None), ("E", "14:00", "14:20:00", "required"),
    ("N", "22:00", "22:15:00", None), ("N", "22:00", "22:16:00", "required"),      # a night shift is measured against 22:00, not 08:00
    ("N", "22:00", "08:16:00", None)])           # (08:16 is not 'late' for a night shift: it is an odd punch, handled elsewhere)
def test_late_arrival_permit_threshold(shift, start, inn, flag):
    d = day([shift], rec(inn, None) if shift != "N" else rec(inn, None), date="2026-10-14", loaded_next=False)
    if (shift, inn) == ("N", "08:16:00"):
        return
    assert d["late_permit"] == flag
    assert ("needs_late_permit" in d["flags"]) == (flag == "required")
    assert d["expected_start"] == start


@pytest.mark.parametrize("shift,end,out,flag", [
    ("D", "16:00", "15:45:00", None), ("D", "16:00", "15:44:59", "required"), ("D", "16:00", "15:30:00", "required"), ("D", "16:00", "16:30:00", None),
    ("M", "15:00", "14:45:00", None), ("M", "15:00", "14:40:00", "required"),
    ("E", "22:00", "21:45:00", None), ("E", "22:00", "21:30:00", "required")])
def test_early_departure_permit_threshold(shift, end, out, flag):
    start = SH[shift]["start_time"]
    d = day([shift], rec(start + ":00", out))
    assert d["early_permit"] == flag and ("needs_early_permit" in d["flags"]) == (flag == "required") and d["expected_end"] == end


def test_night_shift_early_departure_uses_the_next_morning_end():
    d = day(["N"], rec("22:00:00", None), att_next=rec(None, "06:44:00"))       # shift ends 07:00; 06:44 is 16 minutes early
    assert d["early_permit"] == "required" and d["early_raw_minutes"] == 16
    d = day(["N"], rec("22:00:00", None), att_next=rec(None, "06:45:00"))
    assert d["early_permit"] is None


def test_thresholds_are_configurable_and_independent():
    assert day(["D"], rec("08:20:00", "16:00:00"), st={"late_permit_minutes": 30})["late_permit"] is None
    assert day(["D"], rec("08:31:00", "16:00:00"), st={"late_permit_minutes": 30})["late_permit"] == "required"
    d = day(["D"], rec("08:20:00", "15:50:00"), st={"late_permit_minutes": 30, "early_permit_minutes": 5})
    assert d["late_permit"] is None and d["early_permit"] == "required"
    d = day(["D"], rec("08:10:00", "15:50:00"), st={"late_permit_minutes": 5, "early_permit_minutes": 30})
    assert d["late_permit"] == "required" and d["early_permit"] is None


def test_a_permit_on_file_or_a_review_clears_the_flag_but_a_missing_permit_is_never_absence_or_penalty():
    missing = day(["D"], rec("08:40:00", "16:00:00"))
    assert missing["late_permit"] == "required" and missing["status"] == "late"                 # visible, not absent
    assert day(["D"], rec("08:40:00", "16:00:00", permit_text="اذن خاص حضور"), st={"permit_excuses_late": ["اذن خاص حضور"]})["late_permit"] == "provided"
    for decision, still_late in (("provided", False), ("excused", False), ("rejected", True)):
        d = day(["D"], rec("08:40:00", "16:00:00"), reviews={"late": decision})
        assert d["late_permit"] == decision and "needs_late_permit" not in d["flags"] and (d["late_minutes"] > 0) == still_late
    # no automatic score penalty because a permit is missing: only the configured lateness bands apply, never the permit flag
    rules = {**scoring.DEFAULT_RULES, "late": {"tiers": [], "max_deduction": None}, "absence": {"penalty_per_day": 10, "max_deduction": None},
             "categories": [{"name": "A", "min": 0}], "min_judged_days": 1}
    days = [missing]
    r = scoring.compute_score(days, rules, daily.summarize(days), True)
    assert r["score"] == 100 and daily.summarize(days)["late_permit_required"] == 1 and daily.summarize(days)["absent_days"] == 0


# ---------------------------------------------------------------- default shift
@pytest.fixture
def emps(repo):
    add_employees(repo, [{"id": 1, "name": "Ali One", "team": "T"}, {"id": 2, "name": "Omar Two", "team": "T"}])
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "x", "file_sha256": "x", "parsed": {}})
    repo.commit_schedule_version_atomic(2026, 10, imp, [
        {"employee_id": 1, "work_date": "2026-10-12", "shift_code": "N"},          # Mon: explicit night
        {"employee_id": 1, "work_date": "2026-10-13", "shift_code": "OFF"},        # Tue: explicit rest day
        {"employee_id": 1, "work_date": "2026-10-14", "shift_code": "SB"}], "u", replace=False)  # Wed: stand-by only
    return repo


def planned(repo, emp, d, **st):
    repo.save_settings(st) if st else None
    ev = an.evaluate_range(repo, [emp], d, d, dt.datetime(2026, 10, 30))
    return ev[emp][0]


def test_default_shift_is_assigned_only_when_nothing_explicit(emps):
    d = planned(emps, 2, "2026-10-12")                                              # no entry at all on a Monday -> Normal Day
    assert d["planned_codes"] == ["D"] and d["default_shift"] and (d["expected_start"], d["expected_end"]) == ("08:00", "16:00")
    assert planned(emps, 1, "2026-10-12")["planned_codes"] == ["N"]               # explicit Night wins
    assert planned(emps, 1, "2026-10-13")["planned_codes"] == ["OFF"]             # explicit rest day: no default
    assert planned(emps, 1, "2026-10-14")["planned_codes"] == ["SB"]              # stand-by is explicit: no default
    assert planned(emps, 1, "2026-10-15")["planned_codes"] == ["D"]               # unlisted working day -> default
    assert planned(emps, 2, "2026-10-16")["planned_codes"] == []                  # Friday: weekly rest, not a working day
    assert planned(emps, 2, "2026-10-17")["planned_codes"] == []                  # Saturday


def test_default_shift_follows_hr_calendar_holidays_and_settings(emps):
    emps.add_holiday("2026-10-12", "Holiday")
    assert planned(emps, 2, "2026-10-12")["planned_codes"] == [] and planned(emps, 2, "2026-10-12")["status"] == "holiday"
    a = emps.create_attendance_import({"filename": "a", "file_sha256": "a", "parsed": {}})
    emps.commit_attendance_rows_atomic(a, [
        {"employee_id": 2, "work_date": "2026-10-16", "day_type": "يوم عمل"},                 # HR says Friday is a working day
        {"employee_id": 2, "work_date": "2026-10-15", "day_type": "العطلة الاسبوعية"}])        # HR says Thursday is weekly rest
    assert planned(emps, 2, "2026-10-16")["planned_codes"] == ["D"]
    assert planned(emps, 2, "2026-10-15")["planned_codes"] == []
    assert planned(emps, 2, "2026-10-19", default_shift_enabled=False)["planned_codes"] == []   # switched off
    assert planned(emps, 2, "2026-10-19", default_shift_enabled=True, weekly_rest_weekdays=[0])["planned_codes"] == []   # Monday made a rest weekday


def test_default_shift_times_are_configurable_from_the_settings_page(app, emps, admin_b):
    base = dict(action="rules", grace_minutes="0", early_grace_minutes="0", late_basis="from_start", punch_mismatch_hours="4", repeat_late_threshold="3",
                min_rest_hours="8", late_permit_minutes="15", early_permit_minutes="15", default_shift_enabled="1", default_shift_code="D",
                default_start="09:00", default_end="17:00", rest_wd=["4", "5"])
    assert admin_b.post("/admin/settings", base).status_code == 302
    d = planned(emps, 2, "2026-10-12")
    assert (d["expected_start"], d["expected_end"], d["scheduled_hours"]) == ("09:00", "17:00", 8)
    assert admin_b.post("/admin/settings", {**base, "default_shift_code": "OFF"}).status_code == 400         # must be a working shift
    assert admin_b.post("/admin/settings", {**base, "default_start": "25:00"}).status_code == 400
    assert admin_b.post("/admin/settings", {**base, "late_permit_minutes": "-1"}).status_code == 400
    assert any(a["action"] == "shift_type_saved" and a["detail"]["new"] == ["09:00", "17:00"] for a in emps.list_audit())


def test_default_shift_lateness_uses_the_default_hours_not_a_fixed_time(emps):
    a = emps.create_attendance_import({"filename": "a", "file_sha256": "a", "parsed": {}})
    emps.commit_attendance_rows_atomic(a, [{"employee_id": 2, "work_date": "2026-10-12", "in_time": "08:16:00", "out_time": "15:44:00"},
                                           {"employee_id": 1, "work_date": "2026-10-12", "in_time": "22:10:00", "out_time": None}])
    d = planned(emps, 2, "2026-10-12")
    assert d["late_permit"] == "required" and d["early_permit"] == "required" and d["late_minutes"] == 16
    n = planned(emps, 1, "2026-10-12")                                              # explicit Night: 22:10 is only 10 minutes late
    assert n["planned_codes"] == ["N"] and n["late_permit"] is None and n["late_minutes"] == 10


def test_default_shift_leave_does_not_need_a_replacement(emps):
    emps.create_leave_request({"employee_id": 2, "leave_type": "annual", "date_from": "2026-10-12", "date_to": "2026-10-12", "days": 1, "status": "approved"})
    d = planned(emps, 2, "2026-10-12")
    assert d["status"] == "on_leave" and not d["needs_replacement"]


# ---------------------------------------------------------------- staffing percentage
def staffed(rows_spec, st):
    shifts = {"M": {"kind": "work", "name": "Morning"}, "D": {"kind": "work", "name": "Normal day"}}
    rows = [{"team": t, "teams": [t], "planned_primary": c, "status": s} for t, c, s in rows_spec]
    return an.staffing(rows, {**settings_svc.DEFAULT_SETTINGS, **st}, shifts)


def test_staffing_percentage_formula_and_threshold():
    spec = [("A", "M", "present"), ("A", "M", "on_leave"), ("A", "M", "present"), ("A", "M", "on_leave")]      # 4 planned, 2 on leave -> 50 %
    s = staffed(spec, {"min_staffing_pct": 50})[0]
    assert (s["planned"], s["on_leave"], s["available"], s["pct"], s["threshold"], s["short"]) == (4, 2, 2, 50.0, 50.0, False)   # exactly 50 %: not short
    assert staffed(spec, {"min_staffing_pct": 51})[0]["short"] is True
    spec3 = spec[:1] + spec[1:2] * 2 + spec[2:3]                                    # 4 planned, 2 on leave... use 3 on leave
    spec3 = [("A", "M", "present")] + [("A", "M", "on_leave")] * 3                 # 25 %
    s = staffed(spec3, {"min_staffing_pct": 50})[0]
    assert s["pct"] == 25.0 and s["short"] is True


def test_no_threshold_configured_never_flags():
    spec = [("A", "M", "on_leave")] * 3 + [("A", "M", "present")]
    assert staffed(spec, {})[0]["short"] is False and staffed(spec, {})[0]["threshold"] is None
    assert not an.staffing_configured(settings_svc.DEFAULT_SETTINGS)
    assert an.staffing_configured({**settings_svc.DEFAULT_SETTINGS, "min_staffing_pct": 50})


def test_team_and_shift_overrides_and_absence_is_not_subtracted():
    spec = [("A", "M", "on_leave"), ("A", "M", "present"), ("B", "M", "on_leave"), ("B", "M", "present"), ("B", "D", "absent"), ("B", "D", "present")]
    res = {(r["team"], r["shift"]): r for r in staffed(spec, {"min_staffing_pct": 40, "min_staffing_pct_team": {"B": 60}, "min_staffing_pct_shift": {"D": 90}})}
    assert res[("A", "M")]["short"] is False and res[("A", "M")]["threshold"] == 40            # global
    assert res[("B", "M")]["short"] is True and res[("B", "M")]["threshold"] == 60             # team override beats shift/global
    assert res[("B", "D")]["pct"] == 100.0                                                     # an absence is shown separately, not subtracted
    assert settings_svc.threshold_for({"min_staffing_pct_shift": {"D": 90}}, "Z", "D") == 90.0


def test_settings_page_validates_the_staffing_threshold(app, emps, admin_b, repo):
    base = dict(action="rules", grace_minutes="0", early_grace_minutes="0", late_basis="from_start", punch_mismatch_hours="4", repeat_late_threshold="3",
                min_rest_hours="8", late_permit_minutes="15", early_permit_minutes="15", default_shift_enabled="1", default_shift_code="D")
    for bad in ("150", "-5", "abc"):
        assert admin_b.post("/admin/settings", {**base, "min_staffing_pct": bad}).status_code == 400
    assert admin_b.post("/admin/settings", {**base, "min_staffing_pct": "50", "min_staffing_pct_team": "T=60", "min_staffing_pct_shift": "n=40"}).status_code == 302
    st = repo.get_settings()
    assert st["min_staffing_pct"] == 50.0 and st["min_staffing_pct_team"] == {"T": 60.0} and st["min_staffing_pct_shift"] == {"N": 40.0}
    assert admin_b.post("/admin/settings", {**base, "min_staffing_pct": ""}).status_code == 302 and repo.get_settings()["min_staffing_pct"] is None


# ---------------------------------------------------------------- team availability dashboard
@pytest.fixture
def team_world(app, repo, users):
    add_employees(repo, [{"id": i, "name": f"Emp {i}", "team": "Team A", "department": "D"} for i in (11, 12, 13, 14, 15)]
                                  + [{"id": 21, "name": "Other Team", "team": "Team B", "department": "D"}])
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "t", "file_sha256": "t", "parsed": {}})
    ent = [{"employee_id": e, "work_date": "2026-10-14", "shift_code": c} for e, c in ((11, "M"), (12, "M"), (13, "M"), (14, "OFF"), (21, "M"))]
    repo.commit_schedule_version_atomic(2026, 10, imp, ent, "admin", replace=False)
    repo.create_leave_request({"employee_id": 13, "leave_type": "annual", "date_from": "2026-10-14", "date_to": "2026-10-14", "days": 1, "status": "approved"})
    a = repo.create_attendance_import({"filename": "a", "file_sha256": "a", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 11, "work_date": "2026-10-13", "in_time": "07:00:00", "out_time": "15:00:00"}])
    return repo


def test_manager_team_dashboard_shows_expected_present_not_arrived_leave_and_rest(app, team_world, clock, mgr1_b):
    clock.value = dt.datetime(2026, 10, 14, 9, 30)                                 # Wednesday, 09:30: the morning shift (07:00) has started
    a = team_world.create_attendance_import({"filename": "today", "file_sha256": "today", "parsed": {}})
    team_world.commit_attendance_rows_atomic(a, [{"employee_id": 11, "work_date": "2026-10-14", "in_time": "07:05:00", "out_time": None}])
    from calllog.attendance.services import planning_service as ps
    td = ps.team_dashboard(team_world, team_world.get_user("mgr1"), "Team A", "2026-10-14", clock.value)
    by = {r["employee_id"]: r for r in td["rows"]}
    assert set(by) == {11, 12, 13, 14, 15}                                         # the whole team and nobody from another team
    assert by[11]["presence"]["key"] == "present" and by[11]["status"] == "currently_working"
    assert by[12]["presence"]["key"] == "missing" and by[12]["status"] == "not_arrived"       # scheduled, no record: flagged
    assert by[13]["presence"]["key"] == "leave" and by[13]["status"] == "on_leave"            # leave is never "not arrived"
    assert by[14]["presence"]["key"] == "rest" and by[14]["status"] == "rest_day"             # scheduled rest day is never absence
    assert by[15]["planned_primary"] == "D" and by[15]["default_shift"] and by[15]["status"] == "not_arrived"   # default shift 08:00 has started
    assert "Morning 07:00–15:00 · 8 h" in by[12]["expected_text"] and "Normal day 08:00–16:00 · 8 h" in by[15]["expected_text"]
    c = td["counts"]
    assert (c["expected"], c["present"], c["not_arrived"], c["leave"], c["rest"]) == (3, 1, 2, 1, 1)         # expected = scheduled to work and not on leave
    # presence ordering: people who should be here but are not come first
    assert [r["employee_id"] for r in td["rows"][:2]] == [15, 12] or {r["employee_id"] for r in td["rows"][:2]} == {12, 15}
    page = mgr1_b.get("/team?date=2026-10-14").get_data(as_text=True)          # the manager's Team dashboard (his one team)
    assert "Team A" in page and "Other Team" not in page and "Expected to work" in page
    assert "People" in page and "Emp 12" in page and "Not arrived" in page              # the manager's own team is open: its people and states
    assert 'id="team-summary"' in mgr1_b.get("/planning/day?date=2026-10-14").get_data(as_text=True)   # the teams table is on Today


def test_not_arrived_turns_into_absent_only_after_the_shift_when_data_exists(team_world, clock):
    from calllog.attendance.services import planning_service as ps
    adm = team_world.get_user("admin")
    clock.value = dt.datetime(2026, 10, 14, 6, 0)
    assert {r["employee_id"]: r["status"] for r in ps.team_dashboard(team_world, adm, "Team A", "2026-10-14", clock.value)["rows"]}[12] == "scheduled"
    clock.value = dt.datetime(2026, 10, 14, 20, 0)                                 # shift over, attendance loaded for the day
    a = team_world.create_attendance_import({"filename": "t", "file_sha256": "t2", "parsed": {}})
    team_world.commit_attendance_rows_atomic(a, [{"employee_id": 11, "work_date": "2026-10-14", "in_time": "07:00:00", "out_time": "15:00:00"}])
    st = {r["employee_id"]: r["status"] for r in ps.team_dashboard(team_world, adm, "Team A", "2026-10-14", clock.value)["rows"]}
    assert st[12] == "absent" and st[13] == "on_leave" and st[14] == "rest_day"


def test_team_dashboard_permissions(app, team_world, mgr1_b, mgr2_b, gm_b, admin_b):
    assert mgr1_b.get("/team?team=Team%20A").status_code == 200
    assert mgr1_b.get("/team?team=Team%20B").status_code == 403           # another manager's team
    assert mgr2_b.get("/team?team=Team%20A").status_code == 403
    assert b"Team A" not in mgr2_b.get("/team").data and b"Team B" in mgr2_b.get("/team").data      # his own team only
    assert gm_b.get("/team?team=Team%20B").status_code == 200
    assert mgr1_b.get("/team").status_code == 200                              # a one-team manager: his team's dashboard
    assert gm_b.get("/team").status_code == 302 and admin_b.get("/team").status_code == 302      # every team at once = Today


def test_short_staffed_warning_on_manager_and_gm_dashboards(app, team_world, clock, mgr1_b, gm_b, admin_b):
    clock.value = dt.datetime(2026, 10, 14, 9, 0)
    assert "Short staffed" not in mgr1_b.get("/planning/day?date=2026-10-14").get_data(as_text=True)    # nothing configured: never flagged
    assert admin_b.post("/admin/settings", dict(action="rules", grace_minutes="0", early_grace_minutes="0", late_basis="from_start", punch_mismatch_hours="4",
                                                repeat_late_threshold="3", min_rest_hours="8", late_permit_minutes="15", early_permit_minutes="15",
                                                default_shift_enabled="1", default_shift_code="D", min_staffing_pct="75")).status_code == 302
    # Team A morning: 3 planned (11, 12, 13), 13 on leave -> 2/3 = 66.7 % < 75 %
    mgr = mgr1_b.get("/planning/day?date=2026-10-14").get_data(as_text=True)
    assert "Short staffed:" in mgr and "Team A" in mgr and "2/3" in mgr
    gm = gm_b.get("/planning/day?date=2026-10-14").get_data(as_text=True)
    assert "Short staffed:" in gm and "Team A" in gm and "Team B" in gm      # all teams listed on Today, B is 1/1 -> fine
    j = gm_b.get("/api/overview?date=2026-10-14").get_json()
    assert j["kpi"]["teams_short"] == 1 and [(s["team"], s["shift"]) for s in j["short_staffed"]] == [("Team A", "M")]
    assert "Short staffed:" not in mgr2_b_page(app, team_world)


def mgr2_b_page(app, repo):
    from .support import login
    return login(app, "mgr2").get("/planning/day?date=2026-10-14").get_data(as_text=True)


def test_permit_review_is_direct_audited_and_scoped(app, team_world, repo, clock, mgr1_b, mgr2_b, gm_b):
    a = repo.create_attendance_import({"filename": "p", "file_sha256": "p", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 12, "work_date": "2026-10-14", "in_time": "07:40:00", "out_time": "14:30:00"}])
    adm = repo.get_user("admin")
    d = an.evaluate_range(repo, [12], "2026-10-14", "2026-10-14", dt.datetime(2026, 10, 20))[12][0]
    assert d["late_permit"] == "required" and d["early_permit"] == "required" and d["status"] == "late"
    data = dict(emp="12", date="2026-10-14", kind="late", decision="provided", reason="permit found")
    assert mgr2_b.post("/attendance/permit-review", data).status_code == 403
    assert gm_b.post("/attendance/permit-review", data).status_code == 302 and repo.exception_reviews("2026-10-14", "2026-10-14") == {}
    assert mgr1_b.post("/attendance/permit-review", {**data, "reason": ""}).status_code == 400
    assert mgr1_b.post("/attendance/permit-review", data).status_code == 302
    d = an.evaluate_range(repo, [12], "2026-10-14", "2026-10-14", dt.datetime(2026, 10, 20))[12][0]
    assert d["late_permit"] == "provided" and d["early_permit"] == "required"
    assert mgr1_b.post("/attendance/permit-review", {**data, "kind": "late", "decision": "rejected", "reason": "changed my mind"}).status_code == 302
    au = repo.list_audit(action="permit_reviewed")
    assert au[0]["detail"]["previous"] == "provided" and au[0]["detail"]["new"] == "rejected" and au[0]["username"] == "mgr1"
    assert au[0]["detail"]["permit"] == "اذن حضور" and au[0]["detail"]["reason"] == "changed my mind"


def test_permit_exceptions_in_dashboards_and_reports(app, team_world, repo, clock, admin_b, mgr1_b, mgr2_b):
    a = repo.create_attendance_import({"filename": "p", "file_sha256": "p", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 12, "work_date": "2026-10-14", "in_time": "07:40:00", "out_time": "14:30:00"},
                                           {"employee_id": 21, "work_date": "2026-10-14", "in_time": "07:00:00", "out_time": "15:00:00"}])
    gm = admin_b.get("/api/overview?date=2026-10-14").get_json()
    assert gm["kpi"]["late_permit"] == 1 and gm["kpi"]["early_permit"] == 1
    assert any("اذن حضور" in x["text"] for x in gm["attention"]) and any("اذن انصراف" in x["text"] for x in gm["attention"])
    row = next(r for r in gm["rows"] if r["employee_id"] == 12)
    assert row["late_permit"] == "required" and "needs_early_permit" in row["flags"]
    page = admin_b.get("/team?team=Team%20A&date=2026-10-14").get_data(as_text=True)
    assert "اذن حضور" in page and "اذن انصراف" in page
    filtered = admin_b.get("/api/overview?date=2026-10-14&flag=needs_late_permit").get_json()
    assert [r["employee_id"] for r in filtered["rows"]] == [12]
    mg = mgr1_b.get("/team?date=2026-10-14").get_data(as_text=True)
    assert "اذن حضور" in mg and "Review permit" in mg                 # the manager's own team lists its people with the review button
    from calllog.attendance.services import reports_service as rs
    rep = rs.build(repo, repo.get_user("admin"), "permits", "2026-10-14", "2026-10-14", dt.datetime(2026, 10, 20))
    assert {(r[0], r[4].split(" (")[0], r[8]) for r in rep["rows"]} == {(12, "اذن حضور", "Required - not reviewed"), (12, "اذن انصراف", "Required - not reviewed")}
    summary = rs.build(repo, repo.get_user("admin"), "summary", "2026-10-14", "2026-10-14", dt.datetime(2026, 10, 20))
    i = summary["columns"].index("Permit needed: late (اذن حضور)")
    assert next(r for r in summary["rows"] if r[0] == 12)[i] == 1
    scoped = rs.build(repo, repo.get_user("mgr2"), "permits", "2026-10-01", "2026-10-31", dt.datetime(2026, 10, 20))
    assert {r[0] for r in scoped["rows"]} == set()                                  # Team B's 21 is on time; manager B sees nothing of Team A
    assert admin_b.get("/reports?kind=permits&from=2026-10-01&to=2026-10-31").status_code == 200
    assert admin_b.get("/export/report.xlsx?kind=permits&from=2026-10-01&to=2026-10-31").status_code == 200
