"""E-mails the portal itself wants to send (the web app cannot reach Outlook): written here by the portal, sent by the
collector's Outlook on its next maintenance round (``runner.Runner._maintenance`` -> ``send_pending``). One JSON file in
the state folder, under a cross-process lock; sent items are kept 7 days, then dropped. Never raises into the caller."""
import secrets
from datetime import datetime, timedelta

NAME = "mail-outbox"
KEEP_DAYS = 7
MAX_TRIES = 5
CLAIM_MINUTES = 15          # a mail one collector PC is sending: the others leave it alone this long (never sent twice)


def _path(store):
    return store.state_dir / f"{NAME}.json"


def _mutate(store, change):
    with store._lock(f"state-{NAME}")():
        data = store.read_json(_path(store), {"items": []})
        out = change(data)
        store.write_json(_path(store), data)
    return out


def add(store, to, subject, html, kind="", cc=(), now=None):
    """Queue one mail. ``to`` = list of addresses (empty ones dropped). Returns the id, or None when nobody to send to."""
    to = sorted({str(a).strip() for a in to or () if a and "@" in str(a)}, key=str.lower)
    if not to:
        return None
    item = {"id": secrets.token_hex(6), "to": to, "cc": [a for a in cc or () if a], "subject": str(subject)[:250],
            "html": html, "kind": kind, "created": (now or datetime.now()).isoformat(timespec="seconds"),
            "tries": 0, "sent_at": None, "error": None}
    _mutate(store, lambda d: d.setdefault("items", []).append(item))
    return item["id"]


def pending(store):
    data = store.read_json(_path(store), {"items": []})
    return [i for i in data.get("items", []) if not i.get("sent_at") and i.get("tries", 0) < MAX_TRIES]


def _mark(store, item_id, sent_at=None, error=None, now=None):
    def change(d):
        keep_from = ((now or datetime.now()) - timedelta(days=KEEP_DAYS)).isoformat()
        items = []
        for i in d.get("items", []):
            if i["id"] == item_id:
                i["tries"] = i.get("tries", 0) + 1
                i["sent_at"], i["error"] = sent_at, error
                i["claimed_at"] = None                     # a failed one is free for the next round
            if not (i.get("sent_at") and i["sent_at"] < keep_from):
                items.append(i)
        d["items"] = items
    _mutate(store, change)


def _claim(store, item_id, now):
    """Take one mail for sending, under the lock: False when it is sent already, or another collector PC took it less
    than ``CLAIM_MINUTES`` ago."""
    def change(d):
        for i in d.get("items", []):
            if i["id"] != item_id:
                continue
            if i.get("sent_at") or i.get("tries", 0) >= MAX_TRIES:
                return False
            taken = i.get("claimed_at")
            if taken and taken > (now - timedelta(minutes=CLAIM_MINUTES)).isoformat(timespec="seconds"):
                return False
            i["claimed_at"] = now.isoformat(timespec="seconds")
            return True
        return False
    return _mutate(store, change)


def send_pending(store, mailer, log=print, now=None):
    """Send what is waiting with ``mailer`` (an OutlookMailer). Returns (sent, failed)."""
    sent = failed = 0
    for item in pending(store):
        if not _claim(store, item["id"], now or datetime.now()):
            continue                                        # sent meanwhile, or another collector PC is sending it
        try:
            mailer.send(to=item["to"], cc=item.get("cc") or [], subject=item["subject"], html_body=item["html"])
            _mark(store, item["id"], sent_at=(now or datetime.now()).isoformat(timespec="seconds"), now=now)
            sent += 1
        except Exception as exc:                            # noqa: BLE001 - tried again next round (MAX_TRIES)
            _mark(store, item["id"], error=f"{type(exc).__name__}: {exc}"[:300], now=now)
            failed += 1
            log(f"portal e-mail '{item['subject']}' not sent: {type(exc).__name__}: {exc}")
    return sent, failed
