"""Regression tests for the core-module audit (storage, API validation, sign-in / password flow, launcher)."""
import io
import json

import pytest

from calllog import auth, config, storage
from calllog import app as app_module
from calllog.storage import Store


def _add_call(emp):
    r = emp.post("/api/records", {"ip_phone": "75942", "reason": "answered"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["record"]


# ----------------------------------------------------------------------------------------------- input validation
@pytest.mark.parametrize("reason", [["answered"], {"key": "answered"}, 5, None])
def test_reason_that_is_not_text_is_a_400_not_a_crash(make_employee, reason):
    emp = make_employee("e1")
    r = emp.post("/api/records", {"ip_phone": "75942", "reason": reason})
    assert r.status_code == 400
    assert "reason" in r.get_json()["error"].lower()


def test_reason_not_text_on_edit_is_a_400(make_employee):
    emp = make_employee("e1")
    rec = _add_call(emp)
    r = emp.put(f"/api/records/{rec['id']}", {"reason": ["solved"]})
    assert r.status_code == 400


@pytest.mark.parametrize("body", [["x"], "text", 5])
def test_reset_password_with_a_body_that_is_not_an_object(admin, make_employee, body):
    make_employee("e1")
    r = admin.post("/api/admin/users/e1/reset-password", json=body)
    assert r.status_code == 200
    assert len(r.get_json()["temp_password"]) >= 8


@pytest.mark.parametrize("bad_uid", ["../../users", "../../reasons", "..\\..\\users", "a/b"])
def test_records_filter_user_id_never_reaches_the_file_system(admin, make_employee, bad_uid):
    emp = make_employee("e1")
    _add_call(emp)
    r = admin.get("/api/admin/records", query_string={"user_id": bad_uid})
    assert r.status_code == 200
    assert r.get_json()["total"] == 0
    r = admin.get("/export/records.csv", query_string={"user_id": bad_uid})
    assert r.status_code == 200
    assert r.get_data().decode("utf-8-sig").strip().count("\n") == 0     # header only
    # a real filter still works
    r = admin.get("/api/admin/records", query_string={"user_id": "E1"})
    assert r.get_json()["total"] == 1


def test_store_records_between_rejects_a_path_as_user_id(tmp_path):
    store = Store(tmp_path / "data")
    store.init()
    (store.records_dir / "2026-10-11").mkdir()
    assert store.records_between("2026-10-11", "2026-10-11", "../../users") == []


@pytest.mark.parametrize("url", ["/api/admin/users/import", "/api/admin/branches/import"])
@pytest.mark.parametrize("name,data", [("x.xlsx", b"this is not a workbook"), ("x.xlsx", b"PK\x03\x04broken"),
                                       ("x.csv", b"user_id,name\n" + b'"' + b"a" * 200000 + b'",n\n')])
def test_unreadable_import_file_is_a_400(admin, url, name, data):
    r = admin.post(url, data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
    assert r.status_code == 400, r.get_data()[:200]
    assert r.get_json()["error"]


# ------------------------------------------------------------------------------------- sign-in and password change
def test_throttle_keeps_no_entry_for_ids_that_never_failed():
    t = auth.Throttle(limit=5, window=600)
    for i in range(1000):
        assert t.retry_after((f"guess{i}", "1.2.3.4")) == 0
    assert len(t._hits) == 0
    t.fail(("u", "ip"))
    assert t.retry_after(("u", "ip")) == 0
    assert len(t._hits) == 1


def test_login_with_a_huge_user_id_keeps_the_audit_small(app, admin):
    from calllog.storage import iso_now
    b = admin.__class__(app)
    r = b.login("x" * 50000, "whatever")
    assert r.status_code == 401
    day = iso_now(app.config["CLOCK"]())[:10]
    entries = app.extensions["store"].read_audit(day, day)
    failed = [e for e in entries if e["action"] == "login_failed"]
    assert failed and len(failed[-1]["target"]) <= 64


def test_wrong_current_password_is_throttled(app, make_employee):
    emp = make_employee("e1")

    def change(current, new="BrandNew12345"):
        return emp.client.post("/account/password", data={
            "current_password": current, "new_password": new, "confirm_password": new, "csrf_token": emp.csrf})

    for _ in range(5):
        assert change("wrong-guess").status_code == 400
    r = change("MyOwnPass123")                              # even the right one waits now
    assert r.status_code == 429
    assert b"Too many failed attempts" in r.get_data()


def test_right_current_password_clears_the_counter(app, make_employee):
    emp = make_employee("e1")
    for _ in range(4):
        emp.client.post("/account/password", data={"current_password": "nope", "new_password": "BrandNew12345",
                                                   "confirm_password": "BrandNew12345", "csrf_token": emp.csrf})
    r = emp.client.post("/account/password", data={"current_password": "MyOwnPass123", "new_password": "BrandNew12345",
                                                   "confirm_password": "BrandNew12345", "csrf_token": emp.csrf})
    assert r.status_code == 302
    assert app.extensions["throttles"]["user"].retry_after(("e1", "127.0.0.1")) == 0


@pytest.mark.parametrize("nxt", ["/\t/evil.example", "/\n/evil.example", "/\r\n/evil.example", "/\x7f/evil.example"])
def test_language_switch_never_redirects_with_control_characters(admin, nxt):
    r = admin.client.post("/account/lang", data={"lang": "ar", "next": nxt, "csrf_token": admin.csrf})
    assert r.status_code == 302
    assert "evil" not in r.headers["Location"]


def test_language_switch_keeps_a_normal_next(admin):
    r = admin.client.post("/account/lang", data={"lang": "en", "next": "/admin?tab=users", "csrf_token": admin.csrf})
    assert r.headers["Location"].endswith("/admin?tab=users")


def test_empty_secret_key_file_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "APP_DIR", tmp_path)
    (tmp_path / ".secret_key").write_text("  \n")
    key = app_module._secret_key()
    assert len(key) >= 32
    assert (tmp_path / ".secret_key").read_text().strip() == key
    assert app_module._secret_key() == key


# ---------------------------------------------------------------------------------------------------- data files
def test_lists_saved_by_notepad_with_a_bom_still_load(tmp_path):
    store = Store(tmp_path / "data")
    store.init()
    reasons = [{"key": "k1", "label_ar": "أ", "label_en": "One", "status": "resolved"}]
    store.reasons_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(reasons, ensure_ascii=False).encode("utf-8"))
    assert [r["key"] for r in store.reasons()] == ["k1"]


def test_config_json_with_a_bom_loads(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"port": 9123}).encode("utf-8"))
    monkeypatch.setenv("CALLLOG_CONFIG", str(path))
    monkeypatch.delenv("CALLLOG_PORT", raising=False)
    assert config.load_config()["port"] == 9123


def test_a_briefly_locked_file_is_read_after_a_retry(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.init()
    real_open, calls = open, {"n": 0}

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(store.branches_path) and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError(13, "sharing violation")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(storage, "open", flaky_open, raising=False)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    assert store.branches()
    assert calls["n"] == 2


def test_a_file_that_stays_locked_is_a_storage_error(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    store.init()

    def locked(path, *args, **kwargs):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(storage, "open", locked, raising=False)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    with pytest.raises(storage.StorageError):
        store.branches()


def test_audit_file_with_a_broken_character_is_still_read(tmp_path):
    store = Store(tmp_path / "data")
    store.init()
    good = json.dumps({"ts": "2026-10-11 09:00:00", "action": "login"}).encode("utf-8")
    (store.logs_dir / "audit-2026-10-11.jsonl").write_bytes(good + b"\n" + b'{"ts": "2026-10-11 09:01:00", "x": "\xd8')
    entries = store.read_audit("2026-10-11", "2026-10-11")
    assert [e["action"] for e in entries] == ["login"]


# ------------------------------------------------------------------------------------------------------ launcher
def test_pause_never_waits_for_enter_when_unattended(monkeypatch):
    import builtins
    import run

    def no_input(*a, **k):
        raise AssertionError("an unattended server must not wait for Enter")

    monkeypatch.setattr(builtins, "input", no_input)
    monkeypatch.setattr(run.os, "name", "nt")
    for argv in (["run.py", "--service"], ["run.py", "--share", "--no-browser", "--hide-password"]):
        monkeypatch.setattr(run.sys, "argv", argv)
        run.pause()                                      # returns at once
