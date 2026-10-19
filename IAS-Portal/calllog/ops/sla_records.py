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
import io
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
            if not rec["executor"] and col(row, "executor"):     # who handled it (an export names them), no clock
                rec["executor"] = col(row, "executor")
    return records


SCAN_SOURCE = "scan"          # records created from the LIVE Re-Assign scrape (vs the CSV export feed)


def record_live_requests(records, requests, cfg, now, system_team=None):
    """Fold a LIVE Re-Assign scan (the ``sla`` snapshot's ``requests``) into ``records``.

    The Re-Assign page only lists requests still WITH the security team, so every live row is at least at the ASSIGNED
    stage (the clock has started); a request's execution and confirmation leave that page and are picked up later from
    the export feed. Each live row therefore upserts an open record keyed by the scan's stable ``key`` and tagged
    ``source="scan"``; it stamps ``assigned_at`` from the request's sent date and remembers the system, the requester
    and the security user currently holding it (``assignee``). A request that is no longer on the page has been acted
    on by its security user - ``mark_left_queue`` records that as its close; one that shows up again (sent back to the
    queue) is reopened here. Returns ``records``."""
    f = cfg.get("fields") or {}
    now = _clean(now) or datetime.now().strftime(_FMT)
    tmap = {str(k).strip().lower(): v for k, v in (system_team or {}).items()}
    sys_name = f.get("system") or "النظام"
    req_name = f.get("requester") or "اسم الموظف"
    for r in requests or []:
        rid = _clean(r.get("key"))
        if not rid:
            continue
        flds = r.get("fields") or {}
        system = _clean(flds.get(sys_name))
        assignee = _clean(r.get("security_user"))
        if assignee in ("(Unassigned)", "-"):
            assignee = ""
        rec = records.get(rid)
        assigned_at = _clean(r.get("sent_at")) or now
        if rec is None:
            rec = records[rid] = {"id": rid, "system": system, "team": tmap.get(system.lower(), "Open System"),
                                  "requester": _clean(flds.get(req_name)), "first_seen": now, "assigned_at": assigned_at,
                                  "executor": "", "executed_at": "", "exec_outcome": "", "checker": "",
                                  "confirmed_at": "", "final_outcome": "", "last_status": "", "last_seen": now}
        if rec.get("source") == SCAN_SOURCE and rec.get("executed_at") and not rec.get("confirmed_at"):
            rec["executed_at"], rec["executor"], rec["exec_outcome"] = "", "", ""     # back in the queue: open again
        rec["source"] = SCAN_SOURCE
        rec["assignee"] = assignee
        if system:
            rec["system"] = system
            rec["team"] = tmap.get(system.lower(), rec.get("team") or "Open System")
        rec["last_status"] = assignee and "assigned" or "unassigned"
        rec["last_seen"] = now
    return records


def mark_left_queue(records, present_ids, now):
    """Close the live-scan requests that are no longer on the Re-Assign page: their security user has acted on them
    (executed or not - the queue does not say which), so stamp ``executed_at`` = this scan's time (accurate to one scan
    interval) and ``executor`` = the user who was holding it. Only open scan-sourced records are touched. Returns how
    many were closed."""
    now = _clean(now) or datetime.now().strftime(_FMT)
    present = {_clean(x) for x in present_ids or ()}
    closed = 0
    for rid, r in records.items():
        if r.get("source") != SCAN_SOURCE or r.get("executed_at") or r.get("confirmed_at") or rid in present:
            continue
        r["executed_at"] = now
        r["executor"] = r.get("assignee") or ""
        r["exec_outcome"] = ""                    # unknown: «تم التنفيذ» or «لم يتم التنفيذ»
        r["last_status"] = "left_queue"
        closed += 1
    return closed


def _scan_handled(r):
    """A live-scan request that has LEFT the Re-Assign queue: closed as far as the security user's clock goes."""
    return r.get("source") == SCAN_SOURCE and bool(r.get("executed_at")) and not r.get("confirmed_at")


def _closed_at(r):
    """When a request closed: its confirmation, or - for a live-scan request - when it left the queue ("" = open)."""
    return _clean(r.get("confirmed_at")) or (_clean(r.get("executed_at")) if _scan_handled(r) else "")


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
    __slots__ = ("n", "executed", "not_executed", "unknown", "sla_ok", "sla_n", "_mins")

    def __init__(self):
        self.n = self.executed = self.not_executed = self.unknown = self.sla_ok = self.sla_n = 0
        self._mins = []

    def add(self, outcome, minutes, sla_minutes):
        self.n += 1
        if outcome == "not_executed":
            self.not_executed += 1
        elif outcome:
            self.executed += 1
        else:
            self.unknown += 1                     # left the queue: executed or not is not known (yet)
        if minutes is not None:
            self.sla_n += 1
            self._mins.append(minutes)
            if minutes <= sla_minutes:
                self.sla_ok += 1

    def out(self, **extra):
        med = round(sorted(self._mins)[len(self._mins) // 2], 1) if self._mins else None
        return dict(extra, n=self.n, executed=self.executed, not_executed=self.not_executed, unknown=self.unknown,
                    executed_pct=_pct(self.executed, self.executed + self.not_executed),
                    sla_pct=_pct(self.sla_ok, self.sla_n), median_minutes=med)


def _live_groups(open_recs, group_by, out_key, sla, now):
    """[{<out_key>, n, oldest_minutes, breached}] of the OPEN requests grouped by ``group_by`` (the holding security
    user, or the team), worst (most breached, then longest waiting) first. ``breached`` counts open requests whose
    running clock already passed the SLA target. ``oldest_minutes`` is None until a server ``now`` is given (the stored
    records carry no running clock)."""
    groups = {}
    for r in open_recs:
        name = _clean(r.get(group_by)) or "—"
        age = _minutes(r.get("assigned_at"), now) if now else None
        g = groups.setdefault(name, {"n": 0, "oldest": None, "breached": 0})
        g["n"] += 1
        if age is not None:
            g["oldest"] = age if g["oldest"] is None else max(g["oldest"], age)
            if age > sla:
                g["breached"] += 1
    out = [{out_key: name, "n": g["n"], "oldest_minutes": g["oldest"], "breached": g["breached"]}
           for name, g in groups.items()]
    out.sort(key=lambda r: (-r["breached"], -(r["oldest_minutes"] or 0), -r["n"]))
    return out


def analyze(records, cfg, now=None, live_records=None):
    """Build the live SLA metrics from the recorded lifecycles: live buckets (open / awaiting confirmation / closed),
    the EMPLOYEE clock (assignment->execution) by مستخدم السرية, the MANAGER clock (execution->confirmation) by
    مراجع السرية, plus by team / system / day. ``records`` is the dict from record_scan / record_live_requests.
    A ``now`` (ISO string) ages the still-open requests so the live section shows how long each has been waiting.
    ``live_records`` overrides which records feed the "right now" open strip (the caller passes the date-unfiltered set,
    since a still-open request assigned on an earlier day must still count as open today)."""
    sla = int((cfg or {}).get("sla_minutes") or DEFAULT_SLA_MINUTES)
    now = _clean(now)
    recs = list((records or {}).values())
    live_recs = list((live_records if live_records is not None else records or {}).values())

    buckets = {"open": 0, "awaiting": 0, "closed": 0}
    overall = _Agg()                          # closed: assignment -> confirmation (or -> left the queue, live scan)
    emp_clock, mgr_clock = _Agg(), _Agg()
    by_exec, by_check, by_team, by_system, by_day, by_handler = ({} for _ in range(6))
    first_day = last_day = None

    for r in recs:
        handled = _scan_handled(r)
        if r.get("confirmed_at") or handled:
            stage = "closed"                  # confirmed by the checker, or (live scan) left the security user's queue
        elif r.get("executed_at"):
            stage = "awaiting"                # executed by the employee, waiting for the manager to confirm
        else:
            stage = "open"
        buckets[stage] += 1

        emp_min = _minutes(r.get("assigned_at"), r.get("executed_at")) if r.get("executed_at") else None
        mgr_min = _minutes(r.get("executed_at"), r.get("confirmed_at")) if r.get("confirmed_at") else None
        total_min = (_minutes(r.get("assigned_at"), r.get("confirmed_at")) if r.get("confirmed_at")
                     else emp_min if handled else None)
        outcome = r.get("final_outcome") if r.get("confirmed_at") else r.get("exec_outcome")
        team, system = r.get("team") or "Open System", r.get("system") or "—"

        if r.get("executed_at"):
            emp_clock.add(r.get("exec_outcome"), emp_min, sla)
            by_exec.setdefault(r.get("executor") or "—", _Agg()).add(r.get("exec_outcome"), emp_min, sla)
        if stage == "closed":
            overall.add(outcome, total_min, sla)
            if r.get("confirmed_at"):
                mgr_clock.add(r.get("final_outcome"), mgr_min, sla)
                by_check.setdefault(r.get("checker") or "—", _Agg()).add(r.get("final_outcome"), mgr_min, sla)
            handler = _clean(r.get("executor")) or _clean(r.get("assignee")) or "—"
            by_handler.setdefault(handler, _Agg()).add(outcome, total_min, sla)
            by_team.setdefault(team, _Agg()).add(outcome, total_min, sla)
            by_system.setdefault(system, _Agg()).add(outcome, total_min, sla)
            day = _closed_at(r)[:10]
            if day:
                by_day.setdefault(day, _Agg()).add(outcome, total_min, sla)
                first_day = day if first_day is None or day < first_day else first_day
                last_day = day if last_day is None or day > last_day else last_day

    def top(groups, key, n=25):
        out = [g.out(**{key: name}) for name, g in groups.items()]
        out.sort(key=lambda r: -r["n"])
        return out[:n] if n else out

    open_recs = [r for r in live_recs if not r.get("executed_at") and not r.get("confirmed_at")]
    awaiting_now = sum(1 for r in live_recs if r.get("executed_at") and not r.get("confirmed_at")
                       and not _scan_handled(r))
    today = now[:10]
    closed_today = {}
    for r in live_recs:                          # date-unfiltered: a request assigned earlier and closed today counts
        if today and _closed_at(r)[:10] == today:
            t_min = (_minutes(r.get("assigned_at"), r.get("confirmed_at")) if r.get("confirmed_at")
                     else _minutes(r.get("assigned_at"), r.get("executed_at")))
            out_ = r.get("final_outcome") if r.get("confirmed_at") else r.get("exec_outcome")
            who = _clean(r.get("executor")) or _clean(r.get("assignee")) or "—"
            closed_today.setdefault(who, _Agg()).add(out_, t_min, sla)
    open_ages = [_minutes(r.get("assigned_at"), now) for r in open_recs] if now else []
    open_ages = [a for a in open_ages if a is not None]
    live = {"open": len(open_recs), "awaiting": awaiting_now,
            "oldest_minutes": round(max(open_ages), 1) if open_ages else None,
            "breached": sum(1 for a in open_ages if a > sla),
            "by_employee": _live_groups(open_recs, "assignee", "executor", sla, now),
            "by_team": _live_groups(open_recs, "team", "team", sla, now),
            "closed_today": sum(g.n for g in closed_today.values()),
            "closed_today_by_employee": sorted((g.out(executor=k) for k, g in closed_today.items()),
                                               key=lambda x: -x["n"])}

    return {
        "meta": {"requests": len(recs), "sla_minutes": sla, "date_from": first_day, "date_to": last_day},
        "buckets": buckets,
        "live": live,
        "overall": overall.out(),
        "employee_clock": emp_clock.out(),      # assignment -> execution (by مستخدم السرية)
        "manager_clock": mgr_clock.out(),       # execution -> confirmation (by مراجع السرية)
        "by_employee": top(by_exec, "executor"),     # the executor: who ran the request
        "by_checker": top(by_check, "checker"),      # the reviewer/manager: who confirmed it
        "by_handler": top(by_handler, "handler"),    # closed requests by the security user who handled them
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
        if execu and execu not in (_clean(r.get("executor")).lower(), _clean(r.get("assignee")).lower()):
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
        if _clean(r.get("assignee")):               # the security user holding a still-open request (live scan)
            execs.add(_clean(r.get("assignee")))
        if _clean(r.get("checker")):
            checks.add(_clean(r.get("checker")))
        if _clean(r.get("team")):
            teams.add(_clean(r.get("team")))
        day = _clean(r.get("assigned_at"))[:10]
        if day:
            days.append(day)
    return {"executors": sorted(execs), "checkers": sorted(checks), "teams": sorted(teams),
            "date_from": min(days) if days else None, "date_to": max(days) if days else None}


def decode_csv(data):
    """Rows (dicts, headers trimmed) of a CSV given as bytes: UTF-8 (with or without a BOM) or the Arabic Windows code
    page the portal's Excel exports often use; comma, semicolon or tab separated."""
    for enc in ("utf-8-sig", "cp1256"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
    reader.fieldnames = [_clean(c) for c in (reader.fieldnames or [])]
    return [{_clean(k): v for k, v in row.items() if k is not None} for row in reader]


def read_feed_rows(path):
    """The rows of a configured export feed file (an ALL_*.csv / requests_*.csv the portal refreshes)."""
    with open(path, "rb") as f:
        return decode_csv(f.read())


# The portal's own export (ALL_*.csv): one line per system of each CLOSED request (sla_analysis.py describes it).
EXPORT_COLUMNS = {"id": ("ReqID2", "ReqID"), "status": ("SStatusID",), "executor": ("EmpNo",),
                  "system": ("ApplicationName",), "requester": ("EmpName", "UserName"),
                  "assigned_at": ("DateF",), "confirmed_at": ("DateT1",)}
_EXPORT_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def is_portal_export(rows):
    """Is this the portal's ALL_*.csv export (DateF / DateT1 / SStatusID columns) rather than a feed already in the
    column names of the Settings mapping?"""
    keys = set((rows[0] if rows else {}).keys())
    return "SStatusID" in keys and bool(keys & {"DateF", "DateT1"})


def export_time(text):
    """A portal export date ('10/19/2026 01:05:22 PM', US month-first) -> 'YYYY-MM-DDTHH:MM:SS', or '' if unreadable."""
    value = _clean(text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace("ص", "AM").replace("م", "PM")
    for fmt in _EXPORT_TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime(_FMT)
        except ValueError:
            continue
    return ""


def normalize_export(rows, cfg):
    """(rows, cfg) ready for ``record_scan``. A feed in the Settings column names passes through unchanged. The portal's
    ALL_*.csv is translated: ReqID2 -> id, SStatusID -> the final decision, EmpNo -> who handled it, ApplicationName ->
    system, DateF -> assigned at, DateT1 -> confirmed at (US dates turned into ISO). Every export line is a CLOSED
    request, so a final decision missing from the configured labels still counts as closed (executed), like the
    closed-request analysis of the same file always did."""
    rows = list(rows or [])
    if not is_portal_export(rows):
        return rows, cfg
    statuses = (cfg or {}).get("statuses") or default_config()["statuses"]
    closing = set(statuses.get("confirmed_executed") or []) | set(statuses.get("confirmed_not") or [])
    fallback = (statuses.get("confirmed_executed") or ["تم التعزيز على التنفيذ"])[0]

    def pick(row, key):
        for name in EXPORT_COLUMNS[key]:
            if _clean(row.get(name)):
                return _clean(row.get(name))
        return ""

    out = []
    for row in rows:
        status, confirmed = pick(row, "status"), export_time(pick(row, "confirmed_at"))
        if confirmed and status not in closing:
            status = fallback
        out.append({"id": pick(row, "id"), "status": status, "executor": pick(row, "executor"), "checker": "",
                    "system": pick(row, "system"), "requester": pick(row, "requester"),
                    "assigned_at": export_time(pick(row, "assigned_at")), "confirmed_at": confirmed})
    fields = {k: k for k in ("id", "status", "executor", "checker", "system", "requester", "assigned_at",
                             "confirmed_at")}
    fields["executed_at"] = ""
    return out, {**(cfg or {}), "statuses": statuses, "fields": fields}


def apply_export(records, rows, cfg, now, system_team=None):
    """Fold an export (the portal's ALL_*.csv, or a feed in the Settings column names) into ``records``. The export is
    authoritative for the period it covers: live-scan closures (requests that left the queue) assigned within its
    date range and closed before its latest confirmation are replaced by the export's own lines, so nothing is counted
    twice. Returns {"rows", "added", "replaced_live"}."""
    rows, cfg2 = normalize_export(rows, cfg)
    f = cfg2.get("fields") or {}
    assigned = [_clean(r.get(f.get("assigned_at")))[:10] for r in rows if f.get("assigned_at")]
    confirmed = [_clean(r.get(f.get("confirmed_at"))) for r in rows if f.get("confirmed_at")]
    assigned, confirmed = [d for d in assigned if d], [c for c in confirmed if c]
    replaced = 0
    if assigned and confirmed:
        lo, hi = min(assigned), max(confirmed)
        for rid in [k for k, r in records.items() if _scan_handled(r)
                    and _clean(r.get("assigned_at"))[:10] >= lo and _clean(r.get("executed_at")) <= hi]:
            del records[rid]
            replaced += 1
    before = len(records)
    record_scan(records, rows, cfg2, now, system_team)
    return {"rows": len(rows), "added": len(records) - before, "replaced_live": replaced}


def housekeep(records, cfg, now):
    """Keep the JSON store bounded: drop CLOSED requests (confirmed, or left the queue in the live scan) older than
    ``retention_days`` (by when they closed), and if still over ``max_records`` drop the oldest closed ones. OPEN /
    AWAITING (still active) requests are always kept. Edits
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
            for rid in [k for k, r in records.items() if _closed_at(r) and _closed_at(r)[:10] < cutoff]:
                del records[rid]
    if cap and len(records) > cap:
        closed = sorted((k for k, r in records.items() if _closed_at(r)), key=lambda k: _closed_at(records[k]))
        for rid in closed[:len(records) - cap]:
            del records[rid]
    return before - len(records)
