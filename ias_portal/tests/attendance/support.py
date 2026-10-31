"""Helpers for the attendance tests: accounts are the PORTAL's accounts (calllog users), employees are portal users whose User ID is
the HR number."""
import glob
import os
from pathlib import Path

import pytest

from calllog import security
from calllog.storage import iso_now

PW = "Passw0rd!x"
HERE = Path(__file__).resolve().parent

# The two real HR workbooks are not part of the repository. Point ATTENDANCE_SAMPLES at a folder holding them to run the real-file tests.
SAMPLES = Path(os.environ.get("ATTENDANCE_SAMPLES") or HERE.parent.parent.parent / "attendance_module" / "samples")


def sample_files():
    files = glob.glob(str(SAMPLES / "*.xlsx"))
    att = [f for f in files if os.path.basename(f).startswith("attendance")]
    sch = [f for f in files if f not in att]
    return (att[0] if att else None), (sch[0] if sch else None)


ATT_FILE, SCHEDULE_FILE = sample_files()
needs_samples = pytest.mark.skipif(not (ATT_FILE and SCHEDULE_FILE), reason="the two real sample workbooks are not available")

# URLs of the portal itself (everything else a test asks for belongs to the module and lives under /attendance)
PORTAL_PATHS = ("/login", "/logout", "/account/", "/healthz", "/hub", "/admin", "/calls", "/my", "/dashboard", "/email", "/sla", "/itsm", "/system")


HR_SEGMENTS = {"import", "history", "correct", "punch-report", "absence-review", "permit-review"}


def url_of(url):
    """The old standalone URLs of the module -> where they live in the portal."""
    if url.startswith(PORTAL_PATHS) and not url.startswith("/admin/"):
        return url
    if url.startswith("/attendance/"):
        rest = url[len("/attendance/"):]
        if rest.split("/")[0].split("?")[0] not in HR_SEGMENTS:
            return url                            # already a portal URL (for example a redirect target)
        url = "/hr/" + rest                       # the old standalone HR routes became /attendance/hr/...
    return "/attendance" + url


class Browser:
    """A signed-in browser tab of the portal (cookies + CSRF) that knows where the module's pages are."""

    def __init__(self, app):
        self.client = app.test_client()
        self.csrf = None

    def login(self, uid, password):
        self.client.get("/login")
        with self.client.session_transaction() as s:
            token = s["csrf"]
        r = self.client.post("/login", data={"user_id": uid, "password": password, "csrf_token": token})
        if r.status_code in (302, 303):
            with self.client.session_transaction() as s:
                self.csrf = s.get("csrf")
        return r

    def get(self, url, **kw):
        return self.client.get(url_of(url), **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.csrf or ""
        return self.client.post(url_of(url), data=data, **kw)

    def upload(self, url, path, fields=None, direct=False):
        """Upload a workbook. By default the optional "review first" box is ticked (the older tests walk through the review pages);
        ``direct=True`` is the normal administrator flow: the file is imported at once."""
        data = dict(fields or {})
        if not direct:
            data["review"] = "1"
        data["csrf_token"] = self.csrf or ""
        with open(path, "rb") as f:
            data["file"] = (f, os.path.basename(path))
            return self.client.post(url_of(url), data=data, content_type="multipart/form-data")


def store_of(repo):
    return repo.directory.store


def add_user(repo, user_id, role, teams=(), password=PW, name=None, active=True):
    """Create a PORTAL account. role: admin | manager | gm (a portal user who is made General Manager) | user."""
    store = store_of(repo)
    hub_role = {"gm": "user"}.get(role, role)
    store.create_user({"user_id": user_id, "name": name or user_id, "department": "", "role": hub_role, "teams": list(teams),
                       "password_hash": security.hash_password(password), "active": active, "must_change_password": False,
                       "created_at": iso_now(repo.clock()), "last_login": None})
    if role == "gm":
        repo.directory.set_gm(user_id, True)


def set_user(repo, user_id, patch):
    store_of(repo).update_user(user_id, patch)


def add_employees(repo, employees):
    """Employees of the module = portal accounts (role user) whose User ID is the HR number. Returns how many were created."""
    store, n = store_of(repo), 0
    for e in employees:
        if store.get_user(str(e["id"])):
            continue
        teams = [e["team"]] if e.get("team") else []
        store.create_user({"user_id": str(e["id"]), "name": e["name"], "department": e.get("department", ""), "role": "user", "teams": teams,
                           "password_hash": security.hash_password(PW), "active": True, "must_change_password": False,
                           "created_at": iso_now(repo.clock()), "last_login": None})
        n += 1
    return n


def login(app, uid, password=PW):
    b = Browser(app)
    r = b.login(uid, password)
    assert r.status_code in (302, 303), f"login failed for {uid}: {r.status_code}"
    return b
