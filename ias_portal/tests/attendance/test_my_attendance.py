"""My attendance (v5.17): overtime (clock bands, holidays and comp days, the weekly deduction, the 40 % cap), the expected shift
allowance (transport + meals, supervisors morning only) and who may see what."""
from datetime import datetime

import pytest

from calllog.attendance.domain import overtime as otd
from calllog.attendance.services import overtime_service as ots
from calllog.attendance.services import settings as settings_svc

from .support import add_employees, add_user, login

OT = dict(settings_svc.OVERTIME_DEFAULTS)


def dt(s):
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- the rules (pure)
def test_overtime_starts_after_8_hours_and_is_weighted_by_the_clock():
    w = otd.working_day_overtime(dt("2026-10-13T07:00"), dt("2026-10-13T21:00"), OT)     # 15-18 x1, 18-21 x1.5
    assert w["minutes"] == {"day": 180, "evening": 180, "night": 0}
    assert w["raw"] == 6 and w["weighted"] == pytest.approx(3 + 4.5)
    w = otd.working_day_overtime(dt("2026-10-13T14:00"), dt("2026-10-13T23:30"), OT)     # 22:00-23:30 x1.75
    assert w["minutes"]["night"] == 90 and w["weighted"] == pytest.approx(1.5 * 1.75)
    w = otd.working_day_overtime(dt("2026-10-13T16:00"), dt("2026-10-14T02:00"), OT)     # over midnight: 00-02 night
    assert w["minutes"] == {"day": 0, "evening": 0, "night": 120}
    assert otd.working_day_overtime(dt("2026-10-13T08:00"), dt("2026-10-13T16:00"), OT)["raw"] == 0     # exactly 8 h: none


def test_the_night_rate_ends_at_7_and_at_8_on_a_friday_or_saturday_morning():
    w = otd.working_day_overtime(dt("2026-10-13T22:00"), dt("2026-10-14T09:00"), OT, {4, 5})       # Tue night: OT 06-07 night, 07-09 day
    assert w["minutes"] == {"day": 120, "evening": 0, "night": 60}
    w = otd.working_day_overtime(dt("2026-10-15T22:00"), dt("2026-10-16T09:00"), OT, {4, 5})       # Thu night -> Fri morning: night until 08
    assert w["minutes"] == {"day": 60, "evening": 0, "night": 120}
    w = otd.working_day_overtime(dt("2026-10-17T22:00"), dt("2026-10-18T09:00"), OT, {4, 5})       # Sat night -> Sun morning: until 07
    assert w["minutes"] == {"day": 120, "evening": 0, "night": 60}


def test_a_holiday_worked_counts_double_unless_a_comp_day_is_taken():
    assert otd.holiday_overtime(8, False, OT) == {"raw": 8, "weighted": 16}
    assert otd.holiday_overtime(8, True, OT) == {"raw": 0, "weighted": 0}


def test_two_hours_come_off_every_week_and_the_month_is_capped_at_40_percent():
    days = [{"date": f"2026-10-{d:02d}", "raw": 1, "weighted": 1} for d in (11, 12)]            # one week: 2 h -> 0 after the deduction
    t = otd.month_totals(days, OT, 6)
    assert [w["net"] for w in t["weeks"]] == [0] and t["deducted"] == 2
    days = [{"date": f"2026-10-{d:02d}", "raw": 5, "weighted": 5} for d in range(1, 32)]        # 155 h in 5 weeks
    t = otd.month_totals(days, OT, 6)
    assert len(t["weeks"]) == 5 and t["deducted"] == 10 and t["net"] == 145
    assert t["cap"] == pytest.approx(70.4) and t["capped"] == pytest.approx(70.4) and t["over_cap"] == pytest.approx(74.6)
    assert t["pct"] == pytest.approx(40.0) and t["base_hours_month"] == 176


def test_meals_by_shift_supervisors_breakfast_only_and_a_lunch_for_a_long_day():
    assert otd.meals_of("morning", False, 8, OT) == ["breakfast"]
    assert otd.meals_of("evening", False, 8, OT) == ["evening_meal"]
    assert otd.meals_of("night", False, 9, OT) == ["dinner"]
    assert otd.meals_of("evening", True, 8, OT) == []                                             # a supervisor: morning only
    assert otd.meals_of("morning", True, 10, OT) == ["breakfast", "lunch"]
    assert otd.meals_of(None, False, 11, OT) == ["lunch"]                                         # any long day
    assert otd.meals_of("rest_day", False, 8, OT) == []


# ---------------------------------------------------------------- the month of a person
def _long_day(repo):
    a = repo.create_attendance_import({"filename": "b", "file_sha256": "b", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 101, "work_date": "2026-10-13", "in_time": "07:00:00", "out_time": "21:00:00"}])


def test_the_month_of_an_employee(app, world, repo):
    _long_day(repo)
    rep = ots.build(repo, [repo.get_employee(101)], "2026-10", "2026-10-14")[101]
    d13 = next(d for d in rep["days"] if d["date"] == "2026-10-13")
    assert d13["kind"] == "morning" and d13["transport"] == 100 and d13["meals"] == ["breakfast", "lunch"] and d13["meal_amount"] == 200
    assert d13["weighted"] == pytest.approx(7.5) and not d13["expected"]
    assert rep["so_far"]["transport"] == 100 and rep["so_far"]["meals"] == 200
    assert all(d["expected"] for d in rep["days"] if d["date"] >= "2026-10-14")                 # the rest comes from the schedule
    # the rest: 14..31 from the schedule + 1..11, whose HR sheet is not uploaded yet (only the 12th and the 13th are)
    assert rep["rest"]["transport"] == 29 * 100 and rep["rest"]["meal_counts"]["breakfast"] == 29
    assert next(d for d in rep["days"] if d["date"] == "2026-10-05")["note"] == "waiting for the HR sheet"
    assert next(d for d in rep["days"] if d["date"] == "2026-10-12")["expected"] is False                  # uploaded: real (short day)
    assert rep["expected_total"] == rep["so_far"]["total"] + rep["rest"]["total"]
    assert rep["overtime"]["net"] == pytest.approx(5.5)                                           # 7.5 h that week - 2 h


def test_a_rest_day_worked_and_its_comp_day(app, world, repo):
    a = repo.create_attendance_import({"filename": "c", "file_sha256": "c", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 102, "work_date": "2026-10-09", "in_time": "08:00:00", "out_time": "16:00:00"}])   # a Friday
    rep = ots.build(repo, [repo.get_employee(102)], "2026-10", "2026-10-14")[102]
    d = next(x for x in rep["days"] if x["date"] == "2026-10-09")
    assert d["day_kind"] == "rest_day" and d["transport"] == 300 and d["weighted"] == 16
    assert d["meals"] == ["breakfast"]                                                         # a day off eats like its check-in time
    c = repo.add_comp_day({"employee_id": 102, "earned_date": "2026-10-09", "reason": "t"})
    repo.update_comp_day(c["id"] if isinstance(c, dict) else c, {"taken_date": "2026-10-12"})
    rep = ots.build(repo, [repo.get_employee(102)], "2026-10", "2026-10-14")[102]
    d = next(x for x in rep["days"] if x["date"] == "2026-10-09")
    assert d["weighted"] == 0 and d["transport"] == 300 and "comp day" in d["note"]


def test_the_comp_day_must_be_a_working_day_of_the_same_or_the_next_week():
    """Owner: worked Saturday 10 Oct -> a comp day counts on Sun 11 .. Thu 15 only; the next Friday / Saturday are rest days anyway."""
    assert otd.comp_window("2026-10-10", 6, {4, 5}, set()) == ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    assert otd.comp_window("2026-10-09", 6, {4, 5}, set()) == ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    assert otd.comp_window("2026-10-10", 6, {4, 5}, {"2026-10-13"}) == ["2026-10-11", "2026-10-12", "2026-10-14", "2026-10-15"]
    # an official holiday on Tuesday 13 worked: Wed 14, Thu 15 of the same week and Sun 18 .. Thu 22 of the next one
    assert otd.comp_window("2026-10-13", 6, {4, 5}, {"2026-10-13"}) == ["2026-10-14", "2026-10-15", "2026-10-18", "2026-10-19",
                                                                      "2026-10-20", "2026-10-21", "2026-10-22"]


@pytest.mark.parametrize("taken,counts", [("2026-10-16", False), ("2026-10-17", False), ("2026-10-15", True), ("2026-10-25", False)])
def test_a_day_off_on_the_next_friday_or_saturday_is_not_a_comp_day(app, world, repo, taken, counts):
    a = repo.create_attendance_import({"filename": "s", "file_sha256": "s", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 102, "work_date": "2026-10-10", "in_time": "08:00:00", "out_time": "16:00:00"}])   # a Saturday
    c = repo.add_comp_day({"employee_id": 102, "earned_date": "2026-10-10", "reason": "t"})
    repo.update_comp_day(c["id"] if isinstance(c, dict) else c, {"taken_date": taken})
    rep = ots.build(repo, [repo.get_employee(102)], "2026-10", "2026-10-31")[102]
    d = next(x for x in rep["days"] if x["date"] == "2026-10-10")
    assert d["transport"] == 300 and d["weighted"] == (0 if counts else 16), taken


# ---------------------------------------------------------------- the page and who sees what
def test_an_employee_sees_only_their_own_month(app, world, repo):
    _long_day(repo)
    b = login(app, "101")
    for tab in ("attendance", "allowance", "overtime"):
        page = b.get(f"/attendance/my/attendance?month=2026-10&tab={tab}").get_data(as_text=True)
        assert "My attendance" in page and "Ali A1" in page and "Sara A2" not in page, tab
    page = b.get("/attendance/my/attendance?month=2026-10&tab=team").get_data(as_text=True)               # no Team tab: back to their own
    assert 'id="team-overtime"' not in page and 'id="my-attendance"' in page
    nav = page.split('aria-label="Main"')[1].split("</nav>")[0]
    assert ">My attendance<" in nav and ">My schedule<" in nav


def test_a_manager_sees_their_own_month_and_their_team_only(app, world, repo):
    add_user(repo, "301", "manager", ["Team A"], name="Mona Boss")                             # a manager with an HR number
    repo.save_settings({"team_managers": {"Team A": ["301"]}})                                 # the team's one manager (v5.17)
    b = login(app, "301")
    page = b.get("/attendance/my/attendance?month=2026-10").get_data(as_text=True)
    assert 'id="my-attendance"' in page and "Mona Boss" in page
    team = b.get("/attendance/my/attendance?month=2026-10&tab=team").get_data(as_text=True)
    assert "Ali A1" in team and "Sara A2" in team and "Omar B1" not in team and "Supervisor" in team
    nav = page.split('aria-label="Main"')[1].split("</nav>")[0]
    assert ">My attendance<" in nav


def test_a_manager_without_an_hr_number_gets_the_team_tab(app, world, repo, mgr1_b):
    page = mgr1_b.get("/attendance/my/attendance?month=2026-10").get_data(as_text=True)
    assert 'id="team-overtime"' in page and "Ali A1" in page and "Omar B1" not in page


def test_the_administrator_sets_the_overtime_rules(app, world, repo, admin_b):
    form = {"action": "overtime", **{f"ot_{k}": v for k, v in settings_svc.OVERTIME_DEFAULTS.items()}}
    form["ot_evening_rate"], form["ot_meal_lunch"] = "1.6", "120"
    assert admin_b.post("/admin/settings", form).status_code == 302
    ot = settings_svc.overtime_rules(settings_svc.load(repo))
    assert ot["evening_rate"] == 1.6 and ot["meal_lunch"] == 120 and ot["night_from"] == "22:00"
    bad = {**form, "ot_cap_pct": "abc"}
    assert admin_b.post("/admin/settings", bad).status_code == 400
    bad = {**form, "ot_evening_from": "23:00"}                                                  # after the night rate
    assert admin_b.post("/admin/settings", bad).status_code == 400
    page = admin_b.get("/admin/settings").get_data(as_text=True)
    assert 'id="overtime"' in page and "70.4" in page


def test_month_parameter_is_safe():
    assert ots.month_of("2026-13", "2026-10-14") == "2026-10" and ots.month_of("x", "2026-10-14") == "2026-10"
    assert ots.month_of("2026-02", "2026-10-14") == "2026-02"


def test_a_day_uploaded_later_becomes_real(app, world, repo):
    """The administrator may upload yesterday's sheet today: until then the day is expected, then it is counted from the sheet."""
    emp = repo.get_employee(101)
    before = ots.build(repo, [emp], "2026-10", "2026-10-14")[101]
    d11 = next(d for d in before["days"] if d["date"] == "2026-10-11")
    assert d11["expected"] and d11["note"] == "waiting for the HR sheet" and before["overtime"]["raw"] == 0
    a = repo.create_attendance_import({"filename": "late", "file_sha256": "late", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 101, "work_date": "2026-10-11", "in_time": "07:00:00", "out_time": "19:00:00"}])
    after = ots.build(repo, [emp], "2026-10", "2026-10-14")[101]
    d11 = next(d for d in after["days"] if d["date"] == "2026-10-11")
    assert not d11["expected"] and d11["kind"] == "morning" and d11["raw"] == 4 and d11["meals"] == ["breakfast", "lunch"]


def test_a_day_off_worked_eats_like_its_check_in_time():
    start = lambda t: None                                                                     # no shift start matches
    assert otd.meal_shift("09:30", start) == "morning" and otd.meal_shift("15:00", start) == "evening"
    assert otd.meal_shift("23:00", start) == "night" and otd.meal_shift("02:00", start) == "night"
    assert otd.meal_shift("07:05", lambda t: "morning") == "morning"


# ---------------------------------------------------------------- one Workforce team per person (owner, v5.17)
def test_one_workforce_team_unclassified_never_and_the_admin_chooses(app, world, repo, admin_b):
    repo.directory.store.update_user("102", {"teams": ["Unclassified", "Team B", "Team A"]})
    assert repo.get_employee(102)["teams"] == ["Team B"]                                   # the first real team; never "Unclassified"
    page = admin_b.get("/admin/employees").get_data(as_text=True)
    assert 'aria-label="Workforce team of Sara A2"' in page
    assert admin_b.post("/admin/employees", {"user_id": "102", "team": "Team A"}).status_code == 302
    assert repo.get_employee(102)["teams"] == ["Team A"]
    assert admin_b.post("/admin/employees", {"user_id": "102", "team": "Unclassified"}).status_code == 400
    assert admin_b.post("/admin/employees", {"user_id": "102", "team": "Team C"}).status_code == 400   # not on the account
    assert sum(1 for e in repo.list_employees() if e["id"] == 102) == 1                    # listed once, never twice


def test_an_account_without_a_team_is_flagged(app, world, repo, admin_b):
    repo.directory.store.update_user("202", {"teams": ["Unclassified"]})
    page = admin_b.get("/admin/employees").get_data(as_text=True)
    assert "have no team" in page and "Nour B2" in page.split("have no team")[1]


# ---------------------------------------------------------------- ONE manager per team in Workforce (owner, v5.17)
def test_a_second_manager_of_a_team_sees_only_their_own_pages(app, world, repo):
    add_user(repo, "302", "manager", ["Team A"], name="Second Boss")                     # Team A now has mgr1 and 302
    repo.save_settings({"team_managers": {"Team A": ["mgr1"]}})
    b = login(app, "302")
    assert b.get("/planning/day").status_code == 403 and b.get("/team-plan").status_code == 403
    assert b.get("/attendance/my/attendance").status_code == 200                        # their own month
    menu = b.get("/attendance/my/attendance").get_data(as_text=True).split('aria-label="Main"')[1].split("</nav>")[0]
    assert ">My attendance<" in menu and ">My schedule<" in menu and ">Today<" not in menu and ">Schedule<" not in menu
    assert ">E-mail<" in menu or "/email" in menu                                         # the portal itself is unchanged
    assert login(app, "mgr1").get("/planning/day").status_code == 200


def test_a_team_manager_never_sees_the_employees_amounts(app, world, repo, mgr1_b, admin_b, gm_b):
    """Owner (v5.18): a team manager sees the overtime and the shifts of the team, never their money - only their own amounts."""
    page = mgr1_b.get("/attendance/my/attendance?month=2026-10").get_data(as_text=True)
    table = page.split('id="team-overtime"')[1].split("</table>")[0]
    assert "Shifts so far" in table and "Allowance so far" not in table and "EGP" not in table
    for b in (admin_b, gm_b):                                                                   # the office still sees them
        table = b.get("/attendance/my/attendance?month=2026-10&tab=team").get_data(as_text=True).split('id="team-overtime"')[1].split("</table>")[0]
        assert "Allowance so far" in table and "EGP" in table
