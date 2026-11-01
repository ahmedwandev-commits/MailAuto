"""Workforce › Team schedule: a manager builds the month by drag and drop and sends it; once the General Manager approves it
is the official schedule at once (monthly schedule, My schedule, attendance); the administrative affairs add nights / days
off / official holidays (the official schedule follows) and send one e-mail to the To / Cc of the settings. Teams that did
not use the page keep their uploaded schedule."""
from calllog.ops import mail_outbox

from .support import login

M = "2026-11"          # Sun 1 Nov 2026; working days Sun-Thu


def _save(b, team, cells):
    return b.client.post("/attendance/team-plan/save", json={"month": M, "team": team, "cells": cells},
                         headers={"X-CSRF-Token": b.csrf})


def _outbox(app):
    store = app.extensions["ops"]
    return store.read_json(store.state_dir / f"{mail_outbox.NAME}.json", {"items": []})["items"]


def _official(repo):
    return {(e["employee_id"], e["work_date"]): e["shift_code"] for e in repo.schedule_entries("2026-11-01", "2026-11-30")}


def test_build_send_approve_is_official_then_mail(app, world, repo, admin_b, gm_b, mgr1_b, mgr2_b):
    # Team B already has an uploaded November schedule: it must be kept
    repo.commit_schedule_version_atomic(2026, 11, None, [{"employee_id": 201, "work_date": "2026-11-02", "shift_code": "E"}],
                                        "admin", replace=False)
    repo.create_leave_request({"employee_id": 102, "leave_type": "annual", "date_from": "2026-11-03", "date_to": "2026-11-03",
                               "days": 1, "status": "approved", "reason": "", "created_by": "admin"})

    page = mgr1_b.get(f"/attendance/team-plan?month={M}").get_data(as_text=True)          # one team: straight to it
    assert "Team A" in page and "Ali A1" in page and "tpData" in page and "Fri" not in page.split("tp-weeks")[1][:4000]
    assert 'data-code="PL"' not in page                                                  # leaves come from the Leave page
    assert mgr1_b.get(f"/attendance/team-plan?month={M}&team=Team B").status_code == 403

    r = _save(mgr1_b, "Team A", {"2026-11-01": {"M": [101, 201], "E": [102], "N": [101]},      # 201: not my team; N: not mine
                                 "2026-11-03": {"E": [102]},                                     # 102 on leave
                                 "2026-11-06": {"M": [101]}})                                    # a Friday: not a working day
    assert r.status_code == 200 and "on leave" in " ".join(r.get_json()["problems"])
    cells = repo._get("team_plans.json", {})[M]["teams"]["Team A"]["cells"]
    assert cells == {"2026-11-01": {"M": [101], "D": [], "E": [102], "N": [], "OFF": [], "CD": []}}

    assert mgr1_b.post("/team-plan/submit", {"month": M, "team": "Team A"}).status_code == 302
    assert _save(mgr1_b, "Team A", {"2026-11-02": {"M": [101]}}).status_code == 403          # sent: the manager waits
    assert (101, "2026-11-01") not in _official(repo)                                        # not official before approval
    assert mgr1_b.post("/team-plan/holiday", {"month": M, "date": "2026-11-05", "on": "1"}).status_code in (302, 403)
    assert not repo.list_holidays()
    assert gm_b.post("/team-plan/holiday", {"month": M, "date": "2026-11-05", "on": "1"}).status_code in (302, 403)
    assert gm_b.post("/team-plan/review", {"month": M, "team": "Team A", "decision": "return"}).status_code == 400   # a note
    assert gm_b.post("/team-plan/review", {"month": M, "team": "Team A", "decision": "approve"}).status_code == 302

    # approved = official at once: the monthly schedule and the employee's own schedule show it
    got = _official(repo)
    assert got[(101, "2026-11-01")] == "M" and got[(102, "2026-11-01")] == "E" and got[(201, "2026-11-02")] == "E"
    month_page = admin_b.get("/attendance/schedule?year=2026&month=11").get_data(as_text=True)
    assert "Ali A1" in month_page
    assert "Morning" in login(app, "101").get("/attendance/my?year=2026&month=11").get_data(as_text=True)
    assert "Official" in mgr1_b.get(f"/attendance/team-plan?month={M}&team=Team A").get_data(as_text=True)
    assert gm_b.post("/team-plan/publish", {"month": M}).status_code in (302, 403)            # the affairs send the e-mail

    # the administrative affairs: an official holiday and a night shift - the official schedule follows at once
    assert admin_b.post("/team-plan/holiday", {"month": M, "date": "2026-11-05", "name": "Holiday X", "on": "1"}).status_code == 302
    assert _save(admin_b, "Team A", {"2026-11-01": {"M": [101], "E": [102]}, "2026-11-02": {"N": [102]},
                                     "2026-11-05": {"M": [101]}}).status_code == 200             # 5 Nov is a holiday now
    got = _official(repo)
    assert got[(102, "2026-11-02")] == "N" and got[(101, "2026-11-02")] == "OFF"   # no shift on a working day: a rest day
    assert (101, "2026-11-05") not in got and (102, "2026-11-03") not in got   # the holiday; the leave day
    assert (101, "2026-11-06") not in got and got[(201, "2026-11-02")] == "E"
    assert repo._get("team_plans.json", {})[M]["teams"]["Team A"]["status"] == "approved"

    r = admin_b.post("/admin/settings", {"action": "team_plan", "team_plan_to": "hr@nbe.test; boss@nbe.test", "team_plan_cc": "gm@nbe.test"})
    assert r.status_code in (200, 302)
    assert admin_b.post("/team-plan/publish", {"month": M}).status_code == 302
    mails = [i for i in _outbox(app) if i["kind"] == "team_plan"]
    assert len(mails) == 1 and mails[0]["to"] == ["boss@nbe.test", "hr@nbe.test"] and mails[0]["cc"] == ["gm@nbe.test"]
    assert "Team A" in mails[0]["html"] and "Ali A1" in mails[0]["html"] and "Holiday X" in mails[0]["html"]
    assert _save(mgr1_b, "Team A", {"2026-11-02": {"M": [101]}}).status_code == 403          # approved: the manager is done


def test_the_manager_is_scheduled_too(app, world, repo, mgr1_b):
    from .support import add_user
    add_user(repo, "150", "manager", ["Team A"], name="Hany Manager")          # a manager with an HR number: on the roster
    repo.save_settings({"team_managers": {"Team A": ["mgr1"]}})                # mgr1 stays the team's one manager (v5.17)
    page = mgr1_b.get(f"/attendance/team-plan?month={M}&team=Team A").get_data(as_text=True)
    assert "Hany Manager" in page and "tp-mgr" in page and "morning shift is paid" in page
    from calllog.attendance.services import team_plan as tp
    with app.test_request_context():
        b = tp.board(repo, {"user_id": "admin", "role": "admin", "teams": []}, M, "Team A")
    assert {"id": 150, "name": "Hany Manager", "manager": True} in b["employees"]
    assert {"id": 101, "name": "Ali A1", "manager": False} in b["employees"]
    r = _save(mgr1_b, "Team A", {"2026-11-01": {"M": [150]}, "2026-11-02": {"E": [150]}})
    assert r.status_code == 200 and r.get_json()["days"] == 2


def test_comp_day_for_any_number_of_people_and_the_manager(app, world, repo, admin_b, gm_b, mgr1_b):
    from .support import add_user
    add_user(repo, "150", "manager", ["Team A"], name="Hany Manager")
    repo.save_settings({"team_managers": {"Team A": ["mgr1"]}})
    assert _save(mgr1_b, "Team A", {"2026-11-02": {"M": [101], "E": [102]}, "2026-11-03": {"CD": [101, 102, 150]}}).status_code == 200
    assert mgr1_b.post("/team-plan/submit", {"month": M, "team": "Team A"}).status_code == 302
    assert gm_b.post("/team-plan/review", {"month": M, "team": "Team A", "decision": "approve"}).status_code == 302
    rows = {(e["employee_id"], e["work_date"]): e for e in repo.schedule_entries("2026-11-01", "2026-11-30")}
    for emp in (101, 102, 150):                          # the comp day («بدل راحة»): a rest day inside, never absent
        assert rows[(emp, "2026-11-03")]["shift_code"] == "OFF" and rows[(emp, "2026-11-03")]["note"].startswith("Comp day")
    page = admin_b.get("/attendance/schedule?year=2026&month=11").get_data(as_text=True)
    assert ">CD<" in page and "Comp day" in page                     # the monthly schedule
    assert "Comp day" in login(app, "101").get("/attendance/my?year=2026&month=11").get_data(as_text=True)   # My schedule


def test_the_normal_day_is_placed_but_never_paid_as_a_shift(app, world, repo, admin_b, gm_b, mgr1_b):
    assert 'data-code="D"' in mgr1_b.get(f"/attendance/team-plan?month={M}&team=Team A").get_data(as_text=True)
    assert _save(mgr1_b, "Team A", {"2026-11-02": {"D": [101], "M": [102]}}).status_code == 200
    assert mgr1_b.post("/team-plan/submit", {"month": M, "team": "Team A"}).status_code == 302
    assert gm_b.post("/team-plan/review", {"month": M, "team": "Team A", "decision": "approve"}).status_code == 302
    got = _official(repo)
    assert got[(101, "2026-11-02")] == "D" and got[(102, "2026-11-02")] == "M"
    # both check in at 07:00: the Morning shift is paid, the Normal day is not (whatever the check-in time)
    a = repo.create_attendance_import({"filename": "n", "file_sha256": "n", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [
        {"employee_id": 101, "work_date": "2026-11-02", "in_time": "07:00:00", "out_time": "16:00:00"},
        {"employee_id": 102, "work_date": "2026-11-02", "in_time": "07:00:00", "out_time": "15:30:00"}])
    from calllog.attendance.services import allowance_service
    with app.test_request_context():
        days, skipped, _rules, _st = allowance_service.compute(repo, "2026-11-02", "2026-11-02", repo.list_employees())
    assert [(d["employee_id"], d["kind"]) for d in days] == [(102, "morning")]
    assert any(s["employee_id"] == 101 and "normal working day" in s["reason"] for s in skipped)


def test_night_and_day_off_are_only_for_the_affairs(app, world, repo, admin_b, mgr1_b):
    assert _save(admin_b, "Team A", {"2026-11-03": {"N": [101]}, "2026-11-04": {"OFF": [102]}}).status_code == 200
    page = mgr1_b.get(f"/attendance/team-plan?month={M}&team=Team A").get_data(as_text=True)
    assert 'data-code="N"' not in page and 'data-code="OFF"' not in page and 'data-code="CD"' in page   # not shown
    assert 'data-code="N"' in admin_b.get(f"/attendance/team-plan?month={M}&team=Team A").get_data(as_text=True)
    # the manager cannot move the person the affairs put on a night / a day off
    assert _save(mgr1_b, "Team A", {"2026-11-03": {"M": [101]}, "2026-11-04": {"CD": [102]}, "2026-11-05": {"M": [101]}}).status_code == 200
    cells = repo._get("team_plans.json", {})[M]["teams"]["Team A"]["cells"]
    assert cells["2026-11-03"]["N"] == [101] and cells["2026-11-03"]["M"] == []
    assert cells["2026-11-04"]["OFF"] == [102] and cells["2026-11-04"]["CD"] == []
    assert cells["2026-11-05"]["M"] == [101]


def test_the_mail_waits_for_every_team_that_used_it(app, world, repo, admin_b, mgr1_b, mgr2_b):
    assert _save(mgr1_b, "Team A", {"2026-11-01": {"M": [101]}}).status_code == 200
    assert _save(mgr2_b, "Team B", {"2026-11-01": {"M": [201]}}).status_code == 200
    r = admin_b.post("/team-plan/publish", {"month": M})
    assert r.status_code == 400 and "approved" in r.get_data(as_text=True)
    assert mgr1_b.post("/team-plan/submit", {"month": M, "team": "Team B"}).status_code == 403     # not their team
    page = admin_b.get(f"/attendance/team-plan?month={M}").get_data(as_text=True)
    assert "Team A" in page and "Team B" in page and "Official holidays" in page
