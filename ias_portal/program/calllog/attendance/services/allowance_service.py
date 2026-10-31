"""Shift-allowance export (بدل ورديات): the employees' HR attendance of a period turned into the rows and the summary of the template
"نموذج ورديات" (one row per employee / day / shift type, then the count of every type per employee) - counts only, no money - by the
rules the administrator set in Settings -> Shift allowance and by the portal's schedule (``domain/allowance.py``)."""
import calendar
import io
from datetime import date, timedelta

from ..domain import allowance as al
from ..domain.textnorm import norm
from . import settings as settings_svc
from .analytics_service import DefaultShift, apply_corrections
from .schedule_service import ImportError_, shifts_by_code

MAX_DAYS = 366


def _period(d1, d2):
    try:
        a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    except (TypeError, ValueError) as exc:
        raise ImportError_("Choose the first and the last date of the period.") from exc
    if b < a:
        raise ImportError_("The last date is before the first date.")
    if (b - a).days + 1 > MAX_DAYS:
        raise ImportError_("Choose a period of at most one year.")
    return a.isoformat(), b.isoformat()


def month_bounds(ym):
    """'2026-10' -> ('2026-10-01', '2026-10-31')."""
    y, m = (int(x) for x in ym.split("-"))
    return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def compute(repo, d1, d2, employees):
    """The counted days and the not-counted days of ``employees`` (dicts with id / name) between two dates."""
    st = settings_svc.load(repo)
    rules = settings_svc.allowance_rules(st)
    ids = [e["id"] for e in employees]
    rule = DefaultShift(repo, st, shifts_by_code(repo))
    after = (date.fromisoformat(d2) + timedelta(days=1)).isoformat()            # a night shift ends on the next day's row
    att = apply_corrections(repo.attendance_records(d1, after, ids), repo.corrections(d1, after, ids))

    def day_kind(r):
        """From the HR day type (or, when the row has none, the calendar): an official holiday, a weekly rest day or a working day."""
        dt = norm(r["day_type"]) if r.get("day_type") else ""
        if rule.holiday(r["work_date"], dt):
            return "official_holiday"
        if dt in rule.rest_l or (not dt and date.fromisoformat(r["work_date"]).weekday() in rule.rest_days):
            return "rest_day"
        return None

    plan = {}
    for e in repo.schedule_entries(d1, d2, ids):                                  # the shifts of the portal's schedule (imported or changed by a manager)
        kind = al.KIND_OF_CODE.get(e["shift_code"]) or ("normal" if e["shift_code"] == al.NORMAL_CODE else None)
        if kind:
            plan.setdefault((e["employee_id"], e["work_date"]), kind)
    m_from, m_to = month_bounds(d1[:7])[0], month_bounds(d2[:7])[1]
    shift_workers = {e["employee_id"] for e in repo.schedule_entries(m_from, m_to, ids) if e["shift_code"] in al.KIND_OF_CODE}
    managers = {e["id"] for e in employees if (repo.directory.get_user(str(e["id"])) or {}).get("hub_role") == "manager"}
    days, skipped = al.classify(att, d1, d2, rules, day_kind, plan, shift_workers, lambda emp: emp in managers,
                                st["allowance_evening_after"], bool(st["allowance_use_schedule"]))
    return days, skipped, rules, st


def months_between(d1, d2):
    y, m = int(d1[:4]), int(d1[5:7])
    out = []
    while f"{y}-{m:02d}" <= d2[:7]:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build(repo, d1, d2, team=None, include_pending=False):
    """Everything the page and the workbook need for the period (every employee, or one team). A team's shifts are in the sheet once its
    manager approved the month (Monthly shifts); ``include_pending`` also shows the days of months / teams not approved (administrator)."""
    from . import shifts_service
    d1, d2 = _period(d1, d2)
    employees = [e for e in repo.list_employees() if not team or team.strip().lower() in [t.lower() for t in e["teams"]]]
    days, skipped, rules, st = compute(repo, d1, d2, employees)
    teams = sorted({t for e in employees for t in e["teams"]}, key=str.lower)
    approvals = shifts_service.statuses(repo, months_between(d1, d2), teams)
    approved = {(a["month"], a["team"].lower()) for a in approvals if a["status"] == "approved"}
    by_emp = {e["id"]: [t.lower() for t in e["teams"]] for e in employees}
    hidden = 0

    def ok(emp, day):
        return any((day[:7], t) in approved for t in by_emp.get(emp, []))
    for d in days:                                                  # the administrator sees every day, each marked with its manager's approval
        d["approved"] = ok(d["employee_id"], d["date"])
    if not include_pending:
        kept = [d for d in days if ok(d["employee_id"], d["date"])]
        hidden = len(days) - len(kept)
        days = kept
        skipped = [s for s in skipped if ok(s["employee_id"], s["date"])]
    names = {e["id"]: e["name"] for e in employees}
    summary = al.summarise(days)
    rows = [{"employee_id": i, "name": names[i], **summary[i]} for i in sorted(summary, key=lambda i: (names[i], i))]
    totals = {k: {"count": sum(r[k]["count"] for r in rows)} for k in (*al.KINDS, *al.OUT)}
    totals["total_count"] = sum(r["total_count"] for r in rows)
    rates = {k: float(rules[k].get("rate") or 0) for k in al.KINDS}               # the amount per day of Settings › Shift allowance
    for r in rows:
        r["amount"] = sum(r[k]["count"] * rates[k] for k in al.KINDS)
    totals["amount"] = sum(r["amount"] for r in rows)
    return {"from": d1, "to": d2, "team": team or "", "days": days, "skipped": skipped, "rows": rows, "totals": totals, "names": names,
            "rules": rules, "kinds": al.enabled_kinds(rules), "employees": len(employees), "evening_after": st["allowance_evening_after"],
            "use_schedule": bool(st["allowance_use_schedule"]), "data_loaded": bool(repo.dates_with_attendance(d1, d2)),
            "approvals": approvals, "hidden_days": hidden, "include_pending": include_pending, "rates": rates,
            "currency": st.get("allowance_currency") or "EGP", "has_amounts": any(rates.values()),
            "status_note": "" if not include_pending else "includes days not approved by the managers yet"}


def workbook(rep):
    """The template as a workbook: sheet «ورديات» (the rows), «ملخص» (the count of every shift type per employee) and «غير محسوب» (the days
    that had punches but did not count, and why)."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    kinds = rep["kinds"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ورديات"
    ws.sheet_view.rightToLeft = True
    # exactly the columns of the template; shift_type is one of its four words (a weekly rest day and an official holiday are both عطلة)
    ws.append(["employee_ID", "employee_name ", "date", "shift_type", "morning_shift", "Evening_shift", "night_shift", "holidays_GT_6_DAYS", "TASK"])
    for d in rep["days"]:
        o = al.OUT_OF[d["kind"]]
        ws.append([d["employee_id"], rep["names"][d["employee_id"]], date.fromisoformat(d["date"]), al.LABELS[o],
                   int(o == "morning"), int(o == "evening"), int(o == "night"), int(o == "holiday"), None])
        ws.cell(ws.max_row, 3).number_format = "dd/mm/yyyy"

    sm = wb.create_sheet("ملخص")
    sm.sheet_view.rightToLeft = True
    sm.append(["رقم الموظف", "الاسم"] + [al.SUMMARY_TITLES[k] for k in kinds] + ["إجمالي الأيام"])
    for r in rep["rows"]:
        sm.append([r["employee_id"], r["name"]] + [r[k]["count"] for k in kinds] + [r["total_count"]])
    t = rep["totals"]
    sm.append(["Total", None] + [t[k]["count"] for k in kinds] + [t["total_count"]])
    sm.append([])
    sm.append([f"{rep['from']} → {rep['to']}" + (f" · {rep['team']}" if rep["team"] else "") + (f" · {rep['status_note']}" if rep.get("status_note") else "")])

    bold, fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    for sheet in (ws, sm):
        for c in sheet[1]:
            c.font, c.fill = bold, fill
            c.alignment = Alignment(horizontal="center", vertical="center")
        for i in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(i)].width = 38 if i == 2 else 15
        sheet.freeze_panes = sheet.cell(2, 3)
    sm.cell(sm.max_row - 2, 1).font = bold

    ns = wb.create_sheet("غير محسوب")                          # days that had punches but did not count, and why
    ns.sheet_view.rightToLeft = True
    ns.append(["employee_ID", "employee_name ", "date", "check-in", "check-out", "reason"])
    for s in rep["skipped"]:
        ns.append([s["employee_id"], rep["names"].get(s["employee_id"], ""), date.fromisoformat(s["date"]), s["in_time"], s["out_time"], s["reason"]])
        ns.cell(ns.max_row, 3).number_format = "dd/mm/yyyy"
    for c in ns[1]:
        c.font, c.fill = bold, fill
    for i, w in enumerate((14, 38, 14, 12, 12, 80), start=1):
        ns.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
