"""Module settings. Every value below that encodes a *policy* is a placeholder that management must confirm - none of them
is an HR rule taken from a document, except the two attendance-permission thresholds and the default shift, which were
specified by management (15 minutes / 15 minutes / Normal Day 08:00-16:00) and remain configurable."""

DEFAULT_SETTINGS = {
    # --- attendance rules
    "grace_minutes": 0,                 # lateness grace (minutes); 0 = none
    "early_grace_minutes": 0,           # early-departure grace
    "late_basis": "from_start",         # from_start: late minutes count from the shift start once grace is exceeded; after_grace: minutes beyond grace
    "punch_mismatch_hours": 4,          # a punch further than this from the shift start is flagged as an exception
    "repeat_late_threshold": 3,         # "repeated lateness" = at least this many late arrivals in the period
    "min_rest_hours": 8,                # scheduling check: rest between two shifts (CONFIGURABLE - management decision pending)
    # --- attendance permissions (specified by management; both configurable, independently of each other)
    "late_permit_minutes": 15,          # check-in MORE than this many minutes after the shift start -> needs an attendance permit (اذن حضور)
    "early_permit_minutes": 15,         # check-out MORE than this many minutes before the shift end -> needs a departure permit (اذن انصراف)
    "late_permit_label": "اذن حضور",
    "early_permit_label": "اذن انصراف",
    # --- default shift: used on a working day when nobody assigned Morning / Evening / Night
    "default_shift_enabled": True,
    "roster_include_managers": True,    # portal managers are people to be scheduled too (untick: only "user" accounts are employees)
    "default_shift_code": "D",          # a shift type (Administration -> Settings); shipped as Normal Day 08:00-16:00
    "weekly_rest_weekdays": [4, 5],     # weekdays that are not working days when there is no HR calendar for the date (0=Mon ... 6=Sun): Fri, Sat
    # --- short-staffed detection: Staffing % = expected available / required x 100, required = employees planned for the team and shift,
    #     expected available = planned minus those on approved leave / compensatory day. No threshold configured = never flagged.
    "min_staffing_pct": None,           # e.g. 50 -> "Short staffed" below 50 %
    "min_staffing_pct_team": {},        # optional per-team override {"Team A": 60}
    "min_staffing_pct_shift": {},       # optional per-shift override {"N": 40}
    "team_managers": {},                # {"Team A": ["224147", ...]} the accounts shown as the team's manager(s) on Team availability
    # --- shift allowance export (بدل ورديات): how every kind of day is counted, set by the administrator in Settings;
    #     see ``allowance_rules`` (the saved values are layered over the defaults and over the first version of these settings)
    "allowance_rules": {},
    "allowance_use_schedule": True,     # the shift of a day is the one in the portal's schedule (imported or changed by the manager) when there is one
    "allowance_evening_after": "20:00", # a shift worker with no shift planned that day who leaves at or after this time is Evening
    # --- the monthly confirmation of the shift allowance (employees -> team managers -> General Manager -> administrative affairs)
    "allowance_reminders": True,        # a reminder e-mail every working day to whoever has not confirmed yet
    "allowance_reminder_time": "10:00",
    "allowance_currency": "EGP",
    # --- Team schedule (drag and drop): who gets the e-mail of the published month
    "team_plan_to": [],
    "team_plan_cc": [],
    # --- HR file interpretation
    "hr_workday_labels": ["يوم عمل"],
    "hr_weekly_rest_labels": ["العطلة الاسبوعية"],
    "leave_text_map": {"اعتيادية": "annual", "عارضة": "casual", "مرضي": "sick", "مرضية": "sick"},
    "default_leave_type_for_hr_text": "other",
    "comp_day_labels": ["بدل راحة"],                  # HR "permit" text meaning a compensatory rest day was taken (treated as leave type comp)
    "permit_excuses_late": ["اذن خاص حضور"],          # HR permit text that counts as the late-arrival permit
    "permit_excuses_early": ["اذن خاص انصراف"],       # HR permit text that counts as the early-departure permit
    # --- display
    "work_week_start": 6,               # calendar weeks start on this weekday (0=Mon ... 6=Sun)
}


def load(repo):
    return {**DEFAULT_SETTINGS, **repo.get_settings()}


# The kinds of day of the shift-allowance export (counts only - no money). A shift kind (morning / evening / night): the person stays
# ``hours`` hours between check-in and check-out, and - when the portal's schedule has no shift for the day - the check-in is at the
# start of the shift: from ``early`` minutes before ``start`` to ``grace`` minutes after it. A day worked on a weekly rest day or on an
# official holiday (``rest_day`` / ``official_holiday``) is counted whatever the time, with the hours as a condition only when
# ``needs_hours`` is on. ``allowance_evening_after`` and ``allowance_use_schedule`` are single settings (see domain/allowance.py).
ALLOWANCE_DEFAULTS = {          # ``rate``: the amount paid for one such day (EGP) - the transport allowance (owner, v5.17: 100 / 150 / 200 / 300)
    "morning": {"enabled": True, "start": "07:00", "early": 60, "grace": 15, "hours": 8, "rate": 100},
    "evening": {"enabled": True, "start": "14:00", "early": 60, "grace": 15, "hours": 8, "rate": 150},
    "night": {"enabled": True, "start": "22:00", "early": 60, "grace": 15, "hours": 8, "rate": 200},
    "rest_day": {"enabled": True, "needs_hours": True, "hours": 8, "rate": 300},
    "official_holiday": {"enabled": True, "needs_hours": True, "hours": 8, "rate": 300},
}


# Overtime and meals (v5.17, owner's rules - see domain/overtime.py). Saved by the administrator under "overtime_rules".
OVERTIME_DEFAULTS = {
    "base_hours": 8,                # a working day's hours: overtime starts after them
    "day_rate": 1.0,                # an overtime hour before evening_from
    "evening_from": "18:00", "evening_rate": 1.5,
    "night_from": "22:00", "night_rate": 1.75, "night_until": "06:00",
    "holiday_rate": 2.0,            # every hour of a rest day / holiday worked with no comp day on a working day of the following week
    "weekly_deduction_hours": 2,    # taken off every week's overtime
    "cap_pct": 40, "base_days": 22, # the month's overtime is at most cap_pct % of base_days x base_hours (40 % of 176 h = 70.4 h)
    "long_day_hours": 10,           # a stay of this many hours earns a meal (lunch)
    "meal_breakfast": 100, "meal_evening_meal": 100, "meal_dinner": 100, "meal_lunch": 100,   # EGP per meal
}
OVERTIME_TIMES = ("evening_from", "night_from", "night_until")


def overtime_rules(st):
    """The defaults with what the administrator saved on top (unknown keys are ignored)."""
    out = dict(OVERTIME_DEFAULTS)
    saved = st.get("overtime_rules") or {}
    if isinstance(saved, dict):
        out.update({k: v for k, v in saved.items() if k in OVERTIME_DEFAULTS})
    return out


def allowance_rules(st):
    """The allowance rule of every kind: defaults, then the values of the first version of these settings (one grace / early / hours for
    all shifts, kept so that nothing saved earlier is lost), then what the administrator saved per kind. Old money fields are ignored."""
    rules = {k: dict(v) for k, v in ALLOWANCE_DEFAULTS.items()}
    for key, field in (("allowance_grace_minutes", "grace"), ("allowance_early_minutes", "early"), ("allowance_hours", "hours")):
        if key in st:
            for kind in ("morning", "evening", "night"):
                rules[kind][field] = st[key]
            if field == "hours":
                rules["rest_day"]["hours"] = rules["official_holiday"]["hours"] = st[key]
    if "allowance_holiday_needs_hours" in st:
        rules["rest_day"]["needs_hours"] = rules["official_holiday"]["needs_hours"] = bool(st["allowance_holiday_needs_hours"])
    for kind, vals in (st.get("allowance_rules") or {}).items():
        if kind in rules and isinstance(vals, dict):
            old_money = "basis" in vals                    # the first version's money (per hour / per day) is not today's amount per day
            rules[kind].update({k: v for k, v in vals.items() if k in ALLOWANCE_DEFAULTS[kind] and not (old_money and k == "rate")})
    return rules


def threshold_for(st, team, shift_code):
    """Minimum staffing % for a team + shift: team override, then shift override, then the global value; None = not configured."""
    by_team = {str(k).strip().lower(): v for k, v in (st.get("min_staffing_pct_team") or {}).items()}
    if (team or "").strip().lower() in by_team:
        return float(by_team[(team or "").strip().lower()])
    by_shift = st.get("min_staffing_pct_shift") or {}
    if shift_code in by_shift:
        return float(by_shift[shift_code])
    v = st.get("min_staffing_pct")
    return None if v in (None, "") else float(v)
