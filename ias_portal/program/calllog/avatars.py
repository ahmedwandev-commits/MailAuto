"""Profile pictures (v5.16): every user may upload a photo of themselves or choose one of the built-in avatars; without
either, their initials show. An administrator may take a photo down.

The account keeps only what was chosen - ``avatar`` = {"kind": "photo", "ext": "jpg", "v": "<stamp>"} or
{"kind": "preset", "id": "a05"} - and a photo is a file in ``<data_dir>/avatars`` named after a hash of the User ID (never
the ID itself). Only JPEG / PNG / WebP files are accepted, checked by their first bytes (never by the name the browser
sends); the page makes a 512 px square of the photo before sending it, so a picture is small."""
import base64
import binascii
import hashlib
import os
import time
from pathlib import Path

from flask import url_for
from markupsafe import Markup, escape

from . import auth

PRESETS = tuple(f"a{i:02d}" for i in range(1, 13))       # static/avatars/a01.svg ... a12.svg
MAX_BYTES = 1536 * 1024                                    # a photo; the page sends a 512 px JPEG of ~60 KB
FOLDER = "avatars"
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def sniff(data):
    """The kind of picture by its first bytes: "jpg" / "png" / "webp", or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def from_data_url(text):
    """The bytes of a ``data:image/...;base64,`` text (what the page sends), or None."""
    head, _, body = (text or "").partition(",")
    if not head.startswith("data:image/") or ";base64" not in head:
        return None
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return None


def _folder(store):
    return Path(store.root) / FOLDER


def _file(store, uid, ext):
    return _folder(store) / f"{hashlib.sha256(str(uid).encode('utf-8')).hexdigest()[:24]}.{ext}"


def problem(data):
    """Why ``data`` cannot be a profile photo (a message), or None."""
    if not data:
        return "Choose a picture first."
    if len(data) > MAX_BYTES:
        return f"That picture is too large (limit {MAX_BYTES // 1024} KB)."
    if not sniff(data):
        return "That file is not a JPEG, PNG or WebP picture."
    return None


def save_photo(store, uid, data):
    """Keep ``data`` as the photo of ``uid`` (replacing any older one); returns the account's ``avatar`` value."""
    ext = sniff(data)
    folder = _folder(store)
    folder.mkdir(parents=True, exist_ok=True)
    target = _file(store, uid, ext)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    for other in TYPES:
        if other != ext:
            _drop(_file(store, uid, other))
    return {"kind": "photo", "ext": ext, "v": str(int(time.time()))}


def _drop(path):
    """Delete an older photo; one that Windows holds open a moment (another PC reading it) is left - it is never shown,
    the account says which file is the photo."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def remove_photo(store, uid):
    for ext in TYPES:
        _drop(_file(store, uid, ext))


def photo_file(store, user):
    """(path, mimetype) of the user's photo when they have one, else None."""
    pic = (user or {}).get("avatar") or {}
    if pic.get("kind") != "photo" or pic.get("ext") not in TYPES:
        return None
    path = _file(store, user["user_id"], pic["ext"])
    return (path, TYPES[pic["ext"]]) if path.is_file() else None


def src(user):
    """The picture's address for ``user``, or None (then the initials show)."""
    pic = (user or {}).get("avatar") or {}
    if pic.get("kind") == "preset" and pic.get("id") in PRESETS:
        return url_for("static", filename=f"avatars/{pic['id']}.svg")
    if pic.get("kind") == "photo" and pic.get("ext") in TYPES:
        return url_for("pages.picture_file", uid=user["user_id"], v=pic.get("v", ""))
    return None


def initials(name):
    parts = [p for p in (name or "").split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")) if parts else "?"


def tag(user, name=None, cls=""):
    """The picture of ``user`` as HTML: their photo / avatar, or their initials."""
    name = name or (user or {}).get("name") or ""
    address = src(user)
    if address:
        return Markup(f'<img class="avatar avatar-img {escape(cls)}" src="{escape(address)}" alt="" loading="lazy">')
    return Markup(f'<span class="avatar {escape(cls)}" aria-hidden="true" translate="no">{escape(initials(name))}</span>')


def tag_for(uid, name=None, cls=""):
    """``tag`` for a User ID (an Attendance employee's id is their User ID)."""
    try:
        user = auth.store().get_user(str(uid)) if uid not in (None, "") else None
    except Exception:                                   # noqa: BLE001 - a picture never breaks a page
        user = None
    return tag(user, name, cls)
