"""Parser for the HR attendance / departure workbook.

Real layout (``attendance form 1-10 to 13-10.xlsx``): row 1 is a title ("... خلال الفترة من : 01-10-2026 الى : 13-10-2026"),
row 2 the header, then one row per employee per calendar day - weekly rest days, official holidays and leave days are
rows too, just without punches. Columns are found by *name* (Arabic or English synonyms), not by position, so a file with the
columns in another order, or with a few columns missing, still imports. Employee ids are read from the id column only.
"""
import re
from datetime import date, datetime, time, timedelta

from ..domain.textnorm import norm_keep as norm

FIELDS = {  # field -> synonyms (compared after normalisation)
    "employee_id": ["رقم الموظف", "الرقم الوظيفي", "كود الموظف", "employee id", "employee no", "emp id", "id"],
    "name": ["اسم الموظف", "الاسم", "employee name", "name"],
    "department": ["الاداره / الفرع", "الاداره", "القسم", "department", "branch"],
    "sector": ["المنطقه / القطاع", "القطاع", "sector", "region"],
    "date": ["التاريخ", "date"],
    "day_name": ["اليوم", "day"],
    "day_type": ["نوع اليوم", "day type"],
    "in_time": ["توقيت الحضور", "وقت الحضور", "الحضور", "check in", "check-in", "time in", "in"],
    "in_source": ["مصدر اثبات الحضور", "مصدر الحضور", "in source"],
    "out_time": ["توقيت الانصراف", "وقت الانصراف", "الانصراف", "check out", "check-out", "time out", "out"],
    "out_source": ["مصدر اثبات الانصراف", "مصدر الانصراف", "out source"],
    "leave_text": ["موقف الاجازات", "الاجازات", "leave", "leave status"],
    "permit_text": ["موقف ( الاذون )", "موقف (الاذون)", "الاذون", "permission", "permit"],
    "mission_text": ["موقف ( المهمات )", "موقف (المهمات)", "المهمات", "mission"],
}
REQUIRED = ("employee_id", "date")
_SYN = {}
for f, syns in FIELDS.items():
    for s in syns:
        _SYN[re.sub(r"[\s()]+", "", norm(s))] = f


def _key(v):
    return re.sub(r"[\s()]+", "", norm(v)) if v is not None else ""


def detect_columns(header_cells, override=None):
    """{field: column index}. ``override`` ({field: index}) wins - used by the mapping screen."""
    mapping = {}
    for i, v in enumerate(header_cells):
        f = _SYN.get(_key(v))
        if f and f not in mapping:
            mapping[f] = i
    if override:
        mapping.update({k: v for k, v in override.items() if v is not None})
    return mapping


def parse_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return (datetime(1899, 12, 30) + timedelta(days=int(v))).date()
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def parse_time(v):
    """-> (time | None, ok). Blank is (None, True); unreadable is (None, False)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, True
    if isinstance(v, datetime):
        return v.time().replace(microsecond=0), True
    if isinstance(v, time):
        return v.replace(microsecond=0), True
    if isinstance(v, (int, float)) and 0 <= v < 1:
        secs = round(v * 86400)
        return time(secs // 3600 % 24, secs // 60 % 60, secs % 60), True
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*", v)
        if m:
            h, mi, s, ap = int(m[1]), int(m[2]), int(m[3] or 0), m[4]
            if ap:
                h = h % 12 + (12 if ap.lower() == "pm" else 0)
            if h < 24 and mi < 60 and s < 60:
                return time(h, mi, s), True
    return None, False


def _text(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def parse_workbook(wb, override=None, header_row=None):
    """``override``: {field: column index} chosen on the mapping screen; ``header_row``: 1-based row chosen there
    (otherwise the first of the first 15 rows that looks like a header is used)."""
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    head, found_row, mapping, title, grid = None, None, {}, "", []
    for idx, row in enumerate(it, start=1):
        if idx == 1 and row and row[0]:
            title = _text(row[0])
        if idx <= 12:
            grid.append([_text(v) for v in row[:20]])
        m = detect_columns(row, override)
        if header_row:
            if idx == header_row:
                head, found_row, mapping = row, idx, m
                break
        elif all(f in m for f in REQUIRED) and len(m) >= 4:
            head, found_row, mapping = row, idx, m
            break
        if idx >= max(15, header_row or 0):
            break
    header_row = found_row
    if head is None or not all(f in mapping for f in REQUIRED):
        return {"error": "Could not find a header row with at least an employee-id column and a date column.",
                "mapping": mapping, "rows": [], "rejected": [], "duplicates_in_file": 0, "employees": {},
                "title": title, "header_row": header_row, "header": [_text(h) for h in (head or [])], "grid": grid,
                "needs_mapping": True}
    header = [_text(h) for h in head]

    def cell(row, f):
        i = mapping.get(f)
        return row[i] if i is not None and i < len(row) else None

    rows, rejected, employees, seen = [], [], {}, set()
    dup_in_file = 0
    for idx, row in enumerate(it, start=header_row + 1):
        if row is None or all(v in (None, "") for v in row):
            continue
        raw_id = cell(row, "employee_id")
        try:
            emp_id = int(str(raw_id).strip().split(".")[0])
        except (TypeError, ValueError):
            rejected.append({"row": idx, "reason": f"Invalid or missing employee id: {raw_id!r}"})
            continue
        d = parse_date(cell(row, "date"))
        if d is None:
            rejected.append({"row": idx, "employee_id": emp_id, "reason": f"Invalid date: {cell(row, 'date')!r}"})
            continue
        tin, ok1 = parse_time(cell(row, "in_time"))
        tout, ok2 = parse_time(cell(row, "out_time"))
        if not (ok1 and ok2):
            rejected.append({"row": idx, "employee_id": emp_id, "date": d.isoformat(),
                             "reason": f"Unreadable time: in={cell(row, 'in_time')!r} out={cell(row, 'out_time')!r}"})
            continue
        key = (emp_id, d.isoformat())
        if key in seen:
            dup_in_file += 1
            continue
        seen.add(key)
        employees.setdefault(emp_id, {"name": _text(cell(row, "name")), "department": _text(cell(row, "department")),
                                      "sector": _text(cell(row, "sector"))})
        rows.append({"row": idx, "employee_id": emp_id, "work_date": d.isoformat(),
                     "day_type": _text(cell(row, "day_type")),
                     "in_time": tin.isoformat() if tin else None, "in_source": _text(cell(row, "in_source")),
                     "out_time": tout.isoformat() if tout else None, "out_source": _text(cell(row, "out_source")),
                     "leave_text": _text(cell(row, "leave_text")), "permit_text": _text(cell(row, "permit_text")),
                     "mission_text": _text(cell(row, "mission_text"))})
    dates = [r["work_date"] for r in rows]
    return {"error": None, "title": title, "header_row": header_row, "header": header, "mapping": mapping,
            "rows": rows, "rejected": rejected, "duplicates_in_file": dup_in_file, "employees": employees,
            "period": [min(dates), max(dates)] if dates else None, "grid": grid}
