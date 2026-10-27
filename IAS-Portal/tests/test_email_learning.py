"""Learning the e-mail classification from replies (calllog/ops/engines/email_engine.py, email_rules.learn_keyword): an
Unclassified mail a team member answers goes to that member's team, and a keyword of its subject is learned so the next
mail like it classifies itself - only for a mail nothing else classifies. The learned keywords are listed on E-mail ›
Unclassified, where a person may remove one (it is then never learned again).

Fake Outlook (tests/fake_outlook.py), a frozen clock (2026-10-11 12:00), made-up addresses (example.test)."""
from calllog.ops.engines import email_engine as eng
from calllog.ops.engines import email_rules as rules
from test_ops_views import chromium, demo_data, not_yet_written, server, tab  # noqa: F401  (browser fixtures)
from tests.test_email_engine import ALICE, CAROL, MANAGER, archive, email_settings, mail, make_ctx, reply, store  # noqa: F401
from tests.test_ops_api import flex_mgr, make_account, ops  # noqa: F401


def learning_ctx(store, on=True, **kw):  # noqa: F811
    return make_ctx(store, email_settings(classification={"learn_from_replies": on}), **kw)


def rows_of(result):
    return {r["subject"]: r for r in result.snapshot_data["rows"]}


# ---------------------------------------------------------------- the scan
def test_an_unclassified_mail_a_team_member_answers_goes_to_their_team_and_teaches_a_keyword(store):  # noqa: F811
    first = mail("FileNet scan error at branch 12", minutes_ago=100)
    ctx = learning_ctx(store)
    result = eng.run_scan(ctx, archive(first, reply(first, sender=ALICE, after=10)))
    row = rows_of(result)["FileNet scan error at branch 12"]
    assert (row["team"], row["classified_by"], row["status"], row["suggested_team"]) == ("Flex Cube", "reply", "Replied", "")
    assert result.snapshot_data["classification"]["counts"]["reply"] == 1
    learned = store.read_classify_learned()["rules"]
    assert list(learned) == ["FileNet"] and learned["FileNet"]["team"] == "Flex Cube"
    assert learned["FileNet"]["reply_by"] and learned["FileNet"]["subject"] == "FileNet scan error at branch 12"
    assert any("Learned 1 classification keyword(s)" in line and "FileNet -> Flex Cube" in line for line in ctx.logs)

    # the next mail like it, nobody has answered yet: it is Flex Cube's from the start
    again = mail("filenet is slow since the morning", minutes_ago=20)
    row = rows_of(eng.run_scan(learning_ctx(store), archive(again)))["filenet is slow since the morning"]
    assert (row["team"], row["classified_by"], row["status"]) == ("Flex Cube", "learned", "Pending Reply")
    assert row["classification_hint"] == "learned keyword: FileNet"


def test_the_keyword_tells_the_team_apart_and_is_never_a_name_or_an_everyday_word():
    assert rules.learn_keyword("RE: FileNet scan error at branch 12", ["Flexcube GL error"], ["Alice Example"]) == "FileNet"
    assert rules.learn_keyword("Murabaha portal down", ["Murabaha limits"]) == "portal"        # another team's word
    assert rules.learn_keyword("Request from Hosni", [], ["Hosni Example"]) == ""             # a name: never
    assert rules.learn_keyword("مشكلة في برنامج الارشيف الالكتروني") == "الارشيف"
    assert rules.learn_keyword("طلب صلاحية جديدة") == "" and rules.learn_keyword("please help") == ""
    assert rules.learn_keyword("INC0012345 error") == ""                                       # a ticket number
    assert rules.learn_keyword("Swift gpi tracker", ["Swift transfer"]) == "gpi"


def test_a_learned_keyword_never_beats_a_real_rule_and_a_mail_is_learned_from_once(store):  # noqa: F811
    store.update_classify_learned(lambda d: d.update(rules={"Flexcube": {"team": "Card Management"}}))
    row = rows_of(eng.run_scan(learning_ctx(store), archive(mail("Flexcube GL problem"))))["Flexcube GL problem"]
    assert (row["team"], row["classified_by"]) == ("Flex Cube", "system")      # the built-in map first

    first = mail("Murabaha portal down", minutes_ago=100)
    items = [first, reply(first, sender=CAROL, after=5)]
    eng.run_scan(learning_ctx(store), archive(*items))
    assert store.read_classify_learned()["rules"]["Murabaha"]["team"] == "MFA Team"

    def forget(d):                                  # a person removed it: blocked, never learned again
        d["blocked"]["Murabaha"] = d["rules"].pop("Murabaha")
    store.update_classify_learned(forget)
    eng.run_scan(learning_ctx(store), archive(*items))                        # the same mail: already learned from
    other = mail("Murabaha portal error again", minutes_ago=60)
    eng.run_scan(learning_ctx(store), archive(other, reply(other, sender=CAROL, after=5)))
    assert "Murabaha" not in store.read_classify_learned()["rules"]


def test_a_reply_from_someone_with_no_team_or_learning_off_changes_nothing(store):  # noqa: F811
    first = mail("Legacy vault access", minutes_ago=100)
    row = rows_of(eng.run_scan(learning_ctx(store), archive(first, reply(first, sender=MANAGER, after=5))))["Legacy vault access"]
    assert (row["team"], row["classified_by"]) == ("Unclassified", "none")       # the manager belongs to no team

    second = mail("Legacy vault quota", minutes_ago=100)
    result = eng.run_scan(learning_ctx(store, on=False), archive(second, reply(second, sender=ALICE, after=5)))
    row = rows_of(result)["Legacy vault quota"]
    assert (row["team"], row["classified_by"], row["suggested_team"]) == ("Unclassified", "none", "Flex Cube")
    assert store.read_classify_learned()["rules"] == {}                         # only a suggestion to approve


def test_a_check_run_learns_nothing(store):  # noqa: F811
    first = mail("FileNet scan error", minutes_ago=100)
    ctx = learning_ctx(store, dry_run=True)
    row = rows_of(eng.run_scan(ctx, archive(first, reply(first, sender=ALICE, after=5))))["FileNet scan error"]
    assert row["classified_by"] == "reply" and store.read_classify_learned()["rules"] == {}


# ---------------------------------------------------------------- the list on the E-mail page
def learned_doc(d):
    d["rules"].update({
        "FileNet": {"team": "Flex Cube", "subject": "FileNet scan error", "message_id": "<m1@example.test>",
                    "reply_by": "Alice Example", "learned_at": "2026-10-11T11:00:00"},
        "Visa": {"team": "Card Management", "subject": "Visa limits", "message_id": "<m2@example.test>",
                 "reply_by": "Dave Example", "learned_at": "2026-10-11T10:00:00"}})


def test_the_learned_keywords_are_listed_per_team_and_a_manager_removes_their_own(ops, admin, flex_mgr):  # noqa: F811
    ops.update_classify_learned(learned_doc)
    body = admin.get("/api/ops/email/classification/learned").get_json()
    assert body["auto"] is True and [w["keyword"] for w in body["learned"]] == ["FileNet", "Visa"]   # newest first
    mine = flex_mgr.get("/api/ops/email/classification/learned").get_json()["learned"]
    assert [(w["keyword"], w["team"], w["reply_by"]) for w in mine] == [("FileNet", "Flex Cube", "Alice Example")]
    assert flex_mgr.delete("/api/ops/email/classification/learned?keyword=Visa").status_code == 403
    assert flex_mgr.delete("/api/ops/email/classification/learned?keyword=nothing").status_code == 404
    assert flex_mgr.delete("/api/ops/email/classification/learned?keyword=filenet").status_code == 200
    doc = ops.read_classify_learned()
    assert list(doc["rules"]) == ["Visa"] and doc["blocked"]["FileNet"]["team"] == "Flex Cube"
    assert admin.put("/api/ops/settings", {"email": {"classification": {"learn_from_replies": False}}}).status_code == 200
    assert admin.get("/api/ops/email/classification/learned").get_json()["auto"] is False


def test_the_unclassified_tab_lists_the_learned_keywords_and_removes_one(app, demo_data, tab):  # noqa: F811
    store = app.extensions["ops"]
    store.update_classify_learned(learned_doc)
    t = tab()
    page = t.open("/email#unclassified", "#email-page .em-tab")
    page.wait_for_selector(".em-learned code")
    text = " ".join(page.inner_text(".em-learned").split())
    assert "FileNet → Flex Cube" in text and "answered by Alice Example" in text and "Visa → Card Management" in text
    page.click(".em-learned li:has-text('FileNet') button:has-text('Remove')")
    page.click("dialog.dlg button:has-text('Remove')")
    page.wait_for_function("() => ![...document.querySelectorAll('.em-learned code')].some((c) => c.textContent === 'FileNet')")
    assert "FileNet" in store.read_classify_learned()["blocked"]
    assert t.later_problems(*not_yet_written()) == [], t.problems
