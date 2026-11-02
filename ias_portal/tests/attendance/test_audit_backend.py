"""Regression tests of the Attendance / Workforce backend audit: every test here failed before its fix."""
import datetime as dt
import io

import openpyxl
import pytest

from calllog.attendance.importers import schedule_xlsx, templates
from calllog.attendance.services import (allowance_confirm as ac, analytics_service as an, attendance_service as ats, leave_service as ls,
                                         reports_service as rs, schedule_service as ss, team_plan as tp)
from calllog.attendance.services.access import Forbidden
from calllog.attendance.services.schedule_service import ImportError_

from .support import add_employees, add_user, login

NOW = dt.datetime(2026, 10, 20, 12, 0)


# ---------------------------------------------------------------- schedule import: a name with several suggestions is never guessed
def _schedule_import(repo, names, year=2026, month=11):
    """A parsed schedule (as the parser returns it) with one morning entry per name on the 2nd of the month."""
    d = f"{year}-{month:02d}-02"
    entries = [{"row": 4 + i, "col": 3, "date": d, "shift_code": "M", "stream": "", "name_raw": n, "note": "", "name_norm": n,
                "kind": "work", "invalid": False} for i, n in enumerate(names)]
    parsed = {"sheet": "s", "title_year": year, "title_month": month, "groups": [], "issues": [], "entries": entries,
              "days": [{"row": 4, "date": d, "date_raw": d, "day_name": "", "corrected": False, "invalid": False}],
              "years": [str(year)], "months": [f"{month:02d}"]}
    return repo.create_schedule_import({"year": year, "month": month, "filename": "s.xlsx", "file_sha256": "s" * 64,
                                        "content_sha256": "c", "team": "", "uploaded_by": "admin", "status": "preview",
                                        "parsed": parsed})


def test_direct_schedule_import_never_picks_one_of_several_suggestions(app, repo, users):
    add_employees(repo, [{"id": 301, "name": "Mohamed Ali", "team": "Team A"}, {"id": 302, "name": "Mohamed Hassan", "team": "Team A"},
                         {"id": 303, "name": "Sara Adel", "team": "Team A"}])
    imp = _schedule_import(repo, ["محمد", "sara adel"])                    # «محمد» sounds like both Mohameds
    admin = repo.get_user("admin")
    with app.test_request_context():
        res = ss.import_directly(repo, admin, imp)
    planned = {e["employee_id"] for e in repo.schedule_entries("2026-11-01", "2026-11-30")}
    assert planned == {303}                                                 # nobody got the shifts of an ambiguous name
    assert "محمد" in res["auto"]["names_skipped"] and not res["auto"]["suggestions_used"]


def test_direct_schedule_import_still_uses_a_single_suggestion(app, repo, users):
    add_employees(repo, [{"id": 301, "name": "Mohamed Ali", "team": "Team A"}])
    imp = _schedule_import(repo, ["محمد"])
    with app.test_request_context():
        res = ss.import_directly(repo, repo.get_user("admin"), imp)
    assert {e["employee_id"] for e in repo.schedule_entries("2026-11-01", "2026-11-30")} == {301}
    assert res["auto"]["suggestions_used"][0]["id"] == 301


# ---------------------------------------------------------------- the blank schedule template is read back without invented names
def test_the_blank_schedule_template_has_no_stand_by_names_in_its_instructions():
    wb = openpyxl.load_workbook(io.BytesIO(templates.schedule_template(2026, 11)))
    wb.worksheets[0]["C5"] = "Ahmed - Mona"
    buf = io.BytesIO()
    wb.save(buf)
    parsed = schedule_xlsx.parse_workbook(openpyxl.load_workbook(io.BytesIO(buf.getvalue()), data_only=True))
    assert sorted(e["name_raw"] for e in parsed["entries"]) == ["Ahmed", "Mona"]
    assert not [e for e in parsed["entries"] if e["kind"] == "standby"]


# ---------------------------------------------------------------- team / department comparison: attendance % of the evaluated days
def _row(team, scheduled, judged, present):
    keys = ("scheduled_days", "present_days", "absent_days", "late_count", "late_minutes", "early_count", "missing_in", "missing_out",
            "missing_both", "leave_days", "scheduled_hours", "actual_hours")
    s = {k: 0 for k in keys}
    s.update(scheduled_days=scheduled, judged_days=judged, present_days=present, leave_days=scheduled - judged)
    return {"employee": {"team": team, "department": "D"}, "summary": s, "score": {"score": 100}}


def test_team_attendance_pct_is_present_of_judged_days_like_the_employee_summary():
    # 22 planned days, 2 of them on leave: 20 judged, 20 present = 100 % (the employee's own summary says 100 % too)
    out = an.compare_groups([_row("Team A", 22, 20, 20)], "team")
    assert out[0]["attendance_pct"] == 100.0


# ---------------------------------------------------------------- an impossible date never reaches the stored corrections / reviews
@pytest.mark.parametrize("bad", ["2026-02-30", "", "garbage", "2026-13-01"])
def test_corrections_and_reviews_refuse_an_impossible_date(app, world, repo, bad):
    admin = repo.get_user("admin")
    with pytest.raises(ImportError_):
        ats.correct_punch(repo, admin, 101, bad, "in_time", "07:00", "typo")
    with pytest.raises(ImportError_):
        ats.review_absence(repo, admin, 101, bad, "excused", "why")
    with pytest.raises(ImportError_):
        ats.review_permit(repo, admin, 101, bad, "late", "excused", "why")
    with pytest.raises(ImportError_):
        ats.report_forgotten_punch(repo, admin, 101, bad, "in", "why")
    assert repo.corrections("0000-01-01", "9999-12-31") == []


def test_a_correction_on_february_30_cannot_break_the_allowance_export(app, world, repo, admin_b):
    r = admin_b.post("/attendance/hr/correct", {"emp": "101", "date": "2026-02-30", "mode": "set", "in_time": "07:00",
                                                 "out_time": "15:00", "reason": "x"})
    assert r.status_code == 400
    assert admin_b.get("/attendance/allowance?from=2026-02-01&to=2026-03-31").status_code == 200


# ---------------------------------------------------------------- a working shift type needs its times (else every page crashes)
def test_a_working_shift_type_without_times_is_refused(app, world, repo, admin_b):
    r = admin_b.post("/attendance/admin/settings", {"action": "shift", "code": "X", "name": "Extra", "kind": "work", "start": "", "end": ""})
    assert r.status_code == 400 and "X" not in {s["code"] for s in repo.list_shift_types()}
    r = admin_b.post("/attendance/admin/settings", {"action": "shift", "code": "X", "name": "Extra", "kind": "work", "start": "7am", "end": "3pm"})
    assert r.status_code == 400 and "X" not in {s["code"] for s in repo.list_shift_types()}
    r = admin_b.post("/attendance/admin/settings", {"action": "shift", "code": "X", "name": "Extra", "kind": "work", "start": "9:00", "end": "17:00"})
    assert r.status_code == 302
    assert next(s for s in repo.list_shift_types() if s["code"] == "X")[("start_time")] == "09:00"
    # a rest-day kind needs no times
    r = admin_b.post("/attendance/admin/settings", {"action": "shift", "code": "PL", "name": "Planned leave", "kind": "off"})
    assert r.status_code == 302
    assert admin_b.get("/attendance/planning/day?date=2026-10-12").status_code == 200


# ---------------------------------------------------------------- a shift changed in a month that has no schedule yet
def test_a_manager_changes_a_shift_in_a_month_without_a_schedule(app, world, repo, mgr1_b):
    assert repo.active_version(2026, 12) is None
    r = mgr1_b.post("/attendance/schedule/edit", {"emp": "101", "date": "2026-12-06", "codes": "E", "reason": "cover"})
    assert r.status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-12-06", "2026-12-06", [101])] == ["E"]


# ---------------------------------------------------------------- redirects after a form stay inside the portal
def test_name_mapping_add_missing_never_redirects_outside_the_portal(app, world, repo, admin_b):
    imp = _schedule_import(repo, ["Ali A1"], 2026, 10)
    repo.update_schedule_import(imp, {"status": "completed"})
    for evil in ("https://evil.example/x", "//evil.example"):
        r = admin_b.post("/attendance/schedule/names", {"action": "add_missing", "month": "2026-10", "next": evil})
        assert r.status_code == 302 and "evil" not in r.headers["Location"], evil
    r = admin_b.post("/attendance/schedule/names", {"action": "add_missing", "month": "2026-10", "next": "/attendance/planning/day?date=2026-10-12"})
    assert r.headers["Location"].endswith("/attendance/planning/day?date=2026-10-12")


# ---------------------------------------------------------------- a manager with every team («__all__») is the manager of every team
def test_an_all_teams_manager_confirms_and_plans_every_team(app, world, repo, admin_b):
    add_user(repo, "boss", "manager", ["__all__"])
    boss = repo.get_user("boss")
    with app.test_request_context():
        ac.start(repo, repo.get_user("admin"), "2026-10")
        assert ac.confirm_team(repo, boss, "2026-10", "Team B")["people"] == 2
        assert tp.board(repo, boss, "2026-11", "Team A")["can"]["shift"]
    assert "Team A" in tp.teams_for(repo, boss)
    page = login(app, "boss").get("/attendance/allowance/confirm?month=2026-10").get_data(as_text=True)
    assert "Omar B1" in page
    # a manager of one team still cannot reach the other
    with app.test_request_context(), pytest.raises(Forbidden):
        ac.confirm_team(repo, repo.get_user("mgr1"), "2026-10", "Team B")


# ---------------------------------------------------------------- coverage report: a double shift on leave is away from both shifts
def test_coverage_report_counts_a_double_shift_on_leave_in_both_shifts(app, world, repo):
    repo.replace_day_entries_atomic(2026, 10, 101, "2026-10-13", [{"shift_code": "M"}, {"shift_code": "E"}], "double", "admin")
    repo.create_leave_request({"employee_id": 101, "leave_type": "annual", "date_from": "2026-10-13", "date_to": "2026-10-13",
                               "days": 1, "status": "approved", "reason": "", "created_by": "admin"})
    rep = rs.build(repo, repo.get_user("admin"), "coverage", "2026-10-13", "2026-10-13", NOW)
    row = rep["rows"][0]
    cols = dict(zip(rep["columns"], row))
    assert cols["Morning planned"] == 2 and cols["Morning available"] == 1            # 101 (on leave) and 201
    assert cols["Evening planned"] == 2 and cols["Evening available"] == 1            # 101 (on leave) and 102


# ---------------------------------------------------------------- the leave list of a team: a person is in ONE Workforce team (v5.17)
def test_leave_list_team_filter_uses_the_one_workforce_team(app, world, repo, admin_b):
    repo.directory.store.update_user("102", {"teams": ["Team A", "Team C"]})
    repo.create_leave_request({"employee_id": 102, "leave_type": "annual", "date_from": "2026-10-20", "date_to": "2026-10-21",
                               "days": 2, "status": "approved", "reason": "trip-102", "created_by": "admin"})
    assert "trip-102" in admin_b.get("/attendance/leave?team=Team%20A&tab=requests&year=2026&month=10").get_data(as_text=True)
    assert "trip-102" not in admin_b.get("/attendance/leave?team=Team%20C&tab=requests&year=2026&month=10").get_data(as_text=True)


# ---------------------------------------------------------------- allowance confirmation: somebody with two teams is in one, paid once
def test_the_sector_total_counts_a_person_of_two_teams_once(app, world, repo):
    repo.save_settings({"allowance_rules": {"morning": {"rate": 150}}})
    repo.directory.store.update_user("201", {"teams": ["Team B", "Team A"]})
    st = ac.status(repo, "2026-10")
    assert st["total_amount"] == 0                                                              # counts only: no money (checkpoint 4)
    people = [p for t in st["teams"] for p in t["people"] if p["id"] == 201]
    assert len(people) == 1 and [t["team"] for t in st["teams"] if any(p["id"] == 201 for p in t["people"])] == ["Team B"]   # one team, once


# ---------------------------------------------------------------- leave: the days charged must fit the dates
@pytest.mark.parametrize("days", ["abc", "-1", "0", "5"])
def test_leave_days_must_be_a_number_within_the_dates(app, world, repo, days):
    with pytest.raises(ImportError_):
        ls.request_leave(repo, repo.get_user("admin"), 101, "annual", "2026-10-20", "2026-10-21", "trip", days)
    assert repo.leave_requests([101]) == []


def test_leave_half_day_and_default_days(app, world, repo):
    ls.request_leave(repo, repo.get_user("admin"), 101, "annual", "2026-10-20", "2026-10-20", "half", "0.5")
    ls.request_leave(repo, repo.get_user("admin"), 101, "casual", "2026-10-22", "2026-10-23", "two")
    assert sorted(r["days"] for r in repo.leave_requests([101])) == [0.5, 2.0]


# ---------------------------------------------------------------- a month out of range is this month, not an error page
def test_month_out_of_range_is_not_an_error(app, world, admin_b):
    assert admin_b.get("/attendance/schedule?year=2026&month=13").status_code == 200
    assert admin_b.get("/attendance/templates/schedule.xlsx?year=2026&month=13").status_code == 200
    assert admin_b.get("/attendance/templates/schedule.xlsx?year=x&month=y").status_code == 200


# ---------------------------------------------------------------- the PDF carries the totals line of the page and the Excel file
def test_pdf_export_has_the_totals_line(monkeypatch):
    pytest.importorskip("reportlab")
    from calllog.attendance.services import pdf_export
    if not pdf_export.find_font():
        pytest.skip("no TrueType font")
    import reportlab.platypus as platypus
    seen = {}
    real = platypus.Table

    def spy(data, *a, **k):
        seen["data"] = data
        return real(data, *a, **k)
    monkeypatch.setattr(platypus, "Table", spy)
    rep = {"title": "T", "columns": ["ID", "Employee", "Late #"], "rows": [[1, "A", 2], [2, "B", 3]], "notes": [],
           "totals": ["Total", "", 5]}
    pdf_export.render(rep, "2026-10-01", "2026-10-31")
    assert seen["data"][-1][0] == "Total" and seen["data"][-1][2] == "5"
