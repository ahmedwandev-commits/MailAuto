"""Weekly planning, day view, leave calendar, column re-mapping, PDF export, navigation."""
from .support import add_employees, set_user
import io
import datetime as dt

import openpyxl
import pytest

from calllog.attendance.services import pdf_export
from .support import add_user, login


def post(b, url, **data):
    return b.post(url, data)


# ---------------------------------------------------------------- weekly plan
def test_week_page_scopes_to_the_team_and_shows_availability(app, world, repo, mgr1_b):
    page = mgr1_b.get("/planning/week?start=2026-10-11&team=Team%20A").get_data(as_text=True)
    assert "Ali A1" in page and "Sara A2" in page and "Omar B1" not in page
    assert 'name="cell_101_2026-10-12"' in page and 'name="cell_201_2026-10-12"' not in page
    # a manager asking for another team's week gets no one from it
    other = mgr1_b.get("/planning/week?start=2026-10-11&team=Team%20B").get_data(as_text=True)
    assert "Omar B1" not in other and "cell_201" not in other


def test_week_save_records_every_change_with_one_reason(app, world, repo, mgr1_b):
    r = post(mgr1_b, "/planning/week/save", start="2026-10-11", team="Team A", reason="rebalance",
             **{"cell_101_2026-10-12": "E", "cell_101_2026-10-13": "M", "cell_102_2026-10-12": "OFF"})
    assert r.status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-12", "2026-10-12", [101])] == ["E"]
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-12", "2026-10-12", [102])] == ["OFF"]
    ch = repo.list_schedule_changes(2026, 10)
    assert len(ch) == 2 and all(c["reason"] == "rebalance" and c["user_id"] == "mgr1" for c in ch)    # the unchanged cell wrote nothing
    assert {(c["employee_id"], c["old_value"], c["new_value"]) for c in ch} == {(101, "M", "E"), (102, "E", "OFF")}


def test_week_save_rules(app, world, repo, mgr1_b, gm_b):
    base = {"start": "2026-10-11", "team": "Team A"}
    assert post(mgr1_b, "/planning/week/save", **base, reason="", **{"cell_101_2026-10-12": "E"}).status_code == 400
    assert post(mgr1_b, "/planning/week/save", **base, reason="x", **{"cell_101_2026-10-12": ""}).status_code == 400     # cannot un-plan a day
    assert post(mgr1_b, "/planning/week/save", **base, reason="x", **{"cell_201_2026-10-12": "E"}).status_code == 403     # other team
    assert post(mgr1_b, "/planning/week/save", start="2026-10-11", team="Team B", reason="x", **{"cell_201_2026-10-12": "E"}).status_code == 403
    assert post(mgr1_b, "/planning/week/save", **base, reason="x", **{"cell_101_2026-10-30": "E"}).status_code == 403     # outside the week
    assert post(gm_b, "/planning/week/save", **base, reason="x", **{"cell_101_2026-10-12": "E"}).status_code == 302        # read-only role: refused (redirect)
    assert repo.list_schedule_changes() == []


def test_planning_creates_a_manual_schedule_when_the_month_has_none(app, world, repo, mgr1_b):
    assert repo.active_version(2026, 11) is None
    r = post(mgr1_b, "/planning/week/save", start="2026-11-01", team="Team A", reason="first plan", **{"cell_101_2026-11-02": "M"})
    assert r.status_code == 302
    v = repo.active_version(2026, 11)
    assert v and v["import_id"] is None
    assert [e["shift_code"] for e in repo.schedule_entries("2026-11-02", "2026-11-02", [101])] == ["M"]
    assert any(a["action"] == "schedule_created_manually" for a in repo.list_audit())











# ---------------------------------------------------------------- day view + leave calendar
def test_day_view_groups_people_by_shift_and_flags_shortage(app, world, repo, admin_b):
    repo.save_settings({"min_staffing_pct": 100})
    post(admin_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-13", "to": "2026-10-13"})        # Team A morning: 1 planned, 1 on leave -> 0 %
    page = admin_b.get("/planning/day?date=2026-10-13").get_data(as_text=True)
    assert "Short staffed" in page and "Ali A1" in page and "Nour B2" in page
    assert "Omar B1" not in admin_b.get("/planning/day?date=2026-10-13&team=Team%20A").get_data(as_text=True)


def test_leave_calendar_shows_requests_comp_days_and_hr_text(app, world, repo, admin_b):
    post(admin_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-21"})
    rid = repo.leave_requests([101])[0]["id"]
    repo.create_leave_request({"employee_id": 102, "leave_type": "sick", "date_from": "2026-10-22", "date_to": "2026-10-22", "days": 1, "status": "pending"})   # legacy pending row
    post(admin_b, "/comp/new", emp="101", earned="2026-10-09", days="1", reason="duty", planned="2026-10-25")
    a = repo.create_attendance_import({"filename": "l", "file_sha256": "lc", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 201, "work_date": "2026-10-05", "leave_text": "أجازة مرضي عادي"}])
    from calllog.attendance.services import planning_service as ps
    cal = ps.leave_calendar(repo, repo.get_user("admin"), 2026, 10)
    c = cal["cells"]
    assert c[(101, "2026-10-20")] == {"kind": "annual", "state": "approved", "ref": rid}
    assert c[(102, "2026-10-22")]["state"] == "pending" and c[(101, "2026-10-25")]["kind"] == "comp" and c[(101, "2026-10-25")]["state"] == "planned"
    assert c[(201, "2026-10-05")]["kind"] == "sick" and c[(201, "2026-10-05")]["state"] == "hr"                     # HR text mapped to a type
    assert cal["per_day"]["2026-10-20"] == 1 and cal["per_day"]["2026-10-22"] == 0                                # pending does not count as away
    assert admin_b.get("/leave?year=2026&month=10").status_code == 200


def test_planned_comp_day_and_leave_reduce_weekly_availability(app, world, repo, admin_b):
    post(admin_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-13", "to": "2026-10-13"})
    from calllog.attendance.services import planning_service as ps
    wv = ps.week_view(repo, repo.get_user("admin"), "2026-10-11", "Team A")
    assert wv["avail"]["2026-10-13"]["M"] == {"planned": 1, "on_leave": 1, "available": 0, "pct": 0.0, "threshold": None, "short": False}
    assert wv["avail"]["2026-10-12"]["M"]["available"] == 1


# ---------------------------------------------------------------- column mapping on the attendance import
def _book(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_unrecognised_layout_goes_to_the_mapping_screen_and_can_be_fixed(app, world, repo, admin_b, tmp_path):
    p = tmp_path / "odd.xlsx"
    p.write_bytes(_book([["Monthly report"], ["Emp", "Day", "In", "Out"], [101, "2026-10-20", "07:05", "15:00"]]))
    r = admin_b.upload("/attendance/import", str(p))
    assert r.status_code == 302                                         # no longer a dead end
    url = r.headers["Location"]
    page = admin_b.get(url).get_data(as_text=True)
    assert "Could not find a header row" in page and 'name="map_employee_id"' in page
    assert admin_b.post(url + "/confirm", {}).status_code == 400        # cannot confirm before mapping
    m = admin_b.post(url + "/map", {"header_row": "2", "map_employee_id": "0", "map_date": "1", "map_in_time": "2", "map_out_time": "3"})
    assert m.status_code == 302
    page = admin_b.get(url).get_data(as_text=True)
    assert "New records" in page and "Could not find a header row" not in page
    assert admin_b.post(url + "/confirm", {}).status_code == 302
    rec = repo.attendance_records("2026-10-20", "2026-10-20", [101])[0]
    assert (rec["in_time"], rec["out_time"]) == ("07:05:00", "15:00:00")
    assert any(a["action"] == "attendance_import_remapped" for a in repo.list_audit())
    assert repo.load_upload(repo.list_attendance_imports()[0]["file_sha256"])                              # original kept


def test_wrong_automatic_mapping_can_be_corrected(app, world, repo, admin_b, tmp_path):
    p = tmp_path / "swapped.xlsx"
    p.write_bytes(_book([["رقم الموظف", "التاريخ", "توقيت الحضور", "توقيت الأنصراف"], [101, "2026-10-21", "15:00", "07:00"]]))
    url = admin_b.upload("/attendance/import", str(p)).headers["Location"]
    admin_b.post(url + "/map", {"header_row": "1", "map_employee_id": "0", "map_date": "1", "map_in_time": "3", "map_out_time": "2"})   # swap in/out
    parsed = repo.get_attendance_import(int(url.rsplit("/", 1)[1]))["parsed"]
    assert parsed["rows"][0]["in_time"] == "07:00:00" and parsed["rows"][0]["out_time"] == "15:00:00"


def test_preview_reports_punch_quality_on_working_days(app, world, repo, admin_b, tmp_path):
    p = tmp_path / "q.xlsx"
    p.write_bytes(_book([["رقم الموظف", "التاريخ", "نوع اليوم", "توقيت الحضور", "توقيت الأنصراف", "موقف الاجازات"],
                         [101, "2026-10-22", "يوم عمل", "07:00", "15:00", None], [101, "2026-10-23", "يوم عمل", "07:00", None, None],
                         [102, "2026-10-22", "يوم عمل", None, "15:00", None], [102, "2026-10-23", "يوم عمل", None, None, None],
                         [201, "2026-10-22", "العطلة الاسبوعية", None, None, None], [201, "2026-10-23", "يوم عمل", None, None, "أجازة اعتيادية"]]))
    url = admin_b.upload("/attendance/import", str(p)).headers["Location"]
    from calllog.attendance.services import attendance_service as ats
    ps = ats.build_preview(repo, int(url.rsplit("/", 1)[1]))["punch_stats"]
    assert ps == {"working_rows": 5, "missing_in": 1, "missing_out": 1, "missing_both": 1, "complete": 1, "on_leave_or_mission": 1}


# ---------------------------------------------------------------- reports
def test_rest_compliance_report(app, world, repo, admin_b):
    post(admin_b, "/planning/week/save", start="2026-10-11", team="Team A", reason="rest day", **{"cell_101_2026-10-12": "OFF"})   # 101 punched that day
    post(admin_b, "/comp/new", emp="101", earned="2026-10-12", days="1", reason="worked rest day")
    from calllog.attendance.services import reports_service as rs
    rep = rs.build(repo, repo.get_user("admin"), "rest", "2026-10-01", "2026-10-31", dt.datetime(2026, 10, 20))
    row = next(r for r in rep["rows"] if r[0] == 101)
    assert row[3:] == [1, 0, 1, 0.0, 1.0, 0.0]            # planned off 1, rested 0, worked 1, compliance 0 %, comp earned 1, taken 0


def test_ranking_puts_people_with_no_evaluated_days_last(app, world, repo, admin_b):
    admin_b.post("/admin/scoring", {"start": "100", "late_tiers": "1-=5", "categories": "Good=50; Low=0", "min_judged_days": "1", "effective_from": "2026-01-01"})
    repo.save_settings({"default_shift_enabled": False})        # without the default shift an unscheduled person has nothing to evaluate
    add_employees(repo, [{"id": 300, "name": "No Schedule", "team": "Team A"}])
    from calllog.attendance.services import reports_service as rs
    rep = rs.build(repo, repo.get_user("admin"), "ranking", "2026-10-12", "2026-10-12", dt.datetime(2026, 10, 20))
    ids = [r[0] for r in rep["rows"]]
    assert ids.index(300) > ids.index(101) and rep["rows"][-1][20] == 100            # unscored people last


# ---------------------------------------------------------------- PDF
def test_pdf_falls_back_cleanly_when_the_optional_packages_are_missing(app, world, admin_b, monkeypatch):
    monkeypatch.setattr(pdf_export, "available", lambda *a, **k: False)
    r = admin_b.get("/export/report.pdf?kind=summary&from=2026-10-01&to=2026-10-31")
    assert r.status_code == 302 and "/reports" in r.headers["Location"]
    page = admin_b.get(r.headers["Location"]).get_data(as_text=True)
    assert "PDF export needs the optional packages" in page


@pytest.mark.skipif(not pdf_export.available(), reason="optional PDF packages / Arabic font not installed")
def test_pdf_export_renders_arabic_names(app, world, repo, admin_b):
    set_user(repo, "101", {"name": "ياسر بيومى عبداللطيف"})
    r = admin_b.get("/export/report.pdf?kind=summary&from=2026-10-01&to=2026-10-31")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF") and len(r.data) > 2000
    assert any(a["action"] == "report_exported" and a["detail"].get("format") == "pdf" for a in repo.list_audit())
    # manager exports are team scoped too
    add_user(repo, "m9", "manager", ["Team B"])
    b = login(app, "m9")
    assert b.get("/export/report.pdf?kind=summary&from=2026-10-01&to=2026-10-31").status_code == 200


# ---------------------------------------------------------------- navigation per role
@pytest.mark.parametrize("who,present,absent", [          # v5.16: one menu - the portal's
    ("admin", [">Today<", ">Planning<", ">Shift allowance<", ">Analysis<", ">Settings<", ">Logs<"], [">Attendance settings<"]),
    ("mgr1", [">Today<", ">Planning<", ">Analysis<"], [">Settings<", ">Shift allowance<", ">Attendance settings<"]),
    ("gm", [">Today<", ">Planning<", ">Attendance analysis<"], [">Settings<", ">Attendance settings<", "/attendance/modify"])])
def test_navigation_matches_the_role(app, world, who, present, absent):
    html = login(app, who).get("/planning/day").get_data(as_text=True)
    nav = html.split('aria-label="Main"')[1].split("</nav>")[0]
    assert all(p in nav for p in present), nav
    assert not any(a in nav for a in absent)
    assert 'aria-label="Attendance sections"' not in html                     # no second menu inside the module
    if who == "admin":                                                         # its settings pages share one tab bar
        sub = login(app, who).get("/schedule/import").get_data(as_text=True)
        assert "HR attendance" in sub and "Scoring rules" in sub and "Roles &amp; permissions" in sub


def test_weekly_plan_shows_the_default_shift_as_auto_and_counts_it(app, world, repo, admin_b):
    """Working days nobody planned show the default shift as 'auto' and count in the team availability; Fri/Sat do not."""
    add_employees(repo, [{"id": 301, "name": "Default Dan", "team": "Team A"}])                      # no explicit entries at all
    from calllog.attendance.services import planning_service as ps
    wv = ps.week_view(repo, repo.get_user("admin"), "2026-10-11", "Team A")                          # Sun 11 .. Sat 17
    assert wv["default_code"] == "D" and wv["avail_codes"] == ["M", "E", "N", "D"]
    assert {d for (emp, d) in wv["defaults"] if emp == 301} == {"2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"}
    assert (102, "2026-10-12") not in wv["defaults"]                                                 # 102 has an explicit Evening
    assert wv["avail"]["2026-10-12"]["D"]["planned"] == 1 and wv["avail"]["2026-10-16"]["D"]["planned"] == 0
    page = admin_b.get("/planning/week?team=Team%20A&start=2026-10-11").get_data(as_text=True)
    assert "D auto" in page and "Normal day (auto when nothing is planned)" in page
    repo.save_settings({"default_shift_enabled": False})
    wv = ps.week_view(repo, repo.get_user("admin"), "2026-10-11", "Team A")
    assert wv["default_code"] is None and wv["defaults"] == {} and wv["avail_codes"] == ["M", "E", "N"]
