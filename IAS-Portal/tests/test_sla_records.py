# -*- coding: utf-8 -*-
"""Live SLA recording (calllog/ops/sla_records.py): a request is recorded across snapshots; the EMPLOYEE clock
(assignment -> execution, by مستخدم السرية) and the MANAGER clock (execution -> confirmation, by مراجع السرية) are
stamped the first time each stage is seen."""
from calllog.ops import sla_records as sr

CFG = sr.default_config()
ASSIGN = "اسناد لمستخدم السرية"
EXEC = "تم التنفيذ"
NOEXEC = "لم يتم التنفيذ"
CONFIRM = "تم التعزيز على التنفيذ"
CONFIRM_NO = "تم التعزيز على عدم التنفيذ"


def row(rid, status, execr="", checkr="", system="Flexcube"):
    return {"ReqID": rid, "الحالة": status, "مستخدم السرية": execr, "مراجع السرية": checkr, "النظام": system}


def test_lifecycle_records_both_clocks_and_people():
    rec, tmap = {}, {"flexcube": "Flex Cube"}
    sr.record_scan(rec, [row("1", ASSIGN)], CFG, "2026-10-17T10:00:00", tmap)
    sr.record_scan(rec, [row("1", EXEC, execr="Alice")], CFG, "2026-10-17T10:30:00", tmap)              # +30m, by Alice
    sr.record_scan(rec, [row("1", CONFIRM, execr="Alice", checkr="Omar")], CFG, "2026-10-17T11:00:00", tmap)  # +30m, by Omar
    r = rec["1"]
    assert r["executor"] == "Alice" and r["checker"] == "Omar" and r["team"] == "Flex Cube"
    assert r["assigned_at"] == "2026-10-17T10:00:00" and r["executed_at"] == "2026-10-17T10:30:00" and r["confirmed_at"] == "2026-10-17T11:00:00"
    m = sr.analyze(rec, CFG)
    assert m["buckets"] == {"open": 0, "awaiting": 0, "closed": 1}
    assert m["employee_clock"]["median_minutes"] == 30.0        # assignment -> execution
    assert m["manager_clock"]["median_minutes"] == 30.0         # execution -> confirmation
    assert m["overall"]["median_minutes"] == 60.0 and m["overall"]["sla_pct"] == 100.0
    assert m["by_employee"][0]["executor"] == "Alice" and m["by_employee"][0]["n"] == 1
    assert m["by_checker"][0]["checker"] == "Omar" and m["by_checker"][0]["n"] == 1
    assert m["by_team"][0]["team"] == "Flex Cube"


def test_live_buckets_open_awaiting_closed():
    rec = {}
    sr.record_scan(rec, [row("o", ASSIGN), row("a", EXEC, execr="A"),
                         row("c", CONFIRM, execr="A", checkr="C")], CFG, "2026-10-17T09:00:00")
    m = sr.analyze(rec, CFG)
    assert m["buckets"] == {"open": 1, "awaiting": 1, "closed": 1}
    # "a" (awaiting, execution observed) is in the employee clock but not overall; "c" (first seen already confirmed,
    # execution never observed) is in overall but not the employee clock.
    assert m["employee_clock"]["n"] == 1 and m["overall"]["n"] == 1


def test_not_executed_outcome_splits():
    rec = {}
    sr.record_scan(rec, [row("x", NOEXEC, execr="A")], CFG, "2026-10-17T09:00:00")
    sr.record_scan(rec, [row("x", CONFIRM_NO, execr="A", checkr="C")], CFG, "2026-10-17T09:30:00")
    m = sr.analyze(rec, CFG)
    assert m["by_employee"][0]["not_executed"] == 1 and m["overall"]["not_executed"] == 1
    assert m["manager_clock"]["n"] == 1 and m["manager_clock"]["median_minutes"] == 30.0


def test_seen_already_confirmed_leaves_employee_clock_unknown():
    rec = {}
    sr.record_scan(rec, [row("z", CONFIRM, checkr="C")], CFG, "2026-10-17T12:00:00")   # first sight = already confirmed
    assert rec["z"]["executed_at"] == "" and rec["z"]["confirmed_at"] == "2026-10-17T12:00:00"
    m = sr.analyze(rec, CFG)
    assert m["buckets"]["closed"] == 1 and m["employee_clock"]["n"] == 0               # execution never observed


def test_clean_config_normalises_and_fills_defaults():
    cfg = sr.clean_config({"sla_minutes": 120, "statuses": {"executed": ["تم التنفيذ", "  "], "confirmed_executed": []},
                           "fields": {"executor": "sec_user", "assigned_at": "DateF"}, "feed_path": "  C:\\x.csv  "})
    assert cfg["sla_minutes"] == 120
    assert cfg["statuses"]["executed"] == ["تم التنفيذ"]                       # blank line dropped
    assert cfg["statuses"]["confirmed_executed"]                               # empty list -> fell back to default
    assert cfg["fields"]["executor"] == "sec_user" and cfg["fields"]["assigned_at"] == "DateF"
    assert cfg["fields"]["checker"] == sr.default_config()["fields"]["checker"]   # missing -> default
    assert cfg["fields"]["executed_at"] == ""                                  # optional time column defaults to blank
    assert cfg["feed_path"] == "C:\\x.csv"
    assert cfg["max_records"] == sr.DEFAULT_MAX_RECORDS and cfg["retention_days"] == sr.DEFAULT_RETENTION_DAYS
    assert sr.clean_config({"sla_minutes": "oops"})["sla_minutes"] == sr.DEFAULT_SLA_MINUTES
    clamped = sr.clean_config({"max_records": 50, "retention_days": 99999})       # clamped to the allowed range
    assert clamped["max_records"] == 1000 and clamped["retention_days"] == 3650


def test_housekeep_retention_keeps_active():
    rec = {}
    for i, day in enumerate(("01", "02", "03")):
        rec[f"c{i}"] = {"id": f"c{i}", "assigned_at": f"2026-01-{day}T08:00:00", "executed_at": f"2026-01-{day}T08:30:00",
                        "exec_outcome": "executed", "confirmed_at": f"2026-01-{day}T09:00:00", "final_outcome": "executed"}
    rec["open1"] = {"id": "open1", "assigned_at": "2026-01-01T08:00:00"}
    rec["await1"] = {"id": "await1", "assigned_at": "2026-01-01T08:00:00", "executed_at": "2026-01-01T08:30:00", "exec_outcome": "executed"}
    removed = sr.housekeep(rec, {"retention_days": 1, "max_records": 1000}, "2026-01-05T12:00:00")
    assert removed == 3 and set(rec) == {"open1", "await1"}          # old closed dropped; active ones always kept


def test_housekeep_cap_drops_oldest_closed():
    rec = {"open1": {"id": "open1", "assigned_at": "2026-02-01T08:00:00"}}
    for i in range(5):
        rec[f"c{i}"] = {"id": f"c{i}", "assigned_at": "2026-02-01T08:00:00",
                        "confirmed_at": f"2026-02-0{i + 1}T09:00:00", "final_outcome": "executed"}
    removed = sr.housekeep(rec, {"retention_days": 0, "max_records": 3}, "2026-02-10T12:00:00")
    assert removed == 3 and set(rec) == {"c3", "c4", "open1"}        # the 3 oldest closed are dropped


def _scan_req(key, system="Flexcube", owner="Alice", sent="2026-10-17T08:00:00"):
    return {"key": key, "security_user": owner, "sent_at": sent,
            "fields": {"النظام": system, "اسم الموظف": "Someone"}}


def test_record_live_requests_opens_and_ages():
    rec, tmap = {}, {"flexcube": "Flex Cube"}
    sr.record_live_requests(rec, [_scan_req("k1"), _scan_req("k2", owner="(Unassigned)")], CFG,
                            "2026-10-17T11:00:00", tmap)
    assert rec["k1"]["source"] == sr.SCAN_SOURCE and rec["k1"]["assignee"] == "Alice" and rec["k1"]["team"] == "Flex Cube"
    assert rec["k2"]["assignee"] == ""                                     # "(Unassigned)" normalised away
    assert rec["k1"]["assigned_at"] == "2026-10-17T08:00:00" and not rec["k1"]["executed_at"]
    m = sr.analyze(rec, CFG, now="2026-10-17T13:00:00")                    # 5h after assignment (> 180-min target)
    assert m["buckets"] == {"open": 2, "awaiting": 0, "closed": 0}
    assert m["live"]["open"] == 2 and m["live"]["oldest_minutes"] == 300.0
    assert m["live"]["breached"] == 2                                      # both open past the 3h target
    m2 = sr.analyze(rec, CFG, now="2026-10-17T10:00:00")                   # 2h after assignment (within target)
    assert m2["live"]["breached"] == 0 and m2["live"]["oldest_minutes"] == 120.0
    assert [g["executor"] for g in m2["live"]["by_employee"]] == ["Alice", "—"]
    assert m2["live"]["by_employee"][0]["n"] == 1                          # one open request held by Alice


def test_a_request_that_leaves_the_queue_is_closed_by_its_security_user():
    rec = {}
    sr.record_live_requests(rec, [_scan_req("k1"), _scan_req("k2", owner="Bob")], CFG, "2026-10-17T09:00:00")
    # the next scan no longer shows k1: its security user (Alice) acted on it
    sr.record_live_requests(rec, [_scan_req("k2", owner="Bob")], CFG, "2026-10-17T09:45:00")
    assert sr.mark_left_queue(rec, {"k2"}, "2026-10-17T09:45:00") == 1
    assert rec["k1"]["executed_at"] == "2026-10-17T09:45:00" and rec["k1"]["executor"] == "Alice"
    assert rec["k1"]["exec_outcome"] == "" and rec["k2"]["executed_at"] == ""
    assert sr.mark_left_queue(rec, {"k2"}, "2026-10-17T10:00:00") == 0          # already closed: idempotent
    m = sr.analyze(rec, CFG, now="2026-10-17T10:00:00")
    assert m["buckets"] == {"open": 1, "awaiting": 0, "closed": 1}
    assert m["overall"]["n"] == 1 and m["overall"]["median_minutes"] == 105.0  # 08:00 sent -> 09:45 left the queue
    assert m["overall"]["unknown"] == 1 and m["overall"]["executed_pct"] == 0.0  # executed or not: not known
    assert m["by_handler"][0]["handler"] == "Alice" and m["employee_clock"]["n"] == 1
    assert m["manager_clock"]["n"] == 0                                          # no confirmation seen
    assert m["live"]["closed_today"] == 1 and m["live"]["closed_today_by_employee"][0]["executor"] == "Alice"


def test_a_request_sent_back_to_the_queue_is_open_again():
    rec = {}
    sr.record_live_requests(rec, [_scan_req("k1")], CFG, "2026-10-17T09:00:00")
    sr.mark_left_queue(rec, set(), "2026-10-17T09:15:00")
    sr.record_live_requests(rec, [_scan_req("k1")], CFG, "2026-10-17T09:30:00")   # back on the Re-Assign page
    assert rec["k1"]["executed_at"] == "" and sr.analyze(rec, CFG)["buckets"]["open"] == 1


def test_housekeep_drops_old_live_closures_too():
    rec = {}
    sr.record_live_requests(rec, [_scan_req("old", sent="2026-01-01T08:00:00")], CFG, "2026-01-01T09:00:00")
    sr.mark_left_queue(rec, set(), "2026-01-01T10:00:00")
    sr.record_live_requests(rec, [_scan_req("now", sent="2026-03-01T08:00:00")], CFG, "2026-03-01T09:00:00")
    assert sr.housekeep(rec, {"retention_days": 30, "max_records": 1000}, "2026-03-01T12:00:00") == 1
    assert set(rec) == {"now"}                                                   # the open one is always kept


# the portal's own export, as it comes out (US month-first dates with AM/PM, one line per system of a request)
EXPORT = ("ReqID2,FStatusID1,DateF,SStatusID,DateT1,EmpNo,ApplicationName\n"
          "5001,اسناد لمستخدم السرية,10/17/2026 08:00:00 AM,تم التعزيز على التنفيذ,10/17/2026 10:30:00 AM,700101,Flexcube\n"
          "5001,اسناد لمستخدم السرية,10/17/2026 08:00:00 AM,تم التعزيز على التنفيذ,10/17/2026 10:30:00 AM,700101,GL\n"
          "5002,اسناد لمستخدم السرية,10/17/2026 09:00:00 AM,تم التعزيز على عدم التنفيذ,10/17/2026 01:00:00 PM,700102,DWP\n"
          "5003,اسناد لمستخدم السرية,10/17/2026 09:10:00 AM,حالة جديدة غير معروفة,10/17/2026 09:40:00 AM,700102,DWP\n")


def test_decode_csv_handles_a_bom_and_the_arabic_code_page():
    assert sr.decode_csv(("\ufeff" + EXPORT).encode("utf-8"))[0]["ReqID2"] == "5001"
    rows = sr.decode_csv(EXPORT.replace(",", ";").encode("cp1256"))
    assert rows[2]["SStatusID"] == "تم التعزيز على عدم التنفيذ" and len(rows) == 4


def test_portal_export_is_recognised_and_its_dates_read():
    rows = sr.decode_csv(EXPORT.encode("utf-8"))
    assert sr.is_portal_export(rows) and not sr.is_portal_export([{"ReqID": "1", "الحالة": "x"}])
    assert sr.export_time("10/17/2026 01:00:00 PM") == "2026-10-17T13:00:00"
    assert sr.export_time("١٠/١٧/٢٠٢٦ ٠١:٠٠:٠٠ م") == "2026-10-17T13:00:00"     # Arabic digits and ص/م
    assert sr.export_time("garbage") == ""


def test_apply_export_records_closed_requests_with_outcome_and_full_clock():
    rec = {}
    out = sr.apply_export(rec, sr.decode_csv(EXPORT.encode("utf-8")), CFG, "2026-10-18T08:00:00",
                          {"flexcube": "Flex Cube", "dwp": "Card Management"})
    assert out["rows"] == 4 and out["added"] == 3                              # 5001 has two system lines
    assert rec["5001"]["assigned_at"] == "2026-10-17T08:00:00" and rec["5001"]["confirmed_at"] == "2026-10-17T10:30:00"
    assert rec["5001"]["executor"] == "700101" and rec["5001"]["team"] == "Flex Cube"
    assert rec["5002"]["final_outcome"] == "not_executed"
    assert rec["5003"]["final_outcome"] == "executed"                          # an unknown closing label still closes
    m = sr.analyze(rec, CFG)
    assert m["buckets"] == {"open": 0, "awaiting": 0, "closed": 3}
    assert m["overall"]["executed"] == 2 and m["overall"]["not_executed"] == 1
    assert {h["handler"]: h["n"] for h in m["by_handler"]} == {"700101": 1, "700102": 2}
    assert {t["team"] for t in m["by_team"]} == {"Flex Cube", "Card Management"}
    assert sr.apply_export(rec, sr.decode_csv(EXPORT.encode("utf-8")), CFG, "2026-10-18T09:00:00")["added"] == 0


def test_the_export_replaces_the_live_closure_of_the_same_request_only():
    rec = {}
    # the live closure of request 5001 (assigned 17/10 08:00 on Flexcube, like the export line) and one of 18/10
    sr.record_live_requests(rec, [_scan_req("live17", sent="2026-10-17T08:00:00"),
                                  _scan_req("live18", sent="2026-10-18T08:30:00")], CFG, "2026-10-18T09:00:00")
    sr.mark_left_queue(rec, set(), "2026-10-18T09:15:00")
    rec["live17"]["executed_at"] = "2026-10-17T10:00:00"
    out = sr.apply_export(rec, sr.decode_csv(EXPORT.encode("utf-8")), CFG, "2026-10-18T10:00:00")
    assert out["replaced_live"] == 1 and "live17" not in rec and "live18" in rec      # no double counting, nothing lost
    assert rec["5001"]["executor"] == "Alice" and rec["5001"]["executed_at"] == "2026-10-17T10:00:00"


def test_feed_with_explicit_time_columns():
    cfg = sr.default_config()
    cfg["fields"]["assigned_at"], cfg["fields"]["confirmed_at"] = "DateF", "DateT1"
    r = {"ReqID": "9", "الحالة": CONFIRM, "DateF": "2026-10-17T08:00:00", "DateT1": "2026-10-17T09:30:00", "مراجع السرية": "C"}
    rec = {}
    sr.record_scan(rec, [r], cfg, "2026-10-17T20:00:00")
    assert rec["9"]["assigned_at"] == "2026-10-17T08:00:00" and rec["9"]["confirmed_at"] == "2026-10-17T09:30:00"
    m = sr.analyze(rec, cfg)
    assert m["overall"]["median_minutes"] == 90.0 and m["overall"]["sla_pct"] == 100.0


def test_the_right_now_strip_uses_the_live_tabs_breach_time():
    """An open request is breached from the Live tickets tab's breach minute on (>=), not from the closed-request target."""
    rec = {}
    reqs = [{"key": "a", "security_user": "Alice", "sent_at": "2026-10-17T08:00:00", "fields": {"النظام": "Flexcube"}},   # 120 min
            {"key": "b", "security_user": "Alice", "sent_at": "2026-10-17T09:30:00", "fields": {"النظام": "Flexcube"}}]   # 30 min
    sr.record_live_requests(rec, reqs, CFG, "2026-10-17T10:00:00")
    live = sr.analyze(rec, CFG, now="2026-10-17T10:00:00", live_breach_minutes=120)["live"]
    assert live["breached"] == 1 and live["breach_minutes"] == 120 and live["by_employee"][0]["breached"] == 1
    plain = sr.analyze(rec, CFG, now="2026-10-17T10:00:00")["live"]                 # no live limit: the 3 h target
    assert plain["breached"] == 0 and plain["breach_minutes"] == CFG["sla_minutes"]


def test_a_scan_after_the_clock_goes_back_is_still_recorded(tmp_path):
    """Summer time ends (24:00 -> 23:00): the repeated hour's scans look older than the last one, but are new scans."""
    from calllog.ops.store import OpsStore
    store = OpsStore(tmp_path)
    req = lambda key: {"key": key, "security_user": "Alice", "sent_at": "2026-10-29T22:00:00", "fields": {"النظام": "Flexcube"}}
    store.write_snapshot("sla", {"requests": [req("a"), req("b")]}, generated_at="2026-10-29T23:45:00")
    assert sr.fold_latest_scan(store) == 2
    store.write_snapshot("sla", {"requests": [req("b")]}, generated_at="2026-10-29T23:00:00")     # the hour again
    assert sr.fold_latest_scan(store) == 1
    recs = store.read_sla_records()["records"]
    assert recs["a"]["executed_at"] == "2026-10-29T23:00:00" and recs["b"]["executed_at"] == ""
    assert sr.fold_latest_scan(store) == 0                                                         # folded once only


def _left_queue(rec, key, user, sent, system, left_at):
    """A request on the page from ``sent`` on, scanned every 15 minutes, gone at the scan of ``left_at``."""
    from datetime import datetime, timedelta
    req = {"key": key, "security_user": user, "sent_at": sent, "fields": {"النظام": system}}
    at, end = datetime.fromisoformat(sent), datetime.fromisoformat(left_at)
    while at < end:
        sr.record_live_requests(rec, [req], CFG, at.isoformat(timespec="seconds"))
        at += timedelta(minutes=15)
    sr.mark_left_queue(rec, set(), left_at)


def test_the_export_merges_with_the_live_closure_of_the_same_request_and_keeps_the_others():
    """An export line and the live scan's closure of the SAME request (same assignment minute and application) become
    one record: the security user and the time it left the queue from the scan, the confirmation and outcome from the
    export. A live closure the export does not hold yet (not confirmed) stays; nothing is counted twice."""
    rec = {}
    _left_queue(rec, "h1", "مستخدم اول", "2026-10-19T09:00:00", "Flexcube", "2026-10-19T09:40:00")   # in the export
    _left_queue(rec, "h2", "مستخدم ثان", "2026-10-19T10:00:00", "CRM", "2026-10-19T10:30:00")         # not confirmed yet
    export = [{"ReqID2": "9001", "FStatusID1": ASSIGN, "DateF": "10/19/2026 09:00:12 AM", "SStatusID": CONFIRM,
               "DateT1": "10/19/2026 10:05:00 AM", "EmpNo": "700101", "ApplicationName": "Flexcube"},
              {"ReqID2": "9002", "FStatusID1": ASSIGN, "DateF": "10/18/2026 08:00:00 AM", "SStatusID": CONFIRM_NO,
               "DateT1": "10/18/2026 09:00:00 AM", "EmpNo": "700102", "ApplicationName": "DWP"}]
    out = sr.apply_export(rec, export, CFG, "2026-10-19T11:00:00", people={"700101": "مستخدم اول", "700102": "مستخدم ثالث"})
    assert out["replaced_live"] == 1 and set(rec) == {"9001", "9002", "h2"}
    merged = rec["9001"]
    assert merged["executor"] == "مستخدم اول" and merged["executed_at"] == "2026-10-19T09:40:00"
    assert merged["confirmed_at"] == "2026-10-19T10:05:00" and merged["final_outcome"] == "executed"
    assert rec["9002"]["executor"] == "مستخدم ثالث"                     # EmpNo shown as the person's security name
    m = sr.analyze(rec, CFG)
    assert m["buckets"]["closed"] == 3 and m["overall"]["n"] == 3
    assert m["manager_clock"]["median_minutes"] == 25.0                  # left the queue 09:40 -> confirmed 10:05
    again = sr.apply_export(rec, export, CFG, "2026-10-19T11:05:00")     # the same file once more: nothing changes
    assert again["added"] == 0 and again["replaced_live"] == 0 and len(rec) == 3


def test_a_request_without_a_sent_date_gets_no_invented_clock():
    """The Live tab does not count a request without a sent date in the SLA; the recording does not invent one."""
    rec = {}
    sr.record_live_requests(rec, [{"key": "n1", "security_user": "Alice", "sent_at": None, "fields": {}}], CFG,
                            "2026-10-17T10:00:00")
    assert rec["n1"]["assigned_at"] == "" and rec["n1"]["first_seen"] == "2026-10-17T10:00:00"
    live = sr.analyze(rec, CFG, now="2026-10-17T14:00:00", live_breach_minutes=120)["live"]
    assert live["open"] == 1 and live["breached"] == 0 and live["oldest_minutes"] is None
    assert sr.filter_records(rec, {"date_from": "2026-10-17", "date_to": "2026-10-17"}) == rec      # still in "today"
    sr.mark_left_queue(rec, set(), "2026-10-17T15:00:00")
    m = sr.analyze(rec, CFG)
    assert m["buckets"]["closed"] == 1 and m["overall"]["median_minutes"] is None                 # closed, not timed


def test_a_request_handled_after_the_last_scan_of_the_day_closes_at_that_scan_not_next_morning():
    """No scan runs at night: a request gone at the 08:00 scan closed after the 17:00 one - the night is not SLA time."""
    rec = {}
    req = {"key": "q", "security_user": "Alice", "sent_at": "2026-10-15T16:30:00", "fields": {"النظام": "Flexcube"}}
    sr.record_live_requests(rec, [req], CFG, "2026-10-15T16:45:00")
    sr.record_live_requests(rec, [req], CFG, "2026-10-15T17:00:00")               # the last scan of the day
    sr.mark_left_queue(rec, set(), "2026-10-18T08:00:00")                        # gone at Sunday's first scan
    assert rec["q"]["executed_at"] == "2026-10-15T17:00:00"
    near = {}
    sr.record_live_requests(near, [req], CFG, "2026-10-15T16:45:00")
    sr.mark_left_queue(near, set(), "2026-10-15T17:00:00")                       # a normal 15-minute gap: this scan
    assert near["q"]["executed_at"] == "2026-10-15T17:00:00"


def test_a_closure_seen_by_the_scan_is_judged_by_the_live_tabs_target_an_exported_one_by_the_full_clock():
    """Assignment -> leaving the security user's queue is the Live tab's clock (2 h); assignment -> the checker's
    confirmation (the export) is the full clock (3 h). One request never gets two verdicts."""
    rec = {}
    _left_queue(rec, "h1", "Alice", "2026-10-19T09:00:00", "Flexcube", "2026-10-19T11:30:00")       # 150 min
    rec["c1"] = {"id": "c1", "assigned_at": "2026-10-19T09:00:00", "confirmed_at": "2026-10-19T11:30:00",
                 "final_outcome": "executed", "executor": "Bob"}                                   # 150 min, exported
    m = sr.analyze(rec, CFG, now="2026-10-19T12:00:00", live_breach_minutes=120)
    assert m["meta"]["queue_minutes"] == 120 and m["meta"]["sla_minutes"] == 180
    assert m["overall"]["n"] == 2 and m["overall"]["sla_pct"] == 50.0
    handlers = {r["handler"]: r["sla_pct"] for r in m["by_handler"]}
    assert handlers == {"Alice": 0.0, "Bob": 100.0}


def test_open_rows_of_a_feed_are_not_counted_again_next_to_the_live_scan():
    rec = {}
    sr.record_live_requests(rec, [{"key": "k1", "security_user": "Alice", "sent_at": "2026-10-19T09:00:00",
                                   "fields": {"النظام": "Flexcube"}}], CFG, "2026-10-19T09:15:00")
    sr.record_scan(rec, [row("F1", ASSIGN)], CFG, "2026-10-19T09:20:00")        # the same request, open in a feed
    live = sr.analyze(rec, CFG, now="2026-10-19T10:00:00", live_breach_minutes=120)["live"]
    assert live["open"] == 1 and [g["executor"] for g in live["by_employee"]] == ["Alice"]
    feed_only = {}
    sr.record_scan(feed_only, [row("F1", ASSIGN)], CFG, "2026-10-19T09:20:00")   # no live scan at all: the feed counts
    assert sr.analyze(feed_only, CFG, now="2026-10-19T10:00:00")["live"]["open"] == 1


def test_status_labels_match_whatever_the_arabic_spelling():
    noexec = CONFIRM_NO.replace("على", "علي")                                    # ي for ى
    assert sr._stage(noexec, CFG) == ("confirmed", "not_executed")
    assert sr._stage("تم  التنـفيذ", CFG) == ("executed", "executed")             # tatweel, double space


HEAD = "ReqID2,FStatusID1,DateF,SStatusID,DateT1,EmpNo,ApplicationName\n"


def test_export_dates_day_first_or_with_fractions_are_read_and_unreadable_lines_are_skipped_not_stamped_now():
    day_first = sr.decode_csv((HEAD + f"1,{ASSIGN},19/10/2026 09:00:00 AM,{CONFIRM},19/10/2026 10:30:00 AM,700101,Flexcube\n"
                                      f"2,{ASSIGN},05/10/2026 09:00:00 AM,{CONFIRM},05/10/2026 09:30:00 AM,700101,Flexcube\n").encode())
    rec = {}
    out = sr.apply_export(rec, day_first, CFG, "2026-10-20T08:00:00")
    assert out["added"] == 2 and rec["2"]["assigned_at"] == "2026-10-05T09:00:00"        # 5 October, not 10 May
    sql = sr.decode_csv((HEAD + f"3,{ASSIGN},2026-10-19 09:00:00.000,{CONFIRM},2026-10-19 09:45:00.000,700101,CRM\n"
                                f"4,{ASSIGN},,{CONFIRM},10/19/2026 10:00:00 AM,700101,CRM\n"
                                f"5,{ASSIGN},10/19/2026 09:00:00 AM,حالة غير معروفة,,700101,CRM\n").encode())
    out = sr.apply_export(rec, sql, CFG, "2026-10-20T08:00:00")
    assert out["added"] == 1 and out["skipped"] == 2 and rec["3"]["confirmed_at"] == "2026-10-19T09:45:00"
    assert "4" not in rec and "5" not in rec                       # no phantom open request, no upload-time clock
    m = sr.analyze(rec, CFG, now="2026-10-20T08:00:00")
    assert m["live"]["open"] == 0 and m["buckets"]["open"] == 0


def test_an_exported_not_executed_decision_is_not_counted_as_executed():
    rows = sr.decode_csv((HEAD + f"6,{ASSIGN},10/19/2026 09:00:00 AM,{NOEXEC},10/19/2026 10:00:00 AM,700101,CRM\n"
                                 f"7,{ASSIGN},10/19/2026 09:00:00 AM,{CONFIRM_NO.replace('على', 'علي')},10/19/2026 10:00:00 AM,700101,CRM\n").encode())
    rec = {}
    sr.apply_export(rec, rows, CFG, "2026-10-20T08:00:00")
    assert rec["6"]["final_outcome"] == "not_executed" and rec["7"]["final_outcome"] == "not_executed"


def test_a_reimported_line_still_absorbs_the_live_closure_of_the_same_request():
    rec = {}
    line = sr.decode_csv((HEAD + f"8,{ASSIGN},10/19/2026 09:00:00 AM,{CONFIRM},10/19/2026 10:00:00 AM,700101,Flexcube\n").encode())
    sr.apply_export(rec, line, CFG, "2026-10-19T10:30:00")                        # the export came first
    _left_queue(rec, "h8", "Alice", "2026-10-19T09:00:00", "Flexcube", "2026-10-19T09:45:00")   # then the live closure
    out = sr.apply_export(rec, line, CFG, "2026-10-19T11:00:00")                  # the same export again
    assert set(rec) == {"8"} and out["replaced_live"] == 1 and rec["8"]["executor"] == "Alice"


def test_an_export_over_the_record_cap_says_what_it_dropped_and_a_reimport_adds_nothing():
    cfg = {**CFG, "max_records": 3}
    lines = "".join(f"{i},{ASSIGN},10/{i:02d}/2026 09:00:00 AM,{CONFIRM},10/{i:02d}/2026 10:00:00 AM,700101,CRM\n" for i in range(1, 6))
    rows = sr.decode_csv((HEAD + lines).encode())
    rec = {}
    out = sr.apply_export(rec, rows, cfg, "2026-10-20T08:00:00")
    assert out["added"] == 3 and out["dropped"] == 2 and sorted(rec) == ["3", "4", "5"]
    again = sr.apply_export(rec, rows, cfg, "2026-10-20T08:05:00")
    assert again["added"] == 0 and again["dropped"] == 0 and sorted(rec) == ["3", "4", "5"]
