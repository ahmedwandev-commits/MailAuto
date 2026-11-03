"""Shift-allowance classification (بدل ورديات), from the HR punches and the portal's schedule. Counts only - no money.

Five kinds of day (rules: ``services/settings.ALLOWANCE_DEFAULTS``), four shift types in the file (صباحية / مسائية / ليلية / عطلة - the weekly
rest day and the official holiday are both «عطلة»). Who counts: **employees** for every kind; **managers for the morning shift and the
holidays** (a weekly rest day or an official holiday worked), never for an evening or night shift (owner's rule, v5.16).

A working day of an employee is classified in this order; the person must stay at least ``hours`` hours between check-in and check-out
(a night shift ends on the next day) in every case:

1. **The portal's schedule.** When the schedule has a shift (M / E / N) for that day - imported, or changed by the manager in the portal -
   that is the shift, whenever the person checked in (10:00 for an evening shift is still an evening shift).
2. **The check-in time.** No shift in the schedule: a check-in at the start of a shift (from ``early`` minutes before to ``grace`` minutes
   after it) is that shift.
3. **The time of the day**, for a *shift worker* (somebody who has shifts in the schedule of that month) on a day that has none: a check-in
   of the night window is night; leaving at or after ``evening_after`` (default 20:00) is evening; otherwise morning.

Anything else (a normal-day check-in of somebody without shifts, a short stay, no check-out) is listed as not counted with its reason.
A day worked on a weekly rest day or an official holiday counts whatever the time (``needs_hours`` adds the condition of the hours).
"""
from datetime import datetime, timedelta

KINDS = ("morning", "evening", "night", "rest_day", "official_holiday")           # the rules
SHIFT_KINDS = ("morning", "evening", "night")
DAY_KINDS = ("rest_day", "official_holiday")
OUT = ("morning", "evening", "night", "holiday")                                  # the four shift types of the file ("holiday" = both day kinds)
LABELS = {"morning": "صباحية", "evening": "مسائية", "night": "ليلية", "holiday": "عطلة",     # shift_type of the template
          "rest_day": "عطلة", "official_holiday": "عطلة"}
SUMMARY_TITLES = {"morning": "وردية صباحية", "evening": "وردية ظهيرة", "night": "وردية ليلية", "holiday": "ايام العطلات"}
ENGLISH = {"morning": "Morning", "evening": "Evening", "night": "Night", "holiday": "Holiday", "rest_day": "Weekly rest day",
           "official_holiday": "Official holiday"}
OUT_OF = {"morning": "morning", "evening": "evening", "night": "night", "rest_day": "holiday", "official_holiday": "holiday"}
SHIFT_CODES = {"morning": "M", "evening": "E", "night": "N"}
KIND_OF_CODE = {v: k for k, v in SHIFT_CODES.items()}
NORMAL_CODE = "D"           # the Normal day (08:00-16:00): a working day, not a shift - never paid the shift allowance
SOURCES = {"schedule": "portal schedule", "check-in": "check-in time", "time": "time of the day", "holiday": "holiday"}


def minutes(hhmm):
    parts = str(hhmm).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def enabled_kinds(rules):
    """The shift types the file has: a shift when it is on, "holiday" when a weekly rest day or an official holiday is on."""
    return [k for k in OUT if any(rules[r]["enabled"] for r in KINDS if OUT_OF[r] == k)]


def shift_of(check_in, rules):
    """'HH:MM[:SS]' -> morning | evening | night | None: the switched-on shift whose start the check-in is at (minutes, around midnight
    too); when two fit, the nearest start."""
    x = minutes(check_in)
    best = None
    for kind in SHIFT_KINDS:
        r = rules[kind]
        if not r["enabled"]:
            continue
        d = (x - minutes(r["start"]) + 720) % 1440 - 720            # signed distance to the start, -720..719
        if -int(r["early"]) <= d <= int(r["grace"]) and (best is None or abs(d) < best[0]):
            best = (abs(d), kind)
    return best[1] if best else None


def _at(day, hhmm):
    t = str(hhmm)
    return datetime.fromisoformat(f"{day}T{t if len(t) > 5 else t + ':00'}")


def stay(row, nxt, overnight):
    """Seconds between the check-in and the check-out of ``row`` (None = no check-out). A check-out earlier than the check-in is the next
    morning; for a shift that ends the next day the check-out may also be on the next day's row."""
    tin = _at(row["work_date"], row["in_time"])
    tout = None
    if row.get("out_time"):
        tout = _at(row["work_date"], row["out_time"])
        if tout < tin:
            tout += timedelta(days=1)
    if overnight and (tout is None or tout <= tin) and nxt and nxt.get("out_time") and (not nxt.get("in_time") or int(nxt["out_time"][:2]) < 14):
        tout = _at(nxt["work_date"], nxt["out_time"])
    return None if tout is None or tout <= tin else (tout - tin).total_seconds()


def _out_time(row, secs):
    """The check-out used for the stay (it may be on the next day's row): check-in + stay, as HH:MM:SS."""
    return None if secs is None else (_at(row["work_date"], row["in_time"]) + timedelta(seconds=secs)).strftime("%H:%M:%S")


def _hm(seconds):
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}"


def by_time(row, nxt, rules, evening_after):
    """Rule 3: -> (kind, stay seconds). A check-in of the night window is night; leaving at or after ``evening_after`` the same day is
    evening; everything else is morning."""
    n = rules["night"]
    x = minutes(row["in_time"])
    night_from = (minutes(n["start"]) - int(n["early"])) % 1440
    if x >= night_from or x < 4 * 60:
        return "night", stay(row, nxt, True)
    secs = stay(row, nxt, False)
    if secs is not None:
        out = _at(row["work_date"], row["in_time"]) + timedelta(seconds=secs)
        if out.date() > _at(row["work_date"], "00:00").date() or minutes(out.strftime("%H:%M")) >= minutes(evening_after):
            return "evening", secs
    return "morning", secs


def classify(att, d1, d2, rules, day_kind, plan=None, shift_workers=(), is_manager=lambda emp: False, evening_after="20:00", use_schedule=True):
    """att: {(employee_id, date): row}. ``day_kind(row)`` -> "rest_day" | "official_holiday" | None (a working day). ``plan`` is
    {(employee_id, date): "morning" | "evening" | "night"} from the schedule, ``shift_workers`` the ids that have shifts in that month.
    Returns (days, skipped): days = [{employee_id, date, kind, source, in_time, out_time, stay}]; skipped = [{employee_id, date, in_time,
    out_time, reason}]."""
    plan = plan or {}
    days, skipped = [], []

    def skip(r, reason):
        skipped.append({"employee_id": r["employee_id"], "date": r["work_date"], "in_time": r.get("in_time"), "out_time": r.get("out_time"),
                        "reason": reason})

    def verdict(r, kind, source, secs, need_hours):
        need = float(rules[kind].get("hours") or 0) * 3600
        if need_hours and need and (secs is None or secs < need):
            skip(r, "no check-out" if secs is None else f"stayed {_hm(secs)} of {_hm(need)}")
            return
        days.append({"employee_id": r["employee_id"], "date": r["work_date"], "kind": kind, "source": source, "in_time": r["in_time"],
                     "out_time": _out_time(r, secs) or r.get("out_time"), "stay": secs})

    for (emp, d), r in sorted(att.items()):
        if not (d1 <= d <= d2) or not (r.get("in_time") or r.get("out_time")):
            continue
        if not r.get("in_time"):
            skip(r, "no check-in")
            continue
        nxt = att.get((emp, (datetime.fromisoformat(d) + timedelta(days=1)).date().isoformat()))
        dk = day_kind(r)
        if dk:                                                                 # holidays count for everybody, managers included
            if not rules[dk]["enabled"]:
                skip(r, f"{ENGLISH[dk].lower()} is switched off in the allowance settings")
            else:
                verdict(r, dk, "holiday", stay(r, nxt, False), bool(rules[dk].get("needs_hours")))
            continue
        planned = plan.get((emp, d)) if use_schedule else None
        if planned == "normal":                                                # scheduled on the Normal day: not a shift
            skip(r, "a normal working day (08:00-16:00) in the schedule is not a shift")
            continue
        if is_manager(emp):                                                    # a manager: the morning shift only (and holidays, above)
            kind = planned or shift_of(r["in_time"], rules)
            if kind != "morning":
                skip(r, "a manager is paid the morning shift only" if kind else
                     f"check-in {r['in_time'][:5]} is not at the start of the morning shift (a manager is paid the morning shift only)")
            elif not rules["morning"]["enabled"]:
                skip(r, "morning is switched off in the allowance settings")
            else:
                verdict(r, "morning", "schedule" if planned else "check-in", stay(r, nxt, False), True)
            continue
        if planned:                                                            # 1. the portal's schedule (imported, or changed by the manager)
            if not rules[planned]["enabled"]:
                skip(r, f"{ENGLISH[planned].lower()} is switched off in the allowance settings")
                continue
            verdict(r, planned, "schedule", stay(r, nxt, planned == "night"), True)
            continue
        kind = shift_of(r["in_time"], rules)                                   # 2. the check-in is at the start of a shift
        if kind:
            verdict(r, kind, "check-in", stay(r, nxt, kind == "night"), True)
            continue
        if emp in shift_workers:                                               # 3. a shift worker on a day without a shift: by the time of day
            kind, secs = by_time(r, nxt, rules, evening_after)
            if kind == "morning":                                              # a day-time stay with no shift in the schedule is a normal day
                skip(r, f"check-in {r['in_time'][:5]} is a normal working day (no shift in the schedule, left before {evening_after})")
                continue
            if rules[kind]["enabled"]:
                verdict(r, kind, "time", secs, True)
                continue
        skip(r, f"check-in {r['in_time'][:5]} is not at the start of any switched-on shift and the schedule has no shift for the day")
    return days, skipped


def summarise(days):
    """{employee_id: {kind: {'count'}, 'morning'..'holiday': {'count'}, 'total_count'}}. ``kind`` is one of the five rules; the four shift
    types of the file (``OUT``) add the weekly rest day and the official holiday together as "holiday"."""
    out = {}
    for d in days:
        e = out.setdefault(d["employee_id"], {k: {"count": 0} for k in KINDS})
        e[d["kind"]]["count"] += 1
    for e in out.values():
        for o in OUT:
            e[o] = {"count": sum(e[k]["count"] for k in KINDS if OUT_OF[k] == o)}
        e["total_count"] = sum(e[o]["count"] for o in OUT)
    return out
