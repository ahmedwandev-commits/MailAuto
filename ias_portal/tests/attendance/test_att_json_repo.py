"""The attendance JSON files inside the portal's data folder: safety rules (atomic, all-or-nothing, concurrent writers, hand edits)."""
import json
import threading
from pathlib import Path

import pytest

from calllog.attendance.repo import ConflictError

from .support import add_employees, store_of


def folder(repo):
    return Path(store_of(repo).root) / "attendance"


def test_files_live_inside_the_portals_data_folder(app, world, repo):
    names = {p.relative_to(folder(repo)).as_posix() for p in folder(repo).rglob("*") if p.is_file()}
    assert {"meta.json", "shift_types.json", "schedule_versions.json", "leave_types.json"} <= names
    assert any(n.startswith("attendance_records/2026-10") for n in names) and any(n.startswith("schedule_entries/") for n in names)
    assert not [n for n in names if n.endswith(".tmp") or (n.startswith(".") and n != ".lock")]                  # no half-written leftovers
    for n in names:
        if n.endswith(".json"):
            json.loads((folder(repo) / n).read_text(encoding="utf-8"))                       # every file is valid JSON


def test_an_error_inside_a_unit_of_work_changes_nothing(app, world, repo):
    before = repo.active_version(2026, 10)
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.add_holiday("2026-10-06", "x")
            repo.commit_schedule_version_atomic(2026, 10, None, [], "u", replace=True)
            raise RuntimeError("boom")
    assert repo.active_version(2026, 10) == before and repo.list_holidays() == []


def test_unknown_employee_rows_are_refused_before_anything_is_written(app, world, repo):
    a = repo.create_attendance_import({"filename": "x", "file_sha256": "x", "parsed": {}})
    with pytest.raises(ConflictError):
        repo.commit_attendance_rows_atomic(a, [{"employee_id": 101, "work_date": "2026-10-20", "in_time": "08:00:00"},
                                               {"employee_id": 999999, "work_date": "2026-10-20"}])
    assert repo.attendance_records("2026-10-20", "2026-10-20") == []


def test_concurrent_writers_do_not_lose_updates(app, world, repo):
    errors = []

    def work(n):
        try:
            for i in range(5):
                repo.add_comp_day({"employee_id": 101, "earned_date": "2026-10-09", "days": 1, "reason": f"t{n}-{i}"})
        except Exception as exc:                                                              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rows = repo.comp_days([101])
    assert not errors and len(rows) == 30 and len({r["id"] for r in rows}) == 30


def test_a_file_edited_in_notepad_with_a_bom_is_still_read(app, world, repo):
    p = folder(repo) / "settings.json"
    repo.save_settings({"grace_minutes": 7})
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    assert repo.get_settings()["grace_minutes"] == 7


def test_a_damaged_file_gives_a_clear_error_not_silent_data_loss(app, world, repo):
    from calllog.attendance.repo.jsonfiles import FileStoreError
    repo.save_settings({"grace_minutes": 7})
    (folder(repo) / "settings.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(FileStoreError):
        repo.get_settings()


def test_the_replace_of_a_schedule_keeps_the_old_version(app, world, repo):
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "b", "file_sha256": "b", "parsed": {}})
    with pytest.raises(ConflictError):
        repo.commit_schedule_version_atomic(2026, 10, imp, [{"employee_id": 101, "work_date": "2026-10-01", "shift_code": "N"}], "u", replace=False)
    v = repo.commit_schedule_version_atomic(2026, 10, imp, [{"employee_id": 101, "work_date": "2026-10-01", "shift_code": "N"}], "u", replace=True)
    assert v["version_no"] == 2
    assert [(x["version_no"], x["status"]) for x in repo.list_versions(2026, 10)] == [(2, "active"), (1, "superseded")]
    assert len(repo.version_entries(1)) == 4 * 31                                              # the original plan is preserved
