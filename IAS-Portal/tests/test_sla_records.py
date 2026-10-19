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


def test_apply_sla_scan_replaces_scan_set_keeps_closed():
    """A later scan replaces the previous OPEN scan rows; a CLOSED feed record is left alone."""
    rec = {"closed1": {"id": "closed1", "assigned_at": "2026-10-16T08:00:00", "executed_at": "2026-10-16T08:30:00",
                       "exec_outcome": "executed", "confirmed_at": "2026-10-16T09:00:00", "final_outcome": "executed"}}
    sr.record_live_requests(rec, [_scan_req("k1"), _scan_req("k2")], CFG, "2026-10-17T10:00:00")
    assert set(rec) == {"closed1", "k1", "k2"}
    # emulate apply_sla_scan's "drop previous scan set, fold the new one"
    for rid in [k for k, r in rec.items() if r.get("source") == sr.SCAN_SOURCE]:
        del rec[rid]
    sr.record_live_requests(rec, [_scan_req("k3")], CFG, "2026-10-17T10:15:00")
    assert set(rec) == {"closed1", "k3"}                                   # old open rows gone, closed one kept


def test_feed_with_explicit_time_columns():
    cfg = sr.default_config()
    cfg["fields"]["assigned_at"], cfg["fields"]["confirmed_at"] = "DateF", "DateT1"
    r = {"ReqID": "9", "الحالة": CONFIRM, "DateF": "2026-10-17T08:00:00", "DateT1": "2026-10-17T09:30:00", "مراجع السرية": "C"}
    rec = {}
    sr.record_scan(rec, [r], cfg, "2026-10-17T20:00:00")
    assert rec["9"]["assigned_at"] == "2026-10-17T08:00:00" and rec["9"]["confirmed_at"] == "2026-10-17T09:30:00"
    m = sr.analyze(rec, cfg)
    assert m["overall"]["median_minutes"] == 90.0 and m["overall"]["sla_pct"] == 100.0
