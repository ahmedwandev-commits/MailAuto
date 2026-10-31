"""Scheduling rules: conflicts, inferred rest days, staffing coverage. Pure functions over plain dicts.

Entry shape: {'employee_id', 'work_date' (ISO), 'shift_code', ...}; ``shifts`` is {code: {'kind','start_time','end_time'}}.
"""
from collections import defaultdict

from .shifts import interval


def detect_conflicts(entries, shifts, min_rest_hours):
    """Per employee: same-shift duplicates, overlapping shifts and rests shorter than ``min_rest_hours``.
    Returns a list of {'employee_id','work_date','shift_code','type','message'}; ``type`` is
    duplicate | overlap | short_rest."""
    by_emp = defaultdict(list)
    for e in entries:
        sh = shifts.get(e["shift_code"])
        if sh and sh["kind"] == "work" and sh.get("start_time"):
            s, en = interval(e["work_date"], sh["start_time"], sh["end_time"])
            by_emp[e["employee_id"]].append((s, en, e))
    out = []
    for emp, lst in by_emp.items():
        lst.sort(key=lambda x: (x[0], x[1]))
        for i in range(1, len(lst)):
            ps, pe, pe_entry = lst[i - 1]
            cs, ce, c_entry = lst[i]
            base = {"employee_id": emp, "work_date": c_entry["work_date"], "shift_code": c_entry["shift_code"]}
            if pe_entry["work_date"] == c_entry["work_date"] and pe_entry["shift_code"] == c_entry["shift_code"]:
                out.append({**base, "type": "duplicate", "message": "Same shift listed twice on the same day."})
            elif cs < pe:
                out.append({**base, "type": "overlap", "message":
                            f"Overlaps the {pe_entry['shift_code']} shift of {pe_entry['work_date']}."})
            else:
                rest = (cs - pe).total_seconds() / 3600
                if rest < min_rest_hours:
                    out.append({**base, "type": "short_rest", "message":
                                f"Only {rest:.1f} h rest after the {pe_entry['shift_code']} shift of "
                                f"{pe_entry['work_date']} (minimum {min_rest_hours:g} h)."})
    return out


def infer_rest_days(roster_ids, dates, entries):
    """OFF entries for every roster employee on every date where they have neither a work nor a stand-by entry.
    These are *inferred* (the template lists only people who work)."""
    busy = {(e["employee_id"], e["work_date"]) for e in entries}
    return [{"employee_id": emp, "work_date": d, "shift_code": "OFF", "stream": "", "note": "",
             "source": "inferred"} for emp in sorted(roster_ids) for d in dates if (emp, d) not in busy]


def coverage(entries, dates, shifts, min_staff=None):
    """{date: {shift_code: {'count': n, 'min': m|None, 'short': bool}}} for work shifts."""
    min_staff = min_staff or {}
    cnt = defaultdict(set)
    for e in entries:
        sh = shifts.get(e["shift_code"])
        if sh and sh["kind"] == "work":
            cnt[(e["work_date"], e["shift_code"])].add(e["employee_id"])
    out = {}
    for d in dates:
        out[d] = {}
        for code, sh in shifts.items():
            if sh["kind"] != "work":
                continue
            n = len(cnt.get((d, code), ()))
            m = min_staff.get(code)
            out[d][code] = {"count": n, "min": m, "short": bool(m) and n < m}
    return out
