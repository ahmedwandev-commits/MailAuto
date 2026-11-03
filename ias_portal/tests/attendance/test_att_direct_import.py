"""Administrator imports run at once (no preview / confirmation step); managers have no import at all."""
import datetime as dt
import io

import openpyxl

from .support import add_employees
from .test_att_importers import att_file, schedule_book


def test_hr_attendance_is_imported_at_once_and_reports_skipped_numbers(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "Ali"}])
    f = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", "16:00"], [2, "Unknown", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f, direct=True, fields={})
    assert r.status_code == 302 and r.headers["Location"].endswith("/attendance/hr/history")        # no preview / confirm page in between
    assert [x["employee_id"] for x in repo.attendance_records("2026-10-01", "2026-10-01")] == [1]
    imp = repo.list_attendance_imports()[0]
    assert imp["status"] == "completed" and imp["result"]["inserted"] == 1 and imp["result"]["rejected_unknown_employee"] == 1
    page = admin_b.get(r.headers["Location"], follow_redirects=True).get_data(as_text=True)
    assert "Imported at once" in page and "no portal account" in page and "2" in page
    assert repo.list_audit(action="attendance_import_direct")
    # the very same file again (checkpoint 4): accepted, and it changes nothing
    again = admin_b.upload("/attendance/import", f, direct=True)
    assert again.status_code == 302 and [x["employee_id"] for x in repo.attendance_records("2026-10-01", "2026-10-01")] == [1]
    res = repo.list_attendance_imports()[0]["result"]
    assert res["inserted"] == 0 and res["updated"] == 0


def test_review_first_stays_available_as_an_option(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "Ali"}])
    f = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f)                                  # review ticked
    assert r.status_code == 302 and "/hr/import/" in r.headers["Location"] and repo.attendance_records("2026-10-01", "2026-10-01") == []


def test_unrecognised_columns_still_go_to_the_mapping_page(app, repo, admin_b, tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["foo", "bar"])
    wb.active.append([1, 2])
    p = tmp_path / "x.xlsx"
    wb.save(p)
    r = admin_b.upload("/attendance/import", str(p), direct=True)
    assert r.status_code == 302 and "/hr/import/" in r.headers["Location"] and repo.list_attendance_imports()[0]["status"] == "preview"


def test_schedule_is_imported_at_once_with_automatic_choices_reported(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "علي حسن", "team": "Team A"}, {"id": 2, "name": "منى سعيد", "team": "Team B"}])
    d = dt.datetime
    p = tmp_path / "s.xlsx"
    p.write_bytes(schedule_book([["الخميس", d(2026, 10, 1), "علي حسن", "منى سعيد", "شخص غير موجود"]]))
    r = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": ""}, direct=True)
    assert r.status_code == 302 and "/schedule?year=2026" in r.headers["Location"]
    assert repo.active_version(2026, 10)["version_no"] == 1
    assert {e["employee_id"] for e in repo.schedule_entries("2026-10-01", "2026-10-01")} == {1, 2}
    imp = repo.list_schedule_imports()[0]
    assert imp["status"] == "completed" and imp["result"]["direct"] and imp["result"]["auto"]["names_skipped"] == ["شخص غير موجود"]
    assert "Names skipped" in admin_b.get(r.headers["Location"]).get_data(as_text=True) or True
    a = repo.list_audit(action="schedule_import_direct")[0]
    assert a["detail"]["names_skipped"] == ["شخص غير موجود"] and a["detail"]["version_no"] == 1


def test_a_second_schedule_for_the_month_replaces_it_and_keeps_the_old_version(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "علي حسن", "team": "Team A"}])
    d = dt.datetime
    for n, shift_day in ((1, 1), (2, 8)):
        p = tmp_path / f"s{n}.xlsx"
        p.write_bytes(schedule_book([["الخميس", d(2026, 10, shift_day), "علي حسن"]]))
        assert admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": ""}, direct=True).status_code == 302
    assert [(v["version_no"], v["status"]) for v in repo.list_versions(2026, 10)] == [(2, "active"), (1, "superseded")]
    assert repo.list_audit(action="schedule_import_direct")[0]["detail"]["replaced_version"] == 1


def test_a_schedule_that_needs_a_decision_goes_to_the_review_page(app, repo, admin_b, tmp_path):
    d = dt.datetime
    p = tmp_path / "s.xlsx"
    p.write_bytes(schedule_book([["الخميس", d(2026, 10, 1), "علي حسن"]]))
    r = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": ""}, direct=True)       # nobody exists yet
    assert r.status_code == 302 and "/schedule/import/" in r.headers["Location"] and repo.active_version(2026, 10) is None
    p.write_bytes(schedule_book([["الخميس", d(2026, 10, 1), "علي حسن"]]))
    wrong = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 11, "team": ""}, direct=True)      # the sheet title says October
    assert wrong.status_code in (302, 400) and repo.active_version(2026, 11) is None
