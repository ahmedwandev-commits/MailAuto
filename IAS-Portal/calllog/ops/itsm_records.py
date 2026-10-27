# -*- coding: utf-8 -*-
"""Live ITSM change-request (CR) recording for the Ticketing / ITSM module.

The ITSM scoreboard groups change orders by category:

    Implementation Requests    -> waiting to be implemented (pending)
    Implementation Completed   -> executed            <- the CR was run
    Rollback Requests          -> sent back to the maker (reverted)

The EXECUTION TIME is a field ON the change request itself: the business sets WHEN the change should be executed (its
scheduled implementation time). It is read from the scanned CR row (``execution_time``) when the scrape provides that
column (configured by ``itsm.execution_time_header`` in Settings). The CONCLUSION is the terminal category the CR
reached (completed / rollback / still pending); for reference we also keep ``completed_at`` = the first time WE saw the
CR in "Implementation Completed" (the scoreboard carries no completion timestamp). From the accumulated records the ITSM
page shows, per day, every CR with its scheduled execution time and its conclusion.

Pure functions over plain dicts; mirrors ``sla_records`` (same live-recording idea applied to change orders).
"""
from datetime import datetime, timedelta

DEFAULT_MAX_RECORDS = 50000
DEFAULT_RETENTION_DAYS = 180
_FMT = "%Y-%m-%dT%H:%M:%S"


def _clean(text):
    return " ".join(str(text or "").replace("﻿", "").split())


def conclusion_for(category):
    """The CR conclusion a scoreboard category means: 'completed', 'rollback' or 'pending' (matched on the label)."""
    c = _clean(category).lower()
    if "completed" in c:
        return "completed"
    if "rollback" in c:
        return "rollback"
    return "pending"


def record_scan(records, data, now):
    """Fold one ITSM scan (the engine snapshot ``data`` = {roles:[{role, error, categories:{label:{count, rows}}}]})
    into ``records`` (keyed by Change Order #), stamping the first time a CR is seen completed (its execution time) and
    its conclusion. ``now`` is an ISO string (the scan time). Returns the same ``records`` dict."""
    now = _clean(now) or datetime.now().strftime(_FMT)
    for role in (data or {}).get("roles") or []:
        if role.get("error"):
            continue
        rname = _clean(role.get("role"))
        for category, d in (role.get("categories") or {}).items():
            if not isinstance(d, dict) or d.get("count") is None:
                continue
            concl = conclusion_for(category)
            for row in d.get("rows") or []:
                num = _clean(row.get("number"))
                if not num:
                    continue
                rec = records.get(num)
                if rec is None:
                    rec = records[num] = {"number": num, "summary": _clean(row.get("summary")), "role": rname,
                                          "category": category, "first_seen": now, "last_seen": now,
                                          "execution_time": _clean(row.get("execution_time")),
                                          "completed_at": "", "conclusion": "pending"}
                rec["last_seen"] = now
                rec["category"] = category
                if _clean(row.get("summary")):
                    rec["summary"] = _clean(row.get("summary"))
                if rname:
                    rec["role"] = rname
                ex = _clean(row.get("execution_time"))           # the CR's own scheduled execution time (business-set)
                if ex:
                    rec["execution_time"] = ex
                if concl == "completed" and not rec["completed_at"]:
                    rec["completed_at"] = now                    # observed: first time WE saw it in "Implementation Completed"
                    rec["conclusion"] = "completed"
                elif concl == "rollback" and rec["conclusion"] != "completed":
                    rec["conclusion"] = "rollback"
    return records


def recording_start(records):
    """The first scan of the recording (the earliest ``first_seen``), "" when nothing is recorded."""
    seen = [_clean(r.get("first_seen")) for r in (records or {}).values() if _clean(r.get("first_seen"))]
    return min(seen) if seen else ""


def completed_day(rec, started=""):
    """The day a CR's implementation was completed, as far as the scans know: the day it was first seen in
    «Implementation Completed» - "" when it already was there at the recording's very first scan (``started``,
    ``recording_start``): it was completed some day before, which nobody saw."""
    at = _clean((rec or {}).get("completed_at"))
    if not at:
        return ""
    first = _clean(rec.get("first_seen"))
    if started and first and first[:19] <= started[:19] and at[:19] == first[:19]:
        return ""
    return at[:10]


def seen_numbers(data):
    """{role: {"complete": bool, "numbers": {CR#, ...}}} for one scan's engine snapshot. A role is ``complete`` when it
    was read without error and every category listed all of its rows (``count == len(rows)``) - only then is a CR's
    ABSENCE meaningful (it really left the board, rather than the list being cut short)."""
    out = {}
    for role in (data or {}).get("roles") or []:
        rname = _clean(role.get("role"))
        if not rname:
            continue
        info = out.setdefault(rname, {"complete": True, "numbers": set()})
        if role.get("error"):
            info["complete"] = False
            continue
        for d in (role.get("categories") or {}).values():
            if not isinstance(d, dict) or d.get("count") is None:
                info["complete"] = False
                continue
            rows = d.get("rows") or []
            if len(rows) != d.get("count"):
                info["complete"] = False
            for row in rows:
                num = _clean(row.get("number"))
                if num:
                    info["numbers"].add(num)
    return out


def mark_missing_completed(records, data, now):
    """Finish-on-disappear: a CR we were tracking as PENDING that is no longer on its role's fully-read board has been
    implemented - stamp it completed. Only fires for a role whose scan was complete this round (so a short read never
    wrongly closes requests), and only for records already seen as pending (a rollback is stamped by record_scan before
    the CR leaves, so it is not overwritten). Returns how many were newly completed. Call AFTER record_scan."""
    now = _clean(now) or datetime.now().strftime(_FMT)
    seen = seen_numbers(data)
    done = 0
    for rec in records.values():
        if (rec.get("conclusion") or "pending") != "pending":
            continue
        role = seen.get(_clean(rec.get("role")))
        if role and role["complete"] and _clean(rec.get("number")) not in role["numbers"]:
            rec["conclusion"] = "completed"
            rec["completed_at"] = rec.get("completed_at") or now
            done += 1
    return done


def _day(rec):
    """A CR's primary day: its execution day when completed, else the day it was first seen."""
    return (_clean(rec.get("completed_at")) or _clean(rec.get("first_seen")))[:10]


def filter_records(records, filt):
    """Narrow records BEFORE analyze(): a date range (by the primary day), the role, and the conclusion. Blank values
    match everything. ``filt`` is a plain dict of strings (typically the request query)."""
    filt = filt or {}
    dfrom, dto = _clean(filt.get("date_from"))[:10], _clean(filt.get("date_to"))[:10]
    role = _clean(filt.get("role")).lower()
    concl = _clean(filt.get("conclusion")).lower()
    if not (dfrom or dto or role or concl):
        return records or {}
    out = {}
    for num, r in (records or {}).items():
        day = _day(r)
        if dfrom and (not day or day < dfrom):
            continue
        if dto and (not day or day > dto):
            continue
        if role and _clean(r.get("role")).lower() != role:
            continue
        if concl and (r.get("conclusion") or "pending") != concl:
            continue
        out[num] = r
    return out


def filter_options(records):
    """The distinct roles to offer in the ITSM filter bar, plus the full date range (earliest/latest primary day)."""
    roles, days = set(), []
    for r in (records or {}).values():
        if _clean(r.get("role")):
            roles.add(_clean(r.get("role")))
        day = _day(r)
        if day:
            days.append(day)
    return {"roles": sorted(roles), "conclusions": ["completed", "rollback", "pending"],
            "date_from": min(days) if days else None, "date_to": max(days) if days else None}


def analyze(records):
    """Buckets (pending / completed / rollback), the CR list (newest execution first) and per-day / per-role roll-ups."""
    recs = list((records or {}).values())
    buckets = {"pending": 0, "completed": 0, "rollback": 0}
    by_day, by_role = {}, {}
    for r in recs:
        concl = r.get("conclusion") or "pending"
        buckets[concl] = buckets.get(concl, 0) + 1
        day = _day(r)
        if day:
            b = by_day.setdefault(day, {"date": day, "completed": 0, "rollback": 0, "pending": 0, "n": 0})
            b[concl] = b.get(concl, 0) + 1
            b["n"] += 1
        role = _clean(r.get("role")) or "—"
        b = by_role.setdefault(role, {"role": role, "completed": 0, "rollback": 0, "pending": 0, "n": 0})
        b[concl] = b.get(concl, 0) + 1
        b["n"] += 1
    rows = sorted(recs, key=lambda r: (_clean(r.get("completed_at")) or _clean(r.get("first_seen"))), reverse=True)
    return {
        "meta": {"records": len(recs), "date_from": min(by_day) if by_day else None,
                 "date_to": max(by_day) if by_day else None},
        "buckets": buckets,
        "records": rows,
        "by_day": [by_day[d] for d in sorted(by_day)],
        "by_role": sorted(by_role.values(), key=lambda r: -r["n"]),
    }


def housekeep(records, cfg, now):
    """Keep the JSON store bounded: drop TERMINAL (completed / rollback) CRs older than ``retention_days`` (by their
    primary day), and if still over ``max_records`` drop the oldest terminal ones. PENDING CRs are always kept (still
    active). Edits ``records`` in place; returns how many were removed. ``now`` is an ISO string."""
    cfg = cfg or {}
    retention, cap = int(cfg.get("retention_days") or 0), int(cfg.get("max_records") or 0)
    before = len(records)
    terminal = lambda r: (r.get("conclusion") or "pending") in ("completed", "rollback")
    if retention > 0:
        try:
            cutoff = (datetime.strptime(_clean(now)[:19], _FMT) - timedelta(days=retention)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            cutoff = None
        if cutoff:
            for num in [n for n, r in records.items() if terminal(r) and _day(r) and _day(r) < cutoff]:
                del records[num]
    if cap and len(records) > cap:
        ordered = sorted(((n, r) for n, r in records.items() if terminal(r)), key=lambda kv: _day(kv[1]) or "")
        for num, _r in ordered[:max(0, len(records) - cap)]:
            del records[num]
    return before - len(records)


def fold_latest_scan(store, complete_when_missing=False):
    """Fold the latest ITSM scan (the ``itsm`` snapshot) into the change-request recording held by ``store``. The
    collector calls this right after it writes each scan (nothing is lost while no web page is open); the web side calls
    it again as a safety net. The snapshot is read and the records updated inside one store lock, and each scan is
    folded once (exact match on its time). Returns how many NEW change orders were added."""
    snap = store.read_snapshot("itsm")
    generated = (snap or {}).get("generated_at") or ""
    if not generated or not ((snap or {}).get("data") or {}).get("roles"):
        return 0
    if (store.read_itsm_records().get("last_generated") or "") == generated:
        return 0
    added = {"n": 0}

    def change(d):
        latest = store.read_snapshot("itsm") or {}
        stamp, data = latest.get("generated_at") or "", latest.get("data") or {}
        if not stamp or not data.get("roles") or (d.get("last_generated") or "") == stamp:
            return
        records = d.setdefault("records", {})
        before = len(records)
        record_scan(records, data, stamp)
        if complete_when_missing:
            mark_missing_completed(records, data, stamp)       # a pending CR that left the board is finished
        housekeep(records, {"max_records": DEFAULT_MAX_RECORDS, "retention_days": DEFAULT_RETENTION_DAYS}, stamp)
        d["last_generated"] = stamp
        added["n"] = len(records) - before
    store.update_itsm_records(change)
    return added["n"]
