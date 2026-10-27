"""The Best Performance e-mail (calllog/ops/engines/best_performer.py): once a day, at its time on a working day, the
day's top 3 (score in security requests: requests handled + 2 per call answered + 3 per e-mail replied, as Insights ›
Analysis ranks them) get one celebration e-mail in English, with the Cc of the settings. Made-up people and data only."""
import re
from datetime import datetime

from calllog.ops import runner, settings as hub_settings
from calllog.ops.engines import best_performer as bp, common, daily_summary as ds
from calllog.storage import Store
from test_daily_summary import DAY, Mailer, ctx_for, world
from test_performance import _sla_day

ARABIC = re.compile(r"[؀-ۿ]")


def third_person(store, email="c3@example.test"):
    """Person 7003 (Card Management): one call = score 2."""
    store.create_user({"user_id": "7003", "name": "Person 7003", "role": "user", "active": True, "team": "Card Management",
                       "email": email})
    store.add_record({"id": "r7003", "date": DAY, "user_id": "7003", "time": f"{DAY}T11:00:00",
                      "reason": store.reasons()[0]["key"]})


def make_ctx(tmp_path, mailer, best=None, options=None, edit_users=third_person):
    """Person 7001 (Flex Cube): 2 calls, 1 e-mail replied, 1 request executed = score 8 (2x2 + 1x3 + 1); Person
    7002 (Card Management): 1 call, 1 rejected = score 3; Person 7003: 1 call = score 2."""
    ops, data = world(tmp_path)
    _sla_day(ops)
    if edit_users:
        edit_users(Store(str(data)))
    return ctx_for(ops, data, {"enabled": False, "best_performer": best or {}}, mailer, options or {"slot": "15:30"})


def cells(html):
    """The "Today's work" rows: [(employee, score)]."""
    return re.findall(r'<td[^>]*font-weight:700;">(Person \d+)</td>(?:<td[^>]*>[^<]*</td>){4}<td[^>]*>(\d+)</td>', html)


def test_the_days_top_3_get_one_celebration_e_mail_in_english_with_the_cc(tmp_path):
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path, mailer, {"cc": ["boss@example.test", "F1@example.test"]}))
    assert res.ok and res.emails_sent == 1, res.error
    (mail,) = mailer.sent
    assert mail["to"] == ["f1@example.test", "c1@example.test", "c3@example.test"]           # best first
    assert mail["cc"] == ["boss@example.test"]                                                  # never one of them
    assert mail["subject"] == "\U0001F3C6 Best Performance of the day: Person 7001, Person 7002 and Person 7003"
    html = mail["html"]
    assert "Best Performance" in html and "TODAY'S TOP 3" in html
    assert "&#127942;" in html and "★" in html and "CONGRATULATIONS" in html                    # trophy, confetti
    assert all(m in html for m in ("&#129351;", "&#129352;", "&#129353;"))                      # gold, silver, bronze
    assert cells(html) == [("Person 7001", "8"), ("Person 7002", "3"), ("Person 7003", "2")]
    assert "score 8" in html and "a call counts as 2, an e-mail replied as 3" in html
    assert not ARABIC.search(html)                                                              # English only
    assert res.summary.startswith("top 3 celebrated - #1 Person 7001: calls 2, e-mails 1, requests 1 = score 8; #2")
    assert res.summary.endswith("to 3 address(es), cc 1")


def test_how_many_are_celebrated_is_a_setting(tmp_path):
    mailer = Mailer()
    assert bp.run(make_ctx(tmp_path, mailer, {"top": 1})).ok
    (mail,) = mailer.sent
    assert mail["to"] == ["f1@example.test"] and "TODAY'S BEST" in mail["html"]
    assert cells(mail["html"]) == [("Person 7001", "8")] and "Person 7002" not in mail["html"]


def test_fewer_than_three_with_work_are_the_top_of_the_day(tmp_path):
    mailer = Mailer()
    assert bp.run(make_ctx(tmp_path, mailer, edit_users=None)).ok
    assert mailer.sent[0]["to"] == ["f1@example.test", "c1@example.test"] and "TOP 2" in mailer.sent[0]["html"]


def test_a_different_built_in_word_each_day_all_in_english(tmp_path):
    days = [f"2026-10-{d:02d}" for d in range(1, len(bp.MESSAGES) + 1)]
    seen = [bp.message_of({}, d, "A, B and C") for d in days]
    assert len(set(seen)) == len(bp.MESSAGES) and not [m for m in bp.MESSAGES if ARABIC.search(m)]


def test_the_owners_own_words_go_in_turn_one_a_day_with_the_names(tmp_path):
    words = ["Well done {names}!", "", "Bravo {name}, keep going"]
    one, two = bp.message_of({"messages": words}, "2026-10-11", "A and B"), bp.message_of({"messages": words}, "2026-10-12", "A and B")
    assert {one, two} == {"Well done A and B!", "Bravo A and B, keep going"}
    mailer = Mailer()
    assert bp.run(make_ctx(tmp_path, mailer, {"messages": ["Well done {names}!"], "subject": "Stars: {names}"})).ok
    assert "Well done Person 7001, Person 7002 and Person 7003!" in mailer.sent[0]["html"]
    assert mailer.sent[0]["subject"] == "Stars: Person 7001, Person 7002 and Person 7003"


def test_one_without_an_address_is_celebrated_but_not_mailed_and_none_at_all_needs_a_cc(tmp_path):
    def no_address(store):
        third_person(store)
        store.update_user("7002", {"email": ""})
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path, mailer, {"cc": ["boss@example.test"]}, edit_users=no_address))
    assert res.ok and mailer.sent[0]["to"] == ["f1@example.test", "c3@example.test"] and "Person 7002" in mailer.sent[0]["html"]
    assert "(Person 7002: no e-mail address in their account)" in res.summary

    def nobody_has_one(store):
        third_person(store, email="")
        for uid in ("7001", "7002"):
            store.update_user(uid, {"email": ""})
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path / "b", mailer, {"cc": ["boss@example.test"]}, edit_users=nobody_has_one))
    assert res.ok and mailer.sent[0]["to"] == ["boss@example.test"] and mailer.sent[0]["cc"] == []
    assert "to the Cc only" in res.summary
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path / "c", mailer, {}, edit_users=nobody_has_one))
    assert not res.ok and mailer.sent == [] and "no e-mail address in their accounts and no Cc is set" in res.error


def test_nobody_with_work_yet_sends_nothing(tmp_path):
    ops, data = world(tmp_path)
    mailer = Mailer()
    ctx = ctx_for(ops, data, {"enabled": False}, mailer, {"slot": "15:30"})
    ctx.now = lambda: datetime(2026, 10, 12, 15, 30)                   # the next day: no call, mail or request yet
    res = bp.run(ctx)
    assert res.ok and res.skipped and mailer.sent == [] and "nobody" in res.summary


def test_a_check_run_with_e_mail_off_builds_it_and_sends_nothing(tmp_path):
    mailer = Mailer()
    ctx = make_ctx(tmp_path, mailer)
    ctx.no_email = True
    res = bp.run(ctx)
    assert res.ok and mailer.sent == [] and "e-mail off" in res.summary


def test_outlook_too_busy_is_tried_again_and_a_failure_says_it_was_built(tmp_path, monkeypatch):
    pauses = []
    monkeypatch.setattr(ds, "_sleep", pauses.append)

    class Busy:
        def __init__(self, times):
            self.left, self.sent = times, []

        def send(self, **mail):
            if self.left:
                self.left -= 1
                raise common.MailError("Out of memory or system resources. Close some windows or programs and try again.")
            self.sent.append(mail)
    once = Busy(1)
    assert bp.run(make_ctx(tmp_path, once)).ok and len(once.sent) == 1 and pauses == [20]
    stuck = Busy(9)
    res = bp.run(make_ctx(tmp_path / "b", stuck))
    assert not res.ok and "built but Outlook could not send it: Out of memory" in res.error
    assert "at each of 4 tries over 80 s, the last one without the logo" in res.error


# ---- when it goes: once a day at its time, on a working day
def settings_with(**best):
    hub, errors = hub_settings.validate({"summary": {"best_performer": best}})
    assert errors == [], errors
    return hub


def test_it_is_on_by_default_once_a_day_at_15_30_on_working_days_for_the_top_3():
    hub = settings_with()
    best = hub["summary"]["best_performer"]
    assert best["enabled"] is True and best["send_times"] == ["15:30"] and best["top"] == 3
    monday = datetime(2026, 10, 12, 15, 31)
    assert runner.slots_due("best_performer", hub, monday, set()) == ["15:30"]
    assert runner.slots_due("best_performer", hub, monday.replace(hour=15, minute=29), set()) == []
    assert runner.slots_due("best_performer", hub, monday, {"15:30"}) == []                  # already sent today
    assert runner.next_slot("best_performer", hub, monday, {"15:30"}) == datetime(2026, 10, 13, 15, 30)
    friday = datetime(2026, 10, 16, 15, 31)                                                    # not a working day
    assert runner.slots_due("best_performer", hub, friday, set()) == []
    assert runner.slots_due("best_performer", settings_with(enabled=False), monday, set()) == []
    assert runner.slots_due("best_performer", settings_with(send_times=["09:45"]), monday.replace(hour=9, minute=50),
                            set()) == ["09:45"]


def test_one_time_only_it_is_sent_once_a_day_and_at_most_the_top_5():
    _, errors = hub_settings.validate({"summary": {"best_performer": {"send_times": ["15:30", "16:00"]}}})
    assert [e for e in errors if "best_performer.send_times" in str(e)]
    _, errors = hub_settings.validate({"summary": {"best_performer": {"top": 9}}})
    assert [e for e in errors if "best_performer.top" in str(e)]


def test_the_runner_knows_the_job_and_it_waits_for_outlook_like_the_summary():
    assert runner.JOBS["best_performer"] == ("calllog.ops.engines.best_performer", "run")
    assert "best_performer" in runner.SLOT_JOBS and runner.OUTLOOK_WAIT_SECONDS["best_performer"] == 600
    import collector
    from calllog.ops import schemas
    assert "best_performer" in schemas.JOBS and collector.JOB_NAMES == schemas.JOBS
