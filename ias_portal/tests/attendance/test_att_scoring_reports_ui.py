"""The Scoring rules page (ready-made sets, row editors, live preview), the reports (month-end checklist, daily detail, shift changes,
approvals, totals) and Analytics (charts) - one place for the tables, one for the picture."""
import re
from pathlib import Path

from calllog.attendance.services import scoring_service as sc

from .support import login

STATIC = Path(__file__).resolve().parents[2] / "program" / "calllog" / "static" / "attendance"
SAMPLE = {"sample_late": "10, 20, 40, 12", "sample_early": "15", "sample_absences": "1", "sample_missing": "2", "sample_rejected": "0", "sample_days": "22"}


# ---------------------------------------------------------------- Scoring rules
def test_every_ready_made_set_is_valid_and_complete():
    assert set(sc.PRESETS) == {"lenient", "standard", "strict"}
    for key, p in sc.PRESETS.items():
        rules = sc.parse_rules(p["values"])                                   # the portal's own parser accepts it
        assert rules["categories"] and rules["late"]["tiers"] and rules["absence"]["penalty_per_day"] > 0, key
    assert sc.PRESETS["standard"]["values"]["late_tiers"] == "1-15=1; 16-30=2; 31-60=3; 61-=5"
    assert sc.PRESETS["lenient"]["values"]["max_total"] < sc.PRESETS["strict"]["values"]["max_total"]


def test_the_page_has_the_sets_the_row_editors_and_the_preview(app, users, admin_b):
    page = admin_b.get("/admin/scoring").get_data(as_text=True)
    assert page.count('class="btn btn-ghost sc-preset"') == 3 and "Lenient" in page and "Standard" in page and "Strict" in page
    assert 'id="scoringForm"' in page and 'data-kind="tiers"' in page and 'data-kind="steps"' in page and 'data-kind="categories"' in page
    assert 'name="sample_late"' in page and "attendance/scoring.js" in page and 'id="scPreview"' in page
    for name in ("start", "min_judged_days", "max_total", "late_tiers", "late_steps", "early_tiers", "absence_penalty", "missing_punch_penalty",
                 "rejected_permit_penalty", "categories", "effective_from", "late_window", "absence_consec_days"):
        assert f'name="{name}"' in page, name                                  # the form posts exactly the fields it always did


def test_saving_a_ready_made_set_through_the_page_works(app, users, repo, admin_b):
    values = {**sc.PRESETS["standard"]["values"], "effective_from": "2026-10-01", "note": "from the Standard set"}
    assert admin_b.post("/admin/scoring", values).status_code == 302
    rules = repo.scoring_rule_versions()[-1]["rules"]
    assert rules["late"]["tiers"][0] == {"from": 1, "to": 15, "penalty": 1.0} and rules["max_total_deduction"] == 40.0 and rules["absence"]["penalty_per_day"] == 5.0


def test_the_preview_uses_the_real_scoring_code(app, users, admin_b):
    r = admin_b.post("/admin/scoring/preview", {**sc.PRESETS["standard"]["values"], **SAMPLE})
    assert r.status_code == 200
    out = r.get_json()
    by = {d["label"].split(" (")[0]: d for d in out["deductions"]}
    # late 10/20/40/12 min in one month: 1 + 2 + (3 + 0.5) + (1 + 0.5) = 8 ; early 15 min = 1 ; one absence = 5 ; two missing punches = 2
    assert by["Late arrivals"]["total"] == 8 and by["Early departures"]["total"] == 1 and by["Verified unexcused absences"]["total"] == 5 and by["Missing punches"]["total"] == 2
    assert out["score"] == 84 and out["category"] == "Very Good" and out["start"] == 100
    assert by["Late arrivals"]["repeat"] == 1 and by["Late arrivals"]["count"] == 4


def test_the_preview_shows_caps_and_consecutive_runs(app, users, admin_b):
    values = {**sc.PRESETS["standard"]["values"], **SAMPLE, "sample_late": "40, 40, 40, 40, 40, 40, 40, 40, 40, 40", "sample_consecutive": "1"}
    out = admin_b.post("/admin/scoring/preview", values).get_json()
    late = next(d for d in out["deductions"] if d["label"].startswith("Late"))
    assert late["total"] == 25 and late["cap"] == 25 and any("Late arrivals" in c for c in out["caps"])     # the 25 % limit of the Standard set


def test_the_preview_reports_mistakes_and_is_for_the_administrator_only(app, users, admin_b, mgr1_b, gm_b):
    bad = admin_b.post("/admin/scoring/preview", {**sc.PRESETS["standard"]["values"], **SAMPLE, "late_tiers": "abc"})
    assert bad.status_code == 400 and "form" in bad.get_json()["error"]
    assert admin_b.post("/admin/scoring/preview", {**sc.PRESETS["standard"]["values"], **SAMPLE, "sample_late": "ten"}).status_code == 400
    for b in (mgr1_b, gm_b):
        assert b.post("/admin/scoring/preview", {**sc.PRESETS["standard"]["values"], **SAMPLE}).status_code in (302, 403)


def test_the_page_script_follows_the_csp_rules():
    js = (STATIC / "scoring.js").read_text(encoding="utf-8")
    for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function", r"setAttribute\(\s*[\"']style", r"\.cssText", r"<script"):
        assert not re.search(pattern, js), pattern
    assert "FormData(form)" in js and "data-preview-url" in js


# ---------------------------------------------------------------- Reports
def test_the_month_end_checklist_lists_what_needs_a_decision(app, world, repo, admin_b, mgr1_b):
    page = admin_b.get("/reports?kind=review&from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Month-end checklist" in page and "Absence to review" in page and "Sara A2" in page and "Nour B2" in page
    mine = mgr1_b.get("/reports?kind=review&from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Sara A2" in mine and "Nour B2" not in mine                                               # a manager sees only their own team


def test_the_daily_detail_and_the_totals(app, world, repo, admin_b):
    page = admin_b.get("/reports?kind=daily&from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Daily attendance detail" in page and "Ali A1" in page and "07:10" in page
    summary = admin_b.get("/reports?kind=summary&from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "<tfoot>" in summary and "<b>Total</b>" in summary and "Employee attendance summary" in summary
    assert "one line per employee" in summary.lower()                                                  # the description under the title


def test_shift_changes_are_reported_with_who_and_why(app, world, repo, mgr1_b, admin_b):
    r = mgr1_b.post("/schedule/edit", {"emp": "101", "date": "2026-10-14", "codes": "E", "reason": "cover for Sara"})
    assert r.status_code == 302
    page = admin_b.get("/reports?kind=changes&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Schedule changes" in page and "Ali A1" in page and "cover for Sara" in page and "mgr1" in page


def test_the_approvals_report_follows_the_managers(app, world, repo, mgr1_b, admin_b):
    page = admin_b.get("/reports?kind=approvals&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Waiting for the manager" in page and "Team A" in page and "Team B" in page
    mgr1_b.post("/monthly-shifts", {"action": "approve", "month": "2026-10", "team": "Team A"})
    page = admin_b.get("/reports?kind=approvals&from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Approved" in page and "mgr1" in page


def test_the_teams_report_has_the_month_end_columns(app, world, admin_b):
    page = admin_b.get("/reports?kind=teams&from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Missing punches" in page and "Early #" in page


def test_the_reports_are_grouped_and_exported(app, world, repo, admin_b):
    page = admin_b.get("/reports").get_data(as_text=True)
    assert "Month-end" in page and "Changes &amp; trends" in page and "Last 7 days" in page and "Last 30 days" in page
    assert admin_b.get("/export/report.xlsx?kind=review&from=2026-10-12&to=2026-10-12").status_code == 200
    assert admin_b.get("/export/report.xlsx?kind=changes&from=2026-10-01&to=2026-10-31").status_code == 200


# ---------------------------------------------------------------- Analytics: the picture, not the tables
def test_analytics_has_charts_and_no_duplicate_tabs(app, world, repo, admin_b, mgr1_b):
    page = admin_b.get("/analytics?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "What happened on the planned days" not in page and 'class="donut"' not in page                            # the ring moved to the Day view as a strip
    assert "Which day of the week people are late" in page and "How often each team came to work" in page
    assert "How many people in each score category" in page and "Is attendance getting better" in page and "chart-note" in page
    assert "Attendance % by month" not in page and "hbars" in page
    assert "Overview" in page and "Employees" in page
    assert "Team comparison" not in page and "Score ranking" not in page          # those tabs were the same tables as the Reports
    mine = mgr1_b.get("/analytics?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    assert "Team A" in mine and "Team B" not in mine


def test_each_chart_links_to_its_report(app, world, admin_b):
    page = admin_b.get("/analytics?from=2026-10-12&to=2026-10-12").get_data(as_text=True)
    for kind in ("lateness", "teams", "ranking", "trends", "review"):
        assert f"kind={kind}" in page, kind


def test_an_employee_cannot_open_analytics_or_reports(app, world):
    b = login(app, "101")
    assert b.get("/analytics").status_code == 403 and b.get("/reports?kind=review").status_code == 403
