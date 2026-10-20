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
Settings. docs/HUB-DESIGN.md section 5.1 ("SLA Analysis recording").
"""
import csv
import io
import re
from datetime import datetime, timedelta

from .engines.common import norm_arabic

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


def _is(status, labels):
    """Is ``status`` one of ``labels``, whatever the Arabic spelling (ى/ي, hamza, tatweel, spaces)?"""
    s = norm_arabic(_clean(status))
    return bool(s) and any(s == norm_arabic(label) for label in labels or [])


def _stage(status, cfg):
    """(stage, outcome) for a status: stage in assigned|executed|confirmed; outcome executed|not_executed|None."""
    st = cfg.get("statuses") or {}
    if _is(status, st.get("confirmed_executed")):
        return "confirmed", "executed"
    if _is(status, st.get("confirmed_not")):
        return "confirmed", "not_executed"
    if _is(status, st.get("executed")):
        return "executed", "executed"
    if _is(status, st.get("not_executed")):
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
        system = col(row, "system")                          # a request for several applications: one line each
        if system and system != rec.get("system") and system not in (rec.get("systems") or []):
            rec["systems"] = [s for s in [rec.get("system")] + list(rec.get("systems") or []) if s] + [system]
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
        assigned_at = _clean(r.get("sent_at"))    # no sent date: no SLA clock (the Live tab says "not counted")
        if rec is None:
            rec = records[rid] = {"id": rid, "system": system, "team": tmap.get(system.lower(), "Open System"),
                                  "requester": _clean(flds.get(req_name)), "first_seen": now, "assigned_at": assigned_at,
                                  "executor": "", "executed_at": "", "exec_outcome": "", "checker": "",
                                  "confirmed_at": "", "final_outcome": "", "last_status": "", "last_seen": now}
        if rec.get("source") == SCAN_SOURCE and rec.get("executed_at") and not rec.get("confirmed_at"):
            rec["executed_at"], rec["executor"], rec["exec_outcome"] = "", "", ""     # back in the queue: open again
        rec["source"] = SCAN_SOURCE
        rec["assignee"] = assignee
        if assigned_at and not rec.get("assigned_at"):
            rec["assigned_at"] = assigned_at                  # the page shows the sent date now
        if system:
            rec["system"] = system
            rec["team"] = tmap.get(system.lower(), rec.get("team") or "Open System")
        rec["last_status"] = assignee and "assigned" or "unassigned"
        rec["last_seen"] = now
    return records


LEFT_QUEUE_GAP_MINUTES = 60    # longer than this between two scans (night, weekend, collector down): no scan saw it go


def mark_left_queue(records, present_ids, now):
    """Close the live-scan requests that are no longer on the Re-Assign page: their security user has acted on them
    (executed or not - the queue does not say which), so stamp ``executed_at`` = this scan's time (accurate to one scan
    interval) and ``executor`` = the user who was holding it. When no scan ran for a long time before this one (the
    night, a weekend, the collector stopped), the request closes at the LAST scan that still listed it: the night is
    not counted as the security user's time. Only open scan-sourced records are touched. Returns how many closed."""
    now = _clean(now) or datetime.now().strftime(_FMT)
    present = {_clean(x) for x in present_ids or ()}
    closed = 0
    for rid, r in records.items():
        if r.get("source") != SCAN_SOURCE or r.get("executed_at") or r.get("confirmed_at") or rid in present:
            continue
        seen = _clean(r.get("last_seen"))
        gap = _minutes(seen, now) if seen else None
        r["executed_at"] = seen if gap is not None and gap > LEFT_QUEUE_GAP_MINUTES else now
        r["executor"] = r.get("assignee") or ""
        r["exec_outcome"] = ""                    # unknown: «تم التنفيذ» or «لم يتم التنفيذ»
        r["last_status"] = "left_queue"
        closed += 1
    return closed


def fold_latest_scan(store, system_team=None, force=False):
    """Fold the latest LIVE SLA scan (the ``sla`` snapshot) into the recording held by ``store`` (an OpsStore). The
    collector calls this right after it writes each scan - so nothing is lost while no web page is open - and the web
    side calls it again as a safety net (a no-op once the scan is folded). The snapshot is read INSIDE the store lock, so
    an older scan can never be folded over a newer one, and "already folded" is an exact match on its time (a clock
    that goes back - the end of summer time - does not make the next scans look old). Requests on the page are open;
    one that is no longer on it was acted on by its security user and closes at this scan's time. Returns how many
    requests are open in the folded scan (0 when there was nothing new)."""
    snap = store.read_snapshot("sla")
    generated = (snap or {}).get("generated_at") or ""
    if not generated or (not force and (store.read_sla_records().get("last_scan") or "") == generated):
        return 0
    folded = {"open": 0}

    def change(d):
        latest = store.read_snapshot("sla") or {}
        stamp = latest.get("generated_at") or ""
        if not stamp or (not force and (d.get("last_scan") or "") == stamp):
            return                                            # another process folded it a moment ago
        requests = (latest.get("data") or {}).get("requests") or []
        keys = {_clean(r.get("key")) for r in requests if _clean(r.get("key"))}
        cfg = {**default_config(), **(d.get("config") or {})}
        records = d.setdefault("records", {})
        record_live_requests(records, requests, cfg, stamp, system_team)
        mark_left_queue(records, keys, stamp)
        housekeep(records, cfg, stamp)
        d["last_scan"] = stamp
        folded["open"] = len(keys)
    store.update_sla_records(change)
    return folded["open"]


def system_team_map(auto_assign):
    """{application name (lower case): team} from the Auto-Assign application list (what the recording tags with)."""
    return {str(a.get("name") or "").strip().lower(): a.get("team")
            for a in ((auto_assign or {}).get("applications") or []) if a.get("name") and a.get("team")}


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


def _past(age, limit, inclusive):
    return age >= limit if inclusive else age > limit


def _live_view(r, now, sla, inclusive, live_status):
    """(age in minutes or None, breached?) of one OPEN request. When the live scan has a verdict for it (``live_status``
    {record id: "within" | "reminder" | "breached" | "no_date"}) that verdict is used - the very number the Live tickets
    tab, Home and the attention list show; a request without a sent date has no SLA age there, so none here either."""
    status = (live_status or {}).get(r.get("id"))
    if status == "no_date":
        return None, False
    age = _minutes(r.get("assigned_at"), now) if now else None
    if status:
        return age, status == "breached"
    return age, age is not None and _past(age, sla, inclusive)


def _live_groups(open_recs, group_by, out_key, sla, now, inclusive=False, live_status=None):
    """[{<out_key>, n, oldest_minutes, breached}] of the OPEN requests grouped by ``group_by`` (the holding security
    user, or the team), worst (most breached, then longest waiting) first. ``breached`` counts open requests whose
    running clock already passed the SLA target. ``oldest_minutes`` is None until a server ``now`` is given (the stored
    records carry no running clock)."""
    groups = {}
    for r in open_recs:
        name = _clean(r.get(group_by)) or "—"
        age, breached = _live_view(r, now, sla, inclusive, live_status)
        g = groups.setdefault(name, {"n": 0, "oldest": None, "breached": 0})
        g["n"] += 1
        g["breached"] += breached
        if age is not None:
            g["oldest"] = age if g["oldest"] is None else max(g["oldest"], age)
    out = [{out_key: name, "n": g["n"], "oldest_minutes": g["oldest"], "breached": g["breached"]}
           for name, g in groups.items()]
    out.sort(key=lambda r: (-r["breached"], -(r["oldest_minutes"] or 0), -r["n"]))
    return out


def analyze(records, cfg, now=None, live_records=None, live_breach_minutes=None, live_status=None):
    """Build the live SLA metrics from the recorded lifecycles: live buckets (open / awaiting confirmation / closed),
    the EMPLOYEE clock (assignment->execution) by مستخدم السرية, the MANAGER clock (execution->confirmation) by
    مراجع السرية, plus by team / system / day. ``records`` is the dict from record_scan / record_live_requests.
    A ``now`` (ISO string) ages the still-open requests so the live section shows how long each has been waiting.
    ``live_records`` overrides which records feed the "right now" open strip (the caller passes the date-unfiltered set,
    since a still-open request assigned on an earlier day must still count as open today). ``live_breach_minutes`` is
    the Live tickets tab's breach time: the "right now" strip counts an open request as past it from that minute on,
    exactly like the live tab (a request is not counted breached by one tab and within by the other); without it the
    closed-request target ``sla_minutes`` is used. ``live_status`` ({record id: the live scan's status}) makes the strip
    use the scan's own verdict for every request it lists (see ``_live_view``).

    Two clocks, two targets, never two verdicts for one interval: the SECURITY USER's clock (assignment -> leaving the
    queue / execution: the employee clock, and every closure the live scan saw) is judged by the Live tab's breach
    time; the FULL clock (assignment -> the checker's confirmation, from the export) by ``sla_minutes``."""
    sla = int((cfg or {}).get("sla_minutes") or DEFAULT_SLA_MINUTES)
    live_sla = int(live_breach_minutes) if live_breach_minutes else sla
    inclusive = bool(live_breach_minutes)                    # the live tab: breached from breach_minutes on (>=)
    queue_sla = live_sla                                     # the security user's clock: the Live tab's target
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
        target = queue_sla if handled else sla              # left the queue (scan) vs confirmed (export)

        if r.get("executed_at"):
            emp_clock.add(r.get("exec_outcome"), emp_min, queue_sla)
            by_exec.setdefault(r.get("executor") or "—", _Agg()).add(r.get("exec_outcome"), emp_min, queue_sla)
        if stage == "closed":
            overall.add(outcome, total_min, target)
            if r.get("confirmed_at"):
                mgr_clock.add(r.get("final_outcome"), mgr_min, sla)
                by_check.setdefault(r.get("checker") or "—", _Agg()).add(r.get("final_outcome"), mgr_min, sla)
            handler = _clean(r.get("executor")) or _clean(r.get("assignee")) or "—"
            by_handler.setdefault(handler, _Agg()).add(outcome, total_min, target)
            for t in r.get("teams") or [team]:               # a request for several applications counts for each team
                by_team.setdefault(t, _Agg()).add(outcome, total_min, target)
            for sys_ in r.get("systems") or [system]:
                by_system.setdefault(sys_, _Agg()).add(outcome, total_min, target)
            day = _closed_at(r)[:10]
            if day:
                by_day.setdefault(day, _Agg()).add(outcome, total_min, target)
                first_day = day if first_day is None or day < first_day else first_day
                last_day = day if last_day is None or day > last_day else last_day

    def top(groups, key, n=25):
        out = [g.out(**{key: name}) for name, g in groups.items()]
        out.sort(key=lambda r: -r["n"])
        return out[:n] if n else out

    open_recs = [r for r in live_recs if not r.get("executed_at") and not r.get("confirmed_at")]
    if any(r.get("source") == SCAN_SOURCE for r in live_recs):   # the live scan owns "open now": a feed's open rows
        open_recs = [r for r in open_recs if r.get("source") == SCAN_SOURCE]   # are the same requests again
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
            closed_today.setdefault(who, _Agg()).add(out_, t_min, sla if r.get("confirmed_at") else queue_sla)
    views = [_live_view(r, now, live_sla, inclusive, live_status) for r in open_recs]
    open_ages = [a for a, _ in views if a is not None]
    live = {"open": len(open_recs), "awaiting": awaiting_now,
            "oldest_minutes": round(max(open_ages), 1) if open_ages else None,
            "breached": sum(1 for _, b in views if b), "breach_minutes": live_sla,
            "by_employee": _live_groups(open_recs, "assignee", "executor", live_sla, now, inclusive, live_status),
            "by_team": _live_groups(open_recs, "team", "team", live_sla, now, inclusive, live_status),
            "closed_today": sum(g.n for g in closed_today.values()),
            "closed_today_by_employee": sorted((g.out(executor=k) for k, g in closed_today.items()),
                                               key=lambda x: -x["n"])}

    return {
        "meta": {"requests": len(recs), "sla_minutes": sla, "queue_minutes": queue_sla, "date_from": first_day,
                 "date_to": last_day},
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
    a date range, the executor (مستخدم السرية), the checker (مراجع السرية), and the team. The date range is the period's
    WORK: a closed request belongs to the day it closed (like the "by day" table and "Closed today"), a request that is
    still open or awaiting confirmation to every period from its assignment on (it was open all that time) - so the
    morning's "today" holds yesterday evening's requests that are still waiting and the ones closed this morning.
    Blank values match everything. ``filt`` is a plain dict of strings (typically the request query)."""
    filt = filt or {}
    dfrom, dto = _clean(filt.get("date_from"))[:10], _clean(filt.get("date_to"))[:10]
    execu, check, team = (_clean(filt.get(k)).lower() for k in ("executor", "checker", "team"))
    if not (dfrom or dto or execu or check or team):
        return records or {}
    out = {}
    for rid, r in (records or {}).items():
        closed = _closed_at(r)[:10]
        if closed:                                           # closed: in the period it closed in
            if (dfrom and closed < dfrom) or (dto and closed > dto):
                continue
        else:                                                # still open: in every period from its assignment on
            day = (_clean(r.get("assigned_at")) or _clean(r.get("first_seen")))[:10]
            if (dfrom or dto) and not day:
                continue
            if dto and day > dto:
                continue
        if execu and execu not in (_clean(r.get("executor")).lower(), _clean(r.get("assignee")).lower()):
            continue
        if check and _clean(r.get("checker")).lower() != check:
            continue
        if team and team not in {_clean(t).lower() for t in (r.get("teams") or [r.get("team")])}:
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
        for t in r.get("teams") or [r.get("team")]:
            if _clean(t):
                teams.add(_clean(t))
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
_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")
_EXPORT_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M") + _ISO_FORMATS
_EXPORT_TIME_FORMATS_DMY = tuple(f.replace("%m/%d/", "%d/%m/") for f in _EXPORT_TIME_FORMATS)
_SLASH_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/\d{4}")


def is_portal_export(rows):
    """Is this the portal's ALL_*.csv export (DateF / DateT1 / SStatusID columns) rather than a feed already in the
    column names of the Settings mapping?"""
    keys = set((rows[0] if rows else {}).keys())
    return "SStatusID" in keys and bool(keys & {"DateF", "DateT1"})


def date_order(values):
    """'dmy' when the slash dates of a file are day-first (a first number over 12), else 'mdy' (the portal's US
    export). One file uses one order, so one unambiguous date decides for all of them."""
    for value in values:
        m = _SLASH_DATE.match(_clean(value).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
        if m and int(m.group(1)) > 12:
            return "dmy"
        if m and int(m.group(2)) > 12:
            return "mdy"
    return "mdy"


def export_time(text, order="mdy"):
    """A portal export date ('10/19/2026 01:05:22 PM', US month-first; ``order='dmy'`` for day-first; SQL-style
    fractions such as '.000' are ignored) -> 'YYYY-MM-DDTHH:MM:SS', or '' if unreadable."""
    value = _clean(text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace("ص", "AM").replace("م", "PM")
    value = re.sub(r"(\d{1,2}:\d{2}:\d{2})[.,]\d+", r"\1", value)
    for fmt in (_EXPORT_TIME_FORMATS_DMY if order == "dmy" else _EXPORT_TIME_FORMATS):
        try:
            return datetime.strptime(value, fmt).strftime(_FMT)
        except ValueError:
            continue
    return ""


def normalize_export(rows, cfg, people=None, stats=None):
    """(rows, cfg) ready for ``record_scan``. A feed in the Settings column names passes through unchanged. The portal's
    ALL_*.csv is translated: ReqID2 -> id, SStatusID -> the final decision, EmpNo -> who handled it, ApplicationName ->
    system, DateF -> assigned at, DateT1 -> confirmed at (US dates turned into ISO). Every export line is a CLOSED
    request, so a final decision missing from the configured labels still counts as closed (executed), like the
    closed-request analysis of the same file always did. ``people`` ({EmpNo: the person's security-user name}) names
    the handler as the live scan does, so one person is one row in every table. A line whose DateF or DateT1 cannot be
    read is skipped (never clocked from the upload time) and counted in ``stats["skipped"]``; a «لم يتم التنفيذ»
    decision is a not-executed confirmation, whatever its spelling."""
    rows = list(rows or [])
    people = {str(k).strip(): v for k, v in (people or {}).items() if str(k).strip() and v}
    if not is_portal_export(rows):
        return rows, cfg
    statuses = (cfg or {}).get("statuses") or default_config()["statuses"]
    closing = list(statuses.get("confirmed_executed") or []) + list(statuses.get("confirmed_not") or [])
    fallback = (statuses.get("confirmed_executed") or ["تم التعزيز على التنفيذ"])[0]
    not_done = (statuses.get("confirmed_not") or ["تم التعزيز على عدم التنفيذ"])[0]
    order = date_order(row.get(name) for row in rows for key in ("assigned_at", "confirmed_at")
                       for name in EXPORT_COLUMNS[key])
    skipped = 0

    def pick(row, key):
        for name in EXPORT_COLUMNS[key]:
            if _clean(row.get(name)):
                return _clean(row.get(name))
        return ""

    out = []
    for row in rows:
        status = pick(row, "status")
        assigned, confirmed = export_time(pick(row, "assigned_at"), order), export_time(pick(row, "confirmed_at"), order)
        if not assigned or not confirmed:                    # no clock to measure: skip it, say so
            skipped += 1
            continue
        if not _is(status, closing):                         # every export line is a closed request
            status = not_done if _is(status, statuses.get("not_executed")) else fallback
        emp = pick(row, "executor")
        out.append({"id": pick(row, "id"), "status": status, "executor": people.get(emp, emp), "checker": "",
                    "system": pick(row, "system"), "requester": pick(row, "requester"),
                    "assigned_at": assigned, "confirmed_at": confirmed})
    if stats is not None:
        stats["skipped"] = skipped
    fields = {k: k for k in ("id", "status", "executor", "checker", "system", "requester", "assigned_at",
                             "confirmed_at")}
    fields["executed_at"] = ""
    return out, {**(cfg or {}), "statuses": statuses, "fields": fields}


def _match_key(assigned_at, system):
    return _clean(assigned_at)[:16], _clean(system).lower()


def apply_export(records, rows, cfg, now, system_team=None, people=None):
    """Fold an export (the portal's ALL_*.csv, or a feed in the Settings column names) into ``records``. An export line
    and the live scan's closure of the SAME request - the same assignment minute and application (the scan and the
    export have no common request number) - become one record under the export's number: the security user and the
    time the request left their queue come from the scan (the employee clock), the confirmation and the outcome from
    the export (the manager clock). This also happens when the export line was imported before the scan's closure.
    A live closure the export does not hold (not confirmed yet) is kept, so nothing is lost and nothing is counted
    twice. ``people`` maps the export's EmpNo to the person's name (``normalize_export``). Past the ``max_records`` cap
    the oldest closed requests are dropped (and said so); lines older than everything kept are not added again, so a
    re-import adds nothing. Returns {"rows", "added", "replaced_live", "skipped", "dropped"}."""
    stats = {}
    rows, cfg2 = normalize_export(rows, cfg, people, stats)
    initial_ids = set(records)
    f = cfg2.get("fields") or {}
    col = lambda row, key: _clean(row.get(f.get(key))) if f.get(key) else ""          # noqa: E731
    live = {}
    for rid, r in records.items():
        if _scan_handled(r) and _clean(r.get("assigned_at")):
            live.setdefault(_match_key(r.get("assigned_at"), r.get("system")), []).append(rid)
    replaced = 0
    for row in rows:
        rid = col(row, "id")
        same = live.get(_match_key(col(row, "assigned_at"), col(row, "system"))) if rid else None
        if not same:
            continue
        scanned = records.pop(same.pop(0))
        replaced += 1
        if rid in records:                                   # imported earlier: keep it, add what only the scan knew
            rec = records[rid]
            if not rec.get("executed_at"):
                rec["executed_at"], rec["exec_outcome"] = scanned.get("executed_at", ""), scanned.get("exec_outcome", "")
            if scanned.get("executor"):
                rec["executor"] = scanned["executor"]
            rec["scan_key"] = scanned.get("id")
            continue
        records[rid] = {**scanned, "id": rid, "source": "export", "scan_key": scanned.get("id"),
                        "assigned_at": col(row, "assigned_at") or scanned.get("assigned_at")}
    cap = int((cfg or {}).get("max_records") or 0)
    if cap and len(records) >= cap:                          # full: what is older than everything kept would only be
        kept = [_closed_at(r) for r in records.values() if _closed_at(r)]      # dropped again - do not add it
        floor = min(kept) if kept else ""
        rows = [r for r in rows if col(r, "id") in records or not floor or col(r, "confirmed_at") > floor]
    record_scan(records, rows, cfg2, now, system_team)
    dropped = housekeep(records, {"max_records": cap}, now) if cap else 0
    return {"rows": len(rows), "added": len(set(records) - initial_ids), "replaced_live": replaced,
            "skipped": stats.get("skipped", 0), "dropped": dropped}


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
