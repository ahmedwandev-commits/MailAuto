"""Parser for the monthly shift-schedule workbook.

The real template (``جدول الورديات ... عن شهر اكتوبر.xlsx``) is laid out like this - nothing is assumed beyond it:

* row 1   title with the month and year ("شهر اكتوبر 2026");
* row 2   header: اليوم | التاريخ | one cell per shift group (morning / evening / night) with the hours in the text
          ("From 7 AM To 3 PM"); the group spans the columns up to the next group;
* row 3   sub-header: one label per column (FLEX, CARD, OPEN + MFA, 3rd Party ... / All);
* then one row per date: day name, date, and the employee names in each column. A cell may hold several names
  (new line or " - "); "( MFA )" after a name marks the MFA role. Weekends have a single (merged) column per shift;
* a row without a date whose text contains "Stand By" lists on-call people for the date above it
  ("name (VPN/ Stand By Site - CM) - name (...)").

Employees are identified by *first names only* (no employee id) - the matching to ids is done by
``domain.matching``. The parser itself never touches the database.
"""
import re
from datetime import date, datetime, timedelta

from ..domain.textnorm import month_from_text, norm, weekday_from_name

SHIFT_WORDS = (("M", ("صباح", "morning")), ("E", ("مسائ", "evening")), ("N", ("ليل", "night")))
_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)", re.I)
_MFA = re.compile(r"\(\s*MFA\s*\)", re.I)
_STANDBY_ITEM = re.compile(r"([^()]+?)\s*\(([^)]*)\)")
_SPLIT = re.compile(r"\n|\s+-\s+|\s*-\s*(?=\S)|،|\s\+\s")


class TemplateError(ValueError):
    """The workbook does not look like a monthly shift schedule."""


def _hhmm(text):
    m = _TIME.findall(text or "")
    out = []
    for h, mi, ap in m[:2]:
        h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
        out.append(f"{h:02d}:{int(mi or 0):02d}")
    return out if len(out) == 2 else (None, None)


def _shift_code(text):
    n = norm(text)
    for code, words in SHIFT_WORDS:
        if any(norm(w) in n for w in words):
            return code
    return None


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _clean(v):
    return re.sub(r"[ \t]+", " ", str(v)).strip() if v is not None else ""


def split_names(cell):
    """Split one work cell into [(display_name, note)]."""
    out = []
    for part in _SPLIT.split(_clean(cell).replace("\r", "")):
        part = part.strip()
        if not part:
            continue
        note = "MFA" if _MFA.search(part) else ""
        name = _MFA.sub("", part).strip(" ()-")
        if name:
            out.append((name, note))
    return out


def split_standby(cell):
    out = []
    for name, role in _STANDBY_ITEM.findall(_clean(cell).replace("\n", " ")):
        name = name.strip(" -–،")
        if name:
            out.append((name, role.strip()))
    return out


def parse_workbook(wb):
    """Parse an openpyxl workbook (first sheet that looks like a schedule). Returns the structure described below."""
    last_err = None
    for ws in wb.worksheets:
        try:
            return _parse_sheet(ws)
        except TemplateError as exc:
            last_err = last_err or exc                  # the first sheet's reason (not "too short" of a notes sheet after it)
    raise last_err or TemplateError("The workbook has no sheets.")


def _parse_sheet(ws):
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 4:
        raise TemplateError("The sheet is too short to be a shift schedule.")
    issues = []

    # ---- title: month + year
    title_month = title_year = None
    for r in rows[:3]:
        for v in r:
            if isinstance(v, str):
                m = month_from_text(v)
                y = re.search(r"(20\d{2})", v)
                if m and y:
                    title_month, title_year = m, int(y.group(1))
    # ---- header row: contains the day/date labels
    hdr_idx = None
    for i, r in enumerate(rows[:8]):
        labels = [norm(v) for v in r[:3]]
        if "اليوم" in labels and "التاريخ" in labels:
            hdr_idx = i
            break
    if hdr_idx is None:
        raise TemplateError("Could not find the header row (اليوم / التاريخ).")
    hdr = rows[hdr_idx]
    ncols = max(len(r) for r in rows)
    starts = [(c, v) for c, v in enumerate(hdr) if c >= 2 and v and _shift_code(str(v))]
    if not starts:
        raise TemplateError("No shift columns (morning / evening / night) found in the header.")
    groups = []
    for k, (c, v) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else ncols
        s, e = _hhmm(str(v))
        groups.append({"code": _shift_code(str(v)), "label": _clean(v).replace("\n", " "),
                       "start": s, "end": e, "cols": list(range(c, end))})
    sub = rows[hdr_idx + 1]
    col_group = {}
    for g in groups:
        for c in g["cols"]:
            col_group[c] = g
    streams = {c: (_clean(sub[c]) if c < len(sub) and sub[c] else "") for c in col_group}

    # a cell merged across the whole shift group (weekends / holidays) belongs to no particular stream
    merged_wide = set()
    for rng in getattr(ws, "merged_cells", None) and ws.merged_cells.ranges or []:
        if rng.max_col > rng.min_col:
            merged_wide.add((rng.min_row, rng.min_col))

    # ---- data rows
    entries, days = [], []
    cur = None
    prev_date = None
    seen_dates = set()
    for ridx in range(hdr_idx + 2, len(rows)):
        r = rows[ridx]
        xl_row = ridx + 1
        d_raw = _as_date(r[1]) if len(r) > 1 else None
        dayname = _clean(r[0]) if r and r[0] else ""
        if d_raw is not None:
            exp_wd = weekday_from_name(dayname) if dayname else None
            d = d_raw
            corrected = False
            bad_seq = d_raw in seen_dates or (prev_date and d_raw <= prev_date)
            wd_bad = exp_wd is not None and d_raw.weekday() != exp_wd
            if bad_seq or wd_bad:
                guess = (prev_date + timedelta(days=1)) if prev_date else None
                if guess and (exp_wd is None or guess.weekday() == exp_wd):
                    d, corrected = guess, True
                    issues.append({"level": "warning", "row": xl_row, "col": 2, "code": "date_corrected",
                                   "message": f"Date {d_raw.isoformat()} does not fit its position/day name "
                                              f"'{dayname}'; proposed {d.isoformat()}. Confirm before importing."})
                else:
                    issues.append({"level": "error", "row": xl_row, "col": 2, "code": "bad_date",
                                   "message": f"Date {d_raw.isoformat()} conflicts with day name '{dayname}' "
                                              "and no correction could be proposed."})
            seen_dates.add(d)
            prev_date = d
            cur = {"row": xl_row, "date": d.isoformat(), "date_raw": d_raw.isoformat(),
                   "day_name": dayname, "corrected": corrected,
                   "invalid": any(i["level"] == "error" and i["row"] == xl_row for i in issues)}
            days.append(cur)
            for c, g in col_group.items():
                if c < len(r) and r[c] not in (None, ""):
                    for name, note in split_names(r[c]):
                        stream = "" if (xl_row, c + 1) in merged_wide else streams.get(c, "")
                        entries.append({"row": xl_row, "col": c + 1, "date": cur["date"], "shift_code": g["code"],
                                        "stream": stream, "name_raw": name, "note": note,
                                        "name_norm": norm(name), "kind": "work", "invalid": cur["invalid"]})
        elif cur is not None:
            text = " ".join(_clean(v) for v in r if v)
            if text and "stand by" in text.lower():
                for name, role in split_standby(text):
                    entries.append({"row": xl_row, "col": 3, "date": cur["date"], "shift_code": "SB",
                                    "stream": "", "name_raw": name, "note": role, "name_norm": norm(name),
                                    "kind": "standby", "invalid": cur["invalid"]})
            elif text:
                issues.append({"level": "warning", "row": xl_row, "col": 1, "code": "unrecognised_row",
                               "message": f"Row ignored (no date and not a stand-by row): {text[:80]}"})
    if not days:
        raise TemplateError("No date rows found.")

    # ---- completeness of the month
    ys = {d["date"][:4] for d in days}
    ms = {d["date"][5:7] for d in days}
    present = {d["date"] for d in days}
    if title_year and title_month:
        first = date(title_year, title_month, 1)
        nxt = date(title_year + (title_month == 12), title_month % 12 + 1, 1)
        cur_d = first
        while cur_d < nxt:
            if cur_d.isoformat() not in present:
                issues.append({"level": "warning", "row": None, "col": None, "code": "missing_date",
                               "message": f"No row for {cur_d.isoformat()}."})
            cur_d += timedelta(days=1)
    return {"sheet": ws.title, "title_year": title_year, "title_month": title_month, "groups": groups,
            "days": days, "entries": entries, "issues": issues, "years": sorted(ys), "months": sorted(ms)}
