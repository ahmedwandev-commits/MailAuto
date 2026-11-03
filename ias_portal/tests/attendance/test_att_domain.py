"""Pure business-logic tests: no Flask, no database."""
from datetime import datetime

import pytest

from calllog.attendance.domain import daily, matching, schedule_rules, scoring
from calllog.attendance.domain.shifts import interval, scheduled_hours
from calllog.attendance.domain.textnorm import norm, weekday_from_name

SHIFTS = {
    "M": {"code": "M", "kind": "work", "start_time": "07:00", "end_time": "15:00"},
    "E": {"code": "E", "kind": "work", "start_time": "14:00", "end_time": "22:00"},
    "N": {"code": "N", "kind": "work", "start_time": "22:00", "end_time": "07:00"},
    "SB": {"code": "SB", "kind": "standby"},
    "OFF": {"code": "OFF", "kind": "off"},
}
ST = {"grace_minutes": 0, "early_grace_minutes": 0, "late_basis": "from_start", "punch_mismatch_hours": 4}
NOW = datetime(2026, 10, 20, 12, 0)


def day(planned, att=None, att_next=None, loaded=True, loaded_next=True, leave=None, holiday=False, review=None,
        date="2026-10-14", st=None, now=NOW, hr_rest=False):
    return daily.evaluate_day({
        "employee_id": 1, "date": date, "planned": [{"shift_code": c} for c in planned], "shifts": SHIFTS, "att": att,
        "att_next": att_next, "data_loaded": loaded, "data_loaded_next": loaded_next, "leave": leave, "holiday": holiday,
        "review": review, "settings": {**ST, **(st or {})}, "now": now, "hr_rest": hr_rest})


def rec(i=None, o=None, **kw):
    return {"in_time": i, "out_time": o, "mission_text": "", "permit_text": "", **kw}


# ---------------------------------------------------------------- text + shifts
def test_normalisation_unifies_arabic_variants():
    assert norm("أحمد  سمير") == norm("احمد سمير")
    assert norm("منة عماد") == norm("منه عماد") and norm("كريم حمدى") == norm("كريم حمدي")
    assert norm("مينا ماهر ( MFA )") == "مينا ماهر"
    assert weekday_from_name("الإثنين") == 0 and weekday_from_name("الأحد") == 6


def test_shift_intervals_cross_midnight():
    s, e = interval("2026-10-01", "22:00", "07:00")
    assert (e - s).total_seconds() == 9 * 3600 and e.day == 2
    assert scheduled_hours("07:00", "15:00") == 8


# ---------------------------------------------------------------- matching
EMPS = [{"id": 1, "name": "محمد ناصر مصلحى اسماعيل"}, {"id": 2, "name": "محمد صلاح عبدالمنعم"},
        {"id": 3, "name": "عمر احمد هشام"}, {"id": 4, "name": "عبدالله احمد نورالدين احمد عمر"},
        {"id": 5, "name": "محمد زكريا عبدالحفيظ"}, {"id": 6, "name": "ايمن احمد سعيد"}, {"id": 7, "name": "ايمن على عمر"}]


@pytest.mark.parametrize("label,status,emp", [
    ("محمد ناصر", "auto", 1), ("عمر احمد", "auto", 3), ("ايمن على", "auto", 7),
    ("محمد ذكريا", "suggested", None), ("ناصر", "auto", 1), ("زياد خالد", "unmatched", None)])
def test_match_name(label, status, emp):
    idx = matching.build_index(EMPS)
    m = matching.match_name(norm(label), idx)
    assert m["status"] == status and m["employee_id"] == emp
    if status == "suggested":
        assert m["candidates"][0]["id"] == 5


def test_ambiguous_and_alias():
    idx = matching.build_index(EMPS)
    assert matching.match_name(norm("محمد"), idx)["status"] == "ambiguous"
    assert matching.match_name(norm("محمد"), idx, {norm("محمد"): 2})["status"] == "alias"


# ---------------------------------------------------------------- daily evaluation
def test_late_is_measured_against_the_assigned_shift_not_a_fixed_time():
    assert day(["M"], rec("07:20:00", "15:00:00"))["late_minutes"] == 20
    assert day(["E"], rec("14:20:00", "22:00:00"))["late_minutes"] == 20
    assert day(["E"], rec("13:55:00", "22:00:00"))["late_minutes"] == 0
    assert day(["M"], rec("07:20:00", "15:00:00"))["status"] == "late"


def test_grace_and_basis():
    assert day(["M"], rec("07:10:00", "15:00:00"), st={"grace_minutes": 15})["late_minutes"] == 0
    assert day(["M"], rec("07:20:00", "15:00:00"), st={"grace_minutes": 15})["late_minutes"] == 20
    assert day(["M"], rec("07:20:00", "15:00:00"), st={"grace_minutes": 15, "late_basis": "after_grace"})["late_minutes"] == 5


def test_permit_excuses_late_and_early_when_configured():
    st = {"permit_excuses_late": ["اذن خاص حضور"], "permit_excuses_early": ["اذن خاص انصراف"]}
    d = day(["M"], rec("08:00:00", "15:00:00", permit_text="اذن خاص حضور"), st=st)
    assert d["late_minutes"] == 0 and d["excused_late_minutes"] == 60 and "late_excused_by_permit" in d["flags"] and d["status"] == "present"
    assert day(["M"], rec("08:00:00", "15:00:00"), st=st)["late_minutes"] == 60                # no permit: still late
    assert day(["M"], rec("08:00:00", "15:00:00", permit_text="اذن خاص حضور"))["late_minutes"] == 60   # not configured: not excused
    d = day(["M"], rec("07:00:00", "13:00:00", permit_text="اذن خاص انصراف"), st=st)
    assert d["early_minutes"] == 0 and "early_excused_by_permit" in d["flags"]


def test_early_departure_and_hours():
    d = day(["M"], rec("07:00:00", "14:30:00"))
    assert d["early_minutes"] == 30 and d["actual_hours"] == 7.5 and d["scheduled_hours"] == 8


def test_night_shift_out_on_next_day_row():
    d = day(["N"], rec("22:05:00", None), att_next=rec(None, "07:02:00"))
    assert d["status"] == "late" and d["actual_out"] == "07:02:00" and d["early_minutes"] == 0
    assert d["actual_hours"] == pytest.approx(8.95, abs=0.01)


def test_night_shift_out_pending_when_next_day_not_loaded():
    d = day(["N"], rec("22:00:00", None), loaded_next=False, att_next=None)
    assert d["status"] == "present" and "out_pending_next_day_data" in d["flags"]


def test_missing_punches():
    assert "missing_out" in day(["M"], rec("07:00:00", None))["flags"]
    assert day(["M"], rec("07:00:00", None))["status"] == "missing_punch"
    d = day(["M"], rec(None, "15:00:00"))
    assert d["status"] == "missing_punch" and "missing_in" in d["flags"]


def test_absent_only_with_data_and_no_other_explanation():
    assert day(["M"], None, loaded=False)["status"] == "no_data"                      # file not uploaded: NOT absent
    assert day(["M"], rec(), loaded=True)["status"] == "absent"
    assert day(["M"], rec(), leave={"type": "annual", "source": "request"})["status"] == "on_leave"
    assert day(["M"], rec(), holiday=True)["status"] == "holiday"
    assert day(["M"], rec(mission_text="مهمة"))["status"] == "on_mission"
    assert day(["M"], rec(), review="excused")["status"] == "excused_absence"
    assert "absence_verified" in day(["M"], rec(), review="unexcused")["flags"]


def test_future_and_today():
    assert day(["M"], None, date="2026-10-25")["status"] == "scheduled"
    live = datetime(2026, 10, 14, 10, 0)
    assert day(["M"], rec("07:05:00", None), now=live)["status"] == "currently_working"
    assert day(["E"], None, now=live)["status"] == "scheduled"                         # evening shift has not started


def test_rest_standby_and_unscheduled():
    assert day(["OFF"], None)["status"] == "rest_day"
    d = day(["OFF"], rec("08:00:00", "16:00:00"))
    assert d["status"] == "rest_day_worked" and "worked_on_rest_day" in d["flags"]
    assert day(["SB"], None)["status"] == "standby"
    assert day([], rec("08:00:00", "16:00:00"))["status"] == "present_unscheduled"
    assert day([], None)["status"] == "no_schedule"
    assert day([], rec(), hr_rest=True)["status"] == "rest_day"


def test_replacement_flag():
    assert day(["M"], rec(), leave={"type": "annual", "source": "hr_file"})["needs_replacement"]
    assert day(["M"], rec())["needs_replacement"]
    assert not day(["M"], rec("07:00:00", "15:00:00"))["needs_replacement"]


def test_summary_counts():
    days = [day(["M"], rec("07:30:00", "15:00:00"), date="2026-10-11"), day(["M"], rec("07:00:00", "15:00:00"), date="2026-10-12"),
            day(["M"], rec(), date="2026-10-13"), day(["OFF"], None, date="2026-10-14"),
            day(["M"], rec(), leave={"type": "sick", "source": "request"}, date="2026-10-15")]
    s = daily.summarize(days, repeat_threshold=1)
    assert (s["scheduled_days"], s["present_days"], s["absent_days"], s["leave_days"], s["rest_days"]) == (4, 2, 1, 1, 1)
    assert s["late_count"] == 1 and s["late_minutes"] == 30 and s["repeated_lateness"]
    assert s["judged_days"] == 3 and s["attendance_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["missing_both"] == 1 and s["scheduled_hours"] == 24 and s["actual_hours"] == 15.5


# ---------------------------------------------------------------- scheduling rules
def test_conflicts():
    e = lambda d, c: {"employee_id": 1, "work_date": d, "shift_code": c}  # noqa: E731
    c = schedule_rules.detect_conflicts([e("2026-10-22", "M"), e("2026-10-22", "E")], SHIFTS, 8)     # 14-15 overlap
    assert [x["type"] for x in c] == ["overlap"]
    c = schedule_rules.detect_conflicts([e("2026-10-01", "N"), e("2026-10-02", "E")], SHIFTS, 8)     # 7h rest
    assert [x["type"] for x in c] == ["short_rest"]
    c = schedule_rules.detect_conflicts([e("2026-10-01", "N"), e("2026-10-02", "E")], SHIFTS, 6)
    assert c == []
    c = schedule_rules.detect_conflicts([e("2026-10-01", "M"), e("2026-10-01", "M")], SHIFTS, 8)
    assert [x["type"] for x in c] == ["duplicate"]


def test_coverage_and_inferred_rest():
    entries = [{"employee_id": i, "work_date": "2026-10-01", "shift_code": "M"} for i in (1, 2)]
    cov = schedule_rules.coverage(entries, ["2026-10-01"], SHIFTS, {"M": 3, "E": 1})
    assert cov["2026-10-01"]["M"] == {"count": 2, "min": 3, "short": True}
    assert cov["2026-10-01"]["E"]["short"] is True and cov["2026-10-01"]["N"]["short"] is False
    off = schedule_rules.infer_rest_days({1, 2, 3}, ["2026-10-01", "2026-10-02"], entries)
    assert {(o["employee_id"], o["work_date"]) for o in off} == {(1, "2026-10-02"), (2, "2026-10-02"), (3, "2026-10-01"), (3, "2026-10-02")}


# ---------------------------------------------------------------- scoring
RULES = {**scoring.DEFAULT_RULES,
         "late": {"tiers": [{"from": 1, "to": 15, "penalty": 1}, {"from": 16, "to": None, "penalty": 3}], "max_deduction": 5},
         "early": {"tiers": [{"from": 1, "to": None, "penalty": 2}], "max_deduction": None},
         "absence": {"penalty_per_day": 10, "max_deduction": None},
         "categories": [{"name": "Excellent", "min": 90}, {"name": "Good", "min": 75}, {"name": "Critical", "min": 0}],
         "min_judged_days": 3}


def _score(days, rules=RULES, complete=True):
    return scoring.compute_score(days, rules, daily.summarize(days), complete)


def test_default_rules_penalise_nothing_and_say_so():
    days = [day(["M"], rec("08:30:00", "15:00:00")), day(["M"], rec())]
    r = _score(days, scoring.DEFAULT_RULES)
    assert r["score"] == 100 and r["configured"] is False and r["category"] is None
    assert any("not configured" in n for n in r["notes"])


def test_score_is_explainable_and_capped():
    days = [day(["M"], rec("07:10:00", "15:00:00"), date="2026-10-11"),      # 10 min -> 1
            day(["M"], rec("07:30:00", "15:00:00"), date="2026-10-12"),      # 30 min -> 3
            day(["M"], rec("07:40:00", "15:00:00"), date="2026-10-13"),      # 40 min -> 3 (cap 5 reached)
            day(["M"], rec("07:00:00", "14:00:00"), date="2026-10-14")]      # early 60 -> 2
    r = _score(days)
    late = next(d for d in r["deductions"] if d["kind"] == "late")
    assert [i["points"] for i in late["items"]] == [1, 3, 3] and late["total"] == 5 and r["caps_applied"]
    assert r["score"] == 100 - 5 - 2 and r["category"] == "Excellent"
    assert all(i["date"] and i["rule"] for d in r["deductions"] for i in d["items"])


def test_only_verified_absences_are_penalised_and_missing_punch_never():
    days = [day(["M"], rec(), date="2026-10-11"),                                   # unverified
            day(["M"], rec(), date="2026-10-12", review="unexcused"),               # verified
            day(["M"], rec(), date="2026-10-13", review="excused"),                 # excused
            day(["M"], rec("07:00:00", None), date="2026-10-14"),                   # missing OUT
            day(["M"], rec(), date="2026-10-15", leave={"type": "annual", "source": "request"})]
    r = _score(days)
    assert r["score"] == 90 and r["category"] == "Excellent"
    assert any("missing punch" in n for n in r["notes"]) and any("not penalised until reviewed" in n for n in r["notes"])


def test_insufficient_and_incomplete_flags():
    r = _score([day(["M"], rec("07:00:00", "15:00:00"))], complete=False)
    assert r["insufficient_data"] and r["incomplete_period"]
