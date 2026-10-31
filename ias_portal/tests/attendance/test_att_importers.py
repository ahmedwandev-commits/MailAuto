"""Parser variations, validation, duplicate prevention and large-file behaviour (synthetic workbooks)."""
from .support import add_employees
import datetime as dt
import io
import time

import openpyxl
import pytest

from calllog.attendance.importers import attendance_xlsx, schedule_xlsx


def workbook(rows, title=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    if title is not None:
        ws.append([title])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- attendance parser
def test_columns_found_by_name_in_any_order_with_english_headers_and_mixed_formats():
    data = workbook([
        ["Date", "Check Out", "Employee ID", "Check In", "Employee Name", "Department"],
        ["13/10/2026", "16:05", 111, "08:10:30", "Ali", "IT"],
        [dt.datetime(2026, 10, 14), dt.time(17, 0), "112.0", 0.3541666667, "Sara", "HR"],
        ["2026-10-15", None, 111, None, "Ali", "IT"]], title="Report")
    p = attendance_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data), read_only=True))
    assert p["error"] is None and p["header_row"] == 2
    assert {r["employee_id"] for r in p["rows"]} == {111, 112}
    by = {(r["employee_id"], r["work_date"]): r for r in p["rows"]}
    assert by[(111, "2026-10-13")]["in_time"] == "08:10:30" and by[(111, "2026-10-13")]["out_time"] == "16:05:00"
    assert by[(112, "2026-10-14")]["in_time"] == "08:30:00"                       # Excel fraction 0.354166 = 08:30
    assert by[(111, "2026-10-15")]["in_time"] is None
    assert p["period"] == ["2026-10-13", "2026-10-15"]


def test_arabic_header_variants():
    data = workbook([["الرقم الوظيفي", "التاريخ", "وقت الحضور", "وقت الانصراف"], [5, "01-10-2026", "7:59", "15:01"]])
    p = attendance_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data), read_only=True))
    assert p["rows"][0]["in_time"] == "07:59:00" and p["rows"][0]["work_date"] == "2026-10-01"


def test_validation_rejects_bad_rows_and_counts_duplicates():
    data = workbook([
        ["رقم الموظف", "التاريخ", "توقيت الحضور", "توقيت الأنصراف"],
        [1, "2026-10-01", "08:00", "16:00"],
        [1, "2026-10-01", "09:00", "17:00"],            # duplicate inside the file
        ["abc", "2026-10-02", "08:00", "16:00"],          # bad id
        [2, "not a date", "08:00", "16:00"],              # bad date
        [3, "2026-10-03", "8h30", "16:00"],               # bad time
        [None, None, None, None]])                        # empty row ignored
    p = attendance_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data), read_only=True))
    assert len(p["rows"]) == 1 and p["duplicates_in_file"] == 1
    assert [r["row"] for r in p["rejected"]] == [4, 5, 6]
    assert p["rows"][0]["in_time"] == "08:00:00"          # first occurrence wins


def test_missing_required_columns_is_a_clear_error():
    data = workbook([["foo", "bar", "baz", "qux"], [1, 2, 3, 4]])
    p = attendance_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data), read_only=True))
    assert "header row" in p["error"]


# ---------------------------------------------------------------- attendance import service
def att_file(tmp_path, name, rows):
    p = tmp_path / name
    p.write_bytes(workbook([["رقم الموظف", "أسم الموظف", "التاريخ", "توقيت الحضور", "توقيت الأنصراف"]] + rows))
    return str(p)


def test_attendance_preview_counts_unknown_existing_and_differing(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "Ali"}])
    f1 = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", "16:00"], [2, "Unknown", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f1)
    pv = admin_b.get(r.headers["Location"]).get_data(as_text=True)
    assert "Unknown employee ids" in pv
    # unknown ids are rejected unless the admin ticks "create"
    assert admin_b.post(r.headers["Location"] + "/confirm", {}).status_code == 302
    assert repo.get_employee(2) is None and len(repo.attendance_records("2026-10-01", "2026-10-01")) == 1
    res = repo.list_attendance_imports()[0]["result"]
    assert res["inserted"] == 1 and res["rejected_unknown_employee"] == 1
    # re-import a different file with one identical key but a different time: stored value is kept, difference is reported
    f2 = att_file(tmp_path, "b.xlsx", [[1, "Ali", "2026-10-01", "09:30", "17:00"], [1, "Ali", "2026-10-02", "08:00", "16:00"]])
    r2 = admin_b.upload("/attendance/import", f2)
    page = admin_b.get(r2.headers["Location"]).get_data(as_text=True)
    assert "differ from stored" in page
    admin_b.post(r2.headers["Location"] + "/confirm", {})
    got = {x["work_date"]: x for x in repo.attendance_records("2026-10-01", "2026-10-02")}
    assert got["2026-10-01"]["in_time"] == "08:00:00" and got["2026-10-02"]["in_time"] == "08:00:00"
    res2 = repo.list_attendance_imports()[0]["result"]
    assert (res2["inserted"], res2["existing_kept"], res2["differs_from_stored"]) == (1, 1, 1)
    audit = [a for a in repo.list_audit() if a["action"].startswith("attendance_import")]
    assert audit and all(a["username"] == "admin" for a in audit)


def test_cancelled_import_changes_nothing_and_can_be_retried(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "Ali"}])
    f = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", "16:00"]])
    r = admin_b.upload("/attendance/import", f)
    admin_b.post(r.headers["Location"] + "/cancel", {})
    assert repo.attendance_records("2026-10-01", "2026-10-01") == []
    r = admin_b.upload("/attendance/import", f)                    # a cancelled upload is not a duplicate
    assert r.status_code == 302


def test_only_admin_may_import_attendance(app, repo, mgr1_b, gm_b, tmp_path):
    f = att_file(tmp_path, "a.xlsx", [[1, "Ali", "2026-10-01", "08:00", "16:00"]])
    for b in (mgr1_b, gm_b):
        assert b.upload("/attendance/import", f).status_code == 302
    assert repo.list_attendance_imports() == []


# ---------------------------------------------------------------- schedule parser (synthetic)
def schedule_book(rows):
    return workbook([["شهر اكتوبر 2026"],
                     ["اليوم", "التاريخ", "الوردية الصباحية\nFrom 7 AM To 3 PM", None, "الوردية المسائية\nFrom 2 PM To 10 PM", None,
                      " الوردية الليلية\nFrom 10 PM To 7 AM"],
                     [None, None, "FLEX", "CARD", "FLEX", "CARD", "All"]] + rows)


def test_schedule_parser_handles_multiple_names_mfa_and_standby():
    d = dt.datetime
    data = schedule_book([
        ["الخميس", d(2026, 10, 1), "علي حسن\nسعيد", "منى", "ياسر - هبة", "عمر ( MFA )", "خالد"],
        [None, None, "نور (VPN/ Stand By Site - CM) - سلمى  (VPN/ Stand By Site - Flex)"],
        ["الجمعة", d(2026, 10, 2), "علي", None, None, None, "خالد"]])
    p = schedule_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data)))
    e = p["entries"]
    got = {(x["date"], x["shift_code"], x["name_raw"]): x for x in e}
    assert ("2026-10-01", "M", "علي حسن") in got and ("2026-10-01", "M", "سعيد") in got
    assert ("2026-10-01", "E", "ياسر") in got and ("2026-10-01", "E", "هبة") in got
    assert got[("2026-10-01", "E", "عمر")]["note"] == "MFA" and got[("2026-10-01", "E", "عمر")]["stream"] == "CARD"
    assert got[("2026-10-01", "N", "خالد")]["stream"] == "All"
    sb = [x for x in e if x["kind"] == "standby"]
    assert [(x["name_raw"], x["note"]) for x in sb] == [("نور", "VPN/ Stand By Site - CM"), ("سلمى", "VPN/ Stand By Site - Flex")]
    assert all(x["date"] == "2026-10-01" for x in sb)
    assert [(g["code"], g["start"], g["end"]) for g in p["groups"]] == [("M", "07:00", "15:00"), ("E", "14:00", "22:00"), ("N", "22:00", "07:00")]
    assert sum(1 for i in p["issues"] if i["code"] == "missing_date") == 29     # only 2 of 31 days are in this tiny file


def test_schedule_parser_flags_weekday_mismatch_and_unfixable_dates():
    d = dt.datetime
    data = schedule_book([["الخميس", d(2026, 10, 1), "أ"], ["الجمعة", d(2026, 10, 2), "ب"],
                          ["السبت", d(2026, 10, 2), "ج"],                    # duplicate date, Saturday expected -> propose 3 Oct
                          ["الأحد", d(2026, 10, 9), "د"],                    # Sunday label but 9 Oct is a Friday -> propose 4 Oct
                          ["الأربعاء", d(2026, 10, 20), "هـ"]])             # Wednesday label on a Tuesday, no sensible proposal -> blocking error
    p = schedule_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(data)))
    days = {x["row"]: x for x in p["days"]}
    assert days[6]["date"] == "2026-10-03" and days[6]["corrected"]
    assert days[7]["date"] == "2026-10-04" and days[7]["corrected"]
    assert days[8]["invalid"] is True and any(i["code"] == "bad_date" and i["row"] == 8 for i in p["issues"])
    assert all(x["invalid"] for x in p["entries"] if x["row"] == 8)       # its names are never imported


def test_non_schedule_workbook_is_rejected_with_message(app, repo, admin_b, tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_bytes(workbook([["a", "b"], [1, 2]]))
    r = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": "A"})
    assert r.status_code == 400 and b"does not look like a monthly shift schedule" in r.data


# ---------------------------------------------------------------- schedule import service rules
def test_managers_have_no_import_screens_at_all(app, repo, mgr1_b, mgr2_b, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "علي حسن", "team": "Team A"}])
    d = dt.datetime
    p = tmp_path / "s.xlsx"
    p.write_bytes(schedule_book([["الخميس", d(2026, 10, 1), "علي حسن"]]))
    for b in (mgr1_b, mgr2_b):
        assert b.get("/schedule/import").status_code == 302 and b.get("/attendance/import").status_code == 302
        assert b.get("/imports/history").status_code == 302 and b.get("/schedule/history").status_code == 302
        assert b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": "Team A"}).status_code == 302
        assert b.upload("/attendance/import", str(p)).status_code == 302
    assert repo.list_schedule_imports() == [] and repo.list_attendance_imports() == []
    # the administrator still can, and a manager can neither read nor confirm nor cancel the administrator's preview
    url = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": "Team A"}).headers["Location"]
    assert mgr1_b.get(url).status_code == 302 and mgr1_b.post(url + "/confirm", {}).status_code == 302
    assert mgr1_b.post(url + "/cancel", {}).status_code == 302
    assert repo.active_version(2026, 10) is None and repo.get_schedule_import(1)["status"] == "preview"
    assert admin_b.post(url + "/confirm", {}).status_code == 302 and repo.active_version(2026, 10)["version_no"] == 1


def test_manager_scope_rules_of_the_import_service_still_hold(app, repo, tmp_path):
    """Defence in depth: even called directly, the service never lets a manager schedule somebody outside his teams."""
    from calllog.attendance.services import access, schedule_service as ss
    from .support import add_user
    add_employees(repo, [{"id": 1, "name": "علي حسن", "team": "Team A"}, {"id": 2, "name": "منى سعيد", "team": "Team B"}])
    add_user(repo, "m1", "manager", ["Team A"])
    d = dt.datetime
    mgr = repo.get_user("m1")
    imp = ss.start_import(repo, mgr, schedule_book([["الخميس", d(2026, 10, 1), "علي حسن", "منى سعيد"]]), "s.xlsx", 2026, 10, "Team A")
    with pytest.raises(access.Forbidden):
        ss.confirm_import(repo, mgr, imp, {}, True, True, False, False)
    assert repo.active_version(2026, 10) is None


def test_conflicts_are_flagged_but_do_not_block(app, repo, admin_b, tmp_path):
    add_employees(repo, [{"id": 1, "name": "علي حسن"}])
    d = dt.datetime
    p = tmp_path / "s.xlsx"
    p.write_bytes(schedule_book([["الخميس", d(2026, 10, 1), "علي حسن", None, "علي حسن"]]))      # morning + evening overlap 14-15
    url = admin_b.upload("/schedule/import", str(p), {"year": 2026, "month": 10, "team": "A"}).headers["Location"]
    assert "overlap" in admin_b.get(url).get_data(as_text=True)
    assert admin_b.post(url + "/confirm", {}).status_code == 302
    flags = {e["shift_code"]: e["flags"] for e in repo.schedule_entries("2026-10-01", "2026-10-01")}
    assert flags["E"] == "overlap"


# ---------------------------------------------------------------- performance
@pytest.mark.slow
def test_large_attendance_file_imports_in_reasonable_time(app, repo, admin_b, tmp_path):
    n_emp, n_days = 250, 400                                         # 100,000 rows (~13 months of 250 people)
    add_employees(repo, [{"id": i, "name": f"E{i}"} for i in range(1, n_emp + 1)])
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet()
    ws.append(["رقم الموظف", "أسم الموظف", "التاريخ", "توقيت الحضور", "توقيت الأنصراف"])
    start = dt.date(2025, 1, 1)
    for e in range(1, n_emp + 1):
        for d in range(n_days):
            ws.append([e, f"E{e}", start + dt.timedelta(days=d), dt.time(8, d % 60, 0), dt.time(16, d % 60, 0)])
    path = tmp_path / "big.xlsx"
    wb.save(path)
    t0 = time.time()
    r = admin_b.upload("/attendance/import", str(path))
    assert r.status_code == 302
    t_parse = time.time() - t0
    t1 = time.time()
    assert admin_b.post(r.headers["Location"] + "/confirm", {}).status_code == 302
    t_commit = time.time() - t1
    assert repo.list_attendance_imports()[0]["result"]["inserted"] == n_emp * n_days
    t2 = time.time()
    from calllog.attendance.services import analytics_service as an
    ev = an.evaluate_range(repo, list(range(1, 51)), "2025-01-01", "2025-12-31", dt.datetime(2026, 10, 14))
    t_eval = time.time() - t2
    print(f"\nlarge import: parse+preview {t_parse:.1f}s, commit {t_commit:.1f}s, 50 employees x 365 days analytics {t_eval:.1f}s")
    assert t_parse < 120 and t_commit < 60 and t_eval < 30 and len(ev) == 50
