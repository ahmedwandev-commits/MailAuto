"""A mail shared by two teams: another team's reply (who, the text, the rating) is hidden from employees too (owner, v5.17), as it
already was from managers; their own team's reply stays visible."""
from conftest import Browser
from test_audit_ops import make_user, write_email_snapshot


def rows(replier_email, replier_team):
    base = {"message_id": "m1", "subject": "Shared", "received_time": "2026-10-11T08:00:00", "sla_status": "Within SLA"}
    return [{**base, "row_id": "r1", "team": "Flex Cube", "all_teams": ["Flex Cube", "Card Management"], "status": "Pending Reply"},
            {**base, "row_id": "r2", "team": replier_team, "all_teams": ["Flex Cube", "Card Management"], "status": "Replied",
             "first_reply_time": "2026-10-11T09:00:00", "first_reply_by": "Replier", "first_reply_email": replier_email,
             "reply_body_preview": "the reply text", "reply_count": 1, "teams_replied": [replier_team]}]


def test_an_employee_does_not_see_another_teams_reply(app, admin):
    emp = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    make_user(app, admin, "other", teams=["Card Management"], email="other@x.test")
    write_email_snapshot(app, rows("other@x.test", "Card Management"))
    out = emp.get("/api/ops/email/message?message_id=m1").get_json()
    assert out["reply"]["hidden"] is True and out["reply"]["team"] == "Card Management"
    assert "text" not in out["reply"] and "by" not in out["reply"] and "the reply text" not in str(out)


def test_an_employee_still_sees_their_own_teams_reply(app, admin):
    emp = make_user(app, admin, "emp1", teams=["Flex Cube"], email="emp1@x.test")
    make_user(app, admin, "mate", teams=["Flex Cube"], email="mate@x.test")
    write_email_snapshot(app, rows("mate@x.test", "Flex Cube"))
    out = emp.get("/api/ops/email/message?message_id=m1").get_json()
    assert not out["reply"].get("hidden") and out["reply"]["text"] == "the reply text" and out["reply"]["by"] == "Replier"
