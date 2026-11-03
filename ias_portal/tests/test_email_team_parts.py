"""Owner (v5.17): a mail for several teams goes to every one of them and EACH team answers its own part - a team that has
not answered stays Pending (its SLA keeps running) whatever the other teams did."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from calllog.ops.engines import email_engine as ee

NOW = datetime(2026, 11, 2, 12, 0)
SETTINGS = {"business_emails": ["card@nbe.test", "flex@nbe.test"], "sla_target_minutes": 60,
            "teams": ["Card Management", "Flex Cube", "Open System"],
            "team_members_by_team": {"Card Management": ["card1@nbe.test"], "Flex Cube": ["flex1@nbe.test"]},
            "classification": {"address_team_map": {"card@nbe.test": "Card Management", "flex@nbe.test": "Flex Cube"}}}


def mail(sender, minutes_ago, to=("card@nbe.test", "flex@nbe.test"), subject="Codes for a colleague", mid="m1", conv="c1"):
    t = NOW - timedelta(minutes=minutes_ago)
    return {"subject": subject, "clean_subject": ee.clean_subject(subject), "body": "please add the codes", "full_thread_body": "",
            "sender_name": sender.split("@")[0], "sender_email": sender, "received_time": t, "conversation_id": conv,
            "conversation_index_root": "", "entry_id": f"{mid}-{sender}", "store_id": "", "message_id": f"{mid}-{sender}",
            "mailbox": "x", "folder": "Inbox", "to_cc": set(to), "business_addresses_in_to": set(to), "is_team_member": False}


def report(mails):
    cfg = ee.EmailConfig(SETTINGS)
    customers = [m for m in mails if m["sender_email"] not in cfg.team_members]
    rows, _ = ee.build_report(SimpleNamespace(now=lambda: NOW), cfg, customers, mails, now=NOW)
    return {r["team"]: r for r in rows}


def test_only_the_team_that_answered_is_replied_the_other_stays_pending():
    rows = report([mail("branch@nbe.test", 120), mail("flex1@nbe.test", 113)])          # Flex Cube answered after 7 min
    assert set(rows) == {"Card Management", "Flex Cube"}
    assert rows["Flex Cube"]["status"] == "Replied" and rows["Flex Cube"]["response_minutes"] == 7
    assert rows["Flex Cube"]["first_reply_email"] == "flex1@nbe.test"
    card = rows["Card Management"]
    assert card["status"] == "Pending Reply" and card["first_reply_by"] == "" and card["sla_status"] == "Breached"   # 2 h, target 1 h


def test_each_team_gets_its_own_reply_and_response_time():
    rows = report([mail("branch@nbe.test", 120), mail("flex1@nbe.test", 113), mail("card1@nbe.test", 90)])
    assert rows["Flex Cube"]["status"] == rows["Card Management"]["status"] == "Replied"
    assert rows["Flex Cube"]["response_minutes"] == 7 and rows["Card Management"]["response_minutes"] == 30
    assert rows["Card Management"]["first_reply_email"] == "card1@nbe.test"


def test_a_mail_for_one_team_is_unchanged():
    rows = report([mail("branch@nbe.test", 60, to=("card@nbe.test",)), mail("card1@nbe.test", 50, to=("card@nbe.test",))])
    assert list(rows) == ["Card Management"] and rows["Card Management"]["status"] == "Replied"
    assert not rows["Card Management"].get("is_split_child")


def test_nobody_answered_yet_both_wait():
    rows = report([mail("branch@nbe.test", 30)])
    assert {t: r["status"] for t, r in rows.items()} == {"Card Management": "Pending Reply", "Flex Cube": "Pending Reply"}
