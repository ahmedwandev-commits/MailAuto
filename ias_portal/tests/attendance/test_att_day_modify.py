"""Dashboards of the General Manager and the managers (Day view only), the Modification screen, and the removal of imports for managers."""
from calllog.attendance.services import planning_service as ps

from .support import add_employees, add_user, login


def post(b, url, **data):
    return b.post(url, data)


# ---------------------------------------------------------------- Team availability: numbers per team + the managers the administrator chose
def _summary(repo, user, day="2026-10-12", team=None):
    import datetime as dt
    u, now = repo.get_user(user), dt.datetime(2026, 10, 20)
    td = ps.team_dashboard(repo, u, team or "", day, now)
    return {t["team"]: t for t in ps.team_summary(repo, td["rows"], td["shifts"], team)["rows"]}


def choose_managers(repo, mapping):
    repo.save_settings({"team_managers": mapping})


def test_team_summary_has_the_numbers_of_every_team(app, world, repo):
    repo.create_leave_request({"employee_id": 102, "leave_type": "annual", "date_from": "2026-10-12", "date_to": "2026-10-12", "days": 1, "status": "approved"})
    s = _summary(repo, "gm")
    assert set(s) == {"Team A", "Team B"}
    a, b = s["Team A"], s["Team B"]
    assert (a["total"], a["present"], a["leave"]) == (2, 1, 1)                      # 101 checked in (late), 102 is on leave
    assert a["shifts"] == {"M": 1, "E": 1, "N": 0, "D": 0}                           # scheduled per shift (leave still counts as scheduled)
    assert (b["total"], b["present"], b["leave"]) == (2, 1, 0) and b["shifts"] == {"M": 1, "E": 0, "N": 1, "D": 0}
    assert (b["not_arrived"], b["rest"]) == (1, 0)                                    # 202 was expected, no check-in recorded


def test_team_managers_are_only_the_ones_the_administrator_chose(app, world, repo):
    assert _summary(repo, "gm")["Team A"]["managers"] == []                          # nothing chosen: nobody is guessed from the accounts
    add_user(repo, "mgr3", "manager", ["Team A"], name="Second Manager")
    choose_managers(repo, {"Team A": ["mgr3"], "Team B": ["mgr2", "mgr1"]})
    s = _summary(repo, "admin")
    assert s["Team A"]["managers"] == ["Second Manager"] and s["Team B"]["managers"] == ["mgr2", "mgr1"]


def test_each_team_lists_its_own_people_and_the_administrator_picks_the_managers(app, world, repo, admin_b, mgr1_b):
    page = admin_b.get("/admin/settings").get_data(as_text=True)
    assert "Team managers" in page and 'name="mgr:Team A"' in page and 'name="mgr:Team B"' in page
    team_a = page.split('name="mgr:Team A"', 1)[1].split("</fieldset>")[0]
    assert 'value="101"' in team_a and 'value="mgr1"' in team_a and 'value="201"' not in team_a and 'value="mgr2"' not in team_a
    r = admin_b.post("/admin/settings", {"action": "team_managers", "mgr:Team A": ["mgr1", "101", "mgr2", "nobody"], "mgr:Team B": ["mgr2"]})
    assert r.status_code == 302
    assert repo.get_settings()["team_managers"] == {"Team A": ["mgr1", "101"], "Team B": ["mgr2"]}      # only people who are on that team
    assert repo.list_audit(action="settings_changed")
    assert mgr1_b.post("/admin/settings", {"action": "team_managers", "mgr:Team A": ["102"]}).status_code in (302, 403)
    assert repo.get_settings()["team_managers"] == {"Team A": ["mgr1", "101"], "Team B": ["mgr2"]}
    assert admin_b.post("/admin/settings", {"action": "team_managers"}).status_code == 302 and repo.get_settings()["team_managers"] == {}


def test_a_manager_account_is_listed_on_its_team_even_when_managers_are_not_employees(app, world, repo, admin_b):
    repo.save_settings({"roster_include_managers": False})
    assert "mgr1" not in [e["id"] for e in repo.list_employees()]
    assert 'value="mgr1"' in admin_b.get("/admin/settings").get_data(as_text=True)


def test_gm_team_availability_is_numbers_and_a_team_opens_its_people(app, world, repo, gm_b):
    choose_managers(repo, {"Team A": ["mgr1"], "Team B": ["mgr2"]})
    page = gm_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)       # Today: every team, one line of numbers each
    for text in ("Team A", "Team B", "Managers", "Employees", "On leave", "Expected to work", "Arrived"):
        assert text in page, text
    table = page.split('id="team-summary"')[1].split("</table>")[0]
    for text in ("mgr1", "mgr2", "Review permit", "Staffing"):                                     # the managers are a number
        assert text not in table, text
    assert 'href="/attendance/team?team=Team+A&amp;date=2026-10-12"' in page                       # the team is a link
    one = gm_b.get("/team?team=Team%20A&date=2026-10-12").get_data(as_text=True)                      # ...and opens its data
    assert "People" in one and "Ali A1" in one and "Sara A2" in one and "Omar" not in one
    assert "Managers:" in one and "mgr1" in one and "Today" in one and "Review permit" not in one     # the General Manager only reads
    menu = page.split('aria-label="Main"')[1].split("</nav>")[0]
    assert "Change a shift" not in page and "/attendance/modify" not in menu


def test_the_managers_column_is_a_number_and_how_many_of_them_arrived(app, world, repo):
    import datetime as dt
    from calllog.attendance.services import planning_service as ps
    choose_managers(repo, {"Team A": ["mgr1"], "Team B": ["mgr2", "101"]})
    u, now = repo.get_user("gm"), dt.datetime(2026, 10, 12, 20, 0)
    td = ps.team_dashboard(repo, u, "", "2026-10-12", now)
    s = {t["team"]: t for t in ps.team_summary(repo, td["rows"], td["shifts"], None, "2026-10-12", now)["rows"]}
    assert (s["Team A"]["manager_count"], s["Team B"]["manager_count"]) == (1, 2)
    assert s["Team A"]["manager_arrived"] == 0                       # mgr1 is not on the roster list here and has no sign-in on 12 Oct
    unchosen = ps.team_summary(repo, td["rows"], td["shifts"], "Team B", "2026-10-12", now)
    repo.save_settings({"team_managers": {}})
    td = ps.team_dashboard(repo, u, "", "2026-10-12", now)
    assert {t["team"]: t["manager_count"] for t in ps.team_summary(repo, td["rows"], td["shifts"], None, "2026-10-12", now)["rows"]} == {"Team A": 0, "Team B": 0}
    assert unchosen["rows"][0]["team"] == "Team B"


def test_the_managers_of_a_team_are_shown_by_number_only_in_the_table(app, world, repo, gm_b):
    choose_managers(repo, {"Team A": ["mgr1"]})
    page = gm_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)        # the teams table is on Today (v5.16)
    row = next(r for r in page.split("<tr ") if "team=Team+A" in r and "<td" in r)
    assert "mgr1" not in row and ">1" in row.replace(" ", "").replace("\n", "")
    assert "no manager assigned" not in page


def test_manager_team_availability_shows_only_his_team(app, world, repo, mgr1_b):
    choose_managers(repo, {"Team A": ["mgr1"], "Team B": ["mgr2"]})
    page = mgr1_b.get("/team?date=2026-10-12").get_data(as_text=True)               # his single team is opened for him
    assert "Team A" in page and "People" in page and "Ali A1" in page and 'class="crumbs"' in page
    assert 'id="team-summary"' in mgr1_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)
    assert "Team B" not in page and "mgr2" not in page and "Omar" not in page and "Nour" not in page
    assert mgr1_b.get("/team?team=Team%20B").status_code == 403
    assert 'name="team"' not in page                                               # a single-team manager has no team switch


def test_today_is_the_day_view_with_the_counters_and_the_teams(app, world, admin_b, gm_b, mgr1_b):
    # v5.16: dashboard 1 of 3 - the shifts, the day's counters, what needs attention and one line per team
    for b in (admin_b, gm_b, mgr1_b):
        page = b.get("/planning/day?date=2026-10-12").get_data(as_text=True)
        assert 'id="team-summary"' in page and "Needs attention" in page and "Expected to work" in page
        assert "Staffing" not in page and "Stand-by" in page and "Shifts" in page


def test_manager_with_two_teams_can_switch_between_them(app, world, repo):
    add_user(repo, "both", "manager", ["Team A", "Team B"])
    page = login(app, "both").get("/team?date=2026-10-12").get_data(as_text=True)
    assert "Team A" in page and "Team B" in page and 'name="team"' in page
    one = login(app, "both").get("/team?date=2026-10-12&team=Team%20B").get_data(as_text=True)
    assert "Team B" in one and "Team A</b>" not in one


# ---------------------------------------------------------------- the removed screens
def test_the_three_dashboards_today_team_person_for_every_role(app, world, admin_b, gm_b, mgr1_b):
    for b in (admin_b, gm_b, mgr1_b):
        assert b.get("/").headers["Location"].endswith("/attendance/planning/day")
        day = b.get("/planning/day").get_data(as_text=True)
        menu = day.split('aria-label="Main"')[1].split("</nav>")[0]                  # the sections are in the portal's menu
        assert ">Today<" in menu and ">Planning<" in menu and 'aria-label="Attendance sections"' not in day
        r = b.get("/today?date=2026-10-12")                                     # the old overview page is gone: old links land on Today
        assert r.status_code == 302 and r.headers["Location"].endswith("/attendance/planning/day?date=2026-10-12")
    # Today carries the teams table; /team without a team (several teams) is Today, with a team it is that team's dashboard
    page = mgr1_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)
    assert 'id="team-summary"' in page
    assert gm_b.get("/team?date=2026-10-12").headers["Location"].endswith("/attendance/planning/day?date=2026-10-12")
    person = mgr1_b.get("/employee/101?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert 'class="crumbs"' in person and "Today" in person and "Team A" in person      # Person: Today › Team › Person
    # Team availability is scoped like everything else; the General Manager only reads it
    assert b"Team B" not in mgr1_b.get("/team?date=2026-10-12").data and mgr1_b.get("/team?team=Team%20B").status_code == 403
    gm = gm_b.get("/team?team=Team%20B&date=2026-10-12").get_data(as_text=True)
    assert "Team B" in gm and "Review permit" not in gm and "Omar B1" in gm
    # the data API behind the screens is unchanged and still scoped
    assert mgr1_b.get("/api/overview?date=2026-10-12").get_json()["kpi"]["total_employees"] == 2


# ---------------------------------------------------------------- Modification screen
def test_modification_screen_lists_only_the_managers_team(app, world, mgr1_b, mgr2_b, admin_b, gm_b):
    page = mgr1_b.get("/modify?date=2026-10-12").get_data(as_text=True)
    assert "Ali A1" in page and "Sara A2" in page and "Omar" not in page and "Change shift" in page and "Add leave" in page
    assert mgr1_b.get("/modify?team=Team%20B").status_code == 403
    assert "Omar B1" in mgr2_b.get("/modify").get_data(as_text=True)
    assert "Omar B1" in admin_b.get("/modify?team=Team%20B").get_data(as_text=True)
    assert gm_b.get("/modify").status_code == 302                                    # the General Manager is read-only


def test_manager_changes_a_shift_from_the_modification_screen(app, world, repo, mgr1_b):
    r = post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-14", codes="E", reason="cover for Sara", next="/attendance/modify?date=2026-10-14&team=Team%20A")
    assert r.status_code == 302 and "/attendance/modify?date=2026-10-14" in r.headers["Location"]
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-14", "2026-10-14", [101])] == ["E"]
    page = mgr1_b.get("/modify?date=2026-10-14&team=Team%20A").get_data(as_text=True)
    assert "Evening" in page
    a = repo.list_audit(action="schedule_changed")[0]
    assert a["username"] == "mgr1" and a["detail"]["previous"] == "M" and a["detail"]["new"] == "E" and a["detail"]["reason"] == "cover for Sara"
    # a reason is required, other teams are refused, and a foreign "next" target is ignored
    assert post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-14", codes="N", reason="").status_code == 400
    assert post(mgr1_b, "/schedule/edit", emp="201", date="2026-10-14", codes="E", reason="x", next="/attendance/modify").status_code == 403
    r = post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-15", codes="N", reason="x", next="https://evil.example/x")
    assert r.status_code == 302 and "evil" not in r.headers["Location"]


def test_manager_adds_and_cancels_leave_from_the_modification_screen(app, world, repo, mgr1_b, mgr2_b):
    back = "/attendance/modify?date=2026-10-20&team=Team%20A"
    r = post(mgr1_b, "/leave/new", emp="102", type="annual", **{"from": "2026-10-20", "to": "2026-10-21"}, reason="trip", next=back)
    assert r.status_code == 302 and r.headers["Location"].endswith(back)
    req = repo.leave_requests([102])[0]
    assert req["status"] == "approved" and req["decided_by"] == "mgr1"                  # direct: no approval step
    page = mgr1_b.get("/modify?date=2026-10-20&team=Team%20A").get_data(as_text=True)
    assert "annual" in page and "Cancel leave" in page
    assert post(mgr2_b, f"/leave/{req['id']}/cancel", reason="x").status_code == 403
    r = post(mgr1_b, f"/leave/{req['id']}/cancel", reason="plans changed", next=back)
    assert r.status_code == 302 and repo.leave_requests([102])[0]["status"] == "cancelled"
    assert repo.list_audit(action="leave_recorded") and repo.list_audit(action="leave_cancelled")[0]["detail"]["reason"] == "plans changed"
    assert post(mgr1_b, "/leave/new", emp="201", type="annual", **{"from": "2026-10-20", "to": "2026-10-20"}).status_code == 403


def test_modification_screen_has_no_upload_and_no_import_link(app, world, mgr1_b):
    page = mgr1_b.get("/modify").get_data(as_text=True)
    assert 'type="file"' not in page and "/schedule/import" not in page and "/hr/import" not in page
    assert "Import" not in page.split('aria-label="Main"')[1].split("</nav>")[0]
