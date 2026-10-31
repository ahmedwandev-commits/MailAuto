import datetime as dt

import pytest

from calllog import create_app, security

from .support import PW, add_employees, add_user, login, store_of


class Clock:
    def __init__(self, start):
        self.value = start

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock(dt.datetime(2026, 10, 14, 9, 0, 0))


@pytest.fixture
def make_app(tmp_path, clock):
    def factory(**kw):
        return create_app({"data_dir": str(tmp_path / "data"), "clock": clock, "secret_key": "test-secret", "user_edit_days": None, **kw})
    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def repo(app):
    return app.extensions["att_repo"]


@pytest.fixture
def users(app, repo):
    """The portal accounts: the bootstrap administrator (password set), a General Manager and two managers of different teams."""
    store_of(repo).update_user("admin", {"name": "admin", "password_hash": security.hash_password(PW), "must_change_password": False})
    add_user(repo, "gm", "gm")
    add_user(repo, "mgr1", "manager", ["Team A"])
    add_user(repo, "mgr2", "manager", ["Team B"])
    return repo


@pytest.fixture
def admin_b(app, users):
    return login(app, "admin")


@pytest.fixture
def gm_b(app, users):
    return login(app, "gm")


@pytest.fixture
def mgr1_b(app, users):
    return login(app, "mgr1")


@pytest.fixture
def mgr2_b(app, users):
    return login(app, "mgr2")


@pytest.fixture
def world(app, repo, users):
    """Team A: 101,102   Team B: 201,202   (schedule for October + a day of attendance)."""
    add_employees(repo, [
        {"id": 101, "name": "Ali A1", "team": "Team A", "department": "D1"}, {"id": 102, "name": "Sara A2", "team": "Team A", "department": "D1"},
        {"id": 201, "name": "Omar B1", "team": "Team B", "department": "D2"}, {"id": 202, "name": "Nour B2", "team": "Team B", "department": "D2"}])
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "t", "file_sha256": "t", "team": "Team A", "parsed": {}})
    ent = [{"employee_id": e, "work_date": f"2026-10-{d:02d}", "shift_code": c, "source": "import"}
           for e, c in ((101, "M"), (102, "E"), (201, "M"), (202, "N")) for d in range(1, 32)]
    repo.commit_schedule_version_atomic(2026, 10, imp, ent, "admin", replace=False)
    a = repo.create_attendance_import({"filename": "a", "file_sha256": "a", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [
        {"employee_id": 101, "work_date": "2026-10-12", "in_time": "07:10:00", "out_time": "15:00:00"},
        {"employee_id": 201, "work_date": "2026-10-12", "in_time": "07:00:00", "out_time": "15:00:00"},
        {"employee_id": 102, "work_date": "2026-10-12", "in_time": None, "out_time": None},
        {"employee_id": 202, "work_date": "2026-10-12", "in_time": None, "out_time": None}])
    return repo
