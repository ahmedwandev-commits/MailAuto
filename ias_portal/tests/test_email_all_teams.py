""""Assign to all teams" (owner, v5.17): a manager sends an e-mail to every team; each team answers its own part and a team
that has not answered stays Pending."""
from types import SimpleNamespace

from calllog.ops import overlay
from calllog.ops.engines import email_engine as ee
from test_audit_ops import make_user, write_email_snapshot
from test_email_team_parts import NOW, SETTINGS, mail


def test_the_engine_splits_a_mail_sent_to_all_teams_and_waits_for_each():
    cfg = ee.EmailConfig(SETTINGS)
    mails = [mail("branch@nbe.test", 120, to=("card@nbe.test",)), mail("card1@nbe.test", 110, to=("card@nbe.test",))]
    store = SimpleNamespace(overrides=lambda name: {"m1-branch@nbe.test": {"team": "Card Management",
                                                                           "teams": ["Card Management", "Flex Cube", "Open System"]}}
                            if name == "team" else {})
    rows, _ = ee.build_report(SimpleNamespace(now=lambda: NOW, store=store), cfg,
                              [m for m in mails if m["sender_email"] not in cfg.team_members], mails, now=NOW)
    by = {r["team"]: r["status"] for r in rows}
    assert by == {"Card Management": "Replied", "Flex Cube": "Pending Reply", "Open System": "Pending Reply"}


def test_the_page_shows_one_row_per_team_at_once():
    row = {"message_id": "m1", "row_id": "m1", "team": "Card Management", "all_teams": ["Card Management"], "status": "Replied",
           "received_time": "2026-11-02T10:00:00", "first_reply_time": "2026-11-02T10:10:00", "first_reply_by": "Card One",
           "first_reply_email": "card1@nbe.test", "response_minutes": 10, "sla_minutes": 60, "age_days": 0}
    out = overlay.apply_overrides([row], {"team": {"m1": {"team": "Card Management", "teams": ["Card Management", "Flex Cube"]}}},
                                  now=__import__("datetime").datetime(2026, 11, 2, 12, 0))
    by = {r["team"]: r for r in out}
    assert set(by) == {"Card Management", "Flex Cube"}
    assert by["Card Management"]["status"] == "Replied" and by["Card Management"]["first_reply_by"] == "Card One"
    flex = by["Flex Cube"]
    assert flex["status"] == "Pending Reply" and flex["first_reply_by"] == "" and flex["sla_status"] == "Breached"   # 2 h > 1 h


def test_a_manager_assigns_to_all_teams_and_unclassified_is_never_one_of_them(app, admin):
    write_email_snapshot(app, [{"message_id": "m1", "row_id": "m1", "subject": "Codes", "team": "Flex Cube", "all_teams": ["Flex Cube"],
                                "status": "Pending Reply", "received_time": "2026-10-11T08:00:00", "sla_status": "Within SLA"}])
    mgr = make_user(app, admin, "mgr1", role="manager", teams=["Flex Cube"], email="mgr1@x.test")
    r = mgr.post("/api/ops/email/team", {"message_id": "m1", "all": True})
    assert r.status_code == 200, r.get_json()
    saved = app.extensions["ops"].overrides("team")["m1"]
    assert len(saved["teams"]) >= 2 and "Unclassified" not in saved["teams"] and saved["team"] == saved["teams"][0]
    assert mgr.post("/api/ops/email/team", {"message_id": "m1", "teams": ["Flex Cube"]}).status_code == 400   # one team: use Change team
    r = mgr.post("/api/ops/email/team", {"message_id": "m1", "teams": ["Flex Cube", "Card Management", "Unclassified"]})
    assert r.status_code == 200 and app.extensions["ops"].overrides("team")["m1"]["teams"] == ["Flex Cube", "Card Management"]
    r = mgr.post("/api/ops/email/team", {"message_id": "m1", "team": "Open System"})                         # back to one team
    assert r.status_code == 200 and "teams" not in app.extensions["ops"].overrides("team")["m1"]
