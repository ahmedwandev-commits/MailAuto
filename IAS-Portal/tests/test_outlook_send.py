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


class ComError(Exception):
    """Shaped like pywintypes.com_error: (hresult, text, (code, source, description, help, 0, scode), arg)."""

    def __init__(self, description=NO_DATA_FILE):
        super().__init__(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", description, None, 0, -2147221233), None)


class Mail:
    def __init__(self, where):
        self.where, self.sent, self.SendUsingAccount = where, False, None
        self.To = self.CC = self.BCC = self.Subject = self.HTMLBody = ""
        self.Attachments = self

    def Add(self, *args):                                     # Attachments.Add
        raise AssertionError("no attachment expected")

    def Send(self):
        self.sent = True

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
    def __init__(self, default_ok=False, accounts=(), stores=()):
        self.default_ok, self.made = default_ok, []
        self.ns = type("NS", (), {"Accounts": Collection(list(accounts)), "Stores": Collection(list(stores))})()

    def CreateItem(self, kind):
        if not self.default_ok:
            raise ComError()
        self.made.append(Mail("default"))
        return self.made[-1]

    def GetNamespace(self, name):
        return self.ns


def mailer_for(monkeypatch, outlook):
    m = common.OutlookMailer()
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
    assert not res.ok and res.error.startswith("The daily summary was built but Outlook could not send it: Cannot create")
    assert "com_error" not in res.error and "-2147352567" not in res.error
    assert any("built (10:00" in line for line in logs)
