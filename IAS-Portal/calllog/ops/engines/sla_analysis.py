# -*- coding: utf-8 -*-
"""Closed security-request analysis (the "SLA Analysis" view of the Ticketing module).

The portal exports one line per system inside a request to a big ``ALL_*.csv`` (the whole year so far). Each line is a
CLOSED request with two events:

    FStatusID1 + DateF  = «اسناد لمستخدم السرية»  - the request was assigned to a security user (the clock STARTS)
    SStatusID  + DateT1 = the final decision       - «تم التعزيز على التنفيذ» / «تم التعزيز على عدم التنفيذ»
                                                      / «تم تسليم الرد» / «تم ارسال كلمة المرور عن طريق ال SMS» (the clock ENDS)

So the SLA is the FULL clock DateT1 - DateF (target 3 hours); there is no in-between «تم التنفيذ» timestamp in the
export, so the employee and manager halves cannot be split from this file (that intermediate state lives only in the
LIVE portal and is read by the Ticketing collector, not here).

This module is PURE PYTHON (the app has no pandas): it streams the CSV and rolls up the numbers the business asked for -
how many requests closed each day (accumulated), how many were executed vs closed without execution, SLA compliance
against the 3-hour target - split per team, per system and per employee. Teams come from the application -> team map.
"""
import csv
from collections import defaultdict
from datetime import datetime

SLA_HOURS = 3.0
_FMT = "%m/%d/%Y %I:%M:%S %p"

# the final decision (SStatusID) counts as "executed" for any of these; "not executed" is the explicit rejection
STATUS_EXECUTED = ("تم التعزيز على التنفيذ", "تم تسليم الرد", "تم ارسال كلمة المرور عن طريق ال SMS",
                   "تم اعادة ارسال كلمة المرور عن طريق ال SMS")
STATUS_NOT_EXECUTED = ("تم التعزيز على عدم التنفيذ",)
_EXEC, _NOEXEC = set(STATUS_EXECUTED), set(STATUS_NOT_EXECUTED)

# the two employee-side states the portal shows BEFORE a manager confirms (used by the live view, kept here as the
# reference list so the whole workflow is defined in one place)
STATUS_AWAITING = ("تم التنفيذ", "لم يتم التنفيذ")


def _clean(text):
    return " ".join(str(text or "").replace("﻿", "").split())


def _parse(text):
    try:
        return datetime.strptime(_clean(text), _FMT)
    except (ValueError, TypeError):
        return None


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def read_all_rows(path, encoding="utf-8"):
    """Yield the raw rows of an ``ALL_*.csv`` export (a leading BOM on the first header is tolerated)."""
    with open(path, encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [_clean(c) for c in (reader.fieldnames or [])]
        for row in reader:
            yield {_clean(k): v for k, v in row.items()}


class _Bucket:
    """Running totals for one group (overall, a team, a system, a day, an employee)."""
    __slots__ = ("closed", "executed", "not_executed", "within_sla", "eligible", "_hours")

    def __init__(self):
        self.closed = self.executed = self.not_executed = self.within_sla = self.eligible = 0
        self._hours = []

    def add(self, executed, hours, keep_hours):
        self.closed += 1
        if executed:
            self.executed += 1
        else:
            self.not_executed += 1
        if hours is not None:
            self.eligible += 1
            if hours <= SLA_HOURS:
                self.within_sla += 1
            if keep_hours:
                self._hours.append(hours)

    def as_dict(self, **extra):
        med = None
        if self._hours:
            h = sorted(self._hours)
            med = round(h[len(h) // 2], 2)
        return dict(extra, closed=self.closed, executed=self.executed, not_executed=self.not_executed,
                    executed_pct=_pct(self.executed, self.closed), sla_pct=_pct(self.within_sla, self.eligible),
                    median_hours=med)


def compute(rows, system_team=None, sla_hours=SLA_HOURS, top=25, dedup=True):
    """Roll up closed requests into the metrics the SLA Analysis view shows.

    ``rows``        - iterable of dicts with DateF, DateT1, SStatusID, EmpNo, ApplicationName (from read_all_rows).
    ``system_team`` - {application(lower): team}; an unknown application falls in "Open System".
    Returns a JSON-safe dict: meta, overall, by_status, by_day (accumulated), by_team, by_system (top), by_employee (top).
    """
    global SLA_HOURS
    SLA_HOURS = float(sla_hours)
    tmap = {str(k).strip().lower(): v for k, v in (system_team or {}).items()}

    overall = _Bucket()
    status_n = defaultdict(int)
    by_day, by_team, by_system, by_emp = (defaultdict(_Bucket) for _ in range(4))
    first_day = last_day = None
    seen = set()
    n_raw = 0

    for row in rows:
        n_raw += 1
        rid = _clean(row.get("ReqID2"))
        if dedup and rid:
            if rid in seen:
                continue
            seen.add(rid)
        status = _clean(row.get("SStatusID"))
        status_n[status] += 1
        executed = status in _EXEC if (status in _EXEC or status in _NOEXEC) else status not in _NOEXEC
        F, T = _parse(row.get("DateF")), _parse(row.get("DateT1"))
        hours = None
        if F and T:
            diff = (T - F).total_seconds() / 3600.0
            if 0 <= diff < 100000:
                hours = diff
        day = F.strftime("%Y-%m-%d") if F else None
        if day:
            first_day = day if first_day is None or day < first_day else first_day
            last_day = day if last_day is None or day > last_day else last_day
        system = _clean(row.get("ApplicationName")) or "—"
        team = tmap.get(system.lower(), "Open System")
        emp = _clean(row.get("EmpNo")) or "—"

        overall.add(executed, hours, keep_hours=True)
        if day:
            by_day[day].add(executed, hours, keep_hours=False)
        by_team[team].add(executed, hours, keep_hours=True)
        by_system[system].add(executed, hours, keep_hours=False)
        by_emp[emp].add(executed, hours, keep_hours=False)

    def top_list(groups, key, n):
        rows_ = [g.as_dict(**{key: name}) for name, g in groups.items()]
        rows_.sort(key=lambda r: -r["closed"])
        return rows_[:n] if n else rows_

    status_rows = sorted(
        ({"status": s, "n": c, "pct": _pct(c, overall.closed),
          "outcome": ("executed" if (s in _EXEC or s not in _NOEXEC) else "not_executed")}
         for s, c in status_n.items()), key=lambda r: -r["n"])
    day_rows = [by_day[d].as_dict(date=d) for d in sorted(by_day)]

    return {
        "meta": {"rows_raw": n_raw, "requests": overall.closed, "date_from": first_day, "date_to": last_day,
                 "days": len(by_day), "sla_hours": SLA_HOURS, "deduped": bool(dedup)},
        "overall": overall.as_dict(),
        "by_status": status_rows,
        "by_day": day_rows,
        "by_team": top_list(by_team, "team", 0),
        "by_system": top_list(by_system, "system", top),
        "by_employee": top_list(by_emp, "emp", top),
    }


def analyze_file(path, system_team=None, sla_hours=SLA_HOURS, top=25):
    """Convenience: stream an ``ALL_*.csv`` from disk and return the metrics dict."""
    return compute(read_all_rows(path), system_team=system_team, sla_hours=sla_hours, top=top)
