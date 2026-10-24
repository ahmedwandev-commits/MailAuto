# -*- coding: utf-8 -*-
"""The SLA history (calllog/ops/sla_history.py): the inquiry grid, when a request's status history is read again, the
period a request belongs to, the live age of an open ticket from its LAST assignment, and the comparison with the
portal's own inquiry results. Made-up data only."""
from calllog.ops import sla_history as sh
from calllog.ops import sla_records

CFG = sla_records.default_config()
HEAD = list(sh.GRID_COLUMNS.values())


def grid_row(status="اسناد لمستخدم السرية", executor="مستخدم اول", date="20-10-2026", user_no="700301",
             system="Flexcube", request_type="اضافة صلاحية", employee="موظف اختبار"):
    values = {"date": date, "form": "نموذج 5", "branch": "فرع اختبار", "employee": employee, "employee_no": "700201",
              "user_no": user_no, "system": system, "group": "مجموعة", "request_type": request_type,
              "assigner": "موزع اختبار", "executor": executor, "checker": "", "notes": "", "status": status}
    return [values[k] for k in sh.GRID_COLUMNS]


def event(at, step, user="", user_no=""):
    return {"at": at, "step": step, "status": step, "user": user, "user_no": user_no, "user_type": "", "notes": ""}


# ------------------------------------------------------------------------------------------- the inquiry grid
def test_two_identical_inquiry_rows_stay_two_requests():
    """«تاريخ الطلب» has no time: the same employee, system and type can be asked twice in a day. Each row is its own
    request (its own «رقم الطلب»), so neither may be dropped; the first keeps the key it always had."""
    rows = [grid_row(), grid_row(), grid_row(system="T24")]
    grid = sh.grid_rows(HEAD, rows)
    assert len(grid) == 3 and len({g["key"] for g in grid}) == 3
    assert grid[0]["key"] == sh.grid_key(grid[0])                 # unchanged for the first: the index stays valid
    assert sh.grid_rows(HEAD, rows)[1]["key"] == grid[1]["key"]   # stable from one scan to the next


# ------------------------------------------------------------------------------------- re-reading the details
def test_a_reassigned_request_is_read_again_although_its_status_is_the_same():
    item = sh.grid_rows(HEAD, [grid_row(executor="مستخدم ثان")])[0]
    index = {item["key"]: {"id": "R1", "status": "اسناد لمستخدم السرية", "holder": "مستخدم اول"}}
    assert sh.needs_details(item, index) is True                  # re-assigned: the old assignment time is stale
    index[item["key"]]["holder"] = "مستخدم ثان"
    assert sh.needs_details(item, index) is False
    index[item["key"]].pop("holder")                              # read before holders were kept: once more if open
    assert sh.needs_details(item, index) is True
    closed = sh.grid_rows(HEAD, [grid_row(status="تم التعزيز على التنفيذ")])[0]
    assert sh.needs_details(closed, {closed["key"]: {"id": "R2", "status": "تم التعزيز على التنفيذ"}}) is False


def test_the_recording_keeps_the_user_number_and_who_held_the_request_when_it_was_read():
    item = sh.grid_rows(HEAD, [grid_row()])[0]
    records, index = {}, {}
    sh.fold_history(records, index, [{"grid": item, "id": "R1", "history": [
        event("2026-10-20T09:00:00", "assigned", "موزع اختبار", "700401")]}], CFG, "2026-10-20T09:05:00")
    assert records["R1"]["user_no"] == "700301"
    assert index[item["key"]]["holder"] == "مستخدم اول"
    assert sh.grid_state([item], {}, "2026-10-20T09:05:00", CFG)[item["key"]]["user_no"] == "700301"


def test_right_now_never_shows_the_previous_holders_clock():
    item = sh.grid_rows(HEAD, [grid_row(executor="مستخدم ثان")])[0]
    grid = sh.grid_state([item], {}, "2026-10-20T10:00:00", CFG)
    index = {item["key"]: {"id": "R1", "status": "اسناد لمستخدم السرية", "holder": "مستخدم اول"}}
    open_recs = {"R1": {"id": "R1", "stage": "open", "assigned_at": "2026-10-20T08:00:00", "holder": "مستخدم ثان"}}
    now = sh.right_now(grid, open_recs, index, CFG, "2026-10-20T10:00:00")
    assert now["open_now"][0]["minutes"] is None and now["now"]["unread"] == 1
    index[item["key"]]["holder"] = "مستخدم ثان"
    assert sh.right_now(grid, open_recs, index, CFG, "2026-10-20T10:00:00")["open_now"][0]["minutes"] == 120.0


# ------------------------------------------------------------------------------- the period: the assignment day
def _rec(rid, first, stage, confirmed="", **extra):
    return {"id": rid, "source": sh.SOURCE, "first_assigned_at": first, "assigned_at": first, "stage": stage,
            "confirmed_at": confirmed, "executed_at": confirmed, "final_outcome": "executed" if confirmed else "",
            "outcome": "completed" if confirmed else "in_progress", **extra}


def test_a_request_belongs_to_the_day_it_was_assigned_whatever_its_stage():
    recs = {"closed_late": _rec("closed_late", "2026-10-19T15:00:00", "closed", "2026-10-21T09:00:00"),
            "open_old": _rec("open_old", "2026-10-19T10:00:00", "open"),
            "today": _rec("today", "2026-10-21T08:00:00", "open"),
            "branch": {"id": "branch", "source": sh.SOURCE, "stage": "pre", "request_date": "2026-10-19",
                       "registered_at": "2026-10-19T08:00:00"}}
    day19 = sh.filter_records(recs, {"date_from": "2026-10-19", "date_to": "2026-10-19"})
    day21 = sh.filter_records(recs, {"date_from": "2026-10-21", "date_to": "2026-10-21"})
    assert set(day19) == {"closed_late", "open_old", "branch"}
    assert set(day21) == {"today"}


def test_the_by_day_table_counts_a_request_on_its_assignment_day():
    recs = {"a": _rec("a", "2026-10-19T15:00:00", "closed", "2026-10-20T09:00:00")}
    out = sh.analyze(recs, CFG)
    assert [d["date"] for d in out["by_day"]] == ["2026-10-19"]
    assert out["meta"]["date_from"] == out["meta"]["date_to"] == "2026-10-19"


# ------------------------------------------------------------------------- live tickets: the last assignment
def live(key, user_no, system, request_type, holder, sent="2026-10-19T08:00:00"):
    return {"key": key, "security_user": holder, "sent_at": sent,
            "fields": {"رقم المستخدم": user_no, "النظام": system, "نوع الطلب": request_type}}


def test_a_live_ticket_is_matched_to_its_open_history_and_prefers_the_current_holder():
    open_recs = {
        "R1": {"id": "R1", "stage": "open", "user_no": "700301", "system": "Flexcube", "request_type": "اضافة صلاحية",
               "holder": "مستخدم اول", "assigned_at": "2026-10-20T09:00:00"},
        "R2": {"id": "R2", "stage": "open", "user_no": "700301", "system": "Flexcube", "request_type": "اضافة صلاحية",
               "holder": "مستخدم ثان", "assigned_at": "2026-10-20T11:00:00"},
        "R3": {"id": "R3", "stage": "awaiting", "user_no": "700302", "system": "T24", "request_type": "حذف",
               "holder": "مستخدم اول", "assigned_at": "2026-10-20T07:00:00", "executed_at": "2026-10-20T08:00:00"}}
    reqs = [live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم ثان"),
            live("b", "700301", "flexcube", "اضافة  صلاحية", "مستخدم اول"),        # spelling / spaces do not matter
            live("c", "700302", "T24", "حذف", "مستخدم اول"),                       # executed: not an open assignment
            live("d", "700999", "Flexcube", "اضافة صلاحية", "مستخدم اول")]
    got = sh.match_live_requests(reqs, open_recs)
    assert {k: v["id"] for k, v in got.items()} == {"a": "R2", "b": "R1"}


def test_live_ages_come_from_the_last_assignment_else_the_sent_date():
    from datetime import datetime
    open_recs = {"R1": {"id": "R1", "stage": "open", "user_no": "700301", "system": "Flexcube",
                        "request_type": "اضافة صلاحية", "holder": "مستخدم اول", "assigned_at": "2026-10-21T08:30:00"}}
    reqs = [live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم اول", sent="2026-10-19T08:00:00"),
            live("b", "700999", "Flexcube", "اضافة صلاحية", "مستخدم اول", sent="2026-10-21T08:00:00")]
    sh.apply_assignment_ages(reqs, open_recs, datetime(2026, 10, 21, 9, 0), warn=60, breach=120)
    a, b = reqs
    assert a["assigned_at"] == "2026-10-21T08:30:00" and a["age_from"] == "assignment"
    assert a["age_minutes"] == 30 and a["status"] == "within"     # not 2 days "with the employee"
    assert b["assigned_at"] is None and b["age_from"] == "sent" and b["age_minutes"] == 60 and b["status"] == "reminder"


# ------------------------------------------------------------------------- compared with the portal's results
def test_the_comparison_with_the_portal_counts_every_inquiry_row_of_the_period():
    rows = [grid_row(status="تسجيل"), grid_row(status="اسناد لمستخدم السرية"), grid_row(status="تم التنفيذ"),
            grid_row(status="تم التعزيز على التنفيذ"), grid_row(status="رفض", system="T24"),
            grid_row(status="اسناد لمستخدم السرية", date="15-10-2026")]
    grid_items = sh.grid_rows(HEAD, rows)
    grid = sh.grid_state(grid_items, {}, "2026-10-21T09:00:00", CFG)
    index = {grid_items[3]["key"]: {"id": "R4", "status": "تم التعزيز على التنفيذ"}}
    cmp_ = sh.portal_comparison(grid, index, "2026-10-20", "2026-10-20", CFG)
    assert cmp_["rows"] == 5 and cmp_["with_security"] == 3 and cmp_["closed"] == 1
    assert cmp_["with_branch"] == 1 and cmp_["rejected"] == 1
    assert cmp_["read"] == 1 and cmp_["not_read"] == 3         # the branch-only row has no SLA history to read yet
    assert cmp_["rows"] == cmp_["with_security"] + cmp_["with_branch"] + cmp_["rejected"]


# ------------------------------------------------------- the status history as the real «تفاصيل الطلب» shows it
HISTORY_HEAD = ["رقم المستخدم", "اسم المستخدم", "نوع المستخدم", "التاريخ", "الحالة", "ملاحظات"]


def test_the_employee_clock_starts_at_the_assignment_line_of_the_status_history():
    """«الحالة التاريخية للطلب» is sorted by user name, not by time; the date cell is «21/10/2026» + line break + the
    time; the «اسناد لمستخدم السرية» line carries the user who ASSIGNED it. The security user's clock runs from that
    line to «تم التنفيذ» (an «معلق» in between is counted as held), the reviewer's to «تم التعزيز على التنفيذ»."""
    rows = [["700504", "أ موظف فرع", "Maker Branch", "21/10/2026\n08:40:00", "تسجيل", ""],
            ["700503", "ب مشرف فرع", "Checker Branch", "21/10/2026\n08:52:51", "تم التعزيز من قبل مشرف الفرع", ""],
            ["700501", "ج مستخدم سرية", "Assign_Maker_Security", "21/10/2026\n08:54:23", "اسناد لمستخدم السرية", "-----"],
            ["700501", "ج مستخدم سرية", "Assign_Maker_Security", "21/10/2026\n09:09:56", "معلق", ""],
            ["700501", "ج مستخدم سرية", "Assign_Maker_Security", "21/10/2026\n10:28:31", "تم التنفيذ", ""],
            ["700502", "د مراجع سرية", "Checker security", "21/10/2026\n10:45:29", "تم التعزيز على التنفيذ", ""]]
    rows.sort(key=lambda r: r[1], reverse=True)                     # the page's own order: by name
    lc = sh.lifecycle(sh.events_from_table(HISTORY_HEAD, rows, CFG), CFG)
    assert lc["assigned_at"] == lc["first_assigned_at"] == "2026-10-21T08:54:23"
    assert lc["executed_at"] == "2026-10-21T10:28:31" and lc["confirmed_at"] == "2026-10-21T10:45:29"
    assert lc["stage"] == "closed" and lc["outcome"] == "completed" and lc["checker"] == "د مراجع سرية"
    minutes = sh.stage_minutes(lc)
    assert round(minutes["work"], 1) == 94.1 and round(minutes["sla"], 1) == 111.1
    assert round(lc["held_minutes"], 1) == 78.6


def test_an_open_request_is_assigned_by_a_reviewer_and_aged_from_that_line():
    """The assignment line names the assigner («Checker security» here), never the security user who holds the
    request - the holder comes from the inquiry grid's «مستخدم السرية». The live age counts from the assignment."""
    from datetime import datetime
    rows = [["700511", "ه مشرف فرع", "Checker Branch", "21/10/2026\n09:36:00", "تم التعزيز من قبل مشرف الفرع", ""],
            ["700512", "و مراجع سرية", "Checker security", "21/10/2026\n10:23:48", "اسناد لمستخدم السرية", "ملاحظة"],
            ["700513", "ز موظف فرع", "Maker Branch", "21/10/2026\n08:55:02", "تسجيل", "ملاحظة"]]
    item = sh.grid_rows(HEAD, [grid_row(status="اسناد لمستخدم السرية", executor="مستخدم اول")])[0]
    records, index = {}, {}
    sh.fold_history(records, index, [{"grid": item, "id": "R9", "history": sh.events_from_table(HISTORY_HEAD, rows, CFG)}],
                    CFG, "2026-10-21T10:30:00")
    rec = records["R9"]
    assert rec["stage"] == "open" and rec["assigned_at"] == "2026-10-21T10:23:48" and not rec["executed_at"]
    assert rec["assigner"] == "و مراجع سرية" and rec["holder"] == "مستخدم اول"
    reqs = [live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم اول", sent="2026-10-21T09:36:00")]
    sh.apply_assignment_ages(reqs, records, datetime(2026, 10, 21, 11, 0), warn=60, breach=120)
    assert reqs[0]["age_from"] == "assignment" and reqs[0]["age_minutes"] == 36


def test_requests_with_a_security_user_are_read_first_so_their_live_clock_is_exact():
    """When a scan cannot open every row, the requests still with a security user come first (their live age needs the
    assignment line), then the ones awaiting the reviewer, then the closed ones, then the rejected ones; a request still
    with the branch has no SLA step to read."""
    order = [sh.detail_priority(s, CFG) for s in ("اسناد لمستخدم السرية", "معلق", "تم التنفيذ",
                                                  "تم التعزيز على التنفيذ", "رفض", "تسجيل")]
    assert order[0] == order[1] < order[2] < order[3] < order[4] and order[5] is None
