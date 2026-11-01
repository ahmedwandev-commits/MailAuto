"""After an HR attendance file: forgotten check-in / check-out -> the employee and the direct manager are told (bell + e-mail);
an HR number that is not the portal User ID is linked to the person by name («خالد فكري» = «خالد محمد فكري»)."""
from calllog.ops import mail_outbox

from .support import add_employees, add_user, set_user
from .test_att_importers import att_file


def _outbox(app):
    store = app.extensions["ops"]
    return store.read_json(store.state_dir / f"{mail_outbox.NAME}.json", {"items": []})["items"]


def _bell(app, uid):
    from calllog.ops import notifications
    with app.app_context():
        return notifications.read(app.extensions["ops"], uid).get("items") or []


def test_forgotten_punches_reach_the_employee_and_the_manager(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "Ali", "team": "Team A"}, {"id": 2, "name": "Mona", "team": "Team A"},
                         {"id": 3, "name": "Omar", "team": "Team A"}])
    add_user(repo, "mgra", "manager", ["Team A"])
    repo.save_settings({"team_managers": {"Team A": ["mgra"]}})                            # the team's one manager (v5.17)
    for uid in ("1", "2", "3", "mgra"):
        set_user(repo, uid, {"email": f"{uid}@nbe.test"})
    f = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", None], [2, "Mona", "2026-10-01", None, "16:00"],
                                     [3, "Omar", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f, direct=True)
    assert r.status_code == 302
    mails = _outbox(app)
    to = [i["to"] for i in mails]
    assert ["1@nbe.test"] in to and ["2@nbe.test"] in to and ["3@nbe.test"] not in to         # a complete day: nothing
    mgr = [i for i in mails if i["to"] == ["mgra@nbe.test"]]
    assert len(mgr) == 1 and "Ali" in mgr[0]["html"] and "Mona" in mgr[0]["html"] and "Omar" not in mgr[0]["html"]
    assert _bell(app, "1") and "Forgotten punch" in _bell(app, "1")[0]["title"]
    assert _bell(app, "mgra") and "2 of your team" in _bell(app, "mgra")[0]["title"]


def test_an_hr_number_is_linked_to_the_person_by_name(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 240100, "name": "خالد محمد فكري", "team": "Team A"}, {"id": 240101, "name": "منى سعيد", "team": "Team A"}])
    f = att_file(tmp_path, "a.xlsx", [[9001, "خالد فكري", "2026-10-01", "08:00", "16:00"],
                                     [9002, "شخص غير موجود", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f, direct=True)
    assert r.status_code == 302
    rec = repo.attendance_records("2026-10-01", "2026-10-01")
    assert [x["employee_id"] for x in rec] == [240100]                                          # linked automatically by the name
    from calllog.attendance.services import attendance_service as ats
    assert ats.hr_links(repo) == {9001: 240100}
    page = admin_b.get("/hr/links").get_data(as_text=True)
    assert "9002" in page and "شخص غير موجود" in page and "9001" in page
    # the administrator links the other one by hand: its days come from the stored file
    assert admin_b.post("/hr/links", {"link:9002": "240101"}).status_code == 302
    assert sorted(x["employee_id"] for x in repo.attendance_records("2026-10-01", "2026-10-01")) == [240100, 240101]
    assert admin_b.post("/hr/links", {"link:9002": "none"}).status_code == 302 and 9002 not in ats.hr_links(repo)
