"""Shared pieces for the collector engines: job context/result, credentials, Outlook mail sender, small helpers.

Nothing here may import win32com / playwright / keyring at module level (they are imported inside the functions
that need them), so engines stay importable and testable on any OS.
"""
import functools
import html
import os
import re
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime

# One Outlook COM conversation at a time per process (Outlook automation is not thread-safe).
OUTLOOK_LOCK = threading.RLock()

# NBE brand colours used by the e-mails and Excel files.
NBE_GREEN = "#00573F"
NBE_GREEN_LIGHT = "#E6F0EC"
NBE_GOLD = "#C8A45D"
FONT_STACK = "Segoe UI,Calibri,Arial,sans-serif"

KEYRING_SERVICES = {"sla": "nbe-sso-sla-monitor", "itsm": "nbe-itsm"}
PASSWORD_ENV = {"sla": "NBE_SSO_PASSWORD", "itsm": "ITSM_PASS"}


class MailError(RuntimeError):
    pass


class CredentialError(RuntimeError):
    pass


@dataclass
class JobResult:
    """What a job hands back to the runner. The runner writes the snapshot, the history point and the heartbeat."""
    ok: bool = True
    snapshot_data: dict = None        # the "data" part of the snapshot (None = do not write a snapshot)
    snapshot_date: str = None         # YYYY-MM-DD the data is about (default: today)
    summary: str = ""                 # one line for the System page, e.g. "31 open, 4 breached"
    error: str = None
    error_kind: str = None            # "auth" = wrong/expired password: the runner stops retrying (account lock-out protection)
    emails_sent: int = 0
    history_point: dict = None        # appended to ops/history/<module>/<day>.json
    history_extra: dict = None        # merged into that day document
    skipped: bool = False             # nothing to do (e.g. report already sent today)


@dataclass
class JobContext:
    job: str                          # email | email_report | sla | itsm
    settings: dict                    # this module's section of the settings
    general: dict                     # settings["general"]
    store: object                     # OpsStore
    now: callable = datetime.now
    log: callable = print
    mailer: object = None             # OutlookMailer (or a fake in tests)
    debug: bool = False
    dry_run: bool = False             # scan but write/send nothing
    no_email: bool = False
    host: str = field(default_factory=socket.gethostname)
    data_dir: str = ""
    headed: bool = False              # show the browser (SLA / ITSM) for troubleshooting
    options: dict = field(default_factory=dict)   # per-run extras, e.g. {"send_mail": True} from "Run now", {"requested_by": "admin"}
    credentials: callable = None      # credentials(kind) -> password; default get_password
    hub: dict = field(default_factory=dict)       # every section of the settings (read-only), e.g. the team rules

    def password(self, kind, username):
        fn = self.credentials or get_password
        return fn(kind, username)

    def can_email(self):
        return not (self.no_email or self.dry_run)


# ------------------------------------------------------------------ credentials
def get_password(kind, username):
    """Password for 'sla' or 'itsm': environment variable first, then Windows Credential Manager. Never prompts."""
    env = os.environ.get(PASSWORD_ENV[kind])
    if env:
        return env
    try:
        import keyring
        pw = keyring.get_password(KEYRING_SERVICES[kind], str(username))
        if pw:
            return pw
    except Exception:
        pass
    raise CredentialError(
        f"No saved password for the {kind.upper()} account '{username}'. On the collector PC run:  "
        f"python collector.py --set-password {kind}")


def set_password(kind, username, password):
    import keyring
    keyring.set_password(KEYRING_SERVICES[kind], str(username), password)


# ---------------------------------------------------------------------- helpers
def esc(value):
    return "" if value is None else html.escape(str(value))


def fmt_minutes(minutes):
    """90 -> '1 H 30 Min' (the format the old e-mail script used in its tables)."""
    if minutes is None:
        return "-"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} Min"
    h, m = divmod(minutes, 60)
    return f"{h} H {m} Min" if m else f"{h} H"


def norm_email(value):
    return (value or "").strip().lower()


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm_arabic(text):
    """Normalise Arabic text for matching (tatweel, diacritics, alef/yaa/taa marbuta variants, digits, spaces).
    The same few thousand names, statuses and applications come back hundreds of thousands of times on a big
    inquiry grid: strings are remembered (``_norm_text``)."""
    return _norm_text(text) if isinstance(text, str) else _norm_text(str(text or ""))


@functools.lru_cache(maxsize=65536)
def _norm_text(text):
    s = str(text or "").translate(_ARABIC_DIGITS).replace("ـ", "")
    s = re.sub(r"[ً-ْ]", "", s)
    s = re.sub("[إأآ]", "ا", s).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s).strip().lower()


# --------------------------------------------------------------------- Outlook
OL_FOLDER_DRAFTS = 16                    # Outlook's olFolderDrafts
OL_DISCARD = 1                           # Outlook's olDiscard (MailItem.Close)
_SEND_USING_ACCOUNT = 64209              # the DISPID of MailItem.SendUsingAccount (pywin32 cannot always set it by name)


class _suppress:
    """``with _suppress():`` - one Outlook attempt that may fail (the next one is tried)."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        return kind is not None and issubclass(kind, Exception)


def _send_using(mail, account):
    """Send ``mail`` from ``account`` (its own mailbox), the way pywin32 allows."""
    try:
        mail.SendUsingAccount = account
    except Exception:                                    # noqa: BLE001 - the late-bound property setter
        mail._oleobj_.Invoke(_SEND_USING_ACCOUNT, 0, 8, 0, account)


def outlook_error_text(exc):
    """The words Outlook gives for a COM error (its description), not the whole tuple; else the error's own text."""
    args = getattr(exc, "args", ()) or ()
    detail = args[2] if len(args) > 2 and isinstance(args[2], tuple) else ()
    text = detail[2] if len(detail) > 2 and detail[2] else (args[1] if len(args) > 1 and isinstance(args[1], str) else str(exc))
    return " ".join(str(text).split())[:300]


# Outlook's answers that mean "not now" rather than "not this mail": out of memory or resources (it says so while
# another program - the e-mail scan or report - holds many of its items), busy, a call it rejected.
OUTLOOK_BUSY_CODES = {-2147024882,                   # E_OUTOFMEMORY = MAPI_E_NOT_ENOUGH_MEMORY (0x8007000E)
                      -2147418111,                   # RPC_E_CALL_REJECTED (0x80010001)
                      -2147417846}                   # RPC_E_SERVERCALL_RETRYLATER (0x8001010A)
OUTLOOK_BUSY_WORDS = ("out of memory", "system resources", "server busy", "call was rejected", "retry later")


def outlook_busy(exc):
    """Is ``exc`` Outlook saying it cannot do it right now (out of memory or resources, busy) - worth another try?"""
    args = getattr(exc, "args", ()) or ()
    detail = args[2] if len(args) > 2 and isinstance(args[2], tuple) else ()
    codes = {args[0] if args else None, detail[5] if len(detail) > 5 else None}
    text = outlook_error_text(exc).lower()
    return bool(codes & OUTLOOK_BUSY_CODES) or any(word in text for word in OUTLOOK_BUSY_WORDS)


_OUTLOOK_PROCESS = ("$p = Get-Process OUTLOOK -ErrorAction SilentlyContinue | Select-Object -First 1; "
                    "if ($p) { '{0}|{1}|{2}' -f $p.Path, $p.PrivateMemorySize64, $p.VirtualMemorySize64 }")


def outlook_memory(run=subprocess.run, windows=None):
    """What Windows says about the running Outlook, for the message when Outlook answers "out of memory": "Outlook
    (32-bit) uses 1,765 MB (2,048 MB of address space)" - "" when it cannot be told (not Windows, not running)."""
    if not (os.name == "nt" if windows is None else windows):
        return ""
    try:
        out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _OUTLOOK_PROCESS], capture_output=True,
                  text=True, timeout=20, creationflags=0x08000000).stdout.strip()
        path, private, virtual = out.split("|")
        private_mb, virtual_mb = int(private) // 2 ** 20, int(virtual) // 2 ** 20
    except Exception:                                    # noqa: BLE001 - only a hint for the message
        return ""
    bits = "32-bit" if "(x86)" in path.lower() else "64-bit"
    note = f"Outlook ({bits}) uses {private_mb:,} MB ({virtual_mb:,} MB of address space)"
    return note + (" - a 32-bit Outlook cannot go much past 2,000 MB" if bits == "32-bit" else "")


class OutlookMailer:
    """Sends HTML mail through Outlook desktop (COM). Safe to call from any collector thread."""

    def __init__(self, display_only=False):
        self.display_only = display_only

    def _outlook(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()          # every thread needs its own COM apartment
        return win32com.client.Dispatch("Outlook.Application")

    def my_address(self):
        with OUTLOOK_LOCK:
            outlook = self._outlook()
            ns = outlook.GetNamespace("MAPI")
            for getter in (lambda: ns.CurrentUser.AddressEntry.GetExchangeUser().PrimarySmtpAddress,
                           lambda: ns.Accounts.Item(1).SmtpAddress):
                try:
                    value = getter()
                    if value:
                        return value
                except Exception:
                    pass
        raise MailError("Could not find your own e-mail address in Outlook - put a recipient in the settings.")

    def _new_mail(self, outlook):
        """A new mail item. ``CreateItem`` makes it in the profile's DEFAULT data file; when Outlook refuses that
        («Cannot create the email message because a data file to send and receive messages cannot be found» - the
        default is an archive / a .pst that is gone), the mail is made in the Drafts folder of the first e-mail
        account's own mailbox and sent from that account, else of any data file that has a Drafts folder."""
        try:
            return outlook.CreateItem(0)
        except Exception as first:                       # noqa: BLE001 - tried in the account's own mailbox below
            if outlook_busy(first):
                raise                                    # Outlook is busy, not missing a data file: tried again later
            ns = outlook.GetNamespace("MAPI")
            accounts = []
            with _suppress():
                accounts = [ns.Accounts.Item(i) for i in range(1, int(ns.Accounts.Count) + 1)]
            for account in accounts:
                with _suppress():
                    mail = account.DeliveryStore.GetDefaultFolder(OL_FOLDER_DRAFTS).Items.Add(0)
                    with _suppress():                    # else it still goes from the mailbox it was made in
                        _send_using(mail, account)
                    return mail
            stores = []
            with _suppress():
                stores = [ns.Stores.Item(i) for i in range(1, int(ns.Stores.Count) + 1)]
            for store in stores:
                with _suppress():
                    return store.GetDefaultFolder(OL_FOLDER_DRAFTS).Items.Add(0)
            raise MailError("Outlook on this PC cannot create an e-mail: " + outlook_error_text(first)
                            + " - in Outlook › File › Account Settings › Data Files, set your mailbox as the default.") from first

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None,
             bcc=()):
        """``inline_images``: {content_id: path} referenced in the body as <img src="cid:content_id">."""
        to, cc, bcc = [a for a in to if a], [a for a in cc if a], [a for a in bcc if a]
        if not (to or cc or bcc):
            raise MailError("No recipients.")
        display = self.display_only if display_only is None else display_only
        with OUTLOOK_LOCK:
            step, mail = "opening Outlook", None          # the error says where Outlook failed
            try:
                outlook = self._outlook()
                step = "creating the mail"
                mail = self._new_mail(outlook)
                step = "addressing it"
                mail.To = "; ".join(to)
                if cc:
                    mail.CC = "; ".join(cc)
                if bcc:
                    mail.BCC = "; ".join(bcc)
                mail.Subject = subject
                step = "adding the logo"
                for cid, path in (inline_images or {}).items():
                    if path and os.path.exists(path):
                        att = mail.Attachments.Add(path, 1, 0)
                        pa = att.PropertyAccessor
                        pa.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x3712001F", cid)
                        pa.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x7FFE000B", True)
                step = "writing its body"
                mail.HTMLBody = html_body
                step = "adding the attachments"
                for path in attachments:
                    if path and os.path.exists(path):
                        mail.Attachments.Add(str(path))
                step = "showing it" if display else "sending it"
                if display:
                    mail.Display()
                else:
                    mail.Send()
            except Exception as exc:
                if mail is not None:
                    with _suppress():                    # never leave the half-made mail behind (in Drafts)
                        mail.Close(OL_DISCARD)
                if isinstance(exc, MailError):
                    raise
                raise MailError(f"{outlook_error_text(exc)} - Outlook failed while {step}") from exc
            finally:
                att = pa = mail = outlook = None         # noqa: F841 - drop the COM references now


class FakeMailer:
    """Records mails instead of sending them (tests, --dry-run)."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def my_address(self):
        return "me@nbe.com.eg"

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None, bcc=()):
        if self.fail:
            raise MailError("simulated Outlook failure")
        self.sent.append({"to": list(to), "cc": list(cc), "bcc": list(bcc), "subject": subject, "html": html_body,
                          "attachments": list(attachments), "inline_images": dict(inline_images or {})})
