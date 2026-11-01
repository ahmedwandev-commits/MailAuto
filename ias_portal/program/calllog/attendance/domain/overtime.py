"""Overtime and the expected shift allowance (transport + meals) of a person - pure functions, no I/O (v5.17, owner's rules).

Every number is a setting (``services/settings.OVERTIME_DEFAULTS``, Settings › Rules & allowance › Overtime & meals):

Overtime of a **working day**
  The overtime starts once the person has stayed ``base_hours`` (8) hours since the check-in. Each overtime minute is weighted by the
  clock: before ``evening_from`` (18:00) × ``day_rate`` (1), from 18:00 to ``night_from`` (22:00) × ``evening_rate`` (1.5), from 22:00
  to ``night_until`` (06:00) × ``night_rate`` (1.75).
Overtime of a **weekly rest day / official holiday** worked
  No compensatory rest day taken on a *working day of the following week* (the week starts on ``work_week_start``, Sunday; its working
  days are the weekdays that are not weekly rest days - Sunday to Thursday - and not official holidays): every hour of that day ×
  ``holiday_rate`` (2). With such a comp day: no overtime at all - only the holiday's shift allowance. A day off taken on the next Friday
  or Saturday is a rest day anyway, so it is not a comp day (owner, v5.17).
The **week and the month**
  Every week (it starts on the ``work_week_start`` weekday, cut at the month's edges) ``weekly_deduction_hours`` (2) come off that
  week's weighted overtime, never below 0. The month is capped at ``cap_pct`` (40 %) of a normal month: ``base_days`` (22) days ×
  ``base_hours`` (8) = 176 h -> 70.4 h. The percentage shown is the month's (capped) overtime of those 176 h.
**Meals** of a day the shift allowance counts
  An employee: morning -> breakfast, evening -> evening meal, night -> dinner. A supervisor (a manager) is paid the morning shift only,
  so: morning -> breakfast. Anybody who stays ``long_day_hours`` (10) hours or more gets one more meal (lunch), on any day.
"""
from datetime import date, datetime, timedelta

BANDS = ("day", "evening", "night")
SHIFT_MEAL = {"morning": "breakfast", "evening": "evening_meal", "night": "dinner"}
MEALS = ("breakfast", "evening_meal", "dinner", "lunch")
MEAL_NAMES = {"breakfast": "Breakfast", "evening_meal": "Evening meal", "dinner": "Dinner", "lunch": "Lunch (10 h day)"}


def _min(hhmm):
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def band_of(minute_of_day, ot):
    """'day' | 'evening' | 'night' for a minute of the day (0..1439). The night band runs over midnight."""
    ev, ni, nu = _min(ot["evening_from"]), _min(ot["night_from"]), _min(ot["night_until"])
    if ni <= nu:                                   # a night band that does not cross midnight (unusual settings)
        if ni <= minute_of_day < nu:
            return "night"
    elif minute_of_day >= ni or minute_of_day < nu:
        return "night"
    if ev <= minute_of_day < ni:
        return "evening"
    return "day"


def split_bands(start, end, ot):
    """Minutes between two datetimes in each clock band: {'day': m, 'evening': m, 'night': m}."""
    out = {b: 0.0 for b in BANDS}
    if end <= start:
        return out
    marks = sorted({_min(ot["evening_from"]), _min(ot["night_from"]), _min(ot["night_until"])})
    t = start
    while t < end:
        m = t.hour * 60 + t.minute
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = next((x for x in marks if x > m), None)
        boundary = midnight + timedelta(minutes=nxt) if nxt is not None else midnight + timedelta(days=1, minutes=marks[0])
        seg_end = min(end, boundary)
        out[band_of(m, ot)] += (seg_end - t).total_seconds() / 60.0
        t = seg_end
    return out


def rate_of(band, ot):
    return float(ot[{"day": "day_rate", "evening": "evening_rate", "night": "night_rate"}[band]])


def working_day_overtime(tin, tout, ot):
    """-> {'minutes': {band: m}, 'raw': h, 'weighted': h} for a working day: the minutes after ``base_hours`` of stay."""
    start = tin + timedelta(hours=float(ot["base_hours"]))
    bands = split_bands(start, tout, ot) if tout > start else {b: 0.0 for b in BANDS}
    raw = sum(bands.values()) / 60.0
    weighted = sum(bands[b] * rate_of(b, ot) for b in BANDS) / 60.0
    return {"minutes": bands, "raw": raw, "weighted": weighted}


def holiday_overtime(stay_hours, comp_taken, ot):
    """A weekly rest day / official holiday worked: every hour × ``holiday_rate`` unless a comp day was taken (then nothing)."""
    if comp_taken:
        return {"raw": 0.0, "weighted": 0.0}
    return {"raw": stay_hours, "weighted": stay_hours * float(ot["holiday_rate"])}


def meals_of(kind, is_manager, stay_hours, ot):
    """The meals of one day: ``kind`` is the shift-allowance kind the day was counted as (None = not counted)."""
    out = []
    if kind in SHIFT_MEAL and (not is_manager or kind == "morning"):
        out.append(SHIFT_MEAL[kind])
    if stay_hours is not None and stay_hours >= float(ot["long_day_hours"]):
        out.append("lunch")
    return out


def comp_window(day, first_weekday, rest_weekdays, holidays):
    """The dates on which a comp day for working ``day`` (a rest day / holiday) counts: the working days of the following week."""
    nxt = week_start(day, first_weekday) + timedelta(days=7)
    days = (nxt + timedelta(days=i) for i in range(7))
    return [d.isoformat() for d in days if d.weekday() not in rest_weekdays and d.isoformat() not in holidays]


def week_start(day, first_weekday):
    """The first day of the week ``day`` is in; ``first_weekday`` 0=Mon ... 6=Sun."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return d - timedelta(days=(d.weekday() - int(first_weekday)) % 7)


def month_totals(days, ot, first_weekday):
    """days: [{'date', 'weighted', 'raw'}] of one month -> weeks (with the deduction), the month's overtime, the cap and the %.
    Returns {'weeks': [{'start', 'end', 'raw', 'weighted', 'deduction', 'net'}], 'raw', 'weighted', 'deducted', 'net', 'cap',
    'capped', 'over_cap', 'pct', 'cap_pct', 'base_hours_month'}."""
    weeks = {}
    for d in days:
        ws = week_start(d["date"], first_weekday)
        w = weeks.setdefault(ws, {"start": d["date"], "end": d["date"], "raw": 0.0, "weighted": 0.0})
        w["start"], w["end"] = min(w["start"], d["date"]), max(w["end"], d["date"])
        w["raw"] += d.get("raw") or 0.0
        w["weighted"] += d.get("weighted") or 0.0
    out_weeks = []
    for ws in sorted(weeks):
        w = weeks[ws]
        ded = min(float(ot["weekly_deduction_hours"]), w["weighted"])
        out_weeks.append({**w, "deduction": ded, "net": w["weighted"] - ded})
    base_month = float(ot["base_days"]) * float(ot["base_hours"])
    cap = base_month * float(ot["cap_pct"]) / 100.0
    net = sum(w["net"] for w in out_weeks)
    capped = min(net, cap)
    return {"weeks": out_weeks, "raw": sum(w["raw"] for w in out_weeks), "weighted": sum(w["weighted"] for w in out_weeks),
            "deducted": sum(w["deduction"] for w in out_weeks), "net": net, "cap": cap, "capped": capped,
            "over_cap": max(0.0, net - cap), "pct": (100.0 * capped / base_month) if base_month else 0.0,
            "cap_pct": float(ot["cap_pct"]), "base_hours_month": base_month}


def at(day, hhmm):
    t = str(hhmm)
    return datetime.fromisoformat(f"{day}T{t if len(t) > 5 else t + ':00'}")
