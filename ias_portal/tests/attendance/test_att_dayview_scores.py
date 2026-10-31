"""Day view: 'Annual Leaves & Rest days' beside the shifts; the score reports that explain every lost point."""
from calllog.attendance.services import planning_service as pl, scoring_service as sc


def test_annual_leaves_and_rest_days_sit_with_the_shifts(app, world, repo, admin_b):
    page = admin_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)
    assert "Annual Leaves &amp; Rest days" in page and "Rest days &amp; others" not in page
    shifts = page.split('class="shift-rows"')[1].split("</section>")[0]                                              # v5.16 Today: the Shifts card
    assert "Annual Leaves &amp; Rest days" in shifts and shifts.index("Night") < shifts.index("Annual Leaves")          # with the shifts, after them
    assert shifts.index("Stand-by") < shifts.index("Annual Leaves")                                                    # stand-by is its own row


def test_everybody_away_is_in_one_list_once(app, world, repo, users):
    dv = pl.day_view(repo, {"user_id": "admin", "role": "admin", "teams": []}, "2026-10-12", __import__("datetime").datetime(2026, 10, 13, 12, 0))
    ids = [r["employee_id"] for r in dv["away"]]
    assert len(ids) == len(set(ids))
    assert all(r["status"] in ("on_leave", "rest_day", "holiday") or not r["planned_primary"] or r["planned_primary"] == "OFF" for r in dv["away"])


def test_a_double_shift_is_in_both_shifts(app, world, repo, users):
    import datetime
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "t", "file_sha256": "s" * 64, "team": "Team A", "parsed": {}})
    repo.commit_schedule_version_atomic(2026, 10, imp, [{"employee_id": 101, "work_date": "2026-10-20", "shift_code": c, "source": "import"} for c in ("M", "E")], "admin", replace=True)
    dv = pl.day_view(repo, {"user_id": "admin", "role": "admin", "teams": []}, "2026-10-20", datetime.datetime(2026, 10, 21, 12, 0))
    assert 101 in [r["employee_id"] for r in dv["groups"]["M"]] and 101 in [r["employee_id"] for r in dv["groups"]["E"]]
    assert any(r.get("double") for r in dv["groups"]["E"]) and dv["summary"]["E"]["planned"] >= 1


def test_a_double_shift_counts_in_team_availability_and_staffing(app, world, repo, users):
    import datetime
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "t", "file_sha256": "s" * 64, "team": "Team A", "parsed": {}})
    repo.commit_schedule_version_atomic(2026, 10, imp, [{"employee_id": 101, "work_date": "2026-10-20", "shift_code": c, "source": "import"} for c in ("M", "E")], "admin", replace=True)
    admin = {"user_id": "admin", "role": "admin", "teams": []}
    td = pl.team_dashboard(repo, admin, None, "2026-10-20", datetime.datetime(2026, 10, 21, 12, 0))
    row = next(r for r in pl.team_summary(repo, td["rows"], td["shifts"])["rows"] if r["team"] == "Team A")
    assert row["shifts"]["M"] == 1 and row["shifts"]["E"] == 1                       # both shifts, not only the first
    assert {(s["team"], s["shift"]) for s in td["staffing"]} >= {("Team A", "M"), ("Team A", "E")}


def test_only_the_teams_own_manager_approves_the_shifts(app, world, repo, admin_b, gm_b, mgr1_b, mgr2_b):
    from calllog.attendance.services import shifts_service as ss
    for b in (admin_b, gm_b, mgr2_b):                                                 # not the administrator, not the GM, not another team's manager
        b.post("/monthly-shifts", {"action": "approve", "month": "2026-10", "team": "Team A"})
    assert repo.shift_approvals() == {}
    page = admin_b.get("/monthly-shifts?month=2026-10&team=Team A").get_data(as_text=True)
    assert "Only the manager of Team A can approve" in page and "Approve 2026-10" not in page
    assert mgr1_b.post("/monthly-shifts", {"action": "approve", "month": "2026-10", "team": "Team A"}).status_code == 302
    assert "2026-10|Team A" in repo.shift_approvals()


def test_a_name_marked_mfa_counts_for_the_mfa_team_only():
    from calllog.attendance.importers.schedule_xlsx import split_names
    from calllog.attendance.services import access, analytics_service as an
    assert split_names("محمد صلاح (MFA)") == [("محمد صلاح", "MFA")]                                          # the importer keeps the mark
    teams = ["Open System", "MFA Team"]
    assert access.shift_teams(teams, "MFA") == ["MFA Team"] and access.shift_teams(teams, "") == teams
    shifts = {"E": {"kind": "work"}}
    row = {"planned_primary": "E", "planned_codes": ["E"], "planned_marks": {"E": "MFA"}, "teams": teams, "status": "not_arrived"}
    out = {(s["team"], s["shift"]) for s in an.staffing([row], {}, shifts)}
    assert out == {("MFA Team", "E")}                                                                         # not in the evening of Open System
    out = {(s["team"], s["shift"]) for s in an.staffing([{**row, "planned_marks": {}}], {}, shifts)}
    assert out == {("MFA Team", "E"), ("Open System", "E")}                                                   # a plain «OPEN + MFA» name is both


def test_an_arabic_schedule_name_matches_an_english_named_employee_by_sound():
    from calllog.attendance.domain import matching as m
    from calllog.attendance.domain.textnorm import norm
    emps = [{"id": 1, "name": "Mohamed Salah Abdelmonem"}, {"id": 2, "name": "Mohamed Moustafa Mansour"}, {"id": 3, "name": "Ahmed Samir Kamal"},
            {"id": 4, "name": "Ahmed Elsayed Elaraby"}, {"id": 5, "name": "Khaled Fekry"}, {"id": 6, "name": "عنان طه محمد محمود"}]
    idx = m.build_index(emps)
    got = {n: m.match_name(norm(n), idx) for n in ("محمد صلاح", "محمد مصطفي", "أحمد سمير", "احمد العربي", "عنان طه", "سيف حمدي")}
    assert [got[n]["employee_id"] for n in ("محمد صلاح", "محمد مصطفي", "أحمد سمير", "احمد العربي", "عنان طه")] == [1, 2, 3, 4, 6]
    assert got["محمد صلاح"]["status"] == "auto" and got["سيف حمدي"]["status"] == "unmatched"          # nobody called Seif: still unmatched
    assert m.match_name(norm("صلاح"), idx)["employee_id"] is None                                      # one word is never matched by sound alone
    assert m.sound("صلاح") == m.sound("Salah") and m.sound("حمدي") == m.sound("Hamdy") and m.sound("خالد") == m.sound("Khaled")


def test_names_skipped_at_import_are_added_later_without_touching_the_plan(app, world, repo, admin_b):
    from calllog.attendance.domain.textnorm import norm
    from calllog.attendance.services import schedule_service as ss
    emp = next(e for e in repo.list_employees() if e["id"] == 101)
    free = "2026-10-20"
    repo.replace_day_entries_atomic(2026, 10, 101, free, [], "test: nothing planned", "admin")                # a person nothing was planned for
    entry = lambda name, code: {"row": 4, "col": 3, "date": free, "shift_code": code, "stream": "", "name_raw": name, "note": "", "name_norm": norm(name),
                                "kind": "work", "invalid": False}
    imp = repo.create_schedule_import({"year": 2026, "month": 10, "filename": "x.xlsx", "file_sha256": "f" * 64, "content_sha256": "c", "team": "ALL",
                                       "uploaded_by": "admin", "status": "preview", "parsed": {"entries": [entry(emp["name"], "E"), entry("Nobody Here", "E")]}})
    repo.update_schedule_import(imp, {"status": "completed"})
    page = admin_b.get(f"/planning/day?date={free}").get_data(as_text=True)
    assert "Add them now" in page and "Nobody Here" in page and "not linked to anybody" in page
    before = {(e["employee_id"], e["work_date"], e["shift_code"]) for e in repo.schedule_entries("2026-10-01", "2026-10-31")}
    assert admin_b.post("/schedule/names", {"action": "add_missing", "month": "2026-10"}).status_code == 302
    after = {(e["employee_id"], e["work_date"], e["shift_code"]) for e in repo.schedule_entries("2026-10-01", "2026-10-31")}
    assert after - before == {(emp["id"], free, "E")} and before <= after                                    # only the missing day; everything else as it was
    assert ss.stale_months(repo)[0]["missing"] == 0


def test_the_day_view_has_one_status_strip_and_no_ring(app, world, repo, admin_b):
    page = admin_b.get("/planning/day?date=2026-10-12").get_data(as_text=True)
    assert 'class="strip-bar"' in page and "came to work" in page and "on leave" in page and "rest day" in page and 'class="donut"' not in page


def _rules(repo, admin_b):
    assert admin_b.post("/admin/scoring", {**sc.PRESETS["standard"]["values"], "effective_from": "2026-10-01", "note": "t"}).status_code == 302


def test_score_summary_says_why_points_were_lost(app, world, repo, admin_b):
    _rules(repo, admin_b)
    page = admin_b.get("/reports?kind=scores&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Score summary" in page and "Why points were lost" in page and "How to read it" in page and "Points lost" in page
    assert "Lost: late" in page and "Ali A1" in page
    assert admin_b.get("/export/report.xlsx?kind=scores&from=2026-10-01&to=2026-10-31").status_code == 200


def test_score_details_list_every_problem(app, world, repo, admin_b):
    _rules(repo, admin_b)
    page = admin_b.get("/reports?kind=score_details&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Score details" in page and "Points lost" in page and "Repeat extra %" in page
    assert admin_b.get("/export/report.xlsx?kind=score_details&from=2026-10-01&to=2026-10-31").status_code == 200


def test_score_reports_without_rules_say_so(app, world, repo, admin_b):
    page = admin_b.get("/reports?kind=scores&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "not set by management" in page and "Rules not set" in page


def test_score_reports_are_listed_and_scoped(app, world, repo, admin_b, mgr1_b):
    assert "Score summary" in admin_b.get("/reports").get_data(as_text=True)
    mine = mgr1_b.get("/reports?kind=scores&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Ali A1" in mine and "Omar B1" not in mine

