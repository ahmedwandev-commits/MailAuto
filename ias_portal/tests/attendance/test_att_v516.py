"""v5.16: the Attendance administrator, "mark present" / "remove attendance", and the bell when a schedule or a shift changes."""
from .support import add_user, login


def _bell(app, uid):
    from calllog.ops import notifications
    with app.app_context():
        return notifications.read(app.extensions["ops"], uid).get("items") or []


def test_attendance_admin_has_every_attendance_right_and_nothing_else(app, world, repo, admin_b):
    add_user(repo, "hr1", "user", [])                                         # the sector's administrative affairs
    r = admin_b.post("/admin/roles", {"action": "assign_att_admin", "user_id": "hr1"})
    assert r.status_code == 302 and repo.directory.is_att_admin(repo.directory.store.get_user("hr1"))
    assert repo.list_audit(action="role_assigned")[0]["detail"]["role"] == "att_admin"
    b = login(app, "hr1")
    for url in ("/planning/day", "/team?team=Team%20B", "/modify?team=Team%20B", "/admin/settings", "/admin/scoring",
                "/schedule/import", "/hr/import", "/allowance", "/employees", "/reports"):
        assert b.get(url).status_code == 200, url                              # every team, every attendance page
    for url in ("/hub", "/calls", "/email", "/my", "/dashboard", "/system", "/analysis", "/admin"):
        r = b.client.get(url)
        assert r.status_code == 302 and "/attendance/" in r.headers["Location"], url     # nothing else of the portal
    assert b.client.get("/api/ops/overview").status_code == 403
    menu = b.get("/planning/day").get_data(as_text=True).split('aria-label="Main"')[1].split("</nav>")[0]
    assert ">Today<" in menu and "Attendance settings" in menu and ">Calls<" not in menu and "My work" not in menu
    # only a portal administrator hands the role out
    assert b.post("/admin/roles", {"action": "assign_att_admin", "user_id": "mgr1"}).status_code == 400
    assert admin_b.post("/admin/roles", {"action": "remove_att_admin", "user_id": "hr1"}).status_code == 302
    assert login(app, "hr1").get("/planning/day").status_code == 403


def test_the_attendance_admin_is_a_role_on_the_users_page(app, world, repo, admin_b):
    """Admin › Users: "Attendance admin" is chosen like any other role (stored as a portal user + the attendance mark)."""
    h = {"X-CSRF-Token": admin_b.csrf}
    r = admin_b.client.post("/api/admin/users", json={"user_id": "240999", "name": "HR Two", "role": "attendance",
                                                       "password": "Passw0rd!x"}, headers=h)
    assert r.status_code == 201 and r.get_json()["user"]["role"] == "attendance"
    u = repo.directory.store.get_user("240999")
    assert u["role"] == "attendance" and repo.directory.is_att_admin(u)          # its own role ...
    assert not any(e["id"] == 240999 for e in repo.list_employees())             # ... and never an employee (a numeric ID too)
    rows = {u["user_id"]: u for u in admin_b.client.get("/api/admin/users").get_json()["users"]}
    assert rows["240999"]["role"] == "attendance"
    repo.directory.store.update_user("240999", {"must_change_password": False})          # (a new account picks its password first)
    b = login(app, "240999")
    assert b.get("/admin/settings").status_code == 200 and b.client.get("/hub").status_code == 302    # attendance only
    r = admin_b.client.put("/api/admin/users/240999", json={"role": "user"}, headers=h)                     # back to an employee
    assert r.status_code == 200 and r.get_json()["user"]["role"] == "user"
    assert not repo.directory.is_att_admin(repo.directory.store.get_user("240999"))
    assert any(e["action"] == "user_updated" and e["details"].get("role", {}).get("from") == "attendance"
               for e in repo.directory.store.read_audit("2026-10-14", "2026-10-14"))
    # the other attendance permissions are ticks on the same form
    r = admin_b.client.put("/api/admin/users/240999", json={"att_gm": True, "att_allowance": True}, headers=h)
    assert r.status_code == 200 and r.get_json()["user"]["att_gm"] and r.get_json()["user"]["att_allowance"]
    u = repo.directory.store.get_user("240999")
    assert repo.directory.role_of(u) == "gm" and repo.directory.can_allowance(u)
    admin_b.client.put("/api/admin/users/240999", json={"att_gm": False, "att_allowance": False}, headers=h)
    assert repo.directory.role_of(repo.directory.store.get_user("240999")) != "gm"
    page = admin_b.get("/admin/roles").get_data(as_text=True)                                 # the Roles page only shows
    assert "Manage on Admin › Users" in page and "Make General Manager" not in page


def test_mark_present_and_remove_attendance(app, world, repo, admin_b, mgr1_b):
    from calllog.attendance.services import analytics_service as an
    back = "/attendance/team?team=Team%20A&date=2026-10-12"
    r = mgr1_b.post("/hr/correct", {"emp": "102", "date": "2026-10-12", "mode": "set", "in_time": "07:00", "out_time": "15:00",
                                    "reason": "forgot to punch", "next": back})
    assert r.status_code == 302 and r.headers["Location"].endswith(back)
    rows = an.apply_corrections(repo.attendance_records("2026-10-12", "2026-10-12", [102]),
                                repo.corrections("2026-10-12", "2026-10-13", [102]))
    assert rows[(102, "2026-10-12")]["in_time"] == "07:00:00" and rows[(102, "2026-10-12")]["out_time"] == "15:00:00"
    r = admin_b.post("/hr/correct", {"emp": "101", "date": "2026-10-12", "mode": "clear", "reason": "was not here"})
    assert r.status_code == 302
    rows = an.apply_corrections(repo.attendance_records("2026-10-12", "2026-10-12", [101]),
                                repo.corrections("2026-10-12", "2026-10-13", [101]))
    assert rows[(101, "2026-10-12")]["in_time"] is None and rows[(101, "2026-10-12")]["out_time"] is None
    assert mgr1_b.post("/hr/correct", {"emp": "201", "date": "2026-10-12", "mode": "clear", "reason": "x"}).status_code == 403
    assert mgr1_b.post("/hr/correct", {"emp": "102", "date": "2026-10-12", "mode": "set", "reason": "x"}).status_code == 400


def test_the_employee_is_told_when_a_manager_changes_their_shift(app, world, repo, mgr1_b):
    r = mgr1_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-20", "codes": "E", "reason": "cover"})
    assert r.status_code == 302
    items = _bell(app, "101")
    assert items and items[0]["type"] == "shift" and "changed" in items[0]["title"] and items[0]["link"] == "/attendance/my"
    assert "Morning" in items[0]["text"] and "Evening" in items[0]["text"]


def test_a_new_schedule_tells_its_employees_and_their_managers(app, world, repo, users):
    from calllog.attendance.services import notify
    with app.test_request_context():
        notify.schedule_published(repo, repo.get_user("admin"), 2026, 11, [101, 102], replaced=False)
    emp = _bell(app, "102")
    assert emp and emp[0]["type"] == "schedule" and "November 2026" in emp[0]["title"] and emp[0]["link"] == "/attendance/my"
    mgr = _bell(app, "mgr1")
    assert mgr and "your team" in mgr[0]["title"] and "/attendance/schedule?year=2026&month=11" == mgr[0]["link"]
    assert not _bell(app, "mgr2")                                              # another team's manager is not told
