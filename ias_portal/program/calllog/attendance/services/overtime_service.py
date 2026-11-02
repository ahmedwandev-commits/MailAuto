"""My attendance (v5.17): a person's month - check-in / check-out, the overtime (weighted, the weekly deduction, the 40 % cap) and the
expected shift allowance in money (the transport allowance of every counted day + the meals). The rules are in ``domain/overtime.py``;
which day counts as which shift is the shift-allowance classification (``domain/allowance.py`` via ``allowance_service.compute``), so a
day is never judged two different ways.

Days up to today come from the HR attendance (and the corrections); the rest of the month is *expected* from the schedule (the
transport allowance and the meals of every planned shift - an expectation, never overtime)."""
import calendar
from datetime import date, timedelta

from ..domain import allowance as al
from ..domain import overtime as otd
from ..domain.textnorm import norm
from . import settings as settings_svc
from .allowance_service import compute as allowance_compute, day_kind_fn
from .analytics_service import DefaultShift, apply_corrections
from .schedule_service import shifts_by_code

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def month_of(text, today):
    """'YYYY-MM' (anything else -> the month of ``today``)."""
    try:
        y, m = (int(x) for x in str(text or "").split("-"))
        if 2000 <= y <= 2100 and 1 <= m <= 12:
            return f"{y}-{m:02d}"
    except ValueError:
        pass
    return today[:7]


def _days(d1, d2):
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _night_checkin(in_time):
    m = al.minutes(in_time)
    return m >= 20 * 60 or m < 4 * 60


def build(repo, employees, month, today):
    """{employee_id: report} for ``employees`` (dicts with id / name / teams) in ``month`` ('YYYY-MM')."""
    y, m = (int(x) for x in month.split("-"))
    d1, d2 = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    st = settings_svc.load(repo)
    ot = settings_svc.overtime_rules(st)
    rules = settings_svc.allowance_rules(st)
    ids = [e["id"] for e in employees]
    managers = {e["id"] for e in employees if (repo.directory.get_user(str(e["id"])) or {}).get("hub_role") == "manager"}
    far = (date.fromisoformat(d2) + timedelta(days=15)).isoformat()            # comp days of the week after a holiday at the month's end

    upto = min(d2, today)
    counted = {}
    if upto >= d1:
        days, _skipped, rules, st = allowance_compute(repo, d1, upto, employees)
        counted = {(d["employee_id"], d["date"]): d for d in days}
    att = apply_corrections(repo.attendance_records(d1, far, ids), repo.corrections(d1, far, ids))
    rule = DefaultShift(repo, st, shifts_by_code(repo))
    day_kind = day_kind_fn(rule)

    plan, comp = {}, {i: set() for i in ids}
    for e in repo.schedule_entries(d1, far, ids):
        code = e["shift_code"]
        if code == "OFF" and str(e.get("note") or "").startswith("Comp day"):
            comp[e["employee_id"]].add(e["work_date"])                         # a comp day planned on the Team schedule
        elif code in al.KIND_OF_CODE and (e["employee_id"], e["work_date"]) not in plan:
            plan[(e["employee_id"], e["work_date"])] = code
    for c in repo.comp_days(ids):                                              # comp days recorded as taken (Leave › Compensatory days)
        if c.get("taken_date"):
            comp.setdefault(c["employee_id"], set()).add(c["taken_date"])
    comp_labels = [norm(x) for x in st.get("comp_day_labels", [])]
    for (emp, d), r in att.items():                                            # the HR file's «بدل راحة»
        p = norm(r.get("permit_text"))
        if p and any(c in p for c in comp_labels):
            comp.setdefault(emp, set()).add(d)

    first_weekday = int(st.get("work_week_start", 6))
    rate = {k: float(rules[k].get("rate") or 0) if rules[k].get("enabled") else 0.0 for k in al.KINDS}
    meal_price = {k: float(ot["meal_" + k]) for k in otd.MEALS}
    currency = st.get("allowance_currency") or "EGP"
    out = {}
    # every number comes from the administrator's HR sheet (never from the portal sign-in, which only shows that somebody started
    # working). A day whose sheet is not uploaded yet (the administrator may upload a day, two, or a week at once) is still expected
    # from the schedule, and becomes real as soon as its sheet is uploaded.
    loaded = repo.dates_with_attendance(d1, far)
    for emp in employees:
        eid, is_mgr = emp["id"], emp["id"] in managers
        rows = []
        for d in _days(d1, d2):
            r = att.get((eid, d))
            c = counted.get((eid, d))
            planned = plan.get((eid, d))
            row = {"date": d, "planned": planned, "in": None, "out": None, "stay": None, "day_kind": None, "kind": None,
                   "transport": 0.0, "meals": [], "meal_amount": 0.0, "raw": 0.0, "weighted": 0.0, "bands": None,
                   "note": "", "expected": False}
            has_punch = bool(r and r.get("in_time"))
            if d <= today and has_punch:
                nxt = att.get((eid, (date.fromisoformat(d) + timedelta(days=1)).isoformat()))
                secs = c["stay"] if c else al.stay(r, nxt, planned == "N" or _night_checkin(r["in_time"]))
                row["in"] = r["in_time"][:5]
                row["day_kind"] = day_kind(r)
                if secs:
                    tin = otd.at(d, r["in_time"])
                    tout = tin + timedelta(seconds=secs)
                    row["out"], row["stay"] = tout.strftime("%H:%M"), secs / 3600.0
                    if row["day_kind"]:
                        window = otd.comp_window(d, first_weekday, rule.rest_days, rule.hol)
                        taken = sorted(x for x in comp.get(eid, ()) if x in window)
                        h = otd.holiday_overtime(row["stay"], bool(taken), ot)
                        row.update(raw=h["raw"], weighted=h["weighted"])
                        row["note"] = (f"comp day {taken[0]} - no overtime" if taken else
                                       f"no comp day in the same or the next working week - hours x{float(ot['holiday_rate']):g}")
                    else:
                        w = otd.working_day_overtime(tin, tout, ot, rule.rest_days)
                        row.update(raw=w["raw"], weighted=w["weighted"], bands=w["minutes"])
                elif r.get("in_time"):
                    nd = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
                    row["note"] = ("waiting for the next day's HR sheet" if (planned == "N" or _night_checkin(r["in_time"])) and nd not in loaded
                                   else "no check-out" if d < today else "at work")
                if c:
                    row["kind"] = c["kind"]
                    row["transport"] = rate[c["kind"]]
                eats_as = row["kind"]
                if row["day_kind"] and row["stay"]:                           # a day off worked: the meal of its check-in time
                    eats_as = otd.meal_shift(r["in_time"], lambda t: al.shift_of(t, rules))
                row["meals"] = otd.meals_of(eats_as, is_mgr, row["stay"], ot)
                if d == today and not c and not row["stay"]:
                    row["expected"] = True                                    # today, still at work: the planned shift is expected
            elif d >= today:
                row["expected"] = True
            elif d not in loaded:                                              # a past day whose HR sheet is not uploaded yet
                row["expected"] = True
                row["note"] = "waiting for the HR sheet"
            if row["expected"] and not row["kind"] and planned:
                kind = al.KIND_OF_CODE[planned]
                if not is_mgr or kind == "morning":
                    row["kind"] = kind
                    row["transport"] = rate[kind]
                    row["meals"] = otd.meals_of(kind, is_mgr, None, ot)
            row["meal_amount"] = sum(meal_price[x] for x in row["meals"])
            rows.append(row)

        def money(part):
            sel = [x for x in rows if x["expected"] == part]
            meals = {k: sum(1 for x in sel for mm in x["meals"] if mm == k) for k in otd.MEALS}
            shifts = {k: sum(1 for x in sel if x["kind"] == k and x["transport"]) for k in al.KINDS}
            transport = sum(x["transport"] for x in sel)
            meal_amount = sum(x["meal_amount"] for x in sel)
            return {"transport": transport, "meals": meal_amount, "total": transport + meal_amount, "meal_counts": meals,
                    "shift_counts": shifts, "shifts": sum(shifts.values())}          # "shifts": what a team manager sees of the others
        so_far, rest = money(False), money(True)
        out[eid] = {"employee": emp, "is_manager": is_mgr, "month": month, "label": f"{MONTH_NAMES[m - 1]} {y}", "days": rows,
                    "so_far": so_far, "rest": rest, "expected_total": so_far["total"] + rest["total"],
                    "overtime": otd.month_totals([x for x in rows if not x["expected"]], ot, first_weekday),
                    "rates": rate, "meal_price": meal_price, "rules": ot, "currency": currency, "today": today,
                    "data_loaded": bool(repo.dates_with_attendance(d1, upto)) if upto >= d1 else False}
    return out
