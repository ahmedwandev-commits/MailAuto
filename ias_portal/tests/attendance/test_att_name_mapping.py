"""Name mapping: the schedule's names are linked to portal accounts once (administrator) and rebuilt/remembered for every import."""
from calllog.attendance.domain.textnorm import norm_keep
from calllog.attendance.services import schedule_service as ss

from .support import add_employees


def _import(repo, names):
    """A parsed schedule without a workbook: one morning shift on 2026-10-12 and 2026-10-13 for each name."""
    entries = [{"row": i, "invalid": False, "date": d, "name_raw": n, "name_norm": norm_keep(n), "shift_code": "M", "kind": "work",
                "stream": "", "note": ""} for i, (n, d) in enumerate(((n, d) for n in names for d in ("2026-10-12", "2026-10-13")), start=3)]
    parsed = {"entries": entries, "days": [], "issues": [], "groups": [], "title_year": None, "title_month": None, "sheet": "t"}
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "october.xlsx", "file_sha256": "a" * 64, "team": "Team A",
                                       "uploaded_by": "admin", "status": "preview", "parsed": parsed})
    return ss.import_directly(repo, repo.get_user("admin"), imp)


def _staff(repo):
    add_employees(repo, [{"id": 301, "name": "Hassan Ali Mahmoud", "team": "Team A"}, {"id": 302, "name": "Mona Said Fathy", "team": "Team A"}])


def _active_ids(repo):
    return sorted({e["employee_id"] for e in repo.schedule_entries("2026-10-01", "2026-10-31")})


def test_unmatched_names_are_listed_and_a_choice_is_remembered(app, repo, users, admin_b):
    _staff(repo)
    res = _import(repo, ["Hassan Ali", "Monaa S"])
    assert res["auto"]["names_skipped"] == ["Monaa S"] and _active_ids(repo) == [301]
    page = admin_b.get("/schedule/names").get_data(as_text=True)
    assert "Name mapping" in page and "Monaa S" in page and "No match" in page and "1 name(s) have no account yet" in page
    r = admin_b.post("/schedule/names", {"map:" + norm_keep("Monaa S"): "302"})
    assert r.status_code == 302 and repo.get_aliases()[norm_keep("Monaa S")] == 302
    assert repo.list_audit(action="schedule_names_mapped")
    assert "Linked by you" in admin_b.get("/schedule/names").get_data(as_text=True)


def test_rebuild_applies_the_mapping_to_a_month_already_imported(app, repo, users, admin_b):
    _staff(repo)
    _import(repo, ["Hassan Ali", "Monaa S"])
    admin_b.post("/schedule/names", {"map:" + norm_keep("Monaa S"): "302"})
    r = admin_b.post("/schedule/names", {"action": "reapply", "month": "2026-10"})
    assert r.status_code == 302
    assert _active_ids(repo) == [301, 302]
    assert repo.active_version(2026, 10)["version_no"] == 2                         # a new version; version 1 stays in the history
    assert admin_b.post("/schedule/names", {"action": "reapply", "month": "2026-09"}).status_code == 400


def test_a_link_can_be_forgotten_and_only_real_accounts_are_accepted(app, repo, users, admin_b):
    _staff(repo)
    _import(repo, ["Hassan Ali", "Monaa S"])
    key = norm_keep("Monaa S")
    admin_b.post("/schedule/names", {"map:" + key: "999"})                           # not an account
    assert key not in repo.get_aliases()
    admin_b.post("/schedule/names", {"map:" + key: "302"})
    assert key in repo.get_aliases()
    admin_b.post("/schedule/names", {"map:" + key: "clear"})
    assert key not in repo.get_aliases()


def test_manager_names_are_matched_and_can_be_linked(app, repo, users, admin_b):
    from .support import add_user
    add_user(repo, "700", "manager", ["Team A"], name="Karim Manager Selim")
    _staff(repo)
    res = _import(repo, ["Hassan Ali", "Karim Selim"])
    assert res["auto"]["names_skipped"] == [] and _active_ids(repo) == [301, 700]           # managers are on the roster by default
    page = admin_b.get("/schedule/names").get_data(as_text=True)
    assert "Karim Selim" in page and "· manager" in page and "Manager accounts are not on the attendance roster" not in page


def test_managers_off_the_roster_are_explained_and_one_click_brings_them_in(app, repo, users, admin_b):
    from .support import add_user
    add_user(repo, "700", "manager", ["Team A"], name="Karim Manager Selim")
    _staff(repo)
    repo.save_settings({"roster_include_managers": False})
    res = _import(repo, ["Hassan Ali", "Karim Selim"])
    assert res["auto"]["names_skipped"] == ["Karim Selim"]
    assert "Manager accounts are not on the attendance roster" in admin_b.get("/schedule/names").get_data(as_text=True)
    assert admin_b.post("/schedule/names", {"action": "include_managers"}).status_code == 302
    assert repo.get_settings()["roster_include_managers"] is True and repo.list_audit(action="settings_changed")
    page = admin_b.get("/schedule/names").get_data(as_text=True)
    assert "Manager accounts are not on the attendance roster" not in page and "Karim Manager Selim (700) · manager" in page


def test_name_mapping_is_administrator_only(app, users, repo, mgr1_b, gm_b, admin_b):
    for b in (mgr1_b, gm_b):
        assert b.get("/schedule/names").status_code in (302, 403)
        assert b.post("/schedule/names", {"map:x": "1"}).status_code in (302, 403)
    assert admin_b.get("/schedule/names").status_code == 200
    tabs = admin_b.get("/schedule/names").get_data(as_text=True)
    assert "Name mapping" in tabs
