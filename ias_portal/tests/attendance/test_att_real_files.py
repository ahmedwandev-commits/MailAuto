"""End-to-end tests against the two REAL workbooks supplied by HR (copies live in samples/)."""
import pytest

from .support import ATT_FILE, SCHEDULE_FILE, add_employees, needs_samples

pytestmark = needs_samples


def import_attendance(b, repo):
    """In the portal people are accounts: first create one per employee number found in the HR file (as an administrator would on
    the Users page), then import the attendance."""
    from calllog.attendance.services import attendance_service as asv
    r = b.upload("/attendance/import", ATT_FILE)
    assert r.status_code == 302, r.data[:500]
    url = r.headers["Location"]
    pv = b.get(url)
    assert pv.status_code == 200
    unknown = asv.build_preview(repo, int(url.rsplit("/", 1)[1]))["unknown_employees"]
    assert len(unknown) == 40
    add_employees(repo, [{"id": int(i), "name": v.get("name") or f"Employee {i}", "department": v.get("department", "")} for i, v in unknown.items()])
    r = b.post(url + "/confirm", {})
    assert r.status_code == 302
    return url


def schedule_preview(b, team="Applications", year=2026, month=10):
    r = b.upload("/schedule/import", SCHEDULE_FILE, {"year": year, "month": month, "team": team})
    assert r.status_code == 302, r.data[:800].decode("utf-8", "replace")
    return r.headers["Location"]


def confirm_schedule(b, repo, url, **extra):
    """Confirm using the suggestions the preview offers (what a human would click)."""
    imp_id = int(url.rsplit("/", 1)[1])
    from calllog.attendance.services import schedule_service as ss
    pv = ss.build_preview(repo, imp_id)
    data = {"accept_dates": "1"}
    for n, m in pv["matches"].items():
        if m["status"] in ("suggested", "ambiguous") and m["candidates"]:
            data[f"map:{n}"] = str(m["candidates"][0]["id"])
    data.update(extra)
    return b.post(url + "/confirm", data)


@pytest.fixture
def loaded(app, repo, admin_b):
    import_attendance(admin_b, repo)
    return admin_b


def test_attendance_import_real_file(app, repo, admin_b):
    import_attendance(admin_b, repo)
    assert len(repo.list_employees()) == 40
    recs = repo.attendance_records("2026-10-01", "2026-10-13")
    assert len(recs) == 40 * 13
    # a known row: employee 112100 on 2026-10-04 08:13:12 -> 16:32:08
    r = next(x for x in recs if x["employee_id"] == 112100 and x["work_date"] == "2026-10-04")
    assert (r["in_time"], r["out_time"]) == ("08:13:12", "16:32:08")
    # leave text kept for the 1st
    r1 = next(x for x in recs if x["employee_id"] == 112100 and x["work_date"] == "2026-10-01")
    assert "اعتيادية" in r1["leave_text"]
    # same file again is refused and nothing changes
    resp = admin_b.upload("/attendance/import", ATT_FILE)
    assert resp.status_code == 400 and b"already imported" in resp.data
    assert len(repo.attendance_records("2026-10-01", "2026-10-13")) == 40 * 13


def test_real_hr_columns_are_mapped_by_name(app, repo, admin_b):
    import openpyxl
    from calllog.attendance.importers import attendance_xlsx as ax
    p = ax.parse_workbook(openpyxl.load_workbook(ATT_FILE, read_only=True))
    h = {f: p["header"][i] for f, i in p["mapping"].items()}
    assert h["leave_text"] == "موقف الاجازات" and h["permit_text"] == "موقف ( الأذون )" and h["mission_text"] == "موقف ( المهمات )"
    assert h["employee_id"] == "رقم الموظف" and h["in_time"] == "توقيت الحضور" and h["out_time"] == "توقيت الأنصراف"
    import_attendance(admin_b, repo)
    recs = repo.attendance_records("2026-10-01", "2026-10-13")
    assert sorted({r["permit_text"] for r in recs if r["permit_text"]}) == ["اذن خاص حضور", "بدل راحة"]
    assert sorted({r["mission_text"] for r in recs if r["mission_text"]}) == ["مهمه داخل وحدات البنك", "مهمه لاختبارات الترقيات"]
    assert sum(1 for r in recs if r["permit_text"] == "بدل راحة") == 9


def test_rest_in_lieu_is_a_compensatory_day_off_not_an_absence(app, repo, admin_b):
    import datetime as dt
    from calllog.attendance.services import analytics_service as an
    import_attendance(admin_b, repo)
    rows = [r for r in repo.attendance_records("2026-10-01", "2026-10-13") if r["permit_text"] == "بدل راحة"]
    for r in rows:
        ev = an.evaluate_range(repo, [r["employee_id"]], r["work_date"], r["work_date"], dt.datetime(2026, 10, 14))
        d = ev[r["employee_id"]][0]
        assert d["status"] in ("on_leave", "rest_day_worked", "present", "late", "present_unscheduled", "missing_punch"), d
        if not (r["in_time"] or r["out_time"]):
            assert d["status"] == "on_leave" and d["leave_type"] == "comp"


def test_schedule_preview_matching_and_flags(app, repo, loaded):
    url = schedule_preview(loaded)
    from calllog.attendance.services import schedule_service as ss
    pv = ss.build_preview(repo, int(url.rsplit("/", 1)[1]))
    p = pv["parsed"]
    assert (p["title_year"], p["title_month"]) == (2026, 10)
    assert [g["code"] for g in p["groups"]] == ["M", "E", "N"]
    assert [(g["start"], g["end"]) for g in p["groups"]] == [("07:00", "15:00"), ("14:00", "22:00"), ("22:00", "07:00")]
    assert len(p["days"]) == 31
    assert [d["date"] for d in pv["dates_corrected"]] == ["2026-10-31"]       # the 2026-10-26 typo on the last Saturday
    st = {m["display"]: m for m in pv["matches"].values()}
    assert st["محمد ناصر"]["status"] == "auto" and st["محمد ناصر"]["employee_id"] == 242048
    assert st["كريم حمدي"]["employee_id"] == 239008                              # not 227917 (كريم عبدالوهاب)
    assert st["محمد ذكريا"]["status"] == "suggested"                             # ذ/ز typo needs confirmation
    assert st["محمد ذكريا"]["candidates"][0]["id"] == 198858
    assert st["جوزيف فادي"]["status"] == "suggested"                             # فادي vs عبدالفادى
    # the preview page itself renders and lists the correction
    page = loaded.get(url)
    assert page.status_code == 200 and "2026-10-31" in page.get_data(as_text=True)


def test_schedule_requires_date_confirmation_and_all_names(app, repo, loaded):
    url = schedule_preview(loaded)
    r = loaded.post(url + "/confirm", {})
    assert r.status_code == 400                                                  # date correction not accepted
    r = loaded.post(url + "/confirm", {"accept_dates": "1"})
    assert r.status_code == 400 and "still need" in r.get_data(as_text=True)     # suggested names not chosen
    assert repo.active_version(2026, 10) is None


def test_schedule_full_import_and_duplicates(app, repo, loaded):
    url = schedule_preview(loaded)
    r = confirm_schedule(loaded, repo, url)
    assert r.status_code == 302, r.get_data(as_text=True)[:600]
    v = repo.active_version(2026, 10)
    assert v["version_no"] == 1
    entries = repo.schedule_entries("2026-10-01", "2026-10-31")
    work = [e for e in entries if e["shift_code"] in ("M", "E", "N")]
    assert {e["shift_code"] for e in entries} == {"M", "E", "N", "SB"}          # rest days are NOT invented by default
    assert len(work) > 200
    # 31 Oct was corrected from the sheet's second "26 Oct"
    assert any(e["work_date"] == "2026-10-31" and e["shift_code"] == "N" for e in entries)
    # a weekend merged cell has no stream
    assert any(e["work_date"] == "2026-10-02" and e["shift_code"] == "M" and e["stream"] == "" for e in entries)
    # duplicates: same file again -> refused, nothing added
    n_before = len(repo.schedule_entries("2026-10-01", "2026-10-31"))
    resp = loaded.upload("/schedule/import", SCHEDULE_FILE, {"year": 2026, "month": 10, "team": "Applications"})
    assert resp.status_code == 400 and b"already imported" in resp.data
    assert len(repo.schedule_entries("2026-10-01", "2026-10-31")) == n_before
    # the audit trail has the import
    assert any(a["action"] == "schedule_import_completed" for a in repo.list_audit())
    # teams belong to the portal account: the import never changes them
    assert repo.get_employee(242048)["team"] == ""


def test_rest_day_inference_is_optional_and_checked_against_hr_data(app, repo, loaded):
    url = schedule_preview(loaded)
    from calllog.attendance.services import schedule_service as ss
    pv = ss.build_preview(repo, int(url.rsplit("/", 1)[1]))
    # in the real data most "unlisted" days have punches -> the preview must warn that inference is unreliable
    assert pv["rest_check"]["candidate_days"] > 0 and pv["rest_check"]["pct"] > 20
    assert "probably lists only duty shifts" in loaded.get(url).get_data(as_text=True)
    assert confirm_schedule(loaded, repo, url, infer_rest="1").status_code == 302
    entries = repo.schedule_entries("2026-10-01", "2026-10-31")
    roster = {e["employee_id"] for e in entries}
    assert any(e["shift_code"] == "OFF" and e["source"] == "inferred" for e in entries)
    for emp in list(roster)[:5]:
        assert len({e["work_date"] for e in entries if e["employee_id"] == emp}) == 31


def test_replace_needs_explicit_confirmation_and_keeps_old_version(app, repo, loaded, tmp_path):
    import openpyxl
    url = schedule_preview(loaded)
    assert confirm_schedule(loaded, repo, url).status_code == 302
    # a changed copy of the same workbook (one name edited) is NOT a duplicate...
    wb = openpyxl.load_workbook(SCHEDULE_FILE)
    ws = wb.worksheets[0]
    ws["C9"] = "محمد ناصر"
    p2 = tmp_path / "changed.xlsx"
    wb.save(p2)
    r = loaded.upload("/schedule/import", str(p2), {"year": 2026, "month": 10, "team": "Applications"})
    assert r.status_code == 302
    url2 = r.headers["Location"]
    page = loaded.get(url2).get_data(as_text=True)
    assert "already exists" in page
    # ...but it cannot silently overwrite
    resp = confirm_schedule(loaded, repo, url2)
    assert resp.status_code == 400 and "replace" in resp.get_data(as_text=True).lower()
    assert repo.active_version(2026, 10)["version_no"] == 1
    # explicit confirmation creates version 2; version 1 is kept
    resp = confirm_schedule(loaded, repo, url2, replace="1")
    assert resp.status_code == 302
    assert repo.active_version(2026, 10)["version_no"] == 2
    versions = repo.list_versions(2026, 10)
    assert [(x["version_no"], x["status"]) for x in versions] == [(2, "active"), (1, "superseded")]
    assert versions[1]["entry_count"] > 200                                     # old rows are still there


def test_wrong_month_is_blocked(app, repo, loaded):
    url = schedule_preview(loaded, month=11)
    r = loaded.post(url + "/confirm", {"accept_dates": "1"})
    assert r.status_code == 400 and "title says" in r.get_data(as_text=True)


def test_not_an_excel_file_is_rejected(app, repo, loaded, tmp_path):
    bad = tmp_path / "x.xlsx"
    bad.write_bytes(b"not a workbook")
    r = loaded.upload("/schedule/import", str(bad), {"year": 2026, "month": 10, "team": "A"})
    assert r.status_code == 400
    r = loaded.upload("/attendance/import", str(bad))
    assert r.status_code == 400

