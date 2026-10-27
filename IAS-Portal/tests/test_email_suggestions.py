"""Suggested e-mail classifications (calllog/ops/engines/email_engine.py, email_rules.learn_keyword, the
/api/ops/email/classification/suggestion* routes): an Unclassified mail a team member answers is PROPOSED for that
member's team, with a keyword of its subject to learn - nothing changes until the administrator approves it; then the
mail goes to the team and the keyword becomes a classification rule, so the next mail like it classifies itself.

Fake Outlook (tests/fake_outlook.py), a frozen clock (2026-10-11 12:00), made-up addresses (example.test)."""
from calllog.ops import demo
from calllog.ops.engines import email_engine as eng
from calllog.ops.engines import email_rules as rules
from test_ops_views import chromium, demo_data, not_yet_written, server, tab  # noqa: F401  (browser fixtures)
from tests.test_email_engine import ALICE, CAROL, MANAGER, archive, email_settings, mail, make_ctx, reply, store  # noqa: F401
from tests.test_ops_api import flex_mgr, make_account, ops, put_snapshot  # noqa: F401


def rows_of(result):
    return {r["subject"]: r for r in result.snapshot_data["rows"]}


def scanned(store, *items, **email):  # noqa: F811
    return eng.run_scan(make_ctx(store, email_settings(**email) if email else None), archive(*items))


# ---------------------------------------------------------------- the scan proposes, it never applies
def test_an_answered_unclassified_mail_is_proposed_for_the_repliers_team_with_a_keyword(store):  # noqa: F811
    first = mail("FileNet scan error at branch 12", minutes_ago=100)
    row = rows_of(scanned(store, first, reply(first, sender=ALICE, after=10)))["FileNet scan error at branch 12"]
    assert (row["team"], row["classified_by"]) == ("Unclassified", "none")              # nothing applied yet
    assert (row["suggested_team"], row["suggested_keyword"]) == ("Flex Cube", "FileNet")


def test_the_suggested_team_is_the_first_replier_who_belongs_to_a_team(store):  # noqa: F811
    first = mail("Legacy vault access", minutes_ago=100)
    result = scanned(store, first, reply(first, sender=MANAGER, after=5), reply(first, sender=CAROL, after=20))
    assert rows_of(result)["Legacy vault access"]["suggested_team"] == "MFA Team"     # the manager belongs to no team
    lone = mail("Legacy vault quota", minutes_ago=100)
    assert rows_of(scanned(store, lone, reply(lone, sender=MANAGER, after=5)))["Legacy vault quota"]["suggested_team"] == ""


def test_the_proposed_keyword_tells_the_team_apart_and_is_never_a_name_or_an_everyday_word():
    assert rules.learn_keyword("RE: FileNet scan error at branch 12", ["Flexcube GL error"], ["Alice Example"]) == "FileNet"
    assert rules.learn_keyword("Murabaha portal down", ["Murabaha limits"]) == "portal"        # another team's word
    assert rules.learn_keyword("Request from Hosni", [], ["Hosni Example"]) == ""             # a name: never
    assert rules.learn_keyword("مشكلة في برنامج الارشيف الالكتروني") == "الارشيف"
    assert rules.learn_keyword("طلب صلاحية جديدة") == "" and rules.learn_keyword("please help") == ""
    assert rules.learn_keyword("INC0012345 error") == ""                                       # a ticket number
    assert rules.learn_keyword("Swift gpi tracker", ["Swift transfer"]) == "gpi"


# ---------------------------------------------------------------- the administrator approves
def test_only_the_administrator_approves_and_the_next_mail_like_it_classifies_itself(app, ops, admin, flex_mgr, tmp_path):  # noqa: F811
    from calllog.ops.store import OpsStore
    engine_store = OpsStore(tmp_path / "engine").init()
    first, other = mail("FileNet scan error at branch 12", minutes_ago=100), mail("Vault key lost", minutes_ago=90)
    result = scanned(engine_store, first, reply(first, sender=ALICE, after=10), other, reply(other, sender=CAROL, after=5))
    put_snapshot(ops, "email", result.snapshot_data)                    # the real scan's snapshot, as the collector writes it
    mid = rows_of(result)["FileNet scan error at branch 12"]["message_id"]

    mine = flex_mgr.get("/api/ops/email/classification/suggestions").get_json()
    assert mine["may_approve"] is False and [s["suggested_team"] for s in mine["suggestions"]] == ["Flex Cube"]
    assert flex_mgr.post("/api/ops/email/classification/suggestion",
                         {"message_id": mid, "keyword": "FileNet", "team": "Flex Cube"}).status_code == 403
    every = admin.get("/api/ops/email/classification/suggestions").get_json()
    assert every["may_approve"] is True and len(every["suggestions"]) == 2
    one = next(s for s in every["suggestions"] if s["message_id"] == mid)
    assert one["keyword"] == "FileNet" and one["candidates"][0] == "FileNet" and one["reply_by"]

    assert admin.post("/api/ops/email/classification/suggestion",
                      {"message_id": mid, "keyword": one["keyword"], "team": "Flex Cube"}).status_code == 200
    rules_now = admin.get("/api/ops/settings").get_json()["settings"]["email"]["classification"]["system_team_map"]
    assert rules_now.get("FileNet") == "Flex Cube" and ops.overrides("team")[mid]["team"] == "Flex Cube"
    left = admin.get("/api/ops/email/classification/suggestions").get_json()["suggestions"]
    assert [s["suggested_team"] for s in left] == ["MFA Team"]                     # approved once, never again
    assert admin.delete(f"/api/ops/email/classification/suggestion?message_id={left[0]['message_id']}").status_code == 200
    assert admin.get("/api/ops/email/classification/suggestions").get_json()["suggestions"] == []

    # the next scan: the new rule classifies a mail like it before anybody answers
    again = mail("filenet slow since the morning", minutes_ago=20)
    row = rows_of(scanned(engine_store, again, classification={"system_team_map": rules_now}))["filenet slow since the morning"]
    assert (row["team"], row["classified_by"], row["suggested_team"]) == ("Flex Cube", "system", "")


def test_the_unclassified_tab_shows_the_suggestion_approve_for_the_admin_waiting_for_a_manager(app, demo_data, tab, tmp_path):  # noqa: F811
    from calllog.ops.store import OpsStore
    first = mail("FileNet scan error at branch 12", minutes_ago=100)
    result = scanned(OpsStore(tmp_path / "engine").init(), first, reply(first, sender=ALICE, after=10))
    put_snapshot(app.extensions["ops"], "email", result.snapshot_data)
    t = tab()
    page = t.open("/email#unclassified", "#email-page .em-tab")
    page.wait_for_selector(".em-suggest-item")
    assert page.input_value(".em-suggest-item .em-suggest-kw") == "FileNet"
    assert page.locator(".em-suggest-item button:has-text('Approve')").count() == 1
    assert t.later_problems(*not_yet_written()) == [], t.problems

    m = tab(demo.MANAGER_ID, demo_data["password"])                 # the Flex Cube manager follows, never approves
    page = m.open("/email#unclassified", "#email-page .em-tab")
    page.wait_for_selector(".em-suggest-item")
    text = " ".join(page.inner_text(".em-suggest-item").split())
    assert "waiting for the administrator's approval" in text and "FileNet" in text
    assert page.locator(".em-suggest-item button:has-text('Approve')").count() == 0
    assert m.later_problems(*not_yet_written()) == [], m.problems
