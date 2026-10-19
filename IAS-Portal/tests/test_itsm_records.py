# -*- coding: utf-8 -*-
"""Live ITSM change-request recording (calllog/ops/itsm_records.py): a change order is recorded across scans; the first
time it is seen 'Implementation Completed' its EXECUTION TIME is stamped and its CONCLUSION set (completed / rollback /
pending)."""
from calllog.ops import itsm_records as ir

COMPLETED = "Implementation Completed"
OPEN = "Implementation Requests"
ROLLBACK = "Rollback Requests"


def scan(*cats_per_role):
    """Each positional arg is one role's {category: [(number, summary), ...]} -> one scan's engine snapshot data."""
    roles = []
    for i, cats in enumerate(cats_per_role):
        categories = {cat: {"count": len(rows), "rows": [{"number": n, "summary": s} for n, s in rows]}
                      for cat, rows in cats.items()}
        roles.append({"role": f"Role {i + 1}", "error": None, "categories": categories})
    return {"roles": roles}


def test_execution_time_and_conclusion_stamped_once():
    rec = {}
    ir.record_scan(rec, scan({OPEN: [("1001", "add user A")]}), "2026-10-18T09:00:00")
    assert rec["1001"]["conclusion"] == "pending" and rec["1001"]["completed_at"] == ""
    ir.record_scan(rec, scan({COMPLETED: [("1001", "add user A")]}), "2026-10-18T10:30:00")
    assert rec["1001"]["conclusion"] == "completed" and rec["1001"]["completed_at"] == "2026-10-18T10:30:00"
    ir.record_scan(rec, scan({COMPLETED: [("1001", "add user A")]}), "2026-10-18T12:00:00")
    assert rec["1001"]["completed_at"] == "2026-10-18T10:30:00"      # the execution time is not moved by a later scan


def test_execution_time_is_read_from_the_cr_field_and_kept():
    """The execution time is the CR's OWN scheduled time (a column on the request), not when we observed it done."""
    def one(cat, rows):
        return {"roles": [{"role": "R1", "error": None, "categories": {cat: {"count": len(rows), "rows": rows}}}]}
    rec = {}
    ir.record_scan(rec, one(OPEN, [{"number": "900001", "summary": "schedule change", "execution_time": "2026-10-20 22:00"}]), "2026-10-18T09:00:00")
    assert rec["900001"]["execution_time"] == "2026-10-20 22:00" and rec["900001"]["conclusion"] == "pending"
    # a later scan sees it completed (and no longer carries the field) — the scheduled execution time is kept, and the
    # OBSERVED completion time is recorded separately in completed_at.
    ir.record_scan(rec, one(COMPLETED, [{"number": "900001", "summary": "schedule change"}]), "2026-10-21T08:00:00")
    assert rec["900001"]["execution_time"] == "2026-10-20 22:00"
    assert rec["900001"]["conclusion"] == "completed" and rec["900001"]["completed_at"] == "2026-10-21T08:00:00"


def test_rollback_then_completed():
    rec = {}
    ir.record_scan(rec, scan({ROLLBACK: [("2002", "reverted")]}), "2026-10-18T09:00:00")
    assert rec["2002"]["conclusion"] == "rollback" and rec["2002"]["completed_at"] == ""
    ir.record_scan(rec, scan({COMPLETED: [("2002", "reverted")]}), "2026-10-18T11:00:00")
    assert rec["2002"]["conclusion"] == "completed" and rec["2002"]["completed_at"] == "2026-10-18T11:00:00"


def test_analyze_buckets_and_filter():
    rec = {}
    ir.record_scan(rec, scan({COMPLETED: [("1", "a"), ("2", "b")], OPEN: [("3", "c")], ROLLBACK: [("4", "d")]}),
                   "2026-10-18T10:00:00")
    m = ir.analyze(rec)
    assert m["buckets"] == {"pending": 1, "completed": 2, "rollback": 1} and m["meta"]["records"] == 4
    assert {r["number"] for r in m["records"]} == {"1", "2", "3", "4"}
    assert set(ir.filter_records(rec, {"conclusion": "completed"})) == {"1", "2"}
    assert set(ir.filter_records(rec, {"date_from": "2026-10-18", "date_to": "2026-10-18"})) == {"1", "2", "3", "4"}
    assert ir.filter_records(rec, {"date_from": "2026-10-19"}) == {}


def test_filter_by_role_and_options():
    rec = {}
    ir.record_scan(rec, scan({COMPLETED: [("1", "a")]}, {COMPLETED: [("2", "b")]}), "2026-10-18T10:00:00")
    assert set(ir.filter_records(rec, {"role": "Role 1"})) == {"1"}
    opt = ir.filter_options(rec)
    assert opt["roles"] == ["Role 1", "Role 2"] and opt["date_from"] == "2026-10-18"


def test_housekeep_keeps_pending_drops_old_terminal():
    rec = {}
    ir.record_scan(rec, scan({COMPLETED: [("old", "x")]}), "2026-01-01T10:00:00")
    ir.record_scan(rec, scan({OPEN: [("pending", "y")]}), "2026-01-01T10:00:00")
    removed = ir.housekeep(rec, {"retention_days": 30, "max_records": 1000}, "2026-10-18T10:00:00")
    assert removed == 1 and set(rec) == {"pending"}          # old completed dropped; the pending one is always kept
