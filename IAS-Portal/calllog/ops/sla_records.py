# -*- coding: utf-8 -*-
"""Live SLA recording for the Ticketing module.

A security request moves through three stages, and TWO different people act on it:

    assigned  «اسناد لمستخدم السرية»
       |                                    EMPLOYEE clock (assignment -> execution)
       v
    executed  «تم التنفيذ» / «لم يتم التنفيذ»   <- done by the EMPLOYEE  = مستخدم السرية (the executor)
       |                                    MANAGER clock (execution -> confirmation)
       v
    confirmed «تم التعزيز على التنفيذ» / «...عدم التنفيذ» / «تم تسليم الرد» / SMS
                                              <- done by the CHECKER/MANAGER = مراجع السرية

No single portal export has the in-between «تم التنفيذ» timestamp, so this records it LIVE: every snapshot (from the
live portal scrape and/or a configured export feed) carries each request's CURRENT status + the two people; the first
time a request is seen to reach a stage we stamp the time (the row's own time if it has one, else "now") and the
person. From the accumulated records both clocks are computed, split by executor (مستخدم السرية) and by checker
(مراجع السرية). Pure functions over plain dicts; the config (SLA target, status labels, column mapping) comes from
Settings. docs/HUB-DESIGN.md section 5.1.
"""
import csv
from datetime import datetime, timedelta

DEFAULT_SLA_MINUTES = 180            # 3 hours, assignment -> final confirmation
DEFAULT_MAX_RECORDS = 100000         # cap the JSON store; oldest CLOSED requests are dropped past this
DEFAULT_RETENTION_DAYS = 365         # drop CLOSED requests older than this (open/awaiting are always kept)
_FMT = "%Y-%m-%dT%H:%M:%S"


def default_config():
    """The SLA-recording config an administrator edits in Settings (status labels + which column holds what)."""
    return {
        "sla_minutes": DEFAULT_SLA_MINUTES,
        "max_records": DEFAULT_MAX_RECORDS,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "statuses": {
            "assigned": ["اسناد لمستخدم السرية", "قيد الإجراء"],
            "executed": ["تم التنفيذ"],                               # employee ran it
            "not_executed": ["لم يتم التنفيذ"],                        # employee could not run it
            "confirmed_executed": ["تم التعزيز على التنفيذ", "تم تسليم الرد",
                                   "تم ارسال كلمة المرور عن طريق ال SMS", "تم اعادة ارسال كلمة المرور عن طريق ال SMS"],
            "confirmed_not": ["تم التعزيز على عدم التنفيذ"],            # manager confirmed it was not executed
        },
        "fields": {"id": "ReqID", "status": "الحالة", "executor": "مستخدم السرية", "checker": "مراجع السرية",
                   "system": "النظام", "requester": "اسم الموظف",
                   "assigned_at": "", "executed_at": "", "confirmed_at": ""},   # optional time columns, "" = stamp now
    }


def _clean(text):
    return " ".join(str(text or "").replace("﻿", "").split())


_TIME_FIELDS = ("assigned_at", "executed_at", "confirmed_at")


def clean_config(raw):
    """Validate/normalise the SLA-recording config an admin edits in Settings: the SLA target (minutes), the Arabic
    status labels for each stage, the column mapping, and an optional export-feed path. Missing parts fall back to the
    defaults, so a partial save never breaks the recording."""
    raw = raw if isinstance(raw, dict) else {}
    d = default_config()

    def _int(value, fallback, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return fallback

    out = {"sla_minutes": _int(raw.get("sla_minutes"), d["sla_minutes"], 1, 20160),
           "max_records": _int(raw.get("max_records"), d["max_records"], 1000, 2000000),
           "retention_days": _int(raw.get("retention_days"), d["retention_days"], 0, 3650),
           "statuses": {}, "fields": {}, "feed_path": _clean(raw.get("feed_path"))[:500]}
    rs = raw.get("statuses") if isinstance(raw.get("statuses"), dict) else {}
    for key, fallback in d["statuses"].items():
        vals = rs.get(key)
        cleaned = [_clean(s) for s in vals if isinstance(s, str) and _clean(s)] if isinstance(vals, list) else None
        out["statuses"][key] = cleaned if cleaned else list(fallback)
    rf = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    for key, fallback in d["fields"].items():
        v = _clean(rf.get(key))[:100]
        out["fields"][key] = v if v else ("" if key in _TIME_FIELDS else fallback)
    return out


def _stage(status, cfg):
    """(stage, outcome) for a status: stage in assigned|executed|confirmed; outcome executed|not_executed|None."""
    s = _clean(status)
    st = cfg.get("statuses") or {}
    if s in (st.get("confirmed_executed") or []):
        return "confirmed", "executed"
    if s in (st.get("confirmed_not") or []):
        return "confirmed", "not_executed"
    if s in (st.get("executed") or []):
        return "executed", "executed"
    if s in (st.get("not_executed") or []):
        return "executed", "not_executed"
    return "assigned", None                     # assigned / in-progress / anything not yet acted on


_RANK = {"assigned": 0, "executed": 1, "confirmed": 2}


def record_scan(records, rows, cfg, now, system_team=None):
    """Upsert each scanned/exported request into ``records`` (keyed by request id), stamping the first time it is seen
    to reach the executed and confirmed stages, with the person (مستخدم السرية / مراجع السرية) who did it.
    ``rows`` are the raw dicts; ``cfg`` the config; ``now`` an ISO string; returns the same ``records`` dict."""
    f = cfg.get("fields") or {}
    now = _clean(now) or datetime.now().strftime(_FMT)
    tmap = {str(k).strip().lower(): v for k, v in (system_team or {}).items()}

    def col(row, key):
        name = f.get(key)
        return _clean(row.get(name)) if name else ""

    for row in rows:
        rid = col(row, "id")
        if not rid:
            continue
        stage, outcome = _stage(col(row, "status"), cfg)
        rec = records.get(rid)
        if rec is None:
            system = col(row, "system")
            rec = records[rid] = {"id": rid, "system": system, "team": tmap.get(system.lower(), "Open System"),
                                  "requester": col(row, "requester"), "first_seen": now, "assigned_at": col(row, "assigned_at") or now,
                                  "executor": "", "executed_at": "", "exec_outcome": "",
                                  "checker": "", "confirmed_at": "", "final_outcome": "", "last_status": "", "last_seen": now}
        rec["last_status"] = col(row, "status")
        rec["last_seen"] = now
        exec_time = col(row, "executed_at")
        # the employee clock is stamped only when we actually SEE the executed stage (or a feed gives its time) -
        # a request first seen already confirmed skipped our view of the execution, so we leave it unknown.
        if stage == "executed" and not rec["executed_at"]:
            rec["executed_at"] = exec_time or now
            rec["executor"] = col(row, "executor")
            rec["exec_outcome"] = outcome or "executed"
        elif stage == "confirmed" and not rec["executed_at"] and exec_time:
            rec["executed_at"] = exec_time
            rec["executor"] = col(row, "executor")
            rec["exec_outcome"] = outcome or "executed"
        if stage == "confirmed" and not rec["confirmed_at"]:    # first time confirmed by the checker/manager
            rec["confirmed_at"] = col(row, "confirmed_at") or now
            rec["checker"] = col(row, "checker")
            rec["final_outcome"] = outcome or "executed"
    return records


def _minutes(a, b):
    try:
        ta, tb = datetime.strptime(_clean(a)[:19], _FMT), datetime.strptime(_clean(b)[:19], _FMT)
    except (ValueError, TypeError):
        return None
    m = (tb - ta).total_seconds() / 60.0
    return m if m >= 0 else None


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


class _Agg:
    __slots__ = ("n", "executed", "not_executed", "sla_ok", "sla_n", "_mins")

    def __init__(self):
        self.n = self.executed = self.not_executed = self.sla_ok = self.sla_n = 0
        self._mins = []

    def add(self, outcome, minutes, sla_minutes):
        self.n += 1
        if outcome == "not_executed":
            self.not_executed += 1
        else:
            self.executed += 1
        if minutes is not None:
            self.sla_n += 1
            self._mins.append(minutes)
            if minutes <= sla_minutes:
                self.sla_ok += 1

    def out(self, **extra):
        med = round(sorted(self._mins)[len(self._mins) // 2], 1) if self._mins else None
        return dict(extra, n=self.n, executed=self.executed, not_executed=self.not_executed,
                    executed_pct=_pct(self.executed, self.n), sla_pct=_pct(self.sla_ok, self.sla_n), median_minutes=med)


def analyze(records, cfg):
    """Build the live SLA metrics from the recorded lifecycles: live buckets (open / awaiting confirmation / closed),
    the EMPLOYEE clock (assignment->execution) by مستخدم السرية, the MANAGER clock (execution->confirmation) by
    مراجع السرية, plus by team / system / day. ``records`` is the dict from record_scan."""
    sla = int((cfg or {}).get("sla_minutes") or DEFAULT_SLA_MINUTES)
    recs = list((records or {}).values())

    buckets = {"open": 0, "awaiting": 0, "closed": 0}
    overall = _Agg()                          # full clock assignment -> confirmation (closed only)
    emp_clock, mgr_clock = _Agg(), _Agg()
    by_exec, by_check, by_team, by_system, by_day = ({} for _ in range(5))
    first_day = last_day = None

    for r in recs:
        if r.get("confirmed_at"):
            stage = "closed"
        elif r.get("executed_at"):
            stage = "awaiting"                # executed by the employee, waiting for the manager to confirm
        else:
            stage = "open"
        buckets[stage] += 1

        emp_min = _minutes(r.get("assigned_at"), r.get("executed_at")) if r.get("executed_at") else None
        mgr_min = _minutes(r.get("executed_at"), r.get("confirmed_at")) if r.get("confirmed_at") else None
        total_min = _minutes(r.get("assigned_at"), r.get("confirmed_at")) if r.get("confirmed_at") else None
        team, system = r.get("team") or "Open System", r.get("system") or "—"

        if r.get("executed_at"):
            emp_clock.add(r.get("exec_outcome"), emp_min, sla)
            by_exec.setdefault(r.get("executor") or "—", _Agg()).add(r.get("exec_outcome"), emp_min, sla)
        if stage == "closed":
            overall.add(r.get("final_outcome"), total_min, sla)
            mgr_clock.add(r.get("final_outcome"), mgr_min, sla)
            by_check.setdefault(r.get("checker") or "—", _Agg()).add(r.get("final_outcome"), mgr_min, sla)
            by_team.setdefault(team, _Agg()).add(r.get("final_outcome"), total_min, sla)
            by_system.setdefault(system, _Agg()).add(r.get("final_outcome"), total_min, sla)
            day = _clean(r.get("confirmed_at"))[:10]
            if day:
                by_day.setdefault(day, _Agg()).add(r.get("final_outcome"), total_min, sla)
                first_day = day if first_day is None or day < first_day else first_day
                last_day = day if last_day is None or day > last_day else last_day

    def top(groups, key, n=25):
        out = [g.out(**{key: name}) for name, g in groups.items()]
        out.sort(key=lambda r: -r["n"])
        return out[:n] if n else out

    return {
        "meta": {"requests": len(recs), "sla_minutes": sla, "date_from": first_day, "date_to": last_day},
        "buckets": buckets,
        "overall": overall.out(),
        "employee_clock": emp_clock.out(),      # assignment -> execution (by مستخدم السرية)
        "manager_clock": mgr_clock.out(),       # execution -> confirmation (by مراجع السرية)
        "by_employee": top(by_exec, "executor"),     # the executor: who ran the request
        "by_checker": top(by_check, "checker"),      # the reviewer/manager: who confirmed it
        "by_team": top(by_team, "team", 0),
        "by_system": top(by_system, "system"),
        "by_day": [by_day[d].out(date=d) for d in sorted(by_day)],
    }


def filter_records(records, filt):
    """Narrow the records to those matching the filter BEFORE analyze(), so every metric/table reflects the subset:
    a date range (by the assignment day), the executor (مستخدم السرية), the checker (مراجع السرية), and the team.
    Blank values match everything. ``filt`` is a plain dict of strings (typically the request query)."""
    filt = filt or {}
    dfrom, dto = _clean(filt.get("date_from"))[:10], _clean(filt.get("date_to"))[:10]
    execu, check, team = (_clean(filt.get(k)).lower() for k in ("executor", "checker", "team"))
    if not (dfrom or dto or execu or check or team):
        return records or {}
    out = {}
    for rid, r in (records or {}).items():
        day = _clean(r.get("assigned_at"))[:10]
        if dfrom and (not day or day < dfrom):
            continue
        if dto and (not day or day > dto):
            continue
        if execu and _clean(r.get("executor")).lower() != execu:
            continue
        if check and _clean(r.get("checker")).lower() != check:
            continue
        if team and _clean(r.get("team")).lower() != team:
            continue
        out[rid] = r
    return out


def filter_options(records):
    """The distinct values to offer in the SLA-Analysis filter bar (executors, checkers, teams) and the full date
    range (earliest / latest assignment day), computed from the given records (already team-scoped for a manager)."""
    execs, checks, teams, days = set(), set(), set(), []
    for r in (records or {}).values():
        if _clean(r.get("executor")):
            execs.add(_clean(r.get("executor")))
        if _clean(r.get("checker")):
            checks.add(_clean(r.get("checker")))
        if _clean(r.get("team")):
            teams.add(_clean(r.get("team")))
        day = _clean(r.get("assigned_at"))[:10]
        if day:
            days.append(day)
    return {"executors": sorted(execs), "checkers": sorted(checks), "teams": sorted(teams),
            "date_from": min(days) if days else None, "date_to": max(days) if days else None}


def read_feed_rows(path, encoding="utf-8"):
    """Yield the rows of a configured export feed (an ALL_*.csv / requests_*.csv the portal refreshes), tolerating a
    header BOM and whitespace, so ``record_scan`` can fold the feed into the live recording."""
    with open(path, encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [_clean(c) for c in (reader.fieldnames or [])]
        for row in reader:
            yield {_clean(k): v for k, v in row.items()}


def housekeep(records, cfg, now):
    """Keep the JSON store bounded: drop CLOSED requests older than ``retention_days`` (by confirmed_at), and if still
    over ``max_records`` drop the oldest closed ones. OPEN / AWAITING (still active) requests are always kept. Edits
    ``records`` in place and returns how many were removed. ``now`` is an ISO string."""
    cfg = cfg or {}
    retention, cap = int(cfg.get("retention_days") or 0), int(cfg.get("max_records") or 0)
    before = len(records)
    if retention > 0:
        try:
            cutoff = (datetime.strptime(_clean(now)[:10], "%Y-%m-%d") - timedelta(days=retention)).strftime("%Y-%m-%d")
        except ValueError:
            cutoff = None
        if cutoff:
            for rid in [k for k, r in records.items() if r.get("confirmed_at") and _clean(r["confirmed_at"])[:10] < cutoff]:
                del records[rid]
    if cap and len(records) > cap:
        closed = sorted((k for k, r in records.items() if r.get("confirmed_at")),
                        key=lambda k: _clean(records[k].get("confirmed_at")))
        for rid in closed[:len(records) - cap]:
            del records[rid]
    return before - len(records)
