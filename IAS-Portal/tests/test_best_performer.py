"""The best performer e-mail (calllog/ops/engines/best_performer.py): once a day, at its time on a working day, the
day's best performer (calls answered + e-mails replied + requests handled, as Insights › Analysis ranks them) gets a
celebration e-mail with the Cc of the settings. Made-up people and data only."""
import re
from datetime import datetime

import pytest

from calllog.ops import runner, settings as hub_settings
from calllog.ops.engines import best_performer as bp, common, daily_summary as ds
from calllog.storage import Store
from test_daily_summary import DAY, Mailer, ctx_for, world
from test_performance import _sla_day


def make_ctx(tmp_path, mailer, best=None, options=None, edit_users=None):
    """Person 7001 (Flex Cube): 2 calls, 1 e-mail replied, 1 request executed = score 8 (2x2 + 1x3 + 1); Person
    7002: 1 call, 1 rejected = score 3."""
    ops, data = world(tmp_path)
    _sla_day(ops)
    if edit_users:
        edit_users(Store(str(data)))
    return ctx_for(ops, data, {"enabled": False, "best_performer": best or {}}, mailer, options or {"slot": "15:30"})


def kpi(html, label):
    m = re.search(re.escape(label) + r"</div><div[^>]*>([^<]*)</div>", html)
    return m.group(1) if m else None


def test_the_days_best_performer_is_celebrated_with_the_cc_of_the_settings(tmp_path):
    mailer = Mailer()
    res = ds_run = bp.run(make_ctx(tmp_path, mailer, {"cc": ["boss@example.test", "F1@example.test"]}))
    assert ds_run.ok and res.emails_sent == 1, res.error
    (mail,) = mailer.sent
    assert mail["to"] == ["f1@example.test"] and mail["cc"] == ["boss@example.test"]      # never in their own Cc
    assert mail["subject"] == "\U0001F3C6 Today's best performer: Person 7001"
    html = mail["html"]
    assert "Person 7001" in html and "Flex Cube" in html and "CONGRATULATIONS" in html and "ألف مبروك" in html
    assert "&#127942;" in html and "★" in html and "#1 today &middot; score 8" in html             # trophy, confetti
    assert [kpi(html, k) for k in ("Calls answered", "E-mails replied", "Requests handled", "Score")] == ["2", "1", "1", "8"]
    assert "a call counts as 2, an e-mail replied as 3" in html
    assert "Person 7002" not in html
    assert "Person 7001: calls 2, e-mails 1, requests 1 = score 8" in res.summary and "to f1@example.test, cc 1" in res.summary


def test_a_different_built_in_word_each_day_in_english_and_arabic_never_assuming_gender():
    days = [f"2026-10-{d:02d}" for d in range(1, len(bp.MESSAGES) + 1)]
    seen = [tuple(bp.message_of({}, d, "Person 7001")) for d in days]
    assert len(set(seen)) == len(bp.MESSAGES) and all(len(pair) == 2 for pair in seen)
    gendered = {"إنت", "انت", "انتي", "إنتي", "بيك", "بيكي", "معاك", "معاكي", "ليك", "ليكي", "عملت", "عملتي"}
    assert not [ar for _, ar in bp.MESSAGES if gendered & set(re.findall(r"[؀-ۿ]+", ar))]


def test_the_owners_own_words_go_in_turn_one_a_day_with_the_name(tmp_path):
    words = ["Well done {name}!", "", "Bravo {name}, keep going"]
    one, two = bp.message_of({"messages": words}, "2026-10-11", "P"), bp.message_of({"messages": words}, "2026-10-12", "P")
    assert {one[0], two[0]} == {"Well done P!", "Bravo P, keep going"}
    mailer = Mailer()
    assert bp.run(make_ctx(tmp_path, mailer, {"messages": ["Well done {name}!"], "subject": "Star: {name}"})).ok
    assert "Well done Person 7001!" in mailer.sent[0]["html"] and mailer.sent[0]["subject"] == "Star: Person 7001"


def test_without_an_address_it_goes_to_the_cc_and_without_either_it_says_what_to_add(tmp_path):
    def no_address(store):
        store.update_user("7001", {"email": ""})
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path, mailer, {"cc": ["boss@example.test"]}, edit_users=no_address))
    assert res.ok and mailer.sent[0]["to"] == ["boss@example.test"] and mailer.sent[0]["cc"] == []
    assert "to the Cc only (no e-mail address in their account)" in res.summary
    mailer = Mailer()
    res = bp.run(make_ctx(tmp_path / "b", mailer, {}, edit_users=no_address))
    assert not res.ok and mailer.sent == [] and "has no e-mail address in their account and no Cc is set" in res.error


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
    assert not res.ok and "built but Outlook could not send it: Out of memory" in res.error and "3 tries" in res.error


# ---- when it goes: once a day at its time, on a working day
def settings_with(**best):
    hub, errors = hub_settings.validate({"summary": {"best_performer": best}})
    assert errors == [], errors
    return hub


def test_it_is_on_by_default_once_a_day_at_15_30_on_working_days():
    hub = settings_with()
    assert hub["summary"]["best_performer"]["enabled"] is True and hub["summary"]["best_performer"]["send_times"] == ["15:30"]
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


def test_one_time_only_it_is_sent_once_a_day():
    _, errors = hub_settings.validate({"summary": {"best_performer": {"send_times": ["15:30", "16:00"]}}})
    assert [e for e in errors if "best_performer.send_times" in str(e)]


def test_the_runner_knows_the_job_and_it_waits_for_outlook_like_the_summary():
    assert runner.JOBS["best_performer"] == ("calllog.ops.engines.best_performer", "run")
    assert "best_performer" in runner.SLOT_JOBS and runner.OUTLOOK_WAIT_SECONDS["best_performer"] == 600
    import collector
    from calllog.ops import schemas
    assert "best_performer" in schemas.JOBS and collector.JOB_NAMES == schemas.JOBS
