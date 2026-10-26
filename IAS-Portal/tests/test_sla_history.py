# -*- coding: utf-8 -*-
"""The SLA history (calllog/ops/sla_history.py): the inquiry grid, when a request's status history is read again, the
period a request belongs to, the live age of an open ticket from its FIRST «اسناد لمستخدم السرية», which statuses
are steps (and which end a request), and the comparison with the portal's own inquiry results. Made-up data only."""
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
    assert sh.needs_details(closed, {closed["key"]: {"id": "R2", "status": "تم التعزيز على التنفيذ", "day": "2026-10-20"}}) is False


def test_the_recording_keeps_the_user_number_and_who_held_the_request_when_it_was_read():
    item = sh.grid_rows(HEAD, [grid_row()])[0]
    records, index = {}, {}
    sh.fold_history(records, index, [{"grid": item, "id": "R1", "history": [
        event("2026-10-20T09:00:00", "assigned", "موزع اختبار", "700401")]}], CFG, "2026-10-20T09:05:00")
    assert records["R1"]["user_no"] == "700301"
    assert index[item["key"]]["holder"] == "مستخدم اول"
    assert sh.grid_state([item], {}, "2026-10-20T09:05:00", CFG)[item["key"]]["user_no"] == "700301"


def test_right_now_never_shows_the_previous_holders_clock():
    """The request's age counts from its FIRST assignment whoever holds it; the time with the security user holding
    it now is only shown once their assignment is read (never the previous holder's)."""
    item = sh.grid_rows(HEAD, [grid_row(executor="مستخدم ثان")])[0]
    grid = sh.grid_state([item], {}, "2026-10-20T10:00:00", CFG)
    index = {item["key"]: {"id": "R1", "status": "اسناد لمستخدم السرية", "holder": "مستخدم اول"}}
    open_recs = {"R1": {"id": "R1", "stage": "open", "first_assigned_at": "2026-10-20T07:00:00",
                        "assigned_at": "2026-10-20T08:00:00", "holder": "مستخدم ثان"}}
    row = sh.right_now(grid, open_recs, index, CFG, "2026-10-20T10:00:00")["open_now"][0]
    assert row["minutes"] == 180.0 and row["holder_minutes"] is None and row["first_assigned_at"] == "2026-10-20T07:00:00"
    index[item["key"]]["holder"] = "مستخدم ثان"
    row = sh.right_now(grid, open_recs, index, CFG, "2026-10-20T10:00:00")["open_now"][0]
    assert row["minutes"] == 180.0 and row["holder_minutes"] == 120.0


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


# ------------------------------------------------------------------------- live tickets: the first assignment
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


def test_the_first_assignment_is_noted_on_a_live_ticket_and_its_sent_date_age_is_kept():
    """Like the owner's sla_monitor.py, the live SLA runs from «تاريخ الارسال للسرية»: the first assignment the
    history recorded is noted next to it, never its clock."""
    open_recs = {"R1": {"id": "R1", "stage": "open", "user_no": "700301", "system": "Flexcube",
                        "request_type": "اضافة صلاحية", "holder": "مستخدم اول", "assigned_at": "2026-10-21T08:30:00"}}
    reqs = [dict(live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم اول", sent="2026-10-19T08:00:00"),
                 age_minutes=2940, status="breached"),
            dict(live("b", "700999", "Flexcube", "اضافة صلاحية", "مستخدم اول", sent="2026-10-21T08:00:00"),
                 age_minutes=60, status="reminder")]
    assert sh.note_assignments(reqs, open_recs) == 1
    a, b = reqs
    assert a["assigned_at"] == "2026-10-21T08:30:00" and a["holder_since"] is None
    assert a["age_minutes"] == 2940 and a["status"] == "breached"        # still from the sent date
    assert b["assigned_at"] is None and b["age_minutes"] == 60 and b["status"] == "reminder"


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


def test_an_open_request_is_assigned_by_a_reviewer_and_that_line_is_its_assignment():
    """The assignment line names the assigner («Checker security» here), never the security user who holds the
    request - the holder comes from the inquiry grid's «مستخدم السرية». The live ticket notes that assignment."""
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
    sh.note_assignments(reqs, records)
    assert reqs[0]["assigned_at"] == "2026-10-21T10:23:48"


def test_finished_requests_are_read_first_so_the_days_numbers_are_complete():
    """When a scan cannot open every row, the FINISHED requests come first - confirmed, then rejected: one read gives
    every stamp - then the ones awaiting the reviewer, then the ones with a security user (the Live tab counts from
    «تاريخ الارسال للسرية» and needs no history); a request still with the branch has no SLA step to read."""
    order = [sh.detail_priority(s, CFG) for s in ("تم التعزيز على التنفيذ", "رفض", "تم التنفيذ",
                                                  "اسناد لمستخدم السرية", "معلق", "تسجيل")]
    assert order[0] < order[1] < order[2] < order[3] == order[4] and order[5] is None


# --------------------------------------------------- the first assignment of a live ticket, and since when its holder has it
def test_a_live_ticket_notes_its_first_assignment_and_since_when_the_holder_has_it():
    """After a re-assignment the live ticket shows its first «اسناد لمستخدم السرية» and, as ``holder_since``, when the
    security user holding it now got it; its age and status stay the sent date's."""
    open_recs = {"R1": {"id": "R1", "stage": "open", "user_no": "700301", "system": "Flexcube",
                        "request_type": "اضافة صلاحية", "holder": "مستخدم ثان",
                        "first_assigned_at": "2026-10-21T07:00:00", "assigned_at": "2026-10-21T08:30:00"}}
    reqs = [dict(live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم ثان", sent="2026-10-21T08:20:00"),
                 age_minutes=40, status="within")]
    sh.note_assignments(reqs, open_recs)
    assert reqs[0]["assigned_at"] == "2026-10-21T07:00:00" and reqs[0]["holder_since"] == "2026-10-21T08:30:00"
    assert reqs[0]["age_minutes"] == 40 and reqs[0]["status"] == "within"


def _history(*lines):
    return sh.events_from_table(HISTORY_HEAD, [["7005%02d" % i, "مستخدم %d" % i, "", at, status, ""]
                                               for i, (at, status) in enumerate(lines)], CFG_OLD)


# NBE OpsHub's saved settings listed «قيد الإجراء» as an assignment (the migrated sla_records.json still does)
CFG_OLD = sla_records.clean_config({"statuses": {"assigned": ["اسناد لمستخدم السرية", "قيد الإجراء"]}})


def test_in_progress_is_never_an_assignment_even_in_old_saved_settings():
    """The security user's clock runs from «اسناد لمستخدم السرية» to «تم التنفيذ»: a «قيد الإجراء» line in between must
    not restart it, although the settings migrated from NBE OpsHub list it under the assignment."""
    assert sh.step_of("قيد الإجراء", CFG_OLD) is None and sh.step_of("تحت التنفيذ", CFG_OLD) is None
    lc = sh.lifecycle(_history(("21/10/2026 08:00:00", "اسناد لمستخدم السرية"), ("21/10/2026 09:30:00", "قيد الإجراء"),
                               ("21/10/2026 10:00:00", "تم التنفيذ"), ("21/10/2026 10:30:00", "تم التعزيز على التنفيذ")),
                      CFG_OLD)
    minutes = sh.stage_minutes(lc)
    assert lc["assigned_at"] == "2026-10-21T08:00:00" and minutes["work"] == 120.0 and minutes["sla"] == 150.0


def test_every_final_state_ends_the_request_whatever_its_wording():
    """The whole request runs from the first assignment to its final state: the reviewer's confirmation (executed or
    not) or the reply / password sent - in the settings' words or the portal's others."""
    for status in ("تم التعزيز على التنفيذ", "تم تسليم الرد", "تم ارسال الرد", "تم الارسال عن طريق ال SMS",
                   "تم ارسال كلمة المرور عن طريق ال sms", "تم اعادة ارسال كلمة المرور عن طريق ال SMS"):
        assert sh.step_of(status, CFG_OLD) == "confirmed_executed", status
        assert sh.grid_stage(status, CFG_OLD) == "closed", status
    assert sh.step_of("تم التعزيز علي عدم التنفيذ", CFG_OLD) == "confirmed_not"
    for status in ("تم التعزيز من قبل مشرف الفرع", "تم التنفيذ", "لم يتم التنفيذ", "اسناد لمستخدم السرية", "مطلوب تعديل"):
        assert sh.step_of(status, CFG_OLD) not in ("confirmed_executed", "confirmed_not"), status
    lc = sh.lifecycle(_history(("21/10/2026 08:00:00", "اسناد لمستخدم السرية"), ("21/10/2026 09:00:00", "تم ارسال الرد")),
                      CFG_OLD)
    assert lc["stage"] == "closed" and lc["confirmed_at"] == "2026-10-21T09:00:00" and sh.stage_minutes(lc)["sla"] == 60.0


def test_a_record_read_under_the_old_rules_is_corrected_from_its_timeline():
    """A request recorded while «قيد الإجراء» counted as an assignment gets its clocks again from its kept timeline."""
    wrong = sla_records.default_config()
    wrong["statuses"]["in_progress"] = ["تحت التنفيذ"]                    # the old rules: «قيد الإجراء» assigns
    wrong["statuses"]["assigned"] = ["اسناد لمستخدم السرية", "قيد الإجراء"]
    lines = [("21/10/2026 08:00:00", "اسناد لمستخدم السرية"), ("21/10/2026 09:30:00", "قيد الإجراء"),
             ("21/10/2026 10:00:00", "تم التنفيذ")]
    events = sh.events_from_table(HISTORY_HEAD, [["7005%02d" % i, "م", "", at, st, ""] for i, (at, st) in enumerate(lines)],
                                  wrong)
    rec = {"id": "R1", "source": sh.SOURCE, **sh.lifecycle(events, wrong)}
    assert rec["assigned_at"] == "2026-10-21T09:30:00"                      # what the old rules recorded
    fixed = sh.rederive(rec, CFG)
    assert fixed["assigned_at"] == "2026-10-21T08:00:00" and sh.stage_minutes(fixed)["work"] == 120.0
    assert sh.rederive(fixed, CFG) is fixed                                 # nothing left to correct


def test_a_row_the_grid_shows_finished_is_read_again_once_when_its_record_is_not():
    item = sh.grid_rows(HEAD, [grid_row(status="تم ارسال الرد")])[0]
    index = {item["key"]: {"id": "R1", "status": "تم ارسال الرد", "day": ""}}
    assert sh.needs_details(item, index, CFG) is True                       # recorded before «تم ارسال الرد» was final
    index[item["key"]]["checked"] = "تم ارسال الرد"
    assert sh.needs_details(item, index, CFG) is False                      # read once at this status: not again
    index[item["key"]] = {"id": "R1", "status": "تم ارسال الرد", "day": "2026-10-21"}
    assert sh.needs_details(item, index, CFG) is False                      # closed in its record: nothing to do


def test_the_analysis_counts_the_requests_the_portal_lists_before_their_history_is_read():
    rows = [grid_row(status="اسناد لمستخدم السرية"), grid_row(status="تم التنفيذ", user_no="700302"),
            grid_row(status="تم ارسال الرد", user_no="700303"), grid_row(status="رفض", user_no="700304"),
            grid_row(status="تسجيل", user_no="700305"), grid_row(status="تم التعزيز على التنفيذ", user_no="700306")]
    items = sh.grid_rows(HEAD, rows)
    grid = sh.grid_state(items, {}, "2026-10-21T09:00:00", CFG)
    index = {items[5]["key"]: {"id": "R6"}}                                 # read: it is a record, not a stub
    stubs = sh.grid_stubs(grid, index, {}, CFG)
    assert len(stubs) == 4                                                  # the branch-only row has no SLA yet
    a = sh.analyze(stubs, CFG, now="2026-10-21T09:00:00")
    assert a["meta"]["requests"] == 4 and a["buckets"] == {"pre": 0, "open": 1, "awaiting": 1, "closed": 1, "rejected": 1}
    assert a["overall"]["n"] == 1 and a["overall"]["measured"] == 0         # counted, no clock until it is read
    assert all(line["id"] == "" and line["unread"] for line in a["requests_list"])


def test_right_now_lists_every_live_ticket_even_the_ones_the_inquiry_does_not():
    """«Open now» is complete: the inquiry grid's open rows plus every Live ticket (Re-Assign page) they do not list -
    each ticket once."""
    items = sh.grid_rows(HEAD, [grid_row(status="اسناد لمستخدم السرية")])
    grid = sh.grid_state(items, {}, "2026-10-21T09:00:00", CFG)
    tickets = [live("a", "700301", "Flexcube", "اضافة صلاحية", "مستخدم اول"),       # the grid's row: counted once
               dict(live("b", "700399", "T24", "حذف", "مستخدم ثان", sent="2026-10-14T08:00:00"),
                    assigned_at="2026-10-14T09:00:00")]                         # older than the inquiry window
    out = sh.right_now(grid, {}, {}, CFG, "2026-10-21T09:00:00", live=tickets)
    assert out["now"]["open"] == 2 and out["now"]["live_only"] == 1
    older = next(o for o in out["open_now"] if o["source"] == "live")
    assert older["holder"] == "مستخدم ثان" and older["minutes"] == 10080.0 and older["breached"]


# ------------------------------------------------- where a grid row stands: a wording the settings do not name
def test_a_wording_the_settings_do_not_name_is_with_the_security_team_only_when_a_security_user_holds_it():
    """«Open with a security user» must never count a registration: a wording the status list does not name (the grid's
    own words, e.g. «في انتظار اعتماد مشرف الفرع») is open only when the grid names its «مستخدم السرية»."""
    assert sh.grid_stage("في انتظار اعتماد مشرف الفرع", CFG, "") == "pre"
    assert sh.grid_stage("في انتظار اعتماد مشرف الفرع", CFG, "مستخدم اول") == "open"
    assert sh.grid_stage("تحت التنفيذ", CFG, "مستخدم اول") == "open"            # a current-status word, with its holder
    assert sh.grid_stage("تم التسجيل", CFG, "") == "pre"                          # contains «تسجيل»
    assert sh.grid_stage("اسناد لمستخدم السرية", CFG, "") == "open"               # an assignment, whoever is named
    assert sh.grid_stage("تم التعزيز على التنفيذ", CFG, "") == "closed"
    assert sh.grid_stage("رفض", CFG, "") == "rejected"                            # rejected by the assigner
    rows = sh.grid_rows(HEAD, [grid_row(status="في انتظار اعتماد مشرف الفرع", executor=""),
                               grid_row(status="في انتظار اعتماد مشرف الفرع", executor="", system="T24"),
                               grid_row(status="تسجيل", executor="")])
    grid = sh.grid_state(rows, {}, "2026-10-20T09:00:00", CFG)
    assert [g["stage"] for g in grid.values()] == ["pre", "pre", "pre"]
    assert sh.right_now(grid, {}, {}, CFG, "2026-10-20T09:00:00")["now"]["open"] == 0
    assert sh.unknown_wordings(grid, CFG) == [("في انتظار اعتماد مشرف الفرع", 2)]


def test_the_scans_remember_when_a_row_reached_the_security_team_and_got_its_status():
    """``since`` / ``entered`` are only taken as the real moment when the scans watched it happen: at the first read
    nothing is known; a row that moves from the branch to a security user later, or a new row inside the window the
    previous read covered, is."""
    first = sh.grid_rows(HEAD, [grid_row(status="تم التعزيز على التنفيذ", date="19-10-2026"),
                                grid_row(status="تسجيل", executor="", system="T24")])
    g1 = sh.grid_state(first, {}, "2026-10-20T09:00:00", CFG, None)
    done, branch = g1[first[0]["key"]], g1[first[1]["key"]]
    assert done["since_known"] is False and done["entered"] == ""
    moved = sh.grid_rows(HEAD, [grid_row(status="تم التعزيز على التنفيذ", date="19-10-2026"),
                                grid_row(status="اسناد لمستخدم السرية", system="T24"),
                                grid_row(status="اسناد لمستخدم السرية", system="Card", date="20-10-2026")])
    g2 = sh.grid_state(moved, g1, "2026-10-20T11:00:00", CFG, "2026-10-18")
    assert g2[moved[0]["key"]]["since_known"] is False                            # still the first read's status
    t24 = g2[moved[1]["key"]]
    assert t24["since"] == t24["entered"] == "2026-10-20T11:00:00" and t24["since_known"] is True
    new = g2[moved[2]["key"]]
    assert new["entered"] == "2026-10-20T11:00:00" and new["since_known"] is True
    older = sh.grid_rows(HEAD, [grid_row(status="اسناد لمستخدم السرية", system="Old", date="10-10-2026")])
    g3 = sh.grid_state(older, g2, "2026-10-20T12:00:00", CFG, "2026-10-18")      # the window reached back to it
    assert g3[older[0]["key"]]["entered"] == "" and g3[older[0]["key"]]["since_known"] is False
    assert branch["stage"] == "pre"


# ------------------------------------------------------------- «نموذج (7)»: the clocks start at «الفترة من»
def test_a_period_moves_the_clocks_to_its_start_and_keeps_the_real_assignment():
    lc = {"first_assigned_at": "2026-10-19T09:45:00", "assigned_at": "2026-10-19T09:45:00", "pool_at": "2026-10-19T09:20:00",
          "executed_at": "2026-10-21T08:40:00", "confirmed_at": "2026-10-21T09:05:00"}
    got = sh.apply_period(dict(lc), "2026-10-21T08:00:00")
    assert got["first_assigned_at"] == got["assigned_at"] == "2026-10-21T08:00:00"
    assert got["first_assigned_real_at"] == "2026-10-19T09:45:00" and got["pool_at"] == "2026-10-21T08:00:00"
    assert sh.apply_period(dict(lc), "2026-10-18T08:00:00")["first_assigned_at"] == "2026-10-19T09:45:00"   # started before
    early = sh.apply_period(dict(lc, executed_at="2026-10-20T10:00:00"), "2026-10-21T08:00:00")
    assert early["assigned_at"] == "2026-10-19T09:45:00"                         # executed before it: never past it
    assert sh.period_start("21/10/2026", "08:30") == "2026-10-21T08:30:00"
    assert sh.period_start("21/10/2026 10:15") == "2026-10-21T10:15:00" and sh.period_start("") == ""
    assert sh.is_period_form("نموذج (7)") and sh.is_period_form("نموذج (٧)") and not sh.is_period_form("نموذج (17)")


def test_a_form_7_row_read_before_periods_were_taken_is_read_once_more():
    item = sh.grid_rows(HEAD, [grid_row(status="تم التعزيز على التنفيذ")])[0]
    item["form"] = "نموذج (7)"
    entry = {"id": "R7", "status": "تم التعزيز على التنفيذ", "day": "2026-10-20", "checked": "تم التعزيز على التنفيذ"}
    assert sh.needs_details(item, {item["key"]: entry}) is True
    assert sh.needs_details(item, {item["key"]: entry}, period=False) is False
    assert sh.needs_details(item, {item["key"]: dict(entry, pv=sh.PERIOD_VERSION)}) is False
    item["form"] = "نموذج 5"
    assert sh.needs_details(item, {item["key"]: entry}) is False


def test_rederive_keeps_the_period():
    rec = {"id": "R7", "period_from": "2026-10-21T08:00:00", "trail": [
        ["2026-10-19T09:45:00", "assigned", "اسناد لمستخدم السرية", "", "", ""],
        ["2026-10-21T08:40:00", "assigned", "قيد الإجراء", "", "", ""],         # read under old rules: not a step
        ["2026-10-21T08:50:00", "executed", "تم التنفيذ", "", "", ""]]}
    got = sh.rederive(rec, CFG)
    assert got["assigned_at"] == "2026-10-21T08:00:00" and got["assigned_real_at"] == "2026-10-19T09:45:00"


# ------------------------------------------------------------------- one day, the way the daily summary counts it
def _day_world():
    """Today 2026-10-21. Read: R1 assigned and executed today, confirmed today; R2 «لم يتم التنفيذ» today, awaiting the
    reviewer; R3 read while open yesterday, confirmed «على عدم التنفيذ» today (not read again yet); R4 assigned
    yesterday, still open. Not read: a row confirmed today, one rejected today by the assigner, and 3034-like branch
    rows registered today."""
    rows = [grid_row(status="تم التعزيز على التنفيذ", system="A", date="21-10-2026"),
            grid_row(status="لم يتم التنفيذ", system="B", date="19-10-2026"),
            grid_row(status="تم التعزيز على عدم التنفيذ", system="C", date="20-10-2026"),
            grid_row(status="اسناد لمستخدم السرية", system="D", date="20-10-2026"),
            grid_row(status="تم التعزيز على التنفيذ", system="E", date="21-10-2026"),
            grid_row(status="رفض", executor="", system="F", date="21-10-2026")]
    rows += [grid_row(status="في انتظار اعتماد مشرف الفرع", executor="", system=f"X{i}", date="21-10-2026")
             for i in range(6)]
    items = sh.grid_rows(HEAD, rows)
    prev = sh.grid_state(items[:4] + items[6:], {}, "2026-10-21T08:00:00", CFG, None)     # this morning's read
    prev[items[2]["key"]].update(status="اسناد لمستخدم السرية", stage="open")    # R3 was open when read
    grid = sh.grid_state(items, prev, "2026-10-21T13:00:00", CFG, "2026-10-19")
    for k in (items[0]["key"], items[1]["key"]):                                   # A and B changed today
        grid[k].update(since="2026-10-21T10:00:00", since_known=True)
    index = {items[0]["key"]: {"id": "R1", "status": "تم التعزيز على التنفيذ", "day": "2026-10-21"},
             items[1]["key"]: {"id": "R2", "status": "لم يتم التنفيذ"},
             items[2]["key"]: {"id": "R3", "status": "اسناد لمستخدم السرية"},
             items[3]["key"]: {"id": "R4", "status": "اسناد لمستخدم السرية"}}
    records = {
        "R1": {"id": "R1", "source": sh.SOURCE, "stage": "closed", "outcome": "completed", "final_outcome": "executed",
               "first_assigned_at": "2026-10-21T09:00:00", "assigned_at": "2026-10-21T09:00:00",
               "executed_at": "2026-10-21T09:40:00", "exec_outcome": "executed", "confirmed_at": "2026-10-21T10:00:00"},
        "R2": {"id": "R2", "source": sh.SOURCE, "stage": "awaiting", "outcome": "in_progress",
               "first_assigned_at": "2026-10-21T08:00:00", "assigned_at": "2026-10-21T08:00:00",
               "executed_at": "2026-10-21T09:00:00", "exec_outcome": "not_executed"},
        "R3": {"id": "R3", "source": sh.SOURCE, "stage": "open", "outcome": "in_progress",
               "first_assigned_at": "2026-10-20T15:00:00", "assigned_at": "2026-10-20T15:00:00"},
        "R4": {"id": "R4", "source": sh.SOURCE, "stage": "open", "outcome": "in_progress",
               "first_assigned_at": "2026-10-20T16:00:00", "assigned_at": "2026-10-20T16:00:00"}}
    return records, grid, index


def test_the_analysis_counts_a_request_that_moved_on_at_its_new_stage():
    records, grid, index = _day_world()
    cur = sh.with_current_stage(records, grid, index, CFG)
    assert cur["R3"]["stage"] == "closed" and cur["R3"]["moved"] and cur["R3"]["final_outcome"] == "not_executed"
    assert "moved" not in cur["R4"]
    a = sh.analyze({"R3": cur["R3"]}, CFG, now="2026-10-21T13:00:00")
    assert a["buckets"]["closed"] == 1 and a["overall"]["n"] == 1 and a["requests_list"][0]["waiting_minutes"] is None


# ------------------------------------------- a request is dated by what the scans WATCHED, never by a first sighting
def test_the_grid_keeps_only_what_still_matters_so_every_page_stays_fast():
    prev = {"a": {"request_date": "2026-10-20T00:00:00", "stage": "closed"},       # recent: kept
            "b": {"request_date": "2026-10-01T00:00:00", "stage": "closed"},       # old and finished: dropped
            "c": {"request_date": "2026-10-01T00:00:00", "stage": "open"},         # old, still open: followed
            "d": {"request_date": "2026-10-19T00:00:00", "stage": "open"}}         # its day read again: replaced
    fresh = {"e": {"request_date": "2026-10-19T00:00:00", "stage": "closed"}}
    got = sh.merge_grid(prev, fresh, {"2026-10-19"}, "2026-10-19")
    assert sorted(got) == ["a", "c", "e"]


# ------------------------------------- the security team's numbers: the portal's own inquiry, by «تاريخ الطلب»
def _inquiry_day():
    """21/10: two confirmed executed, one confirmed not executed, one executed waiting, one not executed waiting, one
    rejected by the assigner, two still with a security user, one still at the branch; and one row of another day."""
    rows = [grid_row(status="تم التعزيز على التنفيذ", system="A1", date="21-10-2026", executor="أ"),
            grid_row(status="تم التعزيز على التنفيذ", system="A2", date="21-10-2026", executor="ب"),
            grid_row(status="تم التعزيز على عدم التنفيذ", system="A3", date="21-10-2026", executor="أ"),
            grid_row(status="تم التنفيذ", system="A4", date="21-10-2026", executor="ب"),
            grid_row(status="لم يتم التنفيذ", system="A5", date="21-10-2026", executor="أ"),
            grid_row(status="رفض", system="A6", date="21-10-2026", executor=""),
            grid_row(status="اسناد لمستخدم السرية", system="A7", date="21-10-2026", executor="أ"),
            grid_row(status="معلق", system="A8", date="21-10-2026", executor="ب"),
            grid_row(status="تسجيل", system="A9", date="21-10-2026", executor=""),
            grid_row(status="تم التعزيز على التنفيذ", system="B1", date="20-10-2026", executor="أ")]
    items = sh.grid_rows(HEAD, rows)
    items[5]["assigner"] = "موزع"
    grid = sh.grid_state(items, {}, "2026-10-21T13:00:00", CFG)
    for g in grid.values():
        g["team"] = "Flex Cube"
    records = {"R1": {"id": "R1", "source": sh.SOURCE, "stage": "closed", "request_date": "2026-10-21", "team": "Flex Cube",
                      "first_assigned_at": "2026-10-21T09:05:00", "assigned_at": "2026-10-21T09:05:00",
                      "executed_at": "2026-10-21T09:35:00", "exec_outcome": "executed", "executor": "أ",
                      "confirmed_at": "2026-10-21T10:15:00", "final_outcome": "executed"}}
    return grid, records


def test_the_security_teams_numbers_are_the_portals_inquiry_of_those_dates_by_status_now():
    grid, records = _inquiry_day()
    live = [{"security_user": "أ"}, {"security_user": "ب"}, {"security_user": "ب"}]
    n = sh.status_numbers(grid, records, CFG, "2026-10-21", "2026-10-21", live=live, live_team=lambda r: "Flex Cube")
    assert (n["came"], n["executed"], n["not_executed"], n["rejected"], n["open"]) == (8, 3, 2, 1, 2)
    assert n["came"] == n["executed"] + n["not_executed"] + n["rejected"] + n["open"]
    assert (n["waiting"], n["confirmed"], n["confirmed_executed"], n["confirmed_not"]) == (2, 3, 2, 1)
    assert n["pre"] == 1 and n["now"] == 3                    # the registration never counts; Live = 3 now
    people = {p["person"]: p for p in n["by_person"]}
    assert (people["أ"]["came"], people["أ"]["executed"], people["أ"]["not_executed"], people["أ"]["open"]) == (4, 1, 2, 1)
    assert people["موزع"]["rejected"] == 1 and people["ب"]["now"] == 2
    assert people["أ"]["exec_n"] == 1 and people["أ"]["exec_median_minutes"] == 30.0
    # the hours come from the read histories only: R1 came 09:05, was executed 09:35 and confirmed 10:15
    hours = {h["hour"]: h for h in n["hours"]}
    assert (hours[9]["came"], hours[9]["done"], hours[10]["confirmed"]) == (1, 1, 1)
    assert n["untimed"] == {"came": 7, "done": 5, "confirmed": 2}
    assert n["times"]["execution"]["n"] == 1 and n["times"]["whole"]["median_minutes"] == 70.0
    both = sh.status_numbers(grid, records, CFG, "2026-10-20", "2026-10-21")
    assert both["came"] == 9 and both["executed"] == 4                                 # the 20th's row too
