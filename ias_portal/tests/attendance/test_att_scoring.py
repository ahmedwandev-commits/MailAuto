"""The configurable attendance score: every penalty, repeat rule and cap is data, not code."""
import copy

import pytest

from calllog.attendance.domain import scoring
from calllog.attendance.services import scoring_service as sc_svc
from calllog.attendance.services.schedule_service import ImportError_

SUMMARY = {"judged_days": 20, "missing_in": 0, "missing_out": 0, "missing_both": 0}


def day(d, late=0, early=0, status="present", flags=(), late_permit=None, early_permit=None):
    return {"date": f"2026-10-{d:02d}" if isinstance(d, int) else d, "late_minutes": late, "early_minutes": early, "status": status,
            "flags": list(flags), "late_permit": late_permit, "early_permit": early_permit}


def rules(**kw):
    r = scoring.default_rules()
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(r.get(k), dict):
            r[k] = {**r[k], **v}
        else:
            r[k] = v
    r.setdefault("categories", [])
    return r


CATS = [{"name": "Excellent", "min": 90}, {"name": "Good", "min": 75}, {"name": "Low", "min": 0}]


def score(days, r):
    return scoring.compute_score(days, r, SUMMARY)


def ded(res, kind):
    return next(d for d in res["deductions"] if d["kind"] == kind)


# ---------------------------------------------------------------- nothing is built in
def test_unconfigured_rules_apply_no_penalty_and_say_so():
    res = score([day(i, late=40) for i in range(1, 8)], scoring.default_rules())
    assert res["score"] == 100 and not res["configured"] and res["category"] is None and res["total_deduction"] == 0
    assert any("not configured" in n for n in res["notes"])
    assert not scoring.is_configured(rules(late={"flat_pct": 1}))                      # penalties but no categories: still not configured
    assert scoring.is_configured(rules(late={"flat_pct": 1}, categories=CATS))


def test_rule_sets_saved_by_earlier_versions_still_work():
    old = {"start": 100, "late": {"tiers": [{"from": 1, "to": 15, "penalty": 1}, {"from": 16, "to": None, "penalty": 3}], "max_deduction": 20},
           "early": {"tiers": [], "max_deduction": None}, "absence": {"penalty_per_day": 10, "max_deduction": None},
           "categories": CATS, "min_judged_days": 1}
    res = score([day(1, late=10), day(2, late=20)], old)
    assert res["score"] == 96 and res["category"] == "Excellent" and ded(res, "late")["count"] == 2


# ---------------------------------------------------------------- base penalties
def test_flat_percentage_per_late_day_and_ignore_threshold():
    r = rules(late={"flat_pct": 2, "ignore_under_minutes": 5}, categories=CATS)
    res = score([day(1, late=3), day(2, late=6), day(3, late=30), day(4)], r)
    assert res["score"] == 96 and ded(res, "late")["count"] == 2 and res["category"] == "Excellent"        # the 3-minute lateness is ignored


def test_minute_bands_with_flat_fallback_and_percent_of_the_start_score():
    r = rules(late={"tiers": [{"from": 1, "to": 15, "penalty": 1}, {"from": 16, "to": 30, "penalty": 3}], "flat_pct": 5}, categories=CATS)
    res = score([day(1, late=10), day(2, late=20), day(3, late=45)], r)
    assert [i["points"] for i in ded(res, "late")["items"]] == [1, 3, 5] and res["score"] == 91
    r["start"] = 200                                                                  # penalties are percentages of the start score
    assert score([day(1, late=20)], r)["score"] == 200 - 6


def test_absence_missing_punch_and_rejected_permit_are_separate_configurable_rules():
    r = rules(absence={"penalty_per_day": 10}, missing_punch={"penalty_per_day": 2}, rejected_permit={"penalty_per_day": 3}, categories=CATS)
    days = [day(1, status="absent", flags=["absence_verified"]), day(2, status="absent"),          # the second is only a candidate: no penalty
            day(3, status="missing_punch"), day(4, late=30, late_permit="rejected"), day(5, late=30, late_permit="required"),
            day(6, early=30, early_permit="rejected")]
    res = score(days, r)
    assert (ded(res, "absence")["total"], ded(res, "missing_punch")["total"], ded(res, "rejected_permit")["total"]) == (10, 2, 6)
    assert res["score"] == 82 and any("absence candidate" in n for n in res["notes"]) and any("never lowers the score" in n for n in res["notes"])
    # a missing (required / unreviewed) permit never costs anything by itself, even when everything else is switched on
    only_required = score([day(1, late=30, late_permit="required"), day(2, early=30, early_permit="required")], r)
    assert only_required["score"] == 100


# ---------------------------------------------------------------- repeated lateness
def steps(text):
    return sc_svc.parse_steps(text, "t")


def late_rule(**rep):
    base = {"window": "period", "rolling_days": 30, "steps": [], "consecutive_days": None, "consecutive_extra": None}
    return rules(late={"flat_pct": 1, "repeat": {**base, **rep}}, categories=CATS)


def test_repeat_steps_by_occurrence_number_add_extra_percent():
    r = late_rule(steps=steps("3-5=+1; 6-=+2"))
    res = score([day(i, late=20) for i in range(1, 9)], r)
    assert [i["points"] for i in ded(res, "late")["items"]] == [1, 1, 2, 2, 2, 3, 3, 3]
    d = ded(res, "late")
    assert d["base_total"] == 8 and d["repeat_total"] == 9 and res["score"] == 83
    assert "occurrence #3" in d["items"][2]["detail"]


def test_repeat_multiplier_and_both_together():
    r = late_rule(steps=steps("2-=x2"))
    assert [i["points"] for i in ded(score([day(i, late=20) for i in range(1, 4)], r), "late")["items"]] == [1, 2, 2]
    r = late_rule(steps=steps("2-=x2+0.5"))
    assert [i["points"] for i in ded(score([day(i, late=20) for i in range(1, 4)], r), "late")["items"]] == [1, 2.5, 2.5]


def test_repeat_window_period_month_or_rolling():
    days = [day("2026-10-28", late=20), day("2026-10-29", late=20), day("2026-11-02", late=20), day("2026-11-03", late=20)]
    pts = lambda r: [i["points"] for i in ded(score(days, r), "late")["items"]]               # noqa: E731
    assert pts(late_rule(steps=steps("3-=+5"))) == [1, 1, 6, 6]                                  # whole period: the 3rd and 4th occurrence
    assert pts(late_rule(window="month", steps=steps("3-=+5"))) == [1, 1, 1, 1]                  # two in October, two in November: never a third
    assert pts(late_rule(window="rolling", rolling_days=3, steps=steps("2-=+5"))) == [1, 6, 1, 6]     # 28th+29th and 2nd+3rd are within 3 days


def test_consecutive_scheduled_days_get_an_extra_and_days_off_do_not_break_the_run():
    r = late_rule(consecutive_days=3, consecutive_extra=1.5)
    days = [day(1, late=20), day(2, late=20), day(3, status="rest_day"), day(4, late=20), day(5), day(6, late=20)]
    res = score(days, r)
    assert [i["points"] for i in ded(res, "late")["items"]] == [2.5, 2.5, 2.5, 1]                 # 1st, 2nd, 4th form a run of 3 (the rest day does not break it)
    assert score([day(1, late=20), day(2), day(3, late=20), day(4, late=20)], r)["score"] == 100 - 3


def test_caps_per_component_and_overall_and_the_score_never_goes_below_zero():
    r = rules(late={"flat_pct": 10, "max_deduction": 25}, absence={"penalty_per_day": 30}, max_total_deduction=60, categories=CATS)
    days = [day(i, late=20) for i in range(1, 8)] + [day(10 + i, status="absent", flags=["absence_verified"]) for i in range(3)]
    res = score(days, r)
    assert ded(res, "late")["total"] == 25 and res["total_deduction"] == 60 and res["score"] == 40 and len(res["caps_applied"]) == 2
    r2 = rules(absence={"penalty_per_day": 60}, categories=CATS)
    assert score([day(i, status="absent", flags=["absence_verified"]) for i in range(1, 4)], r2)["score"] == 0


def test_category_tones_and_insufficient_data():
    r = rules(late={"flat_pct": 12}, categories=CATS)
    res = [score([day(i, late=20) for i in range(n)], r) for n in (0, 1, 2, 5)]
    assert [(x["score"], x["category"], x["category_tone"]) for x in res] == [(100, "Excellent", "t-ok"), (88, "Good", "t-warn"), (76, "Good", "t-warn"), (40, "Low", "t-danger")]
    few = scoring.compute_score([day(1)], rules(min_judged_days=5, categories=CATS, late={"flat_pct": 1}), {**SUMMARY, "judged_days": 1})
    assert few["category"] is None and few["insufficient_data"]


def test_rules_are_described_in_plain_words():
    r = rules(late={"tiers": [{"from": 1, "to": 15, "penalty": 1}], "flat_pct": 2, "max_deduction": 20,
                    "repeat": {"window": "month", "rolling_days": 30, "steps": steps("3-5=+1; 6-=x2"), "consecutive_days": 3, "consecutive_extra": 1}},
              max_total_deduction=50)
    text = {x["kind"]: x for x in scoring.describe_rules(r)}
    assert "1–15 min = 1 %" in text["late"]["text"] and "2 % per occurrence" in text["late"]["text"]
    assert "same calendar month" in text["late"]["repeat"] and "#3–5: +1 %" in text["late"]["repeat"] and "#6–∞: ×2" in text["late"]["repeat"] and "3+" in text["late"]["repeat"]
    assert text["late"]["cap"] == "capped at 20 %" and text["absence"]["text"] == "no penalty" and text["total"]["text"].endswith("50 %")


# ---------------------------------------------------------------- the configuration form
def test_step_syntax_is_validated():
    assert steps("1-2=+0; 3-5=+1.5; 6-=x2; 2=x1.5+1") == [
        {"from": 1, "to": 2, "extra": 0.0, "factor": None}, {"from": 3, "to": 5, "extra": 1.5, "factor": None},
        {"from": 6, "to": None, "extra": None, "factor": 2.0}, {"from": 2, "to": 2, "extra": 1.0, "factor": 1.5}]
    for bad in ("abc", "0-2=+1", "5-3=+1", "3-=-1", "3-=xyz", "3+1"):
        with pytest.raises(ImportError_):
            steps(bad)


FORM = dict(start="100", min_judged_days="1", categories="Excellent=90; Good=75; Low=0", effective_from="2026-01-01", note="v1",
            late_tiers="1-15=1; 16-=3", late_flat="", late_ignore="2", late_cap="30", late_window="month", late_rolling_days="30",
            late_steps="3-5=+1; 6-=x2", late_consec_days="3", late_consec_extra="1",
            early_flat="1", absence_penalty="10", missing_punch_penalty="2", rejected_permit_penalty="3", max_total="60")


def test_admin_configures_every_rule_from_the_form_and_it_round_trips(app, world, repo, admin_b):
    assert admin_b.post("/admin/scoring", FORM).status_code == 302
    r = repo.scoring_rule_versions()[0]["rules"]
    assert r["late"]["repeat"]["window"] == "month" and r["late"]["repeat"]["steps"][1] == {"from": 6, "to": None, "extra": None, "factor": 2.0}
    assert (r["late"]["ignore_under_minutes"], r["late"]["max_deduction"], r["early"]["flat_pct"], r["max_total_deduction"]) == (2, 30, 1, 60)
    assert r["missing_punch"]["penalty_per_day"] == 2 and r["rejected_permit"]["penalty_per_day"] == 3 and r["absence"]["penalty_per_day"] == 10
    page = admin_b.get("/admin/scoring").get_data(as_text=True)
    for text in ('value="3-5=+1; 6-=x2"', 'value="1-15=1; 16-=3"', "The rules in force", "same calendar month", "Rejected permits", 'value="60"'):
        assert text in page, text
    audit = repo.list_audit(action="scoring_rules_changed")[0]
    assert audit["username"] == "admin" and audit["detail"]["rules"]["late"]["repeat"]["consecutive_days"] == 3


def test_invalid_rule_values_are_refused_and_nothing_is_saved(app, world, repo, admin_b):
    for bad in ({"late_flat": "150"}, {"late_flat": "abc"}, {"late_steps": "x"}, {"absence_penalty": "-2"}, {"late_window": "weekly"}, {"max_total": "-1"},
                {"late_ignore": "two"}, {"start": "0"}):
        assert admin_b.post("/admin/scoring", {**FORM, **bad}).status_code == 400, bad
    assert repo.scoring_rule_versions() == []


def test_only_the_administrator_can_change_scoring(app, world, repo, mgr1_b, gm_b):
    for b in (mgr1_b, gm_b):
        assert b.post("/admin/scoring", FORM).status_code == 302 and b.get("/admin/scoring").status_code == 302
    assert repo.scoring_rule_versions() == []


# ---------------------------------------------------------------- the score as people see it
def test_scorecard_and_profile_show_the_score_and_where_the_points_went(app, world, repo, admin_b, mgr1_b, mgr2_b):
    # employee 101 is Morning, 07:10 on the 12th = 10 minutes late
    admin_b.post("/admin/scoring", {**FORM, "late_tiers": "1-15=2; 16-=4", "late_steps": "", "late_consec_days": "", "min_judged_days": "1", "late_ignore": ""})
    page = admin_b.get("/employee/101/score?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Attendance scorecard" in page and 'class="sb-val">98<' in page and "100 − 2 = <b>98</b>" in page
    assert "Excellent" in page and "Where the points went" in page and "Late arrivals" in page and "10 min" in page and "The rules behind this score" in page
    prof = admin_b.get("/employee/101?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "100 − 2 = <b>98</b>" in prof and "Late arrivals −2" in prof and "Open the scorecard" in prof
    assert mgr1_b.get("/employee/101/score?from=2026-10-12&to=2026-10-12").status_code == 200
    assert mgr2_b.get("/employee/101/score?from=2026-10-12&to=2026-10-12").status_code == 403           # other team
    j = admin_b.get("/api/employee/101/summary?from=2026-10-12&to=2026-10-12").get_json()
    assert j["score"]["score"] == 98 and j["score"]["category"] == "Excellent"


def test_old_periods_keep_the_rules_that_applied_to_them(app, world, repo, admin_b):
    admin_b.post("/admin/scoring", {**FORM, "late_tiers": "1-=2", "late_steps": "", "late_consec_days": "", "late_ignore": "", "effective_from": "2026-01-01"})
    admin_b.post("/admin/scoring", {**FORM, "late_tiers": "1-=10", "late_steps": "", "late_consec_days": "", "late_ignore": "", "effective_from": "2026-10-15", "note": "stricter"})
    old = admin_b.get("/api/employee/101/summary?from=2026-10-12&to=2026-10-12").get_json()
    assert old["rules_version"] == 1 and old["score"]["score"] == 98
    assert "stricter" in admin_b.get("/admin/scoring").get_data(as_text=True)
    assert copy.deepcopy(repo.scoring_rule_versions()[0]["rules"])["late"]["tiers"][0]["penalty"] == 2
