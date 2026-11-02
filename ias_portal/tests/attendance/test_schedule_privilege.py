"""Admin › Users (owner, v5.18): a manager is "Manager only" or "Manager + the team's schedule". Only the administrator chooses, and
one manager per team holds the schedule: giving it to one takes it from the other."""
from .support import add_user


def _put(b, uid, body):
    return b.client.put(f"/api/admin/users/{uid}", json=body, headers={"X-CSRF-Token": b.csrf})


def _row(b, uid):
    return next(u for u in b.client.get("/api/admin/users").get_json()["users"] if u["user_id"] == uid)


def test_the_administrator_gives_one_manager_the_team_schedule(app, world, repo, admin_b, mgr1_b):
    from .support import login
    add_user(repo, "mgr3", "manager", ["Team A"])
    mgr3_b = login(app, "mgr3")
    assert (_row(admin_b, "mgr1")["att_team"], _row(admin_b, "mgr1")["att_schedule"]) == ("Team A", False)   # two managers: nobody yet
    assert mgr1_b.get("/attendance/team-plan").status_code == 403 and mgr3_b.get("/attendance/team-plan").status_code == 403

    assert _put(admin_b, "mgr3", {"att_schedule": True}).status_code == 200
    assert repo.get_settings()["team_managers"]["Team A"] == ["mgr3"]
    assert _row(admin_b, "mgr3")["att_schedule"] and not _row(admin_b, "mgr1")["att_schedule"]
    assert mgr3_b.get("/attendance/team-plan").status_code == 200 and mgr1_b.get("/attendance/team-plan").status_code == 403

    assert _put(admin_b, "mgr1", {"att_schedule": True}).status_code == 200                       # takes it from mgr3
    assert repo.get_settings()["team_managers"]["Team A"] == ["mgr1"]
    assert mgr1_b.get("/attendance/team-plan").status_code == 200 and mgr3_b.get("/attendance/team-plan").status_code == 403

    assert _put(admin_b, "mgr1", {"att_schedule": False}).status_code == 200                      # "Manager only": nobody has it
    assert repo.get_settings()["team_managers"]["Team A"] == [] and mgr1_b.get("/attendance/team-plan").status_code == 403


def test_only_a_manager_of_one_team_has_the_choice(app, world, repo, admin_b, mgr1_b):
    assert _put(admin_b, "101", {"att_schedule": True}).status_code == 200                        # an employee: ignored
    assert "Team A" not in (repo.get_settings().get("team_managers") or {}) or repo.get_settings()["team_managers"]["Team A"] != ["101"]
    assert _row(admin_b, "101")["att_schedule"] is False
    assert mgr1_b.client.put("/api/admin/users/mgr1", json={"att_schedule": True}, headers={"X-CSRF-Token": mgr1_b.csrf}).status_code == 403
