"""Admin › Users (owner, v5.18): a manager is "Manager only" or "Manager + the team's schedule". Only the administrator chooses; one
or several managers of a team may have it (giving it to one never takes it from another)."""
from .support import add_user


def _put(b, uid, body):
    return b.client.put(f"/api/admin/users/{uid}", json=body, headers={"X-CSRF-Token": b.csrf})


def _row(b, uid):
    return next(u for u in b.client.get("/api/admin/users").get_json()["users"] if u["user_id"] == uid)


def test_the_administrator_gives_the_team_schedule_to_one_or_several_managers(app, world, repo, admin_b, mgr1_b):
    from .support import login
    add_user(repo, "mgr3", "manager", ["Team A"])
    mgr3_b = login(app, "mgr3")
    assert (_row(admin_b, "mgr1")["att_team"], _row(admin_b, "mgr1")["att_schedule"]) == ("Team A", False)   # two managers: nobody yet
    assert mgr1_b.get("/attendance/team-plan").status_code == 403 and mgr3_b.get("/attendance/team-plan").status_code == 403

    assert _put(admin_b, "mgr3", {"att_schedule": True}).status_code == 200
    assert repo.get_settings()["team_managers"]["Team A"] == ["mgr3"]
    assert _row(admin_b, "mgr3")["att_schedule"] and not _row(admin_b, "mgr1")["att_schedule"]
    assert mgr3_b.get("/attendance/team-plan").status_code == 200 and mgr1_b.get("/attendance/team-plan").status_code == 403

    assert _put(admin_b, "mgr1", {"att_schedule": True}).status_code == 200                       # a second one: both have it
    assert repo.get_settings()["team_managers"]["Team A"] == ["mgr3", "mgr1"]
    assert mgr1_b.get("/attendance/team-plan").status_code == 200 and mgr3_b.get("/attendance/team-plan").status_code == 200
    assert _row(admin_b, "mgr1")["att_schedule"] and _row(admin_b, "mgr3")["att_schedule"]

    assert _put(admin_b, "mgr3", {"att_schedule": False}).status_code == 200                      # "Manager only": mgr1 keeps it
    assert repo.get_settings()["team_managers"]["Team A"] == ["mgr1"]
    assert mgr3_b.get("/attendance/team-plan").status_code == 403 and mgr1_b.get("/attendance/team-plan").status_code == 200

    assert _put(admin_b, "mgr1", {"att_schedule": False}).status_code == 200                      # nobody has it now
    assert repo.get_settings()["team_managers"]["Team A"] == [] and mgr1_b.get("/attendance/team-plan").status_code == 403


def test_only_a_manager_of_one_team_has_the_choice(app, world, repo, admin_b, mgr1_b):
    assert _put(admin_b, "101", {"att_schedule": True}).status_code == 200                        # an employee: ignored
    assert "Team A" not in (repo.get_settings().get("team_managers") or {}) or repo.get_settings()["team_managers"]["Team A"] != ["101"]
    assert _row(admin_b, "101")["att_schedule"] is False
    assert mgr1_b.client.put("/api/admin/users/mgr1", json={"att_schedule": True}, headers={"X-CSRF-Token": mgr1_b.csrf}).status_code == 403


def test_an_older_list_of_several_managers_is_not_a_choice(app, world, repo, admin_b):
    """The "Team availability" list of an older version named several managers: only its first runs the team, until the
    administrator chooses in this version - then exactly the chosen ones."""
    from .support import login
    add_user(repo, "mgr3", "manager", ["Team A"])
    add_user(repo, "mgr4", "manager", ["Team B"])
    repo.save_settings({"team_managers": {"Team A": ["mgr3", "mgr1"], "Team B": ["mgr4", "mgr2"]}})       # an older version's lists
    assert repo.directory.team_managers_of("Team A") == ["mgr3"] and repo.directory.team_managers_of("Team B") == ["mgr4"]
    assert login(app, "mgr1").get("/attendance/team-plan").status_code == 403
    assert _put(admin_b, "mgr1", {"att_schedule": True}).status_code == 200                       # the first choice in this version
    assert repo.directory.team_managers_of("Team A") == ["mgr3", "mgr1"]
    assert repo.directory.team_managers_of("Team B") == ["mgr4"]                                  # the other teams stay as they worked
    assert repo.get_settings()["team_managers_chosen"] is True
