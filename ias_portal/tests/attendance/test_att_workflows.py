"""Leave, compensatory days, corrections, absence review, scoring-rule versions, schedule edits - all direct (no approval step)."""


def post(b, url, **data):
    return b.post(url, data)


# ---------------------------------------------------------------- leave
def test_manager_records_leave_directly_without_approval(app, world, repo, admin_b, mgr1_b):
    assert post(admin_b, "/leave/entitlement", emp="101", year="2026", type="annual", entitlement="21", opening="3").status_code == 302
    r = post(mgr1_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-22"}, reason="trip")
    assert r.status_code == 302
    req = repo.leave_requests([101])[0]
    assert req["status"] == "approved" and req["days"] == 3 and req["decided_by"] == "mgr1"          # counts at once, nobody else approves
    from calllog.attendance.services import leave_service as ls
    adm = repo.get_user("admin")
    bal = ls.balances(repo, adm, 2026, [101])[101]
    assert bal["annual"] == {"entitlement": 21, "opening": 3, "taken": 3, "pending": 0, "counts": True, "remaining": 21}
    assert bal["casual"]["remaining"] is None                              # no entitlement entered -> unknown, never guessed
    # leave feeds the schedule analytics: planned shift becomes "on leave" + replacement needed
    k = admin_b.get("/api/overview?date=2026-10-21").get_json()
    row = next(x for x in k["rows"] if x["employee_id"] == 101)
    assert row["status"] == "on_leave" and row["leave_type"] == "annual" and row["needs_replacement"]
    assert k["kpi"]["annual_leave"] == 1
    # audited with who / what / reason / previous / new
    a = repo.list_audit(action="leave_recorded")[0]
    assert a["username"] == "mgr1" and a["detail"]["reason"] == "trip" and a["detail"]["previous"] is None
    assert a["detail"]["new"] == {"type": "annual", "from": "2026-10-20", "to": "2026-10-22", "days": 3.0, "status": "approved"}


def test_there_are_no_approve_or_reject_routes_any_more(app, world, admin_b, mgr1_b):
    rid = post(mgr1_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-20"}) and 1
    for b in (admin_b, mgr1_b):
        assert post(b, "/leave/1/approve").status_code in (404, 405)
        assert post(b, "/leave/1/reject").status_code in (404, 405)
        assert post(b, "/schedule/week", team="Team A", week_start="2026-10-11", action="approve").status_code in (404, 405)
        assert b.get("/planning/approvals").status_code == 404
    assert rid


def test_cancelling_leave_is_direct_and_audited(app, world, repo, admin_b, mgr1_b, mgr2_b):
    post(mgr1_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-22"})
    rid = repo.leave_requests([101])[0]["id"]
    assert post(mgr2_b, f"/leave/{rid}/cancel", reason="x").status_code == 403           # another manager's team
    assert post(mgr1_b, f"/leave/{rid}/cancel", reason="trip postponed").status_code == 302
    assert repo.leave_requests([101])[0]["status"] == "cancelled"
    a = repo.list_audit(action="leave_cancelled")[0]
    assert a["detail"]["previous"]["status"] == "approved" and a["detail"]["new"] == {"status": "cancelled"} and a["detail"]["reason"] == "trip postponed"
    assert post(admin_b, f"/leave/{rid}/cancel").status_code == 400                       # already cancelled


def test_overlapping_leave_is_refused_and_no_double_deduction(app, world, repo, admin_b):
    post(admin_b, "/leave/new", emp="101", type="annual", **{"from": "2026-10-20", "to": "2026-10-22"})
    r = post(admin_b, "/leave/new", emp="101", type="sick", **{"from": "2026-10-22", "to": "2026-10-23"})
    assert r.status_code == 400 and b"Overlaps" in r.data
    assert len(repo.leave_requests([101])) == 1
    from calllog.attendance.services import leave_service as ls
    t = ls.leave_totals(repo, repo.get_user("admin"), 101, "2026-10-01", "2026-10-31")
    assert t["total"] == 3 and t["status"]["approved"] == 1


def test_hr_file_leave_text_counts_once_when_a_record_exists(app, world, repo, admin_b):
    a = repo.create_attendance_import({"filename": "l", "file_sha256": "l", "parsed": {}})
    repo.commit_attendance_rows_atomic(a, [{"employee_id": 102, "work_date": "2026-10-14", "leave_text": "أجازة اعتيادية"}])
    k = admin_b.get("/api/overview?date=2026-10-14").get_json()
    assert next(x for x in k["rows"] if x["employee_id"] == 102)["leave_type"] == "annual"
    post(admin_b, "/leave/new", emp="102", type="annual", **{"from": "2026-10-14", "to": "2026-10-14"})
    from calllog.attendance.services import analytics_service as an
    import datetime as dt
    ev = an.evaluate_range(repo, [102], "2026-10-14", "2026-10-14", dt.datetime(2026, 10, 20))
    assert ev[102][0]["status"] == "on_leave"
    emp = repo.get_user("admin")
    from calllog.attendance.services import leave_service as ls
    assert ls.balances(repo, emp, 2026, [102])[102]["annual"]["taken"] == 1      # not 2


# ---------------------------------------------------------------- compensatory days
def test_comp_days_flow(app, world, repo, mgr1_b):
    assert post(mgr1_b, "/comp/new", emp="101", earned="2026-10-09", days="1", reason="worked Friday", planned="2026-10-20").status_code == 302
    assert post(mgr1_b, "/comp/new", emp="101", earned="2026-10-10", days="1", reason="worked Saturday").status_code == 302
    assert post(mgr1_b, "/comp/new", emp="101", earned="2026-10-10", days="1", reason="").status_code == 400
    from calllog.attendance.services import leave_service as ls
    u = repo.get_user("mgr1")
    b = ls.comp_balances(repo, u, [101])[101]
    assert (b["earned"], b["taken"], b["remaining"], b["planned_open"]) == (2, 0, 2, 1)
    cid = repo.comp_days([101])[-1]["id"]
    assert post(mgr1_b, f"/comp/{cid}/update", planned="", taken="2026-10-20").status_code == 302
    b = ls.comp_balances(repo, u, [101])[101]
    assert (b["taken"], b["remaining"]) == (1, 1)
    assert any(a["action"] == "comp_day_earned" for a in repo.list_audit())


# ---------------------------------------------------------------- corrections, reports, reviews, scoring
def test_correction_requires_reason_keeps_old_value_and_changes_analytics(app, world, repo, admin_b):
    # employee 102 has an empty record on the 12th (evening shift)
    assert post(admin_b, "/attendance/correct", emp="102", date="2026-10-12", field="in_time", value="14:05", reason="").status_code == 400
    assert post(admin_b, "/attendance/correct", emp="102", date="2026-10-12", field="in_time", value="25:99", reason="x").status_code == 400
    assert post(admin_b, "/attendance/correct", emp="102", date="2026-10-12", field="in_time", value="14:05", reason="badge failed").status_code == 302
    assert post(admin_b, "/attendance/correct", emp="102", date="2026-10-12", field="out_time", value="22:00", reason="badge failed").status_code == 302
    c = repo.corrections("2026-10-12", "2026-10-12", [102])
    assert [(x["field"], x["old_value"], x["new_value"], x["reason"], x["user_id"]) for x in c] == [
        ("in_time", None, "14:05:00", "badge failed", "admin"), ("out_time", None, "22:00:00", "badge failed", "admin")]
    stored = repo.attendance_records("2026-10-12", "2026-10-12", [102])[0]
    assert stored["in_time"] is None                                         # the imported record itself is untouched
    row = next(x for x in admin_b.get("/api/overview?date=2026-10-12").get_json()["rows"] if x["employee_id"] == 102)
    assert row["status"] == "late" and row["late_minutes"] == 5
    assert any(a["action"] == "attendance_corrected" for a in repo.list_audit())


def test_missing_vs_reported_forgotten_punches_are_separate(app, world, repo, admin_b):
    post(admin_b, "/attendance/punch-report", emp="101", date="2026-10-12", kind="out", reason="phone died")
    rep = repo.punch_reports()[0]
    assert rep["status"] == "pending"
    post(admin_b, f"/attendance/punch-report/{rep['id']}/verified", emp="101")
    assert repo.punch_reports()[0]["status"] == "verified"
    html = admin_b.get("/reports?kind=punches&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Reported forgotten" in html


def test_absence_review_drives_the_score(app, world, repo, admin_b):
    post(admin_b, "/admin/scoring", start="100", late_tiers="1-15=1; 16-=3", absence_penalty="10", min_judged_days="1",
         categories="Excellent=90; Good=75; Critical=0", effective_from="2026-01-01", note="test rules")
    base = admin_b.get("/api/employee/102/summary?from=2026-10-12&to=2026-10-12").get_json()
    assert base["score"]["score"] == 100 and base["summary"]["absent_days"] == 1       # unverified absence: no penalty
    assert post(admin_b, "/attendance/absence-review", emp="102", date="2026-10-12", decision="unexcused", reason="").status_code == 400
    assert post(admin_b, "/attendance/absence-review", emp="102", date="2026-10-12", decision="unexcused", reason="no excuse given").status_code == 302
    s = admin_b.get("/api/employee/102/summary?from=2026-10-12&to=2026-10-12").get_json()
    assert s["score"]["score"] == 90 and s["score"]["category"] == "Excellent"
    post(admin_b, "/attendance/absence-review", emp="102", date="2026-10-12", decision="excused", reason="medical certificate")
    s = admin_b.get("/api/employee/102/summary?from=2026-10-12&to=2026-10-12").get_json()
    assert s["score"]["score"] == 100 and s["summary"]["excused_absence_days"] == 1
    page = admin_b.get("/employee/102/score?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "The rules behind this score" in page


def test_scoring_rule_versions_preserve_history(app, world, repo, admin_b):
    rules = dict(start="100", late_tiers="1-=5", absence_penalty="0", categories="Good=50; Low=0", min_judged_days="1")
    assert post(admin_b, "/admin/scoring", **rules, effective_from="2026-01-01", note="v1").status_code == 302
    assert post(admin_b, "/admin/scoring", **{**rules, "late_tiers": "1-=20"}, effective_from="2026-10-15", note="stricter").status_code == 302
    vs = repo.scoring_rule_versions()
    assert [v["version"] for v in vs] == [1, 2] and vs[0]["rules"]["late"]["tiers"][0]["penalty"] == 5
    old = admin_b.get("/api/employee/101/summary?from=2026-10-12&to=2026-10-12").get_json()       # period before 15 Oct -> version 1
    assert old["rules_version"] == 1 and old["score"]["score"] == 95
    # a malformed rule is rejected, not stored
    assert post(admin_b, "/admin/scoring", **{**rules, "late_tiers": "abc"}, effective_from="2026-10-15").status_code == 400
    assert len(repo.scoring_rule_versions()) == 2
    assert repo.list_audit(action="scoring_rules_changed")


def test_only_admin_can_change_scoring_rules(app, world, repo, mgr1_b, gm_b):
    for b in (mgr1_b, gm_b):
        assert post(b, "/admin/scoring", start="100", effective_from="2026-01-01").status_code == 302
    assert repo.scoring_rule_versions() == []


# ---------------------------------------------------------------- schedule edit + weekly approval
def test_edit_warns_about_conflicts_but_saves(app, world, repo, mgr1_b):
    # 101: set the 5th to Night, the 6th stays Morning -> only 8h rest is fine; set the 6th to Evening -> only 7h? (N ends 07:00, E 14:00 = 7h)
    post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-05", codes="N", reason="cover")
    r = post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-06", codes="E", reason="cover")
    assert r.status_code == 302
    assert [e["shift_code"] for e in repo.schedule_entries("2026-10-06", "2026-10-06", [101])] == ["E"]
    assert "Only" in repo.list_audit(action="schedule_changed")[0]["detail"]["warnings"][0]
    assert post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-07", codes=["OFF", "M"], reason="x").status_code == 400
    assert post(mgr1_b, "/schedule/edit", emp="101", date="2026-10-07", reason="x").status_code == 400








# ---------------------------------------------------------------- settings
def test_settings_change_is_audited_and_changes_lateness(app, world, repo, admin_b):
    assert post(admin_b, "/admin/settings", action="rules", grace_minutes="15", early_grace_minutes="0", late_basis="from_start",
                punch_mismatch_hours="4", repeat_late_threshold="3", min_rest_hours="8", late_permit_minutes="15", early_permit_minutes="15",
                default_shift_code="D", default_shift_enabled="1", min_staffing_pct="50").status_code == 302
    assert repo.get_settings()["grace_minutes"] == 15 and repo.get_settings()["min_staffing_pct"] == 50.0
    row = next(x for x in admin_b.get("/api/overview?date=2026-10-12").get_json()["rows"] if x["employee_id"] == 101)
    assert row["late_minutes"] == 0 and row["status"] == "present"                      # 7:10 is inside the 15-minute grace
    ev = repo.list_audit(action="settings_changed")[0]["detail"]
    assert ev["previous"]["grace_minutes"] == 0 and ev["new"]["grace_minutes"] == 15
