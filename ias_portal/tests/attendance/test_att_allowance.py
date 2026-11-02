"""Shift-allowance sheet (بدل ورديات): counts only, the shift from the portal's schedule first, managers for mornings and holidays only, the managers'
monthly approval, the template workbook, the settings and who may use it."""
import io

import openpyxl

from calllog.attendance.domain import allowance as al
from calllog.attendance.services import allowance_service as svc, settings as settings_svc, shifts_service

from .support import add_user, login

RULES = settings_svc.allowance_rules({})
Q = "from=2026-09-01&to=2026-09-30"


def _rows(repo, rows):
    imp = repo.create_attendance_import({"filename": "hr.xlsx", "file_sha256": "h" * 64, "parsed": {}})
    repo.commit_attendance_rows_atomic(imp, rows)


def day(emp, d, tin, tout=None, day_type="يوم عمل"):
    return {"employee_id": emp, "work_date": d, "in_time": tin, "out_time": tout, "day_type": day_type}


def kind_of(r):
    return {"يوم عمل": None, "العطلة الاسبوعية": "rest_day"}.get(r["day_type"], "official_holiday")


def run(rows, rules=None, **kw):
    att = {(r["employee_id"], r["work_date"]): r for r in rows}
    return al.classify(att, "2026-09-01", "2026-09-30", rules or RULES, kind_of, **kw)


def with_rules(**per_kind):
    rules = {k: dict(v) for k, v in RULES.items()}
    for k, vals in per_kind.items():
        rules[k].update(vals)
    return rules


def form(**per_kind):
    """The POST of the allowance settings: every kind as in the defaults, some fields changed."""
    out = {"action": "allowance", "use_schedule": "1", "evening_after": "20:00"}
    for k, r in with_rules(**per_kind).items():
        for f, v in r.items():
            if f in ("enabled", "needs_hours"):
                if v:
                    out[f"{k}_{f}"] = "1"
            else:
                out[f"{k}_{f}"] = str(v)
    return out


def attendance(repo):
    """101 (Team A, no schedule in September): morning, evening, a night that ends on the next day's row, a weekly rest day worked, then days
    that do not count. 201 (Team B): its morning and an official holiday that was too short."""
    _rows(repo, [day(101, "2026-09-07", "07:10:00", "15:20:00"), day(101, "2026-09-08", "14:00:00", "22:05:00"),
                 day(101, "2026-09-09", "22:10:00", None), day(101, "2026-09-10", None, "06:20:00"),
                 day(101, "2026-09-11", "09:00:00", "17:30:00", "العطلة الاسبوعية"),
                 day(101, "2026-09-14", "07:30:00", "15:40:00"), day(101, "2026-09-15", "07:00:00", "14:30:00"), day(101, "2026-09-16", "08:00:00", "16:00:00"),
                 day(201, "2026-09-07", "06:50:00", "14:55:00"), day(201, "2026-09-12", "20:00:00", "23:00:00", "عيد القوات المسلحة")])


def approve(b, month="2026-09", team="Team A"):
    return b.post("/monthly-shifts", {"action": "approve", "month": month, "team": team})


# ---------------------------------------------------------------- the rules of the settings
def test_the_rules_have_an_amount_per_day_set_in_the_settings():
    assert [(k, RULES[k]["start"], RULES[k]["hours"]) for k in ("morning", "evening", "night")] == [("morning", "07:00", 8), ("evening", "14:00", 8), ("night", "22:00", 8)]
    assert all("rate" not in r and "basis" not in r for r in RULES.values()) and "normal" not in RULES   # counts only: no money


def test_the_first_version_of_the_settings_is_kept_and_old_money_is_ignored():
    old = {"allowance_grace_minutes": 30, "allowance_hours": 7, "allowance_rates": {"morning": 5}, "allowance_holiday_needs_hours": False,
           "allowance_rules": {"morning": {"rate": 9, "basis": "hour", "start": "06:30"}}}
    r = settings_svc.allowance_rules(old)
    assert (r["morning"]["grace"], r["night"]["hours"], r["morning"]["start"]) == (30, 7, "06:30") and "rate" not in r["morning"] and "basis" not in r["morning"]
    assert "rate" not in settings_svc.allowance_rules({"allowance_rules": {"night": {"rate": 120}}})["night"]          # counts only: a saved rate is ignored
    assert r["rest_day"]["needs_hours"] is False and r["rest_day"]["hours"] == 7


# ---------------------------------------------------------------- the shift of a day
def test_morning_starts_at_seven_and_the_latest_check_in_is_seven_fifteen():
    f = lambda t, rules=None: al.shift_of(t, rules or RULES)
    assert [f("07:00"), f("07:15"), f("07:15:40"), f("06:00")] == ["morning"] * 4
    assert [f("07:16"), f("08:00"), f("05:59"), f("11:00")] == [None] * 4
    assert [f("14:15"), f("13:00"), f("14:16")] == ["evening", "evening", None]
    assert [f("22:15"), f("21:00"), f("23:30")] == ["night", "night", None]


def test_the_shift_planned_in_the_portal_is_the_shift_whatever_the_check_in():
    plan = {(1, "2026-09-16"): "evening", (1, "2026-09-14"): "morning"}
    rows = [day(1, "2026-09-16", "10:00:00", "18:10:00"), day(1, "2026-09-14", "07:40:00", "15:50:00")]
    days, skipped = run(rows, plan=plan)
    assert [(d["date"], d["kind"], d["source"]) for d in days] == [("2026-09-14", "morning", "schedule"), ("2026-09-16", "evening", "schedule")] and skipped == []
    days, skipped = run(rows)                                                             # without the plan both are not counted
    assert days == [] and len(skipped) == 2
    days, _ = run(rows, plan=plan, use_schedule=False)
    assert days == []                                                                     # the switch in the settings


def test_a_planned_shift_still_needs_the_full_hours():
    days, skipped = run([day(1, "2026-09-16", "10:00:00", "17:30:00")], plan={(1, "2026-09-16"): "evening"})
    assert days == [] and skipped[0]["reason"] == "stayed 7h30 of 8h00"


def test_a_planned_night_shift_ends_on_the_next_day():
    plan = {(1, "2026-09-07"): "night"}
    days, _ = run([day(1, "2026-09-07", "23:30:00", None), day(1, "2026-09-08", None, "07:45:00")], plan=plan)
    assert [d["kind"] for d in days] == ["night"] and days[0]["out_time"] == "07:45:00"


def test_a_shift_worker_with_no_planned_shift_is_decided_by_the_time_of_day():
    kw = {"shift_workers": {1}, "evening_after": "20:00"}
    days, _ = run([day(1, "2026-09-07", "10:00:00", "18:10:00"), day(1, "2026-09-08", "12:00:00", "20:30:00"), day(1, "2026-09-09", "22:40:00", "07:00:00"),
                   day(1, "2026-09-10", "09:00:00", "17:00:00")], **kw)
    assert [(d["date"], d["kind"], d["source"]) for d in days] == [("2026-09-08", "evening", "time"), ("2026-09-09", "night", "time")]   # 10:00-18:10 and 09:00-17:00 are normal days
    assert run([day(1, "2026-09-08", "12:00:00", "19:59:00")], **kw)[0] == []                                  # 7h59: the hours are needed first
    assert [d["kind"] for d in run([day(1, "2026-09-08", "12:00:00", "20:00:00")], **kw)[0]] == ["evening"]    # exactly 8 h and leaving at 20:00
    assert [d["kind"] for d in run([day(1, "2026-09-08", "12:00:00", "20:05:00")], **kw)[0]] == ["evening"]
    assert run([day(1, "2026-09-08", "12:00:00", "20:05:00")], shift_workers={1}, evening_after="21:00")[0] == []          # the setting: not past 21:00, so a normal day


def test_somebody_without_shifts_is_a_normal_day_and_is_not_counted():
    days, skipped = run([day(1, "2026-09-07", "08:05:00", "16:10:00")])
    assert days == [] and "schedule has no shift for the day" in skipped[0]["reason"]


def test_a_manager_is_counted_for_the_morning_shift_and_holidays_only():
    plan = {(7, "2026-09-07"): "morning", (7, "2026-09-08"): "evening"}
    rows = [day(7, "2026-09-07", "07:05:00", "15:10:00"), day(7, "2026-09-08", "14:00:00", "22:10:00"),
            day(7, "2026-09-11", "09:00:00", "17:30:00", "العطلة الاسبوعية"), day(7, "2026-09-12", "09:00:00", "17:30:00", "عيد")]
    days, skipped = run(rows, plan=plan, is_manager=lambda emp: emp == 7)
    assert [d["kind"] for d in days] == ["morning", "rest_day", "official_holiday"]                # the evening is not paid to a manager
    assert skipped[0]["date"] == "2026-09-08" and "morning shift only" in skipped[0]["reason"]
    assert [d["kind"] for d in run(rows[1:2], plan=plan)[0]] == ["evening"]                        # an employee with the same punches is counted


def test_a_kind_that_is_switched_off_is_never_counted():
    days, skipped = run([day(1, "2026-09-07", "07:05:00", "15:10:00")], with_rules(morning={"enabled": False}))
    assert days == [] and "schedule has no shift for the day" in skipped[0]["reason"]
    days, skipped = run([day(1, "2026-09-07", "10:00:00", "18:10:00")], with_rules(morning={"enabled": False}), plan={(1, "2026-09-07"): "morning"})
    assert days == [] and skipped[0]["reason"] == "morning is switched off in the allowance settings"


def test_a_shift_needs_the_full_eight_hours_after_the_check_in():
    days, skipped = run([day(1, "2026-09-07", "07:10:00", "15:10:00"), day(1, "2026-09-08", "07:10:00", "15:09:00"),
                         day(1, "2026-09-09", "07:00:00", "14:59:59"), day(1, "2026-09-10", "07:00:00", None)])
    assert [d["date"] for d in days] == ["2026-09-07"]
    assert [s["reason"] for s in skipped] == ["stayed 7h59 of 8h00", "stayed 7h59 of 8h00", "no check-out"]


# ---------------------------------------------------------------- weekly rest days and official holidays
def test_a_weekly_rest_day_and_an_official_holiday_are_both_holiday():
    rows = [day(1, "2026-09-11", "09:00:00", "17:00:00", "العطلة الاسبوعية"), day(1, "2026-09-12", "09:00:00", "17:00:00", "عيد القوات المسلحة")]
    days, _ = run(rows)
    s = al.summarise(days)[1]
    assert [d["kind"] for d in days] == ["rest_day", "official_holiday"] and s["holiday"]["count"] == 2 and s["total_count"] == 2


def test_the_hours_of_a_holiday_are_required_only_when_the_rule_says_so():
    rows = [day(1, "2026-09-11", "09:00:00", "12:00:00", "العطلة الاسبوعية"), day(1, "2026-09-12", "09:00:00", "12:00:00", "عيد")]
    days, skipped = run(rows)
    assert days == [] and [s["reason"] for s in skipped] == ["stayed 3h00 of 8h00"] * 2
    assert [d["kind"] for d in run(rows, with_rules(rest_day={"needs_hours": False}))[0]] == ["rest_day"]
    assert [d["kind"] for d in run(rows, with_rules(rest_day={"hours": 3}, official_holiday={"hours": 3}))[0]] == ["rest_day", "official_holiday"]


def test_a_holiday_kind_can_be_switched_off():
    days, skipped = run([day(1, "2026-09-12", "09:00:00", "17:00:00", "عيد")], with_rules(official_holiday={"enabled": False}))
    assert days == [] and skipped[0]["reason"] == "official holiday is switched off in the allowance settings"


def test_days_without_a_check_in_are_listed_and_empty_days_are_silent():
    days, skipped = run([day(1, "2026-09-14", None, "16:00:00"), day(1, "2026-09-15", None, None)])
    assert days == [] and [s["reason"] for s in skipped] == ["no check-in"]


def test_summary_is_counts_only():
    days = [{"employee_id": 1, "date": f"2026-09-0{i}", "kind": k} for i, k in enumerate(["morning", "morning", "night", "rest_day", "official_holiday"], 1)]
    s = al.summarise(days)[1]
    assert (s["morning"]["count"], s["night"]["count"], s["rest_day"]["count"], s["official_holiday"]["count"], s["holiday"]["count"]) == (2, 1, 1, 1, 2)
    assert s["total_count"] == 5 and all(set(s[k]) == {"count"} for k in al.OUT)                  # nothing but counts


# ---------------------------------------------------------------- the page and the workbook (after the approval)
def test_nothing_reaches_the_sheet_before_the_manager_approves(app, world, repo, admin_b, mgr1_b):
    attendance(repo)
    assert svc.build(repo, "2026-09-01", "2026-09-30")["rows"] == []                              # not approved: hidden
    page = admin_b.get(f"/allowance?{Q}").get_data(as_text=True)                                  # the administrator sees everything at once
    assert "Waiting for the manager" in page and "Not approved yet" in page and "Ali A1" in page and "Omar B1" in page and "not shown" not in page
    assert svc.build(repo, "2026-09-01", "2026-09-30", include_pending=True)["rows"]
    assert approve(mgr1_b).status_code == 302
    rep = svc.build(repo, "2026-09-01", "2026-09-30")
    assert [r["employee_id"] for r in rep["rows"]] == [101] and rep["hidden_days"] > 0              # Team A is in, Team B is not
    assert {(a["team"], a["status"]) for a in rep["approvals"]} == {("Team A", "approved"), ("Team B", "pending")}
    page = admin_b.get(f"/allowance?{Q}").get_data(as_text=True)
    assert "Approved" in page and "Not approved yet" in page and "Omar B1" in page              # Team B's days are still there, marked


def test_page_summarises_the_period_per_employee(app, world, repo, admin_b):
    attendance(repo)
    page = admin_b.get(f"/allowance?{Q}&pending=1").get_data(as_text=True)
    assert "Shift allowance" in page and "Ali A1" in page and "Omar B1" in page and "allowance-table" in page and "allowance-skipped" in page
    assert "Value" not in page and "value" not in page.split('id="allowance-table"')[1].split("</table>")[0].lower()           # no money anywhere
    rep = svc.build(repo, "2026-09-01", "2026-09-30", include_pending=True)
    by = {r["employee_id"]: r for r in rep["rows"]}
    a = by[101]
    assert (a["morning"]["count"], a["evening"]["count"], a["night"]["count"], a["holiday"]["count"]) == (1, 1, 1, 1) and a["total_count"] == 4
    assert by[201]["morning"]["count"] == 1 and by[201]["holiday"]["count"] == 0
    why = {(s["employee_id"], s["date"]): s["reason"] for s in rep["skipped"]}
    assert why[(201, "2026-09-12")] == "stayed 3h00 of 8h00" and why[(101, "2026-09-10")] == "no check-in" and why[(101, "2026-09-15")] == "stayed 7h30 of 8h00"
    assert why[(101, "2026-09-16")].startswith("check-in 08:00 is not at the start of any switched-on shift")
    assert [r["employee_id"] for r in svc.build(repo, "2026-09-01", "2026-09-30", "Team B", True)["rows"]] == [201]


def test_export_is_the_template_workbook_without_money(app, world, repo, admin_b):
    attendance(repo)
    r = admin_b.get(f"/allowance/export/xlsx?{Q}&pending=1")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["Content-Type"] and "attachment" in r.headers["Content-Disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.data))
    assert wb.sheetnames == ["ورديات", "ملخص", "غير محسوب"]
    ws = wb["ورديات"]
    assert [c.value for c in ws[1]] == ["employee_ID", "employee_name ", "date", "shift_type", "morning_shift", "Evening_shift", "night_shift",
                                        "holidays_GT_6_DAYS", "TASK"]
    rows = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
    first = rows[0]
    assert first[0] == 101 and first[1] == "Ali A1" and first[3] == "صباحية" and first[4:8] == [1, 0, 0, 0] and first[2].date().isoformat() == "2026-09-07"
    assert sorted(r[3] for r in rows) == sorted(["صباحية", "مسائية", "ليلية", "عطلة", "صباحية"])
    sm = wb["ملخص"]
    assert [c.value for c in sm[1]] == ["رقم الموظف", "الاسم", "وردية صباحية", "وردية ظهيرة", "وردية ليلية", "ايام العطلات", "إجمالي الأيام"]   # counts, no value columns
    assert {row[0].value: [c.value for c in row] for row in sm.iter_rows(min_row=2)}[101][2:] == [1, 1, 1, 1, 4]
    nc = [[c.value for c in row] for row in wb["غير محسوب"].iter_rows(min_row=2)]
    assert {r[5].split(" ")[0] for r in nc} >= {"check-in", "stayed", "no"}
    assert repo.list_audit(action="allowance_exported")
    again = openpyxl.load_workbook(io.BytesIO(admin_b.get(f"/allowance/export/xlsx?{Q}").data))["ورديات"]                    # the administrator: all, approved or not
    assert again.max_row == ws.max_row


def test_bad_period_is_refused(app, world, admin_b):
    assert admin_b.get("/allowance?from=2026-09-30&to=2026-09-01").status_code == 400
    assert admin_b.get("/allowance?from=2025-01-01&to=2026-09-30").status_code == 400


# ---------------------------------------------------------------- the schedule in the portal decides the shift
def shifted(repo):
    """September for 101: M on 7 and 14 Sep, E on 16 Sep (changed by the manager); HR check-ins that do not match the start."""
    imp = repo.create_schedule_import({"year": 2026, "month": 9, "filename": "t", "file_sha256": "s" * 64, "team": "Team A", "parsed": {}})
    repo.commit_schedule_version_atomic(2026, 9, imp, [{"employee_id": 101, "work_date": d, "shift_code": c, "source": s} for d, c, s in
                                                      (("2026-09-07", "M", "import"), ("2026-09-14", "M", "import"), ("2026-09-16", "E", "manual"))], "admin", replace=False)
    _rows(repo, [day(101, "2026-09-07", "07:05:00", "15:10:00"), day(101, "2026-09-14", "07:40:00", "15:50:00"), day(101, "2026-09-16", "10:00:00", "18:10:00"),
                 day(101, "2026-09-17", "12:00:00", "20:30:00"), day(101, "2026-09-18", "09:00:00", "17:00:00"), day(102, "2026-09-07", "08:00:00", "16:00:00")])


def test_the_portal_schedule_and_the_time_of_day_in_the_service(app, world, repo):
    shifted(repo)
    rep = svc.build(repo, "2026-09-01", "2026-09-30", include_pending=True)
    mine = {d["date"]: (d["kind"], d["source"]) for d in rep["days"] if d["employee_id"] == 101}
    assert mine == {"2026-09-07": ("morning", "schedule"), "2026-09-14": ("morning", "schedule"), "2026-09-16": ("evening", "schedule"),
                    "2026-09-17": ("evening", "time")}          # 101 has shifts that month: leaving after 20:00 is Evening; 09:00-17:00 with no shift is a normal day
    assert 102 not in {d["employee_id"] for d in rep["days"]}                                         # 102 has no shift in September: a normal day


def test_a_manager_account_is_paid_the_morning_shift_only(app, world, repo):
    add_user(repo, "777", "manager", ["Team A"], name="Manager Seven")
    imp = repo.create_schedule_import({"year": 2026, "month": 9, "filename": "t", "file_sha256": "m" * 64, "team": "Team A", "parsed": {}})
    repo.commit_schedule_version_atomic(2026, 9, imp, [{"employee_id": 777, "work_date": "2026-09-07", "shift_code": "M", "source": "import"}], "admin", replace=False)
    _rows(repo, [day(777, "2026-09-07", "07:05:00", "15:10:00"), day(777, "2026-09-11", "09:00:00", "17:30:00", "العطلة الاسبوعية")])
    rep = svc.build(repo, "2026-09-01", "2026-09-30", include_pending=True)
    assert [(d["employee_id"], d["kind"]) for d in rep["days"] if d["employee_id"] == 777] == [(777, "morning"), (777, "rest_day")]
    assert not any(s["employee_id"] == 777 for s in rep["skipped"])


# ---------------------------------------------------------------- Monthly shifts: the manager checks and approves
def test_the_manager_sees_the_people_and_approves_the_month(app, world, repo, mgr1_b, admin_b):
    attendance(repo)
    page = mgr1_b.get("/monthly-shifts?month=2026-09").get_data(as_text=True)
    assert "Monthly shifts" in page and "Ali A1" in page and "Sara A2" in page and "Omar" not in page and "Not approved yet" in page and "Approve 2026-09" in page
    assert "Shift changes" in page and "monthly-shifts" in page
    r = approve(mgr1_b)
    assert r.status_code == 302
    page = mgr1_b.get("/monthly-shifts?month=2026-09").get_data(as_text=True)
    assert "Approved" in page and "in the allowance sheet" in page and "Reopen" in page
    rec = repo.shift_approvals()["2026-09|Team A"]
    assert rec["by"] == "mgr1" and rec["employees"] == 2 and rec["days"] == 4 and repo.list_audit(action="shifts_approved")


def test_a_change_after_the_approval_needs_a_new_approval(app, world, repo, mgr1_b):
    attendance(repo)
    approve(mgr1_b)
    assert shifts_service.month_view(repo, repo.get_user("mgr1"), "2026-09", "Team A")["status"] == "approved"
    _rows(repo, [day(102, "2026-09-21", "14:00:00", "22:30:00")])                                   # a new HR row changes the sheet
    page = mgr1_b.get("/monthly-shifts?month=2026-09").get_data(as_text=True)
    assert "Changed after approval" in page
    assert svc.build(repo, "2026-09-01", "2026-09-30")["rows"] == []                              # hidden until approved again
    approve(mgr1_b)
    assert [r["employee_id"] for r in svc.build(repo, "2026-09-01", "2026-09-30")["rows"]] == [101, 102]


def test_a_manager_can_reopen_and_cannot_touch_another_team(app, world, repo, mgr1_b, mgr2_b, gm_b):
    attendance(repo)
    approve(mgr1_b)
    assert mgr2_b.post("/monthly-shifts", {"action": "approve", "month": "2026-09", "team": "Team A"}).status_code == 403
    assert mgr1_b.get("/monthly-shifts?month=2026-09&team=Team%20B").status_code == 403
    assert gm_b.get("/monthly-shifts?month=2026-09&team=Team%20A").status_code == 200                                  # the General Manager reads
    assert gm_b.post("/monthly-shifts", {"action": "approve", "month": "2026-09", "team": "Team B"}).status_code == 403
    assert mgr1_b.post("/monthly-shifts", {"action": "reopen", "month": "2026-09", "team": "Team A"}).status_code == 302
    assert "2026-09|Team A" not in repo.shift_approvals() and repo.list_audit(action="shifts_reopened")


def test_only_approved_teams_reach_a_payroll_account(app, world, repo, admin_b, mgr1_b):
    attendance(repo)
    add_user(repo, "payroll", "user", [], name="Payroll Clerk")
    admin_b.post("/admin/roles", {"action": "assign_allowance", "user_id": "payroll"})
    p = login(app, "payroll")
    assert "allowance-days" in p.get(f"/allowance?{Q}").get_data(as_text=True)
    assert p.get(f"/allowance?{Q}&pending=1").get_data(as_text=True).count("Omar B1") == 0                  # the switch is the administrator's only
    approve(mgr1_b)
    assert "Ali A1" in p.get(f"/allowance?{Q}").get_data(as_text=True)
    assert openpyxl.load_workbook(io.BytesIO(p.get(f"/allowance/export/xlsx?{Q}&pending=1").data))["ورديات"].max_row == 1 + 4


# ---------------------------------------------------------------- the settings screen
def test_the_settings_screen(app, world, admin_b):
    page = admin_b.get("/admin/settings").get_data(as_text=True)
    table = page.split('id="allowance-rules"')[1].split("</table>")[0]
    for k in al.KINDS:
        assert f'name="{k}_enabled"' in table and f'name="{k}_hours"' in table
    assert not any(f'name="{k}_rate"' in table for k in al.KINDS) and "_basis" not in table and "normal" not in table.lower()   # counts only, no normal day
    assert 'name="reminders"' in page and 'name="reminder_time" value="10:00"' in page
    assert 'name="use_schedule"' in page and 'name="evening_after" value="20:00"' in page and 'name="rest_day_needs_hours"' in table


def test_the_administrator_sets_the_rules(app, world, repo, admin_b):
    attendance(repo)
    r = admin_b.post("/admin/settings", {**form(official_holiday={"enabled": False}, morning={"grace": 30, "hours": 6}), "evening_after": "21:30"})
    assert r.status_code == 302
    st = settings_svc.load(repo)
    assert st["allowance_evening_after"] == "21:30" and st["allowance_use_schedule"] is True
    saved = settings_svc.allowance_rules(st)
    assert saved["morning"]["grace"] == 30 and saved["morning"]["hours"] == 6.0 and not saved["official_holiday"]["enabled"]
    rep = svc.build(repo, "2026-09-01", "2026-09-30", include_pending=True)
    sk = {(s["employee_id"], s["date"]): s["reason"] for s in rep["skipped"]}
    assert sk[(201, "2026-09-12")] == "official holiday is switched off in the allowance settings"
    a = {r["employee_id"]: r for r in rep["rows"]}[101]
    assert a["morning"]["count"] == 3                                                                  # 07 Sep, 14 Sep (07:30 is inside the 30 minutes now) and 15 Sep (7h30 is enough for 6 h)
    off = {k: v for k, v in form().items() if k != "use_schedule"}
    admin_b.post("/admin/settings", off)
    assert settings_svc.load(repo)["allowance_use_schedule"] is False and repo.list_audit(action="settings_changed")


def test_the_allowance_settings_are_validated(app, world, repo, admin_b):
    assert admin_b.post("/admin/settings", form(morning={"start": "7am"})).status_code == 400
    assert admin_b.post("/admin/settings", {**form(), "evening_after": "late"}).status_code == 400
    assert admin_b.post("/admin/settings", form(evening={"hours": 25})).status_code == 400
    assert admin_b.post("/admin/settings", form(morning={"grace": "abc"})).status_code == 400
    assert admin_b.post("/admin/settings", form(morning={"early": 400})).status_code == 400
    assert settings_svc.allowance_rules(settings_svc.load(repo)) == RULES


# ---------------------------------------------------------------- who may use it
def test_only_an_assigned_account_can_use_it_and_sees_nothing_else(app, world, repo, admin_b, gm_b, mgr1_b):
    attendance(repo)
    add_user(repo, "payroll", "user", [], name="Payroll Clerk")
    assert login(app, "payroll").get("/allowance").status_code == 403
    r = admin_b.post("/admin/roles", {"action": "assign_allowance", "user_id": "payroll"})
    assert r.status_code == 302 and repo.directory.can_allowance(repo.directory.store.get_user("payroll"))
    p = login(app, "payroll")
    assert p.get(f"/allowance?{Q}").status_code == 200 and p.get(f"/allowance/export/xlsx?{Q}").status_code == 200
    nav = p.get(f"/allowance?{Q}").get_data(as_text=True).split('aria-label="Main"')[1].split("</nav>")[0]   # the portal's menu
    assert "Shift allowance" in nav and ">Today<" not in nav and "Attendance settings" not in nav and ">Schedule<" not in nav
    assert p.get("/attendance/").headers["Location"].endswith("/attendance/allowance")
    for url in ("/planning/day", "/team", "/employees", "/admin/settings", "/admin/roles", "/schedule", "/reports", "/hr/import", "/modify", "/monthly-shifts", "/api/overview"):
        assert p.get(url).status_code == 403, url
    for b in (gm_b, mgr1_b):
        assert b.get("/allowance").status_code in (302, 403) and b.get("/allowance/export/xlsx").status_code == 403
    assert admin_b.get("/allowance").status_code == 200
    assert admin_b.post("/admin/roles", {"action": "remove_allowance", "user_id": "payroll"}).status_code == 302
    assert login(app, "payroll").get("/allowance").status_code == 403


def test_a_manager_keeps_the_role_and_gets_the_export_on_top(app, world, repo, admin_b, mgr1_b):
    admin_b.post("/admin/roles", {"action": "assign_allowance", "user_id": "mgr1"})
    page = mgr1_b.get(f"/allowance?{Q}").get_data(as_text=True)
    nav = page.split('aria-label="Main"')[1].split("</nav>")[0]
    assert ">Allowance<" in nav and ">Today<" in nav and ">Schedule<" in nav
    tabs = page.split('class="subtabs"')[1].split("</nav>")[0]                 # v5.17: the sheet is a tab of the Allowance group
    assert "Monthly shifts" in tabs and "Confirmation" in tabs and "Allowance sheet" in tabs
    assert mgr1_b.get("/planning/day").status_code == 200
    assert admin_b.post("/admin/roles", {"action": "assign_allowance", "user_id": "admin"}).status_code == 400


def test_an_employee_cannot_open_monthly_shifts(app, world, repo):
    assert login(app, "101").get("/monthly-shifts").status_code == 403
