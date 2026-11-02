"""The monthly confirmation of the shift allowance: administrator sends -> employees confirm / object -> team managers confirm ->
General Manager confirms the sector -> the administrative affairs are told. E-mails go to the outbox the collector sends."""
from calllog.ops import mail_outbox

from .support import add_user, login, set_user


def _outbox(app):
    store = app.extensions["ops"]
    return store.read_json(store.state_dir / f"{mail_outbox.NAME}.json", {"items": []})["items"]


def _bell(app, uid):
    from calllog.ops import notifications
    with app.app_context():
        return notifications.read(app.extensions["ops"], uid).get("items") or []


def test_the_whole_month_is_confirmed_step_by_step(app, world, repo, admin_b, mgr1_b, mgr2_b, gm_b):
    for uid in ("101", "102", "201", "202", "mgr1", "mgr2", "gm"):
        set_user(repo, uid, {"email": f"{uid}@nbe.test"})
    add_user(repo, "hr1", "attendance")
    set_user(repo, "hr1", {"email": "hr1@nbe.test"})
    r = admin_b.post("/allowance/confirm/start", {"month": "2026-10"})
    assert r.status_code == 302
    sent = {a for i in _outbox(app) for a in i["to"]}
    assert {"101@nbe.test", "202@nbe.test", "mgr1@nbe.test", "mgr2@nbe.test"} <= sent and "gm@nbe.test" not in sent
    assert _bell(app, "101") and "confirm" in _bell(app, "101")[0]["title"]

    e101, e102 = login(app, "101"), login(app, "102")
    page = e101.get("/attendance/my").get_data(as_text=True)
    assert "Shift allowance · October 2026" in page and "Confirm my shift allowance" in page
    assert e101.post("/attendance/my/allowance", {"month": "2026-10", "answer": "confirm"}).status_code == 302
    assert e102.post("/attendance/my/allowance", {"month": "2026-10", "answer": "object", "note": ""}).status_code == 400   # a note is required
    assert e102.post("/attendance/my/allowance", {"month": "2026-10", "answer": "object", "note": "missing 2 nights"}).status_code == 302
    assert any("Objection" in i["subject"] and i["to"] == ["mgr1@nbe.test"] for i in _outbox(app))

    # a manager sees only their own team, and cannot confirm another
    page = mgr1_b.get("/allowance/confirm?month=2026-10").get_data(as_text=True)
    assert "Team A" in page and "Team B" not in page and "missing 2 nights" in page
    assert mgr1_b.post("/allowance/confirm/team", {"month": "2026-10", "team": "Team B"}).status_code == 403
    assert gm_b.post("/allowance/confirm/gm", {"month": "2026-10"}).status_code == 400            # teams first
    assert mgr1_b.post("/allowance/confirm/team", {"month": "2026-10", "team": "Team A"}).status_code == 302
    rnd = repo._get("allowance_rounds.json", {})["2026-10"]
    assert rnd["employees"]["102"]["status"] == "confirmed" and rnd["employees"]["102"]["on_behalf"]
    assert rnd["employees"]["102"]["objection"] == "missing 2 nights" and rnd["employees"]["101"]["on_behalf"] is False
    assert login(app, "102").post("/attendance/my/allowance", {"month": "2026-10", "answer": "confirm"}).status_code == 400   # team closed
    assert mgr2_b.post("/allowance/confirm/team", {"month": "2026-10", "team": "Team B"}).status_code == 302
    assert any(i["to"] == ["gm@nbe.test"] and "sector" in i["subject"] for i in _outbox(app))

    assert gm_b.post("/allowance/confirm/gm", {"month": "2026-10"}).status_code == 302
    done = [i for i in _outbox(app) if "fully confirmed" in i["subject"]]
    assert done and done[0]["to"] == ["hr1@nbe.test"]                                         # the administrative affairs
    assert mgr1_b.post("/allowance/confirm/member", {"month": "2026-10", "emp": "101"}).status_code == 400   # closed


def test_only_an_administrator_sends_and_reminders_go_once_a_day(app, world, repo, admin_b, mgr1_b, clock):
    import datetime as dt
    from calllog.attendance.services import allowance_confirm as ac
    assert mgr1_b.post("/allowance/confirm/start", {"month": "2026-10"}).status_code in (302, 403)
    assert repo._get("allowance_rounds.json", {}) == {}                                          # refused: nothing opened
    set_user(repo, "101", {"email": "101@nbe.test"})
    admin_b.post("/allowance/confirm/start", {"month": "2026-10"})
    store = app.extensions["ops"]
    before = len(_outbox(app))
    assert ac.queue_reminders(repo, dt.datetime(2026, 10, 15, 9, 0), store) == 0                # before 10:00
    assert ac.queue_reminders(repo, dt.datetime(2026, 10, 15, 10, 5), store) > 0
    assert ac.queue_reminders(repo, dt.datetime(2026, 10, 15, 11, 0), store) == 0               # once a day
    assert any(i["subject"].startswith("Reminder") for i in _outbox(app)[before:])


def test_the_collector_sends_the_outbox(app, world, repo, admin_b):
    from calllog.ops.engines.common import FakeMailer
    set_user(repo, "101", {"email": "101@nbe.test"})
    admin_b.post("/allowance/confirm/start", {"month": "2026-10"})
    store = app.extensions["ops"]
    mailer = FakeMailer()
    sent, failed = mail_outbox.send_pending(store, mailer)
    assert sent >= 1 and failed == 0 and mailer.sent[0]["to"]
    assert mail_outbox.pending(store) == []


def test_the_allowance_is_counts_only_no_money(app, world, repo, admin_b):
    """Owner's decision (checkpoint 4): the shift allowance is counted in days - no amount, no currency, even an old saved rate."""
    from calllog.attendance.services import allowance_confirm as ac
    repo.save_settings({"allowance_rules": {"morning": {"rate": 150}}})
    nums, rates, currency, _ = ac.figures(repo, "2026-10")
    assert not any(rates.values()) and currency == ""
    assert nums[201]["morning"] == 1 and nums[201]["amount"] == 0                                # 07:00-15:00: a full morning
    assert nums[101]["total"] == 0 and _not_counted(repo)[101]                                   # 07:10-15:00 is short of 8 h


def _not_counted(repo):
    from calllog.attendance.services import allowance_confirm as ac
    return ac.figures(repo, "2026-10")[3]
