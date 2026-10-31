"""Role-based access, team isolation, CSRF, login throttling, and that every page renders."""
from .support import set_user
import pytest

from .support import Browser, add_user, login


def test_anonymous_is_redirected_and_api_is_401(app):
    b = Browser(app)
    assert b.get("/today").status_code == 302 and "/login" in b.get("/today").headers["Location"]
    assert b.get("/api/overview").status_code == 401
    assert b.get("/export/report.xlsx").status_code == 401


def test_no_public_registration(app):
    for url in ("/register", "/signup", "/api/register"):
        assert Browser(app).get(url).status_code == 404


def test_login_rules_and_throttle(app, users):
    b = Browser(app)
    assert b.login("admin", "wrong").status_code == 401
    assert b.login("nobody", "x").status_code == 401
    for _ in range(5):
        b.login("mgr1", "bad")
    assert b.login("mgr1", "Passw0rd!x").status_code == 429                  # locked out after repeated failures


def test_disabled_account_cannot_sign_in_or_keep_session(app, repo, users):
    b = login(app, "mgr1")
    assert b.get("/planning/day").status_code == 200
    set_user(repo, "mgr1", {"active": False})
    assert b.get("/planning/day").status_code == 302                           # session dropped
    assert Browser(app).login("mgr1", "Passw0rd!x").status_code == 403


def test_csrf_required_on_post(app, users):
    b = login(app, "admin")
    r = b.client.post("/schedule/edit", data={"emp": "1"})                    # no token
    assert r.status_code == 400
    r = b.client.post("/logout")
    assert r.status_code == 400


def test_forced_password_change_and_password_rules(app, repo):
    add_user(repo, "new", "manager", ["Team A"])
    set_user(repo, "new", {"must_change_password": True})
    b = login(app, "new")
    assert b.get("/planning/day").status_code == 302 and "/account/password" in b.get("/planning/day").headers["Location"]
    assert b.post("/account/password", {"current_password": "Passw0rd!x", "new_password": "short", "confirm_password": "short"}).status_code == 400
    assert b.post("/account/password", {"current_password": "Passw0rd!x", "new_password": "BrandNew#123", "confirm_password": "BrandNew#123"}).status_code == 302
    assert b.get("/planning/day").status_code == 200
    assert Browser(app).login("new", "BrandNew#123").status_code == 302


def test_manager_sees_only_own_team(app, world, mgr1_b, mgr2_b):
    r = mgr1_b.get("/api/overview?date=2026-10-12").get_json()
    assert {x["employee_id"] for x in r["rows"]} == {101, 102}
    assert r["kpi"]["total_employees"] == 2
    r2 = mgr2_b.get("/api/overview?date=2026-10-12").get_json()
    assert {x["employee_id"] for x in r2["rows"]} == {201, 202}
    assert mgr1_b.get("/employee/101").status_code == 200
    assert mgr1_b.get("/employee/201").status_code == 403                        # another team's record
    assert mgr1_b.get("/api/employee/201/summary").status_code == 403
    assert mgr1_b.get("/employee/201/score").status_code == 403
    assert b"Omar" not in mgr1_b.get("/employees").data and b"Ali" in mgr1_b.get("/employees").data
    assert b"Omar" not in mgr1_b.get("/schedule?year=2026&month=10").data


def test_manager_cannot_change_other_teams_data(app, world, repo, mgr1_b):
    r = mgr1_b.post("/schedule/edit", {"emp": "201", "date": "2026-10-05", "codes": "E", "reason": "x"})
    assert r.status_code == 403
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-05", "2026-10-05", [201])] == ["M"]
    assert mgr1_b.post("/leave/new", {"emp": "201", "type": "annual", "from": "2026-10-20", "to": "2026-10-21"}).status_code == 403
    assert mgr1_b.post("/attendance/correct", {"emp": "201", "date": "2026-10-12", "field": "in_time", "value": "08:00", "reason": "x"}).status_code == 403
    assert mgr1_b.post("/comp/new", {"emp": "201", "earned": "2026-10-09", "reason": "x"}).status_code == 403
    assert repo.leave_requests([201]) == [] and repo.corrections("2026-10-01", "2026-10-31") == []


def test_manager_edit_own_team_requires_reason_and_is_audited(app, world, repo, mgr1_b):
    r = mgr1_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-05", "codes": "E", "reason": ""})
    assert r.status_code == 400
    r = mgr1_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-05", "codes": "E", "reason": "swap with Sara"})
    assert r.status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-05", "2026-10-05", [101])] == ["E"]
    ch = repo.list_schedule_changes(2026, 10, 101)[0]
    assert (ch["old_value"], ch["new_value"], ch["reason"], ch["user_id"]) == ("M", "E", "swap with Sara", "mgr1")
    assert repo.list_audit(action="schedule_changed")[0]["username"] == "mgr1"
    # original import rows are preserved
    v = repo.active_version(2026, 10)
    assert any(h["shift_code"] == "M" and not h["is_current"] for h in repo.version_entries(v["id"], include_history=True) if h["work_date"] == "2026-10-05" and h["employee_id"] == 101)


def test_gm_sees_everything_but_is_read_only(app, world, repo, gm_b):
    r = gm_b.get("/api/overview?date=2026-10-12").get_json()
    assert r["kpi"]["total_employees"] == 4
    assert gm_b.get("/employee/201").status_code == 200
    assert gm_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-05", "codes": "E", "reason": "x"}).status_code == 302   # denied -> redirect
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-05", "2026-10-05", [101])] == ["M"]
    assert gm_b.get("/schedule/import").status_code == 302
    assert gm_b.get("/admin/settings").status_code == 302 and gm_b.get("/attendance/import").status_code == 302
    assert gm_b.post("/leave/new", {"emp": "101", "type": "annual", "from": "2026-10-20", "to": "2026-10-21"}).status_code == 302
    assert repo.leave_requests() == []


def test_manager_cannot_use_admin_pages(app, world, mgr1_b):
    for url in ("/admin/roles", "/admin/settings", "/admin/scoring", "/admin/audit", "/admin/employees", "/attendance/import"):
        assert mgr1_b.get(url).status_code == 302, url


def test_overview_numbers_for_a_day(app, world, admin_b):
    r = admin_b.get("/api/overview?date=2026-10-12").get_json()
    k = r["kpi"]
    assert (k["total_employees"], k["present_today"], k["late"], k["absent"]) == (4, 2, 1, 2)
    assert (k["morning"], k["evening"], k["night"]) == (2, 1, 1)
    assert k["needs_replacement"] == 2 and k["missing_records"] == 2
    by = {x["employee_id"]: x for x in r["rows"]}
    assert by[101]["late_minutes"] == 10 and by[101]["status"] == "late" and by[101]["expected_start"] == "07:00"
    assert by[102]["status"] == "absent"
    # a date without any attendance data never produces absences
    r = admin_b.get("/api/overview?date=2026-10-13").get_json()
    assert r["kpi"]["absent"] == 0 and r["kpi"]["no_data"] == 4 and r["data_loaded"] is False
    # filters
    r = admin_b.get("/api/overview?date=2026-10-12&team=Team%20B&shift=N").get_json()
    assert [x["employee_id"] for x in r["rows"]] == [202]
    # historical + upcoming dates are accepted
    assert admin_b.get("/api/overview?date=2026-10-30").get_json()["kpi"]["total_employees"] == 4


@pytest.mark.parametrize("url", [
    "/team?team=Team%20A&date=2026-10-12", "/schedule?year=2026&month=10", "/schedule?year=2026&month=10&view=days",
    "/schedule?year=2026&month=11", "/schedule/import", "/imports/history", "/schedule/edit?emp=101&date=2026-10-05", "/planning/week?team=Team%20A&start=2026-10-11",
    "/planning/week", "/planning/day?date=2026-10-12", "/planning/day?team=Team%20B", "/modify", "/modify?team=Team%20A&date=2026-10-12", "/team", "/team?team=Team%20A", "/team?date=2026-10-12", "/leave?tab=requests", "/leave?month=11&year=2026",
    "/employees", "/employees?q=ali", "/employee/101?from=2026-10-01&to=2026-10-13", "/employee/101/score?from=2026-10-01&to=2026-10-13",
    "/leave", "/comp", "/reports", "/attendance/import", "/templates/attendance.xlsx",
    "/templates/schedule.xlsx?year=2026&month=11", "/admin/roles", "/admin/employees", "/admin/settings", "/admin/scoring", "/admin/audit",
    "/account/password", "/healthz"] + [f"/reports?kind={k}&from=2026-10-01&to=2026-10-31" for k in (
        "summary", "ranking", "teams", "departments", "lateness", "hours", "punches", "leave", "comp", "coverage", "planned_actual", "trends", "rest")])
def test_every_page_renders_for_admin(app, world, admin_b, url):
    r = admin_b.get(url, follow_redirects=url.startswith("/team?date") or url == "/team")   # every team at once = Today (v5.16)
    assert r.status_code == 200, (url, r.data[:300])


def test_exports_are_valid_excel_files(app, world, admin_b):
    import io
    import openpyxl
    for kind in ("summary", "ranking", "coverage", "teams"):
        r = admin_b.get(f"/export/report.xlsx?kind={kind}&from=2026-10-01&to=2026-10-31")
        assert r.status_code == 200 and r.mimetype.endswith("spreadsheetml.sheet")
        wb = openpyxl.load_workbook(io.BytesIO(r.data))
        assert wb.active.max_row >= 5
    assert any(a["action"] == "report_exported" for a in world.list_audit())


def test_manager_export_is_team_scoped(app, world, mgr1_b):
    import io
    import openpyxl
    r = mgr1_b.get("/export/report.xlsx?kind=summary&from=2026-10-01&to=2026-10-31")
    ws = openpyxl.load_workbook(io.BytesIO(r.data)).active
    ids = {row[0] for row in ws.iter_rows(min_row=5, values_only=True) if isinstance(row[0], int)}
    assert ids == {101, 102}


def test_security_headers(app, users):
    r = Browser(app).get("/login")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"
    assert "inline" not in r.get_data(as_text=True).lower() or "<script>" not in r.get_data(as_text=True)


def test_no_inline_scripts_or_styles_in_any_template():
    import re
    from pathlib import Path
    for f in (Path(__file__).resolve().parents[2] / "calllog" / "templates" / "attendance").glob("*.html"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), f.name
        assert not re.search(r"<style|\sstyle=", text), f.name
        assert not re.search(r"\son(click|change|submit|load)=", text), f.name

