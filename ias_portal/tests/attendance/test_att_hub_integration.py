"""The attendance module inside the portal: ONE set of accounts, the portal's teams and sessions, the portal's audit log and top bar."""
import json
from pathlib import Path

from .support import PW, Browser, add_employees, add_user, login, set_user, store_of


def post(b, url, **data):
    return b.post(url, data)


# ---------------------------------------------------------------- shared accounts: no second roster, no second user table
def test_employees_are_the_portal_accounts(app, repo, users):
    add_employees(repo, [{"id": 111, "name": "Ali", "team": "Team A", "department": "IT"}])
    e = repo.get_employee(111)
    assert (e["id"], e["name"], e["team"], e["teams"], e["department"], e["active"]) == (111, "Ali", "Team A", ["Team A"], "IT", True)
    assert [x["id"] for x in repo.list_employees()] == [111]
    # nothing was copied: the module stores no accounts and no roster of its own
    files = {p.name for p in (Path(store_of(repo).root) / "attendance").rglob("*") if p.is_file()}
    assert "users.json" not in files and "employees.json" not in files
    # a change on the portal shows up at once
    set_user(repo, "111", {"name": "Ali Hassan", "teams": ["Team B"]})
    e = repo.get_employee(111)
    assert e["name"] == "Ali Hassan" and e["teams"] == ["Team B"]
    set_user(repo, "111", {"active": False})
    assert repo.list_employees() == [] and repo.list_employees(active_only=False)[0]["active"] is False


def test_who_counts_as_an_employee(app, repo, users):
    add_employees(repo, [{"id": 5, "name": "Numeric user"}])
    add_user(repo, "alice", "user")                          # not an HR number: a portal account but not an attendance employee
    add_user(repo, "900", "manager", ["Team A"], name="Manager Nine")
    assert [e["id"] for e in repo.list_employees()] == [5, 900]          # managers are on the roster by default
    repo.save_settings({"roster_include_managers": False})                # ...unless the administrator unticks it
    assert [e["id"] for e in repo.list_employees()] == [5]
    repo.save_settings({"roster_include_managers": True})
    assert [e["id"] for e in repo.list_employees()] == [5, 900]
    assert repo.get_employee(900)["teams"] == ["Team A"]
    assert repo.get_employee(1) is None


def test_the_all_teams_marker_is_a_scope_not_a_team(app, repo, users):
    add_user(repo, "901", "manager", ["__all__"])
    repo.save_settings({"roster_include_managers": True})
    assert repo.get_employee(901)["teams"] == [] and repo.get_user("901")["teams"] == ["__all__"]


def test_creating_a_portal_user_through_the_portal_api_creates_an_attendance_employee(app, repo, users):
    b = Browser(app)
    b.login("admin", PW)
    r = b.client.post("/api/admin/users", json={"user_id": "230966", "name": "New Colleague", "department": "Identity", "password": "TempPass123",
                                                 "teams": ["Flex Cube"]}, headers={"X-CSRF-Token": b.csrf})
    assert r.status_code == 201, r.get_json()
    e = repo.get_employee(230966)
    assert e and e["name"] == "New Colleague" and e["teams"] == ["Flex Cube"]
    # and a manager of that team sees that very person in the attendance module, other managers do not
    add_user(repo, "mflex", "manager", ["Flex Cube"])
    assert b"New Colleague" in login(app, "mflex").get("/employees").data
    assert b"New Colleague" not in login(app, "mgr2").get("/employees").data


# ---------------------------------------------------------------- the portal's permissions decide
def test_attendance_access_follows_the_portal_role(app, repo, users):
    add_user(repo, "plain", "user")
    assert Browser(app).get("/today").status_code == 302                                     # anonymous -> the portal's sign-in page
    assert "/login" in Browser(app).get("/today").headers["Location"]
    assert Browser(app).get("/api/overview").status_code == 401
    plain = login(app, "plain")
    assert plain.get("/planning/day").status_code == 403 and plain.get("/api/overview").status_code == 403
    for who in ("admin", "mgr1", "gm"):
        assert login(app, who).get("/planning/day").status_code == 200


def test_manager_scope_comes_from_the_teams_on_the_portal_account(app, world, repo):
    add_user(repo, "none", "manager")                                                         # a manager without teams
    add_user(repo, "all", "manager", ["__all__"])
    add_user(repo, "both", "manager", ["Team A", "Team B"])
    add_user(repo, "uncl", "manager", ["Unclassified", "Team B"])                             # "Unclassified" is e-mail only
    ids = lambda who: {x["employee_id"] for x in login(app, who).get("/api/overview?date=2026-10-12").get_json().get("rows", [])}  # noqa: E731
    assert ids("none") == set()                                                               # never "everything" by accident
    assert ids("all") == {101, 102, 201, 202}                                                 # every team: still every team
    # ONE manager per team (v5.17): two managers on Team A and none chosen -> neither has the team
    assert ids("both") == set() and ids("mgr1") == set()
    repo.save_settings({"team_managers": {"Team A": ["both"], "Team B": ["uncl"]}})
    assert ids("both") == {101, 102}                                                          # one Workforce team: the first
    assert ids("uncl") == {201, 202}
    assert ids("mgr1") == set() and ids("mgr2") == set()                                      # not chosen: no team pages
    repo.save_settings({"team_managers": {"Team A": ["mgr1"], "Team B": ["mgr2"]}})
    assert ids("mgr1") == {101, 102} and ids("mgr2") == {201, 202}
    assert login(app, "none").get("/employee/101").status_code == 403


def test_a_manager_cannot_reach_another_managers_team_by_any_route(app, world, repo, mgr1_b):
    assert mgr1_b.get("/planning/day?team=Team%20B").status_code == 403
    assert mgr1_b.get("/modify?team=Team%20B").status_code == 403
    for url in ("/planning/week?team=Team%20B", "/planning/day?team=Team%20B", "/reports?team=Team%20B", "/employees?q=omar", "/schedule?year=2026&month=10"):
        assert b"Omar" not in mgr1_b.get(url).data and b"Nour" not in mgr1_b.get(url).data, url          # not even by name
    assert mgr1_b.post("/planning/week/save", {"start": "2026-10-11", "team": "Team B", "reason": "x", "cell_201_2026-10-12": "E"}).status_code == 403
    assert mgr1_b.post("/hr/permit-review", {"emp": "201", "date": "2026-10-12", "kind": "late", "decision": "excused", "reason": "x"}).status_code == 403
    assert mgr1_b.post("/leave/new", {"emp": "201", "type": "annual", "from": "2026-10-20", "to": "2026-10-20", "reason": "x"}).status_code == 403
    assert repo.list_schedule_changes() == [] and repo.leave_requests() == [] and repo.exception_reviews("2026-10-01", "2026-10-31") == {}


# ---------------------------------------------------------------- one audit log
def test_attendance_actions_go_into_the_portals_audit_log(app, world, repo, mgr1_b):
    assert mgr1_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-05", "codes": "E", "reason": "swap with Sara"}).status_code == 302
    rows = [r for r in store_of(repo).read_audit("2026-10-14", "2026-10-14") if r["action"] == "attendance.schedule_changed"]
    assert len(rows) == 1
    r = rows[0]
    assert r["actor"] == "mgr1" and r["actor_name"] == "mgr1" and r["ts"].startswith("2026-10-14 09:00")
    d = r["details"]
    assert d["reason"] == "swap with Sara" and d["previous"] == "M" and d["new"] == "E" and d["entity_id"] == "101"          # who / when / previous / new / reason
    # the module's own audit page reads the same trail
    assert repo.list_audit(action="schedule_changed")[0]["username"] == "mgr1"
    assert b"schedule_changed" in login(app, "admin").get("/admin/audit").data


def test_every_write_is_audited_with_previous_and_new_values(app, world, repo, admin_b, mgr1_b):
    mgr1_b.post("/leave/new", {"emp": "101", "type": "annual", "from": "2026-10-20", "to": "2026-10-20", "reason": "trip"})
    mgr1_b.post("/comp/new", {"emp": "101", "earned": "2026-10-09", "reason": "duty"})
    mgr1_b.post("/attendance/correct", {"emp": "102", "date": "2026-10-12", "field": "in_time", "value": "14:05", "reason": "badge"})
    admin_b.post("/hr/permit-review", {"emp": "101", "date": "2026-10-12", "kind": "late", "decision": "excused", "reason": "ok"})
    log = {r["action"]: r["details"] for r in store_of(repo).read_audit("2026-10-14", "2026-10-14")}
    for action in ("attendance.leave_recorded", "attendance.comp_day_earned", "attendance.attendance_corrected"):
        assert action in log, action
    assert log["attendance.leave_recorded"]["new"]["status"] == "approved" and log["attendance.leave_recorded"]["reason"] == "trip"
    assert log["attendance.permit_reviewed"]["previous"] is None and log["attendance.permit_reviewed"]["new"] == "excused"
    assert log["attendance.attendance_corrected"]["previous"] is None and log["attendance.attendance_corrected"]["new"] == "14:05:00"
    # the portal's other audit entries are not mixed into the module's page
    assert all(r["action"] for r in repo.list_audit())
    assert all(not r["action"].startswith("attendance.") for r in repo.list_audit())


# ---------------------------------------------------------------- the portal's top bar
def test_top_bar_shows_the_attendance_link_only_to_people_with_a_role(app, repo, users):
    add_user(repo, "plain", "user")
    for who, shown in (("admin", True), ("mgr1", True), ("gm", True), ("plain", False)):
        html = login(app, who).get("/dashboard" if who == "plain" else "/hub", follow_redirects=True).get_data(as_text=True)
        assert ('href="/attendance/planning/day"' in html) is shown, who               # v5.16: "Today" in the portal's menu
    assert 'href="/attendance/planning/day" aria-current="page"' in login(app, "admin").get("/attendance/planning/day").get_data(as_text=True)


def test_module_pages_wear_the_portals_top_bar_and_theme(app, world, admin_b):
    html = admin_b.get("/planning/day").get_data(as_text=True)
    assert "IAS" in html and "op-nav" in html and 'href="/hub"' in html and "theme.js" in html and 'class="att"' in html
    assert "attendance/attendance.css" in html and "<style" not in html


def test_module_front_door_sends_each_role_to_its_landing_page(app, world, admin_b, mgr1_b, gm_b):
    assert admin_b.get("/").headers["Location"].endswith("/attendance/planning/day")
    assert gm_b.get("/").headers["Location"].endswith("/attendance/planning/day")
    assert mgr1_b.get("/").headers["Location"].endswith("/attendance/planning/day")


def test_portal_password_rules_and_forced_change_apply_to_the_module(app, repo, users):
    set_user(repo, "mgr1", {"must_change_password": True})
    b = login(app, "mgr1")
    r = b.get("/planning/day")
    assert r.status_code == 302 and "/account/password" in r.headers["Location"]


def test_disabled_portal_account_loses_attendance_access_at_once(app, world, repo, mgr1_b):
    assert mgr1_b.get("/planning/day").status_code == 200
    set_user(repo, "mgr1", {"active": False})
    assert mgr1_b.get("/planning/day").status_code == 302


def test_upload_limits_cover_the_hr_workbooks(app):
    from calllog.app import LARGE_UPLOADS
    assert LARGE_UPLOADS[("POST", "/attendance/hr/import")] >= 16 * 1024 * 1024
    assert LARGE_UPLOADS[("POST", "/attendance/schedule/import")] >= 8 * 1024 * 1024
