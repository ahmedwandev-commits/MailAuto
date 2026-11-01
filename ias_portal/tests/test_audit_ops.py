"""Regression tests for bugs found in the Operations Hub audit (calllog/ops)."""
from datetime import datetime

import pytest

from calllog import auth as auth_module
from calllog.ops import mail_outbox, settings as hub_settings
from calllog.ops.store import OpsStore
from conftest import Browser


# ----------------------------------------------------------------------------------------------------------- helpers
def make_user(app, admin, uid, role="user", teams=None, email=None):
    body = {"user_id": uid, "name": uid.title(), "department": "X", "password": "TempPass123", "role": role,
            "teams": ["Flex Cube"] if teams is None else teams}                         # nobody is without a team (v5.17)
    if email:
        body["email"] = email
    r = admin.post("/api/admin/users", body)
    assert r.status_code == 201, r.get_json()
    b = Browser(app)
    b.login(uid, "TempPass123")
    b.client.post("/account/password", data={"current_password": "TempPass123", "new_password": "MyOwnPass123",
                                              "confirm_password": "MyOwnPass123", "csrf_token": b.csrf})
    return b


def mail_row(mid, team, status="Pending Reply"):
    return {"message_id": mid, "row_id": mid, "subject": f"Subject {mid}", "team": team, "all_teams": [team],
            "status": status, "received_time": "2026-10-11T08:00:00", "sla_status": "Within SLA"}


def write_email_snapshot(app, rows):
    app.extensions["ops"].write_snapshot("email", {"stats": {}, "teams": [], "rows": rows},
                                         generated_at="2026-10-11T08:30:00")


def rules_on_disk(app):
    fresh = OpsStore(app.config["SETTINGS"]["data_dir"])
    return hub_settings.load(fresh)["email"]["classification"]["system_team_map"]


# ------------------------------------------------------------------------------------- mail outbox: no lost mails
def test_outbox_keeps_a_mail_queued_by_another_process(tmp_path):
    """The collector marked a mail sent from its cached copy of the outbox and dropped a mail the web app had queued a
    moment before (another process, same shared folder)."""
    web, collector = OpsStore(str(tmp_path)).init(), OpsStore(str(tmp_path)).init()
    now = datetime(2026, 10, 31, 10, 0)
    mail_outbox.add(web, ["a@example.test"], "first", "<p>1</p>", now=now)
    first = mail_outbox.pending(collector)                  # the collector reads (and caches) the outbox
    mail_outbox.add(web, ["b@example.test"], "second", "<p>2</p>", now=now)
    mail_outbox._mark(collector, first[0]["id"], sent_at=now.isoformat(), now=now)
    doc = OpsStore(str(tmp_path)).read_json(web.state_dir / "mail-outbox.json")
    assert sorted(i["subject"] for i in doc["items"]) == ["first", "second"]
    assert [i["subject"] for i in mail_outbox.pending(OpsStore(str(tmp_path)))] == ["second"]


def test_outbox_claim_still_answers_true_once(tmp_path):
    store = OpsStore(str(tmp_path)).init()
    now = datetime(2026, 10, 31, 10, 0)
    item_id = mail_outbox.add(store, ["a@example.test"], "x", "<p>x</p>", now=now)
    assert mail_outbox._claim(store, item_id, now) is True
    assert mail_outbox._claim(OpsStore(str(tmp_path)), item_id, now) is False


# ------------------------------------------------------------------------------- settings: atomic read-modify-write
def test_settings_patch_does_not_lose_a_change_saved_by_another_process(tmp_path):
    a, b = OpsStore(str(tmp_path)).init(), OpsStore(str(tmp_path)).init()
    hub_settings.load(a)                                    # a's cache holds the settings as they were
    hub_settings.patch(b, {"email": {"classification": {"system_team_map": {"KeyB": "Flex Cube"}}}})
    saved = hub_settings.patch(a, {"email": {"sla_target_minutes": 45}})
    assert saved["email"]["classification"]["system_team_map"] == {"KeyB": "Flex Cube"}
    assert saved["email"]["sla_target_minutes"] == 45


def test_settings_update_saves_nothing_when_invalid_or_change_raises(tmp_path):
    store = OpsStore(str(tmp_path)).init()
    hub_settings.patch(store, {"email": {"sla_target_minutes": 30}})
    with pytest.raises(hub_settings.SettingsError):
        hub_settings.patch(store, {"email": {"sla_target_minutes": 0}})

    def boom(current):
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        hub_settings.update(store, boom)
    assert hub_settings.load(OpsStore(str(tmp_path)))["email"]["sla_target_minutes"] == 30


@pytest.mark.parametrize("value", ["²", "--5", "1²"])
def test_settings_number_check_never_crashes(value):
    clean, errors = hub_settings.validate({"email": {"interval_minutes": value}})
    assert any(e["path"] == "email.interval_minutes" for e in errors)
    assert clean["email"]["interval_minutes"] == 1


def test_two_managers_adding_rules_at_once_both_keep_theirs(app, admin, monkeypatch):
    """Manager A's rule request read the settings, manager B saved a rule meanwhile, then A saved its copy of the WHOLE
    rule map - B's rule was gone."""
    a = make_user(app, admin, "mgra", role="manager", teams=["Flex Cube"], email="a@x.test")
    original = auth_module.may_act_on_team
    injected = {}

    def may_act(team, user=None):
        if not injected:                                    # B's request finishes while A's is under way
            injected["done"] = True
            other = OpsStore(app.config["SETTINGS"]["data_dir"])
            hub_settings.patch(other, {"email": {"classification": {"system_team_map": {"CardKey": "Card Management"}}}})
        return original(team, user)
    monkeypatch.setattr(auth_module, "may_act_on_team", may_act)
    r = a.post("/api/ops/email/classification/rule", {"keyword": "FlexKey", "team": "Flex Cube"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["system_team_map"] == {"CardKey": "Card Management", "FlexKey": "Flex Cube"}
    assert rules_on_disk(app) == {"CardKey": "Card Management", "FlexKey": "Flex Cube"}


def test_classification_rule_permissions_still_apply(app, admin):
    a = make_user(app, admin, "mgra", role="manager", teams=["Flex Cube"], email="a@x.test")
    b = make_user(app, admin, "mgrb", role="manager", teams=["Card Management"], email="b@x.test")
    assert a.post("/api/ops/email/classification/rule", {"keyword": "FlexKey", "team": "Flex Cube"}).status_code == 200
    r = b.post("/api/ops/email/classification/rule", {"keyword": "flexkey", "team": "Card Management"})
    assert r.status_code == 403                             # another team's keyword: not taken over
    assert b.delete("/api/ops/email/classification/rule?keyword=FlexKey").status_code == 403
    assert b.delete("/api/ops/email/classification/rule?keyword=Nope").status_code == 404
    assert rules_on_disk(app) == {"FlexKey": "Flex Cube"}
    r = a.delete("/api/ops/email/classification/rule?keyword=flexkey")
    assert r.status_code == 200 and r.get_json()["system_team_map"] == {}
    assert rules_on_disk(app) == {}


def test_settings_put_still_saves_and_merges_with_rules_added_meanwhile(app, admin):
    base = admin.get("/api/ops/settings").get_json()["settings"]
    a = make_user(app, admin, "mgra", role="manager", teams=["Flex Cube"], email="a@x.test")
    assert a.post("/api/ops/email/classification/rule", {"keyword": "FlexKey", "team": "Flex Cube"}).status_code == 200
    page = hub_settings.deep_merge(base, {"email": {"sla_target_minutes": 50}})
    r = admin.put("/api/ops/settings", {"settings": page, "base": base})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["changed"] == ["email.sla_target_minutes"]
    saved = hub_settings.load(OpsStore(app.config["SETTINGS"]["data_dir"]))
    assert saved["email"]["sla_target_minutes"] == 50
    assert saved["email"]["classification"]["system_team_map"] == {"FlexKey": "Flex Cube"}
    bad = admin.put("/api/ops/settings", {"settings": hub_settings.deep_merge(base, {"email": {"sla_target_minutes": 0}})})
    assert bad.status_code == 400


# ------------------------------------------------------------------------------------------- e-mail actions: input
def test_employee_moving_an_assigned_mail_that_left_the_scan_gets_404_not_500(app, admin):
    write_email_snapshot(app, [mail_row("m1", "Flex Cube")])
    emp = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    make_user(app, admin, "mgr1", role="manager", teams=["Flex Cube"], email="mgr1@x.test")
    app.extensions["ops"].set_override("assignments", "gone", {"assigned_to_email": "emp1@x.test", "team": "Flex Cube",
                                                               "marked_by_id": "admin"})
    r = emp.post("/api/ops/email/team", {"message_id": "gone", "team": "Card Management"})
    assert r.status_code == 404
    assert "gone" not in app.extensions["ops"].overrides("team")
    mgr = Browser(app)
    mgr.login("mgr1", "MyOwnPass123")
    assert mgr.get("/api/ops/notifications").get_json()["unread"] == 0      # nobody told about a move that did not happen


def test_employee_moving_an_open_team_mail_still_works(app, admin):
    write_email_snapshot(app, [mail_row("m1", "Flex Cube")])
    emp = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    r = emp.post("/api/ops/email/team", {"message_id": "m1", "team": "Card Management"})
    assert r.status_code == 200, r.get_json()
    assert app.extensions["ops"].overrides("team")["m1"]["team"] == "Card Management"


def test_numeric_user_ids_in_a_json_body_do_not_crash(app, admin):
    write_email_snapshot(app, [mail_row("m1", "Flex Cube")])
    emp = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    make_user(app, admin, "emp2", teams=["Flex Cube"], email="emp2@x.test")
    app.extensions["ops"].set_override("assignments", "m1", {"assigned_to_email": "emp1@x.test", "team": "Flex Cube",
                                                             "marked_by_id": "admin"})
    r = emp.post("/api/ops/my/email/reassign", {"message_id": "m1", "to_user_id": 12345})
    assert r.status_code == 400                              # not one of the teammates - a clear answer, not a 500
    r = emp.post("/api/ops/email/done", {"message_id": "m1", "as": 7})
    assert r.status_code == 403                              # "somebody else's page" - read-only, not a crash
    r = emp.post("/api/ops/my/email/reassign", {"message_id": "m1", "to_user_id": "emp2"})
    assert r.status_code == 200, r.get_json()


# ------------------------------------------------------------------------------- timeline: collector health is admin-only
def test_collector_failures_on_the_timeline_are_for_administrators_only(app, admin):
    """A manager of every team (scope None) got the collector's 'scan failed: <error text>' events, which every other
    page hides from managers (they act on mails and tickets; only administrators monitor the collectors)."""
    from calllog.ops import events
    ops = app.extensions["ops"]
    ops.add_events([events.job_event("email", "2026-10-11T08:00:00", False, True, "C:\\share\\secret path: access denied"),
                    {"ts": "2026-10-11T08:05:00", "module": "itsm", "level": "info", "kind": "itsm_new", "text": "1 new X",
                     "items": [{"number": "CO1", "summary": "s", "role": "IMP Card Management"}], "count": 1}])
    allteams = make_user(app, admin, "mgrall", role="manager", teams=["__all__"], email="all@x.test")   # every team (v5.17: no team is refused)
    kinds = [e["kind"] for e in allteams.get("/api/ops/events?day=2026-10-11").get_json()["events"]]
    assert kinds == ["itsm_new"]
    kinds = [e["kind"] for e in admin.get("/api/ops/events?day=2026-10-11").get_json()["events"]]
    assert sorted(kinds) == ["itsm_new", "scan_failed"]


# ------------------------------------------------------------------------------- Done / Reopen: assignee or managers only
def test_a_teammate_cannot_mark_done_or_reopen_somebody_elses_assigned_mail(app, admin):
    """An employee may open / take an OPEN mail of their team, but marking a teammate's assigned mail Done (audited as
    "by manager") or reopening it was allowed too."""
    write_email_snapshot(app, [mail_row("m1", "Flex Cube")])
    other = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    owner = make_user(app, admin, "emp2", teams=["Flex Cube"], email="emp2@x.test")
    mgr = make_user(app, admin, "mgr1", role="manager", teams=["Flex Cube"], email="mgr1@x.test")
    ops = app.extensions["ops"]
    ops.set_override("assignments", "m1", {"assigned_to_email": "emp2@x.test", "team": "Flex Cube", "marked_by_id": "admin"})
    assert other.get("/api/ops/email/message?message_id=m1").status_code == 200      # reading it is still allowed
    assert other.post("/api/ops/email/done", {"message_id": "m1"}).status_code == 404
    assert not ops.overrides("assignments")["m1"].get("done_at")
    assert owner.post("/api/ops/email/done", {"message_id": "m1"}).status_code == 200
    assert other.delete("/api/ops/email/done?message_id=m1").status_code == 404
    assert ops.overrides("assignments")["m1"].get("done_at")
    assert mgr.delete("/api/ops/email/done?message_id=m1").status_code == 200          # the team's manager still can
    assert not ops.overrides("assignments")["m1"].get("done_at")
