"""The portal's mail outbox: two collector PCs never send the same mail twice; a failed mail is tried again."""
from datetime import datetime, timedelta

from calllog.ops import mail_outbox
from calllog.ops.store import OpsStore


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, **mail):
        if self.fail:
            raise RuntimeError("Outlook is busy")
        self.sent.append(mail["subject"])


def test_two_collectors_send_a_mail_once(tmp_path, monkeypatch):
    store = OpsStore(str(tmp_path)).init()
    now = datetime(2026, 10, 31, 10, 0)
    mail_outbox.add(store, ["a@example.test"], "Please confirm", "<p>x</p>", now=now)
    seen_by_both = mail_outbox.pending(store)                      # both PCs read the outbox at the same moment
    monkeypatch.setattr(mail_outbox, "pending", lambda s: [dict(i) for i in seen_by_both])
    first, second = Mailer(), Mailer()
    assert mail_outbox.send_pending(store, first, log=lambda *_: None, now=now) == (1, 0)
    assert mail_outbox.send_pending(store, second, log=lambda *_: None, now=now) == (0, 0)
    assert first.sent == ["Please confirm"] and second.sent == []


def test_a_failed_mail_is_tried_again(tmp_path):
    store = OpsStore(str(tmp_path)).init()
    now = datetime(2026, 10, 31, 10, 0)
    mail_outbox.add(store, ["a@example.test"], "Reminder", "<p>x</p>", now=now)
    assert mail_outbox.send_pending(store, Mailer(fail=True), log=lambda *_: None, now=now) == (0, 1)
    ok = Mailer()
    assert mail_outbox.send_pending(store, ok, log=lambda *_: None, now=now + timedelta(minutes=1)) == (1, 0)
    assert ok.sent == ["Reminder"] and not mail_outbox.pending(store)
