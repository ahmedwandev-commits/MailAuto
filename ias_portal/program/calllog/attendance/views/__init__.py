"""HTML pages. Each module is a blueprint; they call ``services`` only (never SQL)."""
from datetime import date

from markupsafe import Markup

from . import ui
from ..domain import scoring

STATUS = {  # status -> (label, tone class, icon)
    "present": ("Present", "t-ok", "check-circle"), "late": ("Late", "t-warn", "clock"),
    "currently_working": ("Working now", "t-info", "user-check"), "missing_punch": ("Missing punch", "t-warn", "alert-circle"),
    "absent": ("Absent (unverified)", "t-danger", "user-x"), "excused_absence": ("Excused absence", "t-na", "check"),
    "on_leave": ("On leave", "t-info", "plane"), "rest_day": ("Rest day", "t-na", "coffee"),
    "rest_day_worked": ("Worked on rest day", "t-warn", "alert-triangle"), "holiday": ("Holiday", "t-na", "calendar"),
    "standby": ("Stand-by", "t-na", "shield"), "no_schedule": ("Not on shift roster", "t-na", "calendar"),
    "no_data": ("No attendance data", "t-na", "info"), "scheduled": ("Scheduled", "t-na", "clock"),
    "not_arrived": ("Not arrived", "t-warn", "user-x"),
    "on_mission": ("On mission", "t-ok", "user-check"), "present_unscheduled": ("Present (off-roster)", "t-na", "user"),
}
SHIFT_NAMES = {"M": "Morning", "E": "Evening", "N": "Night", "D": "Normal day", "SB": "Stand-by", "OFF": "Rest day",
               "CD": "Comp day"}
SHIFT_ICONS = {"M": "sun", "E": "sunset", "N": "moon", "D": "building", "SB": "shield", "OFF": "coffee", "CD": "repeat"}
REST_MARKS = (("Comp day", "CD"),)             # a rest day of the Team schedule, by the start of its note


def register_filters(app):
    @app.template_filter("status_label")
    def status_label(s):
        return STATUS.get(s, (s, "t-na", "info"))[0]

    @app.template_filter("status_class")
    def status_class(s):
        return STATUS.get(s, (s, "t-na", "info"))[1]

    @app.template_filter("status_icon")
    def status_icon(s):
        return STATUS.get(s, (s, "t-na", "info"))[2]

    @app.template_filter("plan_code")
    def plan_code(entry):
        """The code a schedule table shows: "CD" for a rest day the manager planned as a comp day (it stays "OFF" inside,
        so attendance treats it as a rest day)."""
        code = entry.get("shift_code", "")
        note = str(entry.get("note") or "")
        if code == "OFF":
            for prefix, shown in REST_MARKS:
                if note.startswith(prefix):
                    return shown
        return code

    @app.template_filter("hm")
    def hm(t):
        return (t or "")[:5] if t else ""

    @app.template_filter("dayname")
    def dayname(iso):
        return date.fromisoformat(iso).strftime("%a")

    @app.template_filter("dmy")
    def dmy(iso):
        return date.fromisoformat(iso).strftime("%d %b %Y") if iso else ""

    @app.template_filter("dm")
    def dm(iso):
        return date.fromisoformat(iso).strftime("%d %b") if iso else ""

    @app.template_filter("num")
    def num(v, nd=1):
        if v is None:
            return "—"
        if not isinstance(v, float):
            return str(v)
        s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    @app.template_filter("initials")
    def initials(name):
        parts = [p for p in (name or "").split() if p]
        return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")) if parts else "?"

    @app.template_filter("short_name")
    def short_name(name, n=3):
        return " ".join((name or "").split()[:n])

    @app.template_filter("ts")
    def ts(iso):
        return (iso or "")[:16].replace("T", " ")

    app.jinja_env.globals.update(STATUS=STATUS, SHIFT_NAMES=SHIFT_NAMES, SHIFT_ICONS=SHIFT_ICONS,
                                 donut=lambda *a, **k: Markup(ui.donut(*a, **k)), hbars=lambda *a, **k: Markup(ui.hbars(*a, **k)), strip=lambda *a, **k: Markup(ui.strip(*a, **k)),
                                 progress=lambda *a, **k: Markup(ui.progress(*a, **k)), describe=scoring.describe_rules)

    @app.context_processor
    def inject_nav():
        from flask import g, request
        if not g.get("att_user"):
            return {}
        return {"nav": ui.build_nav(g.att_user, request.path, request.args)}


def parse_date(text, default):
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return default


def month_bounds(year, month):
    import calendar
    last = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last:02d}"
