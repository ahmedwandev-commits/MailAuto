"""Arrived from the portal sign-in: the first successful login of the day marks a person who is expected today as arrived, until the HR
file of the day is imported. It is only a mark - nothing about lateness, permits or scores comes from it."""
import datetime as dt

from calllog.attendance.services import analytics_service as an, planning_service as ps

from .support import login

TODAY = "2026-10-14"                                   # the clock of the tests: 09:00, the Morning shift (07:00) has started


def row(repo, emp, day=TODAY):
    td = ps.team_dashboard(repo, repo.get_user("admin"), "", day, dt.datetime(2026, 10, 14, 9, 0))
    return next(r for r in td["rows"] if r["employee_id"] == emp)


def test_nobody_has_arrived_before_signing_in_or_having_an_hr_row(app, world, repo):
    r = row(repo, 101)
    assert r["status"] == "not_arrived" and r["arrived_login"] is None and r["presence"]["key"] == "missing"


def test_the_first_sign_in_of_the_day_marks_the_person_as_arrived(app, world, repo, clock):
    clock.value = dt.datetime(2026, 10, 14, 7, 42, 10)
    login(app, "101")                                                        # 07:42 - the first
    clock.value = dt.datetime(2026, 10, 14, 9, 0, 0)
    login(app, "101")                                                        # a second sign-in changes nothing
    r = row(repo, 101)
    assert r["arrived_login"] == "07:42" and r["presence"]["key"] == "present" and "Arrived 07:42" in r["presence"]["label"]
    assert r["status"] == "not_arrived" and r["late_minutes"] == 0 and r["late_permit"] in (None, "")      # only a mark
    assert row(repo, 102)["arrived_login"] is None                           # somebody who did not sign in


def test_the_team_page_counts_and_shows_the_arrival(app, world, repo, clock, admin_b):
    clock.value = dt.datetime(2026, 10, 14, 7, 42, 10)
    login(app, "101")
    clock.value = dt.datetime(2026, 10, 14, 9, 0, 0)
    page = admin_b.get(f"/team?team=Team%20A&date={TODAY}").get_data(as_text=True)
    assert "Arrived 07:42" in page and "login 07:42" in page and "Not arrived" in page
    td = ps.team_dashboard(repo, repo.get_user("admin"), "Team A", TODAY, clock.value)
    assert (td["counts"]["present"], td["counts"]["not_arrived"]) == (1, 0)       # 102 is on the Evening shift: not started, so only "expected"
    day = admin_b.get(f"/planning/day?date={TODAY}").get_data(as_text=True)
    assert "Arrived 07:42" in day
    ov = admin_b.get(f"/api/overview?date={TODAY}").get_json()["kpi"]
    assert ov["arrived_by_login"] == 1 and ov["present_today"] >= 1


def test_the_hr_file_of_the_day_takes_over(app, world, repo, clock):
    clock.value = dt.datetime(2026, 10, 14, 7, 42, 10)
    login(app, "101")
    clock.value = dt.datetime(2026, 10, 14, 9, 0, 0)
    a = repo.create_attendance_import({"filename": "today.xlsx", "file_sha256": "t" * 64, "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 101, "work_date": TODAY, "in_time": "07:30:00", "out_time": None},
                                           {"employee_id": 102, "work_date": TODAY, "in_time": None, "out_time": None}])
    r = row(repo, 101)
    assert r["arrived_login"] is None and r["actual_in"] == "07:30:00" and r["late_minutes"] == 30       # HR: the real check-in and lateness
    assert row(repo, 102)["arrived_login"] is None                           # HR says no check-in: the sign-in does not override it


def test_past_days_and_leave_are_not_changed_by_a_sign_in(app, world, repo, clock):
    clock.value = dt.datetime(2026, 10, 14, 7, 42, 10)
    login(app, "101")
    login(app, "102")
    clock.value = dt.datetime(2026, 10, 14, 9, 0, 0)
    repo.create_leave_request({"employee_id": 102, "leave_type": "annual", "date_from": TODAY, "date_to": TODAY, "days": 1, "status": "approved"})
    assert row(repo, 102)["status"] == "on_leave" and row(repo, 102)["arrived_login"] is None           # on leave stays on leave
    assert an.todays_logins(repo, "2026-10-13", clock.value) == {}           # yesterday is the HR file's business


def test_managers_arrived_is_counted_by_number(app, world, repo, clock):
    repo.save_settings({"team_managers": {"Team A": ["mgr1"], "Team B": ["mgr2"]}})
    clock.value = dt.datetime(2026, 10, 14, 7, 5, 0)
    login(app, "mgr1")
    clock.value = dt.datetime(2026, 10, 14, 9, 0, 0)
    td = ps.team_dashboard(repo, repo.get_user("admin"), "", TODAY, clock.value)
    s = {t["team"]: t for t in ps.team_summary(repo, td["rows"], td["shifts"], None, TODAY, clock.value)["rows"]}
    assert (s["Team A"]["manager_count"], s["Team A"]["manager_arrived"]) == (1, 1)
    assert (s["Team B"]["manager_count"], s["Team B"]["manager_arrived"]) == (1, 0)
    assert "mgr1" not in str({k: v for k, v in s["Team A"].items() if k != "managers"})
