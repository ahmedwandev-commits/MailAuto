"""Sending through Outlook (calllog/ops/engines/common.OutlookMailer) on a PC whose Outlook profile has no usable
DEFAULT data file: ``CreateItem`` fails with «Cannot create the email message because a data file to send and receive
messages cannot be found» (MAPI_E_NOT_FOUND), so the mail is made in the e-mail account's own mailbox instead. A fake
Outlook stands in for the COM objects; made-up addresses only."""
from datetime import datetime

import pytest

from calllog.ops.engines import common, daily_summary
from calllog.ops.store import OpsStore

NO_DATA_FILE = ("Cannot create the email message because a data file to send and receive messages cannot be found. "
                "Check your settings in this Microsoft Outlook profile.")
OUT_OF_MEMORY = "Out of memory or system resources. Close some windows or programs and try again."


class ComError(Exception):
    """Shaped like pywintypes.com_error: (hresult, text, (code, source, description, help, 0, scode), arg)."""

    def __init__(self, description=NO_DATA_FILE, scode=-2147221233):
        super().__init__(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", description, None, 0, scode), None)


def out_of_memory():
    return ComError(OUT_OF_MEMORY, scode=-2147024882)                        # MAPI_E_NOT_ENOUGH_MEMORY


class Mail:
    def __init__(self, where, refuse=None):
        self.where, self.sent, self.SendUsingAccount, self.closed = where, False, None, None
        self.To = self.CC = self.BCC = self.Subject = self.HTMLBody = ""
        self.Attachments = self
        self.refuse = refuse                                  # () -> the error Send raises, or None

    def Add(self, *args):                                     # Attachments.Add
        raise AssertionError("no attachment expected")

    def Send(self):
        error = self.refuse() if self.refuse else None
        if error:
            raise error
        self.sent = True

    def Close(self, how):
        self.closed = how

    def Display(self):
        raise AssertionError("not displayed")


class Items:
    def __init__(self, where, fail=False):
        self.where, self.fail, self.made = where, fail, []

    def Add(self, kind):
        assert kind == 0
        if self.fail:
            raise ComError("This folder cannot hold mail.")
        self.made.append(Mail(self.where))
        return self.made[-1]


class Folder:
    def __init__(self, items):
        self.Items = items


class Store:
    def __init__(self, name, fail=False):
        self.items = Items(name, fail)

    def GetDefaultFolder(self, kind):
        assert kind == common.OL_FOLDER_DRAFTS
        return Folder(self.items)


class Account:
    def __init__(self, store):
        self.DeliveryStore = store


class Collection:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, i):
        return self._items[i - 1]


class Outlook:
    def __init__(self, default_ok=False, accounts=(), stores=(), send_errors=(), create_errors=()):
        self.default_ok, self.made = default_ok, []
        self.ns = type("NS", (), {"Accounts": Collection(list(accounts)), "Stores": Collection(list(stores))})()
        self.send_errors, self.create_errors = list(send_errors), list(create_errors)

    def CreateItem(self, kind):
        if self.create_errors:
            raise self.create_errors.pop(0)
        if not self.default_ok:
            raise ComError()
        self.made.append(Mail("default", refuse=lambda: self.send_errors.pop(0) if self.send_errors else None))
        return self.made[-1]

    def GetNamespace(self, name):
        return self.ns


def mailer_for(monkeypatch, outlook, **kw):
    m = common.OutlookMailer(**kw)
    monkeypatch.setattr(m, "_outlook", lambda: outlook)
    return m


def test_the_default_data_file_is_used_when_outlook_can_make_the_mail_there(monkeypatch):
    outlook = Outlook(default_ok=True, accounts=[Account(Store("mailbox"))])
    mailer_for(monkeypatch, outlook).send(["a@example.test"], subject="S", html_body="<p>x</p>")
    assert outlook.made[0].sent and outlook.made[0].To == "a@example.test"


def test_without_a_usable_default_the_mail_is_made_and_sent_from_the_accounts_own_mailbox(monkeypatch):
    mailbox = Store("mailbox")
    account = Account(mailbox)
    outlook = Outlook(accounts=[account])
    mailer_for(monkeypatch, outlook).send(["a@example.test"], cc=["b@example.test"], subject="Daily", html_body="<p>x</p>")
    mail = mailbox.items.made[0]
    assert mail.sent and mail.SendUsingAccount is account and mail.To == "a@example.test" and mail.CC == "b@example.test"
    assert mail.Subject == "Daily" and mail.HTMLBody == "<p>x</p>"


def test_an_account_whose_mailbox_refuses_is_skipped_for_the_next_data_file(monkeypatch):
    broken, other = Store("archive", fail=True), Store("other")
    outlook = Outlook(accounts=[Account(broken)], stores=[broken, other])
    mailer_for(monkeypatch, outlook).send(["a@example.test"], subject="S", html_body="x")
    assert not broken.items.made and other.items.made[0].sent


def test_when_nothing_can_hold_a_mail_the_error_says_outlooks_words_and_what_to_change(monkeypatch):
    outlook = Outlook(accounts=[Account(Store("archive", fail=True))])
    with pytest.raises(common.MailError) as err:
        mailer_for(monkeypatch, outlook).send(["a@example.test"], subject="S", html_body="x")
    text = str(err.value)
    assert "a data file to send and receive messages cannot be found" in text and "set your mailbox as the default" in text
    assert "-2147352567" not in text                                         # Outlook's words, not the COM tuple


def test_outlook_error_text_takes_the_description_of_a_com_error():
    assert common.outlook_error_text(ComError()).startswith("Cannot create the email message")
    assert common.outlook_error_text(ValueError("plain")) == "plain"


def test_a_summary_outlook_cannot_send_says_it_was_built_and_why(tmp_path):
    class Refusing:
        def send(self, *a, **kw):
            raise ComError()
    store = OpsStore(tmp_path / "data").init()
    logs = []
    ctx = common.JobContext(job="daily_summary", settings={"to": ["summary@example.test"]}, general={}, store=store,
                            now=lambda: datetime(2026, 10, 25, 10, 35), log=logs.append, mailer=Refusing(),
                            data_dir=str(tmp_path / "data"), options={"slot": "10:00"})
    res = daily_summary.run(ctx)
    assert not res.ok and res.error.startswith("The daily summary was built but Outlook could not send 1 of 1 mail(s): "
                                               "every team: Cannot create")
    assert "com_error" not in res.error and "-2147352567" not in res.error
    assert any("built (10:00" in line for line in logs)


# ---- Outlook too busy («Out of memory or system resources»: the e-mail scan or report was holding many of its items)
def test_a_mail_outlook_refuses_is_discarded_not_left_behind(monkeypatch):
    outlook = Outlook(default_ok=True, send_errors=[out_of_memory()])
    with pytest.raises(ComError):
        mailer_for(monkeypatch, outlook).send(["a@example.test"], subject="S", html_body="x")
    assert not outlook.made[0].sent and outlook.made[0].closed == common.OL_DISCARD      # never left in Drafts


def test_outlook_too_busy_to_create_a_mail_is_not_taken_for_a_missing_data_file(monkeypatch):
    mailbox = Store("mailbox")
    outlook = Outlook(default_ok=True, accounts=[Account(mailbox)], create_errors=[out_of_memory()])
    with pytest.raises(ComError) as err:
        mailer_for(monkeypatch, outlook).send(["a@example.test"], subject="S", html_body="x")
    assert common.outlook_error_text(err.value) == OUT_OF_MEMORY and not mailbox.items.made   # no "data file" advice


def test_outlook_busy_knows_outlooks_busy_answers():
    assert common.outlook_busy(out_of_memory()) and common.outlook_busy(ComError(OUT_OF_MEMORY, scode=0))
    assert common.outlook_busy(ComError("x", scode=-2147418111))                  # RPC_E_CALL_REJECTED
    assert not common.outlook_busy(ComError()) and not common.outlook_busy(ValueError("plain"))


def summary_ctx(tmp_path, monkeypatch, outlook, pauses, logs, settings=None):
    monkeypatch.setattr(daily_summary, "_sleep", pauses.append)
    monkeypatch.setattr(daily_summary.er, "mail_images", lambda extra=None: {})   # the fake mail takes no logo
    store = OpsStore(tmp_path / "data").init()
    return common.JobContext(job="daily_summary", settings=settings or {"to": ["summary@example.test"]}, general={},
                             store=store, now=lambda: datetime(2026, 10, 25, 10, 35), log=logs.append,
                             mailer=mailer_for(monkeypatch, outlook), data_dir=str(tmp_path / "data"),
                             options={"slot": "10:00"})


def test_a_summary_outlook_is_too_busy_for_is_sent_at_the_next_try(tmp_path, monkeypatch):
    pauses, logs = [], []
    outlook = Outlook(default_ok=True, send_errors=[out_of_memory()])
    res = daily_summary.run(summary_ctx(tmp_path, monkeypatch, outlook, pauses, logs))
    assert res.ok and res.emails_sent == 1 and pauses == [20]
    first, second = outlook.made
    assert first.closed == common.OL_DISCARD and not first.sent and second.sent
    assert any("Outlook is busy" in line and "trying again in 20 s" in line for line in logs)


def test_when_outlook_stays_out_of_memory_the_summary_says_so_and_the_next_mail_tries_once(tmp_path, monkeypatch):
    pauses, logs = [], []
    outlook = Outlook(default_ok=True, send_errors=[out_of_memory() for _ in range(9)])
    settings = {"to": ["summary@example.test"], "team_recipients": {"Flex Cube": {"to": ["flex@example.test"]}}}
    res = daily_summary.run(summary_ctx(tmp_path, monkeypatch, outlook, pauses, logs, settings))
    assert not res.ok and res.emails_sent == 0 and pauses == [20, 60]          # Outlook is stuck: no more pauses
    assert "every team: " + OUT_OF_MEMORY + " (Outlook said so at each of 3 tries over 80 s" in res.error
    assert "restarting Outlook on the collector PC" in res.error and "Flex Cube: " + OUT_OF_MEMORY in res.error
    assert len(outlook.made) == 4 and not any(m.sent for m in outlook.made)


def test_a_summary_error_that_is_not_outlook_being_busy_is_not_tried_again(tmp_path, monkeypatch):
    pauses, logs = [], []
    outlook = Outlook(default_ok=True, send_errors=[ComError("The operation failed.", scode=-2147467259)])
    res = daily_summary.run(summary_ctx(tmp_path, monkeypatch, outlook, pauses, logs))
    assert not res.ok and pauses == [] and len(outlook.made) == 1 and "The operation failed." in res.error
