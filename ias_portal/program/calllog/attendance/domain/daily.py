"""Planned-versus-actual evaluation of one employee-day, and period summaries.

The schedule is the single source of truth for what was *planned*; the HR attendance file only says what happened.
Nothing is called an absence unless attendance data for that date exists, the employee has no punches, no leave, no
holiday and no mission - and even then it is "absent (unverified)" until someone reviews it (see scoring).
"""
import math
from datetime import datetime, timedelta

from .shifts import interval, scheduled_hours
from .textnorm import norm

# primary statuses
SCHEDULED, NO_DATA, PRESENT, LATE, WORKING = "scheduled", "no_data", "present", "late", "currently_working"
MISSING_PUNCH, ABSENT, EXCUSED, LEAVE, REST = "missing_punch", "absent", "excused_absence", "on_leave", "rest_day"
REST_WORKED, HOLIDAY, STANDBY, NO_SCHEDULE, MISSION, UNSCHEDULED = (
    "rest_day_worked", "holiday", "standby", "no_schedule", "on_mission", "present_unscheduled")
NOT_ARRIVED = "not_arrived"          # today, the shift has started and nobody has checked in yet (not an absence)

ATTENDED = {PRESENT, LATE, WORKING, MISSING_PUNCH, MISSION, UNSCHEDULED}


def _dt(day_iso, hms):
    return datetime.fromisoformat(f"{day_iso}T{hms}") if hms else None


def _minutes(a, b):
    return int((a - b).total_seconds() // 60)


def _next_day(day_iso):
    return (datetime.fromisoformat(day_iso) + timedelta(days=1)).date().isoformat()


def _permit(att, labels):
    """Does the HR permit text of this day match one of the configured excuse labels?"""
    p = norm(att.get("permit_text")) if att else ""
    return bool(p) and any(norm(x) in p for x in (labels or []))


def evaluate_day(ctx):
    """ctx keys: employee_id, date, planned (list of entries), shifts, att, att_next, data_loaded, data_loaded_next,
    leave ({'type','source'} | None), holiday (bool), review ('unexcused'|'excused'|None), settings, now.
    Returns a flat dict (JSON-friendly) - see the end of the function for every key."""
    s = ctx["settings"]
    day, shifts = ctx["date"], ctx["shifts"]
    now = ctx["now"]
    att = ctx.get("att")
    work = [e for e in ctx["planned"] if shifts.get(e["shift_code"], {}).get("kind") == "work"]
    off = any(e["shift_code"] == "OFF" for e in ctx["planned"])
    standby = any(shifts.get(e["shift_code"], {}).get("kind") == "standby" for e in ctx["planned"])
    has_punch = bool(att and (att.get("in_time") or att.get("out_time")))
    out = {"employee_id": ctx["employee_id"], "date": day, "planned_codes": [e["shift_code"] for e in ctx["planned"]],
           "planned_marks": {e["shift_code"]: e["note"] for e in ctx["planned"] if e.get("note") and e["shift_code"] in shifts and shifts[e["shift_code"]].get("kind") == "work"},
           # a rest day the manager planned as a comp day (Team schedule): counted as a rest day, shown as such
           "planned_comp": any(e["shift_code"] == "OFF" and str(e.get("note") or "").startswith("Comp day") for e in ctx["planned"]),
           "status": NO_SCHEDULE, "flags": [], "expected_start": None, "expected_end": None, "actual_in": None,
           "actual_out": None, "late_minutes": 0, "early_minutes": 0, "excused_late_minutes": 0, "scheduled_hours": 0.0, "actual_hours": None,
           "needs_replacement": False, "leave_type": None, "note": "", "default_shift": False,
           "late_permit": None, "early_permit": None, "late_raw_minutes": 0, "early_raw_minutes": 0, "expected_present": False}
    reviews = ctx.get("permit_reviews") or {}

    def done(status):
        out["status"] = status
        return out

    if work:
        starts, ends = [], []
        for e in work:
            sh = shifts[e["shift_code"]]
            st, en = interval(day, sh["start_time"], sh["end_time"])
            starts.append(st), ends.append(en)
            out["scheduled_hours"] += scheduled_hours(sh["start_time"], sh["end_time"])
        start, end = min(starts), max(ends)
        out["expected_start"], out["expected_end"] = start.strftime("%H:%M"), end.strftime("%H:%M")
        if len(work) > 1:
            out["flags"].append("multiple_shifts")
        out["default_shift"] = all(e.get("source") == "default" for e in work)
    out["planned_primary"] = work[0]["shift_code"] if work else (
        "OFF" if off else ("SB" if standby else None))

    leave = ctx.get("leave")
    if leave and not has_punch:
        out["leave_type"] = leave["type"]
        out["needs_replacement"] = bool(work) and not out["default_shift"]
        return done(LEAVE)
    if leave:
        out["flags"].append("punch_on_leave")
    if not work:
        if off:
            if has_punch:
                out["flags"].append("worked_on_rest_day")
                out["actual_in"], out["actual_out"] = (att.get("in_time"), att.get("out_time"))
                return done(REST_WORKED)
            return done(REST)
        if standby:
            return done(STANDBY)
        if ctx.get("holiday") and not has_punch:
            return done(HOLIDAY)
        if has_punch:
            out["actual_in"], out["actual_out"] = att.get("in_time"), att.get("out_time")
            out["flags"].append("no_schedule_for_day")
            return done(UNSCHEDULED)
        if ctx.get("hr_rest"):              # the HR calendar says weekly rest and nothing contradicts it
            return done(REST)
        return done(NO_SCHEDULE)

    # ---- there is a planned work shift
    if ctx.get("holiday") and not has_punch:
        return done(HOLIDAY)
    today = now.date().isoformat()
    out["expected_present"] = True
    if day > today or (day == today and now < start and not (att and att.get("in_time"))):
        return done(SCHEDULED)
    if day == today and not (att and att.get("in_time")) and now >= start:
        # the shift has started and there is no check-in: "not arrived" until the shift is over (then, if the day has data, an absence)
        if att and att.get("mission_text"):
            return done(MISSION)
        if not (ctx.get("data_loaded") and now >= end):
            if not ctx.get("data_loaded"):
                out["flags"].append("no_attendance_data_today")
            return done(NOT_ARRIVED)
    if not ctx.get("data_loaded"):
        return done(NO_DATA)

    a_in = _dt(day, att.get("in_time")) if att else None
    a_out = _dt(day, att.get("out_time")) if att else None
    crosses = end.date() > start.date()
    pending_out = False
    if crosses:
        # night shift: the OUT is on the next calendar day (either on this row, earlier than the IN, or on the next row)
        if a_out and a_in and a_out < a_in:
            a_out += timedelta(days=1)
        elif a_in:
            nxt = ctx.get("att_next")
            n_out = _dt(_next_day(day), nxt.get("out_time")) if nxt else None
            if nxt and nxt.get("out_time") and (not nxt.get("in_time") or n_out.hour < 14):
                a_out = n_out
            elif not ctx.get("data_loaded_next"):
                a_out, pending_out = None, True
            else:
                a_out = None
        elif a_out:   # only an OUT on this row: it belongs to the previous night's shift
            a_out = None
    out["actual_in"] = a_in.strftime("%H:%M:%S") if a_in else None
    out["actual_out"] = a_out.strftime("%H:%M:%S") if a_out else None

    if not a_in and not a_out and not pending_out:
        if att and att.get("mission_text"):
            return done(MISSION)
        out["flags"].append("missing_both")
        if att and att.get("permit_text"):
            out["flags"].append("has_permit")
        out["needs_replacement"] = not out["default_shift"]
        rv = ctx.get("review")
        if rv == "excused":
            return done(EXCUSED)
        if rv == "unexcused":
            out["flags"].append("absence_verified")
        return done(ABSENT)

    if a_in:
        late = _minutes(a_in, start)
        grace = int(s.get("grace_minutes", 0))
        hr_permit = _permit(att, s.get("permit_excuses_late"))
        review = reviews.get("late")
        excused = hr_permit or review in ("provided", "excused")
        if late > grace:
            raw = late if s.get("late_basis", "from_start") == "from_start" else late - grace
            if excused:
                out["excused_late_minutes"] = raw
                out["flags"].append("late_excused_by_permit" if hr_permit else "late_excused_by_review")
            else:
                out["late_minutes"] = raw
        # attendance permission (اذن حضور): needed when the check-in is MORE than the threshold after the shift start; exactly the threshold is fine
        late_s = (a_in - start).total_seconds()                       # seconds: "more than 15 minutes" means beyond 15:00 exactly
        out["late_raw_minutes"] = math.ceil(max(late_s, 0) / 60)
        if late_s > int(s.get("late_permit_minutes", 15)) * 60:
            out["late_permit"] = "provided" if hr_permit else (review or "required")
            if out["late_permit"] == "required":
                out["flags"].append("needs_late_permit")
        if abs(late) > float(s.get("punch_mismatch_hours", 4)) * 60:
            out["flags"].append("punch_far_from_shift")
    if a_out:
        early = _minutes(end, a_out)
        hr_permit = _permit(att, s.get("permit_excuses_early"))
        review = reviews.get("early")
        if early > int(s.get("early_grace_minutes", 0)):
            if hr_permit or review in ("provided", "excused"):
                out["flags"].append("early_excused_by_permit" if hr_permit else "early_excused_by_review")
            else:
                out["early_minutes"] = early
        # attendance permission (اذن انصراف): needed when the check-out is MORE than the threshold before the shift end
        early_s = (end - a_out).total_seconds()
        out["early_raw_minutes"] = math.ceil(max(early_s, 0) / 60)
        if early_s > int(s.get("early_permit_minutes", 15)) * 60:
            out["early_permit"] = "provided" if hr_permit else (review or "required")
            if out["early_permit"] == "required":
                out["flags"].append("needs_early_permit")
    if a_in and a_out:
        out["actual_hours"] = round((a_out - a_in).total_seconds() / 3600, 2)
    if a_in and not a_out and not pending_out:
        if day == today and now < end:
            return done(WORKING)
        out["flags"].append("missing_out")
        return done(MISSING_PUNCH)
    if pending_out:
        out["flags"].append("out_pending_next_day_data")
    if not a_in and a_out:
        out["flags"].append("missing_in")
        return done(MISSING_PUNCH)
    return done(LATE if out["late_minutes"] > 0 else PRESENT)


def summarize(days, repeat_threshold=3):
    """Aggregate a list of evaluate_day() results of one employee."""
    c = lambda pred: sum(1 for d in days if pred(d))  # noqa: E731
    scheduled = [d for d in days if d["planned_primary"] not in (None, "OFF", "SB")]
    judged = [d for d in scheduled if d["status"] not in (SCHEDULED, NO_DATA, HOLIDAY, LEAVE, NOT_ARRIVED)]
    present = c(lambda d: d["status"] in ATTENDED and d["planned_primary"] not in (None, "OFF", "SB"))
    late = [d for d in days if d["late_minutes"] > 0]
    actual_hours = sum(d["actual_hours"] or 0 for d in days if d["planned_primary"] not in ("OFF", "SB", None))
    sched_hours = sum(d["scheduled_hours"] for d in judged)
    return {
        "scheduled_days": len(scheduled),
        "judged_days": len(judged),
        "present_days": present,
        "absent_days": c(lambda d: d["status"] == ABSENT),
        "excused_absence_days": c(lambda d: d["status"] == EXCUSED),
        "verified_unexcused_days": c(lambda d: d["status"] == ABSENT and "absence_verified" in d["flags"]),
        "leave_days": c(lambda d: d["status"] == LEAVE),
        "rest_days": c(lambda d: d["status"] in (REST, REST_WORKED)),
        "worked_on_rest_day": c(lambda d: d["status"] == REST_WORKED),
        "planned_off": c(lambda d: "OFF" in d["planned_codes"]),
        "holiday_days": c(lambda d: d["status"] == HOLIDAY),
        "no_data_days": c(lambda d: d["status"] == NO_DATA),
        "no_schedule_days": c(lambda d: d["status"] == NO_SCHEDULE),
        "attendance_pct": round(100.0 * present / len(judged), 1) if judged else None,
        "late_count": len(late),
        "late_minutes": sum(d["late_minutes"] for d in late),
        "avg_late_minutes": round(sum(d["late_minutes"] for d in late) / len(late), 1) if late else 0,
        "repeated_lateness": len(late) >= repeat_threshold,
        "late_excused": c(lambda d: "late_excused_by_permit" in d["flags"] or "late_excused_by_review" in d["flags"]),
        "late_permit_required": c(lambda d: d.get("late_permit") == "required"),
        "early_permit_required": c(lambda d: d.get("early_permit") == "required"),
        "permits_reviewed": c(lambda d: d.get("late_permit") in ("provided", "excused", "rejected")
                              or d.get("early_permit") in ("provided", "excused", "rejected")),
        "early_count": c(lambda d: d["early_minutes"] > 0),
        "early_minutes": sum(d["early_minutes"] for d in days),
        "missing_in": c(lambda d: "missing_in" in d["flags"]),
        "missing_out": c(lambda d: "missing_out" in d["flags"]),
        "missing_both": c(lambda d: "missing_both" in d["flags"]),
        "scheduled_hours": round(sched_hours, 2),
        "actual_hours": round(actual_hours, 2),
        "hours_diff": round(actual_hours - sched_hours, 2),
        "exceptions": c(lambda d: any(f in d["flags"] for f in
                                      ("worked_on_rest_day", "punch_far_from_shift", "multiple_shifts",
                                       "no_schedule_for_day", "needs_late_permit", "needs_early_permit"))),
    }
