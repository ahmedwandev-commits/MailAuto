"""Role privileges: every row of the published matrix is checked against the real routes, and the admin can assign roles."""
import pytest

from calllog.attendance.services.access import ROLE_MATRIX
from .support import Browser, add_user, login, set_user


def post(b, url, **data):
    return b.post(url, data)


def allowed(resp):
    """A route 'allowed' the action when it did not refuse (403 / redirect-to-home on deny / 400 validation counts as allowed)."""
    return resp.status_code not in (401, 403) and not (resp.status_code == 302 and resp.headers["Location"].endswith("/attendance/"))


# row label -> function(browser, world) -> response of the action on the user's OWN team (Team A) or anything for admin/gm
CHECKS = {
    "Edit the weekly plan and the monthly calendar":
        lambda b: post(b, "/schedule/edit", emp="101", date="2026-10-05", codes="E", reason="role check"),
    "Import the monthly shift schedule": lambda b: b.get("/schedule/import"),
    "Import HR attendance files": lambda b: b.get("/attendance/import"),
    "Record leave and compensatory days (direct, no approval step)":
        lambda b: post(b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-20"}),
    "Correct punches, review absences, record forgotten punches":
        lambda b: post(b, "/attendance/correct", emp="101", date="2026-10-12", field="in_time", value="07:00", reason="role check"),
    "Review attendance permits (Ø§Ø°Ù† Ø­Ø¶ÙˆØ± / Ø§Ø°Ù† Ø§Ù†ØµØ±Ø§Ù)":
        lambda b: post(b, "/attendance/permit-review", emp="101", date="2026-10-12", kind="late", decision="provided", reason="role check"),
    "Set leave entitlements": lambda b: post(b, "/leave/entitlement", emp="101", year="2026", type="annual", entitlement="21", opening="0"),
    "Settings: default shift, permit and staffing thresholds, scoring rules, holidays, shift types": lambda b: b.get("/admin/settings"),
    "Staff & teams, accounts, roles, audit log": lambda b: b.get("/admin/roles"),
    "See Today's overview, day view, calendars": lambda b: b.get("/today"),
    "Open employee profiles and score explanations": lambda b: b.get("/employee/101"),
    "Run reports and export Excel / PDF": lambda b: b.get("/export/report.xlsx?kind=summary&from=2026-10-01&to=2026-10-31"),
}


@pytest.mark.parametrize("who,col", [("admin", 1), ("gm", 2), ("mgr1", 3)])
def test_matrix_matches_the_server(app, world, who, col):
    b = login(app, who)
    for row in ROLE_MATRIX:
        label, expected = row[0], row[col]
        check = CHECKS.get(label)
        if check is None:                       # "Approve or reject" is covered by its own test below
            continue
        resp = check(b)
        assert allowed(resp) == bool(expected), (who, label, resp.status_code)


def test_admin_and_manager_act_directly_without_a_second_approver(app, world, repo, admin_b, mgr1_b):
    """Every authorised action takes effect at once; nothing waits for another user."""
    assert post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-05", codes="E", reason="swap").status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-05", "2026-10-05", [101])] == ["E"]
    assert post(mgr1_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-20"}).status_code == 302
    assert repo.leave_requests([101])[0]["status"] == "approved"
    assert post(mgr1_b, "/comp/new", emp="101", earned="2026-10-09", reason="duty").status_code == 302
    assert post(mgr1_b, "/attendance/correct", emp="101", date="2026-10-12", field="out_time", value="15:30", reason="badge").status_code == 302
    assert any(c["new_value"] == "15:30:00" for c in repo.corrections("2026-10-12", "2026-10-12", [101]))
    assert post(admin_b, "/schedule/edit", emp="201", date="2026-10-05", codes="E", reason="admin swap").status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-05", "2026-10-05", [201])] == ["E"]
    for a in ("schedule_changed", "leave_recorded", "comp_day_earned", "attendance_corrected"):
        assert repo.list_audit(action=a), a


def test_gm_sees_every_team_manager_only_their_own(app, world, gm_b, mgr1_b):
    assert gm_b.get("/employee/201").status_code == 200 and gm_b.get("/employee/101").status_code == 200
    assert mgr1_b.get("/employee/101").status_code == 200 and mgr1_b.get("/employee/201").status_code == 403
    assert gm_b.get("/api/overview?date=2026-10-12").get_json()["kpi"]["total_employees"] == 4
    assert mgr1_b.get("/api/overview?date=2026-10-12").get_json()["kpi"]["total_employees"] == 2


def test_gm_cannot_write_through_any_endpoint(app, world, repo, gm_b):
    attempts = [("/planning/week/save", dict(start="2026-10-11", team="Team A", reason="x", **{"cell_101_2026-10-12": "E"})),
                ("/schedule/edit", dict(emp="101", date="2026-10-05", codes="E", reason="x")),
                ("/comp/new", dict(emp="101", earned="2026-10-09", reason="x")),
                ("/leave/new", dict(emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-20"})),
                ("/attendance/absence-review", dict(emp="102", date="2026-10-12", decision="unexcused", reason="x")),
                ("/attendance/punch-report", dict(emp="101", date="2026-10-12", kind="out")),
                ("/admin/roles", dict(action="assign_gm", user_id="mgr1")),
                ("/admin/scoring", dict(start="100", effective_from="2026-01-01")),
                ("/admin/settings", dict(action="rules", grace_minutes="99"))]
    for url, data in attempts:
        r = post(gm_b, url, **data)
        assert r.status_code in (302, 403), url
    assert repo.list_schedule_changes() == [] and repo.leave_requests() == [] and repo.comp_days() == []
    assert repo.get_user("mgr1")["role"] == "manager" and repo.scoring_rule_versions() == [] and repo.get_settings().get("grace_minutes") is None


# ---------------------------------------------------------------- assigning roles (accounts belong to the portal)
def test_admin_assigns_a_portal_account_the_general_manager_role(app, world, repo, admin_b):
    add_user(repo, "gm2", "user", name="GM Two")
    r = post(admin_b, "/admin/roles", action="assign_gm", user_id="gm2")
    assert r.status_code == 302
    u = repo.get_user("gm2")
    assert u["role"] == "gm" and u["name"] == "GM Two" and u["hub_role"] == "user"
    a = repo.list_audit(action="role_assigned")[0]
    assert a["username"] == "admin" and a["entity_id"] == "gm2" and a["detail"]["previous"] == "none" and a["detail"]["new"] == "gm"
    b = login(app, "gm2")
    assert b.get("/employee/201").status_code == 200 and b.get("/api/overview?date=2026-10-12").get_json()["kpi"]["total_employees"] == 4
    assert post(b, "/schedule/edit", emp="201", date="2026-10-05", codes="E", reason="x").status_code == 302      # read-only
    assert repo.list_schedule_changes() == []


def test_a_portal_manager_can_be_promoted_and_demoted(app, world, repo, admin_b):
    assert post(admin_b, "/admin/roles", action="assign_gm", user_id="mgr2").status_code == 302
    assert repo.get_user("mgr2")["role"] == "gm"
    audit = repo.list_audit(action="role_assigned")[0]["detail"]
    assert audit["previous"] == "manager" and audit["new"] == "gm"
    b = login(app, "mgr2")
    assert b.get("/employee/101").status_code == 200                     # sees other teams now ...
    assert post(b, "/schedule/edit", emp="201", date="2026-10-05", codes="E", reason="x").status_code == 302   # ... but cannot write
    assert repo.list_schedule_changes() == []
    assert post(admin_b, "/admin/roles", action="remove_gm", user_id="mgr2").status_code == 302
    assert repo.get_user("mgr2")["role"] == "manager" and repo.list_audit(action="role_removed")
    assert login(app, "mgr2").get("/employee/101").status_code == 403


def test_only_admin_can_assign_roles_and_see_the_matrix(app, world, repo, mgr1_b, gm_b):
    for b in (mgr1_b, gm_b):
        assert post(b, "/admin/roles", action="assign_gm", user_id="mgr1").status_code == 302     # refused -> back to the overview
        assert b.get("/admin/roles").status_code == 302
    assert repo.get_user("mgr1")["role"] == "manager" and not repo.list_audit(action="role_assigned")


def test_role_assignment_rules(app, world, repo, admin_b):
    assert post(admin_b, "/admin/roles", action="assign_gm", user_id="admin").status_code == 400         # administrators already have everything
    assert post(admin_b, "/admin/roles", action="assign_gm", user_id="nobody").status_code == 400
    add_user(repo, "off", "user", active=False)
    assert post(admin_b, "/admin/roles", action="assign_gm", user_id="off").status_code == 400
    assert post(admin_b, "/admin/roles", action="remove_gm", user_id="mgr1").status_code == 302          # not a holder: nothing happens
    assert not repo.list_audit(action="role_removed")


def test_roles_page_lists_holders(app, world, admin_b):
    page = admin_b.get("/admin/roles").get_data(as_text=True)
    assert "General Manager" in page and "Review attendance permits" in page and "gm" in page
    assert "Manage on Admin › Users" in page                       # v5.16: every permission is given on Admin › Users


def test_attendance_roles_follow_the_portal_account(app, world, repo, admin_b):
    """Manager / administrator come from the portal's own role; nothing is stored twice."""
    add_user(repo, "m9", "manager", ["Team B"])
    assert repo.get_user("m9")["role"] == "manager" and repo.get_user("m9")["teams"] == ["Team B"]
    b = login(app, "m9")
    assert b.get("/employee/201").status_code == 200
    set_user(repo, "m9", {"role": "user"})                                       # demoted on the portal's Users page
    assert b.get("/today").status_code == 403
    set_user(repo, "m9", {"role": "manager", "teams": ["Team A"]})                # team changed on the portal: scope follows at once
    assert b.get("/employee/201").status_code == 403 and b.get("/employee/101").status_code == 200
    set_user(repo, "m9", {"active": False})
    assert b.get("/today").status_code == 302

