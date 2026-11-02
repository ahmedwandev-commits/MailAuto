"""Administrator pages: accounts, employees/teams, settings, shift types, holidays, scoring rules, audit log."""
import json
import re

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .. import auth
from ...ops import overlay
from ..services import scoring_service as sc, settings as settings_svc
from ..services.schedule_service import ImportError_
from ..domain import allowance as al, scoring
from ..services.analytics_service import rules_for

bp = Blueprint("att_admin", __name__)


def _pct(text, label):
    """'' -> None, '50' -> 50.0; must be between 0 and 100."""
    text = (text or "").strip().rstrip("%").strip()
    if not text:
        return None
    try:
        v = float(text)
    except ValueError as exc:
        raise ImportError_(f"{label}: enter a number between 0 and 100.") from exc
    if not 0 <= v <= 100:
        raise ImportError_(f"{label}: enter a number between 0 and 100.")
    return v


def _pct_map(text, label, upper=False):
    """'Team A=60; Team B=40' -> {'Team A': 60.0, 'Team B': 40.0}"""
    out = {}
    for part in (text or "").replace("\n", ";").split(";"):
        if not part.strip():
            continue
        name, sep, v = part.rpartition("=")
        if not sep or not name.strip():
            raise ImportError_(f"{label}: use the form name=percent, separated by ';'.")
        out[name.strip().upper() if upper else name.strip()] = _pct(v, label)
    return out


def _hhmm(text, label):
    import re
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ImportError_(f"{label}: enter a time as HH:MM.")
    return f"{int(m[1]):02d}:{m[2]}"


def _team_names(repo):
    """Every team that has at least one active portal account (employees and managers), without the 'every team' marker."""
    d = repo.directory
    teams = {d.att_team(u).strip() for u in d.store.users().values() if u.get("active", True) and u.get("role") != "admin"}
    return sorted((t for t in teams if t), key=str.lower)


@bp.route("/admin/roles", methods=["GET", "POST"])
@auth.admin_required
def roles():
    """What each role can do, who holds it, and the General Manager assignment. Administrators and managers are the portal's own
    roles (changed on the portal's Users page); the General Manager is an attendance role the administrator gives here."""
    from ..services.access import ROLE_MATRIX, ROLE_SUMMARY
    d = auth.directory()
    if request.method == "POST":
        uid = (request.form.get("user_id") or "").strip().lower()
        action = request.form.get("action")
        target = d.get_user(uid)
        if action in ("assign_att_admin", "remove_att_admin") and target:
            if g.att_user.get("hub_role") != "admin":          # only a portal administrator hands out every attendance right
                raise ImportError_("Only a portal administrator can change the Attendance administrators.")
            if target["hub_role"] == "admin":
                raise ImportError_("Portal administrators already have every attendance right.")
            if action == "assign_att_admin" and not target["active"]:
                raise ImportError_("This account is disabled.")
            before = d.set_att_admin(uid, action == "assign_att_admin")
            if before != (action == "assign_att_admin"):
                auth.audit("role_assigned" if action == "assign_att_admin" else "role_removed", "user", uid,
                           {"previous": "att_admin" if before else "none", "new": "att_admin" if action == "assign_att_admin" else "none",
                            "name": target["name"], "role": "att_admin"})
            flash("Saved.", "ok")
            return redirect(url_for("att_admin.roles"))
        if action in ("assign_allowance", "remove_allowance") and target:
            if target["hub_role"] == "admin":
                raise ImportError_("Administrators can always export the shift allowance.")
            if action == "assign_allowance" and not target["active"]:
                raise ImportError_("This account is disabled.")
            before = d.set_allowance(uid, action == "assign_allowance")
            if before != (action == "assign_allowance"):
                auth.audit("allowance_assigned" if action == "assign_allowance" else "allowance_removed", "user", uid, {"name": target["name"]})
            flash("Saved.", "ok")
            return redirect(url_for("att_admin.roles"))
        if action not in ("assign_gm", "remove_gm") or not target:
            raise ImportError_("Choose an existing portal account.")
        if target["hub_role"] == "admin":
            raise ImportError_("Administrators already have every permission; the General Manager role is for other accounts.")
        if action == "assign_gm" and not target["active"]:
            raise ImportError_("This account is disabled.")
        before = d.set_gm(uid, action == "assign_gm")
        if before != (action == "assign_gm"):
            auth.audit("role_assigned" if action == "assign_gm" else "role_removed", "user", uid,
                       {"previous": "gm" if before else (target["hub_role"] if target["hub_role"] == "manager" else "none"),
                        "new": "gm" if action == "assign_gm" else (target["hub_role"] if target["hub_role"] == "manager" else "none"),
                        "name": target["name"], "role": "gm"})
        flash("Saved.", "ok")
        return redirect(url_for("att_admin.roles"))
    candidates = sorted(d.gm_candidates(), key=lambda u: u["name"])
    return render_template("attendance/admin_roles.html", matrix=ROLE_MATRIX, summary=ROLE_SUMMARY, users=d.list_users(),
                           candidates=candidates, allowance_holders=[u for u in candidates if u["can_allowance"]],
                           att_admins=[u for u in candidates if u["att_admin"]], portal_admin=g.att_user.get("hub_role") == "admin")


@bp.route("/admin/employees", methods=["GET", "POST"])
@auth.admin_required
def employees():
    """The roster is the portal's accounts. In Workforce every person is in ONE team (owner, v5.17): the first team on the account,
    or the one chosen here when the account has several (for the E-mail module); "Unclassified" is never a Workforce team."""
    repo = auth.repo()
    d = repo.directory
    if request.method == "POST":
        uid = (request.form.get("user_id") or "").strip()
        u = d.store.get_user(uid)
        team = (request.form.get("team") or "").strip()
        if not u or team not in d.team_choices(u):
            raise ImportError_("Choose one of the teams on that person's account.")
        chosen = dict(settings_svc.load(repo).get("attendance_team_of") or {})
        before = d.att_team(u)
        chosen[uid] = team
        repo.save_settings({"attendance_team_of": chosen})
        auth.audit("attendance_team_set", "user", uid, {"previous": before, "new": team})
        flash(f"{u.get('name') or uid} is now in {team} in Workforce.", "ok")
        return redirect(url_for("att_admin.employees", q=request.form.get("q") or None))
    q = request.args.get("q", "").strip().lower()
    emps = repo.list_employees(False)
    users = d.store.users()
    for e in emps:
        u = users.get(str(e["id"])) or {}
        e["choices"] = d.team_choices(u)
        e["account_teams"] = overlay.user_teams(u)
    if q:
        emps = [e for e in emps if q in f"{e['id']} {e['name']} {' '.join(e['account_teams'])}".lower()]
    no_team = sorted((u for u in users.values() if u.get("active", True) and u.get("role") in ("user", "manager", "attendance")
                      and not d.att_teams(u)), key=lambda u: u.get("name") or u["user_id"])
    return render_template("attendance/admin_employees.html", emps=emps, q=q, teams=sorted({t for e in emps for t in e["teams"]}),
                           no_team=no_team, include_managers=bool(repo.get_settings().get("roster_include_managers", True)))


@bp.route("/admin/settings", methods=["GET", "POST"])
@auth.admin_required
def settings():
    repo = auth.repo()
    if request.method == "POST":
        a = request.form.get("action")
        if a == "rules":
            try:
                vals = {"grace_minutes": int(request.form.get("grace_minutes") or 0),
                        "early_grace_minutes": int(request.form.get("early_grace_minutes") or 0),
                        "late_basis": request.form.get("late_basis") if request.form.get("late_basis") in ("from_start", "after_grace") else "from_start",
                        "punch_mismatch_hours": float(request.form.get("punch_mismatch_hours") or 4),
                        "repeat_late_threshold": int(request.form.get("repeat_late_threshold") or 3),
                        "min_rest_hours": float(request.form.get("min_rest_hours") or 0),
                        "late_permit_minutes": int(request.form.get("late_permit_minutes") or 15),
                        "early_permit_minutes": int(request.form.get("early_permit_minutes") or 15),
                        "default_shift_enabled": bool(request.form.get("default_shift_enabled")),
                        "roster_include_managers": bool(request.form.get("roster_include_managers")),
                        "default_shift_code": (request.form.get("default_shift_code") or "D").strip().upper(),
                        "weekly_rest_weekdays": sorted({int(x) for x in request.form.getlist("rest_wd") if x.isdigit() and 0 <= int(x) <= 6}),
                        "min_staffing_pct": _pct(request.form.get("min_staffing_pct"), "Minimum staffing %"),
                        "min_staffing_pct_team": _pct_map(request.form.get("min_staffing_pct_team"), "Team thresholds"),
                        "min_staffing_pct_shift": _pct_map(request.form.get("min_staffing_pct_shift"), "Shift thresholds", upper=True)}
                if vals["late_permit_minutes"] < 0 or vals["early_permit_minutes"] < 0:
                    raise ImportError_("Permit thresholds cannot be negative.")
                shifts_now = {s["code"]: s for s in repo.list_shift_types()}
                if vals["default_shift_enabled"]:
                    sh = shifts_now.get(vals["default_shift_code"])
                    if not sh or sh["kind"] != "work":
                        raise ImportError_("The default shift must be an existing working shift type.")
                    start = _hhmm(request.form.get("default_start") or sh["start_time"], "Default shift start")
                    end = _hhmm(request.form.get("default_end") or sh["end_time"], "Default shift end")
                    if (start, end) != (sh["start_time"], sh["end_time"]):
                        repo.save_shift_type({**sh, "start_time": start, "end_time": end})
                        auth.audit("shift_type_saved", "shift_type", sh["code"], {"previous": [sh["start_time"], sh["end_time"]], "new": [start, end],
                                                                               "reason": "default shift"})
                for key in ("comp_day_labels", "permit_excuses_late", "permit_excuses_early"):
                    vals[key] = [x.strip() for x in request.form.get(key, "").split(";") if x.strip()]
            except ValueError as exc:
                raise ImportError_("Enter numbers in the numeric fields.") from exc
            old = settings_svc.load(repo)
            repo.save_settings(vals)
            auth.audit("settings_changed", "settings", "rules", {"previous": {k: old[k] for k in vals}, "new": vals})
        elif a == "team_managers":                             # ONE manager per team (owner, v5.17)
            chosen = {}
            for t in _team_names(repo):
                ids = {m["user_id"] for m in repo.directory.team_manager_candidates(t)}
                pick = (request.form.get("mgr:" + t) or "").strip()
                chosen[t] = [pick] if pick in ids else []       # [] = nobody (an explicit choice)
            old = settings_svc.load(repo)
            repo.save_settings({"team_managers": chosen})
            auth.audit("settings_changed", "settings", "team_managers", {"previous": old["team_managers"], "new": chosen})
        elif a == "allowance":
            rules = {}
            for k in al.KINDS:
                label = al.ENGLISH[k]
                r = {"enabled": bool(request.form.get(f"{k}_enabled"))}
                try:
                    r["hours"] = float(request.form.get(f"{k}_hours") or 0)
                    r["rate"] = float(request.form.get(f"{k}_rate") or 0)
                    if k in al.SHIFT_KINDS:
                        r["early"] = int(request.form.get(f"{k}_early") or 0)
                        r["grace"] = int(request.form.get(f"{k}_grace") or 0)
                except ValueError as exc:
                    raise ImportError_(f"{label}: enter numbers for the hours and the minutes.") from exc
                if not 0 <= r["hours"] <= 24:
                    raise ImportError_(f"{label}: the hours must be between 0 and 24.")
                if not 0 <= r["rate"] <= 100000:
                    raise ImportError_(f"{label}: the amount per day must be between 0 and 100,000.")
                if k in al.SHIFT_KINDS:
                    r["start"] = _hhmm(request.form.get(f"{k}_start"), f"{label} check-in time")
                    if not (0 <= r["early"] <= 360 and 0 <= r["grace"] <= 180):
                        raise ImportError_(f"{label}: minutes before the start 0-360, minutes after 0-180.")
                else:
                    r["needs_hours"] = bool(request.form.get(f"{k}_needs_hours"))
                rules[k] = r
            vals = {"allowance_rules": rules, "allowance_use_schedule": bool(request.form.get("use_schedule")),
                    "allowance_evening_after": _hhmm(request.form.get("evening_after"), "Evening if left at or after"),
                    "allowance_reminders": bool(request.form.get("reminders")),
                    "allowance_reminder_time": _hhmm(request.form.get("reminder_time") or "10:00", "Reminder time"),
                    "allowance_currency": (request.form.get("currency") or "EGP").strip()[:8] or "EGP"}
            old = settings_svc.load(repo)
            repo.save_settings(vals)
            auth.audit("settings_changed", "settings", "allowance", {"previous": {"allowance_rules": settings_svc.allowance_rules(old),
                                                                                  "allowance_use_schedule": old["allowance_use_schedule"],
                                                                                  "allowance_evening_after": old["allowance_evening_after"]}, "new": vals})
        elif a == "overtime":                                  # v5.17: overtime and meals (My attendance)
            vals, limits = {}, {"base_hours": (1, 24), "day_rate": (0, 5), "evening_rate": (0, 5), "night_rate": (0, 5), "holiday_rate": (0, 5),
                                "weekly_deduction_hours": (0, 40), "cap_pct": (0, 200), "base_days": (1, 31),
                                "long_day_hours": (1, 24), "meal_breakfast": (0, 100000), "meal_evening_meal": (0, 100000),
                                "meal_dinner": (0, 100000), "meal_lunch": (0, 100000)}
            for k, (lo, hi) in limits.items():
                try:
                    v = float(request.form.get("ot_" + k, settings_svc.OVERTIME_DEFAULTS[k]))
                except ValueError as exc:
                    raise ImportError_("Overtime & meals: enter numbers in the numeric fields.") from exc
                if not lo <= v <= hi:
                    raise ImportError_(f"Overtime & meals: {k.replace('_', ' ')} must be between {lo:g} and {hi:g}.")
                vals[k] = int(v) if k == "base_days" else v
            for k in settings_svc.OVERTIME_TIMES:
                vals[k] = _hhmm(request.form.get("ot_" + k) or settings_svc.OVERTIME_DEFAULTS[k], k.replace("_", " ").capitalize())
            if not vals["evening_from"] < vals["night_from"]:
                raise ImportError_("Overtime & meals: the evening rate must start before the night rate.")
            old = settings_svc.load(repo)
            repo.save_settings({"overtime_rules": vals})
            auth.audit("settings_changed", "settings", "overtime", {"previous": settings_svc.overtime_rules(old), "new": vals})
        elif a == "team_plan":
            def addresses(field):
                found = [x.strip() for x in re.split(r"[;,\s]+", request.form.get(field) or "") if x.strip()]
                bad = [x for x in found if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x)]
                if bad:
                    raise ImportError_(f"Not an e-mail address: {', '.join(bad[:3])}")
                return list(dict.fromkeys(found))[:40]
            vals = {"team_plan_to": addresses("team_plan_to"), "team_plan_cc": addresses("team_plan_cc")}
            old = settings_svc.load(repo)
            repo.save_settings(vals)
            auth.audit("settings_changed", "settings", "team_plan", {"previous": {k: old.get(k) for k in vals}, "new": vals})
        elif a == "shift":
            code = (request.form.get("code") or "").strip().upper()
            kind = request.form.get("kind")
            if not code or kind not in ("work", "off", "standby"):
                raise ImportError_("Code and kind are required.")
            start, end = request.form.get("start") or None, request.form.get("end") or None
            if kind == "work":              # a working shift without readable times would break every page that evaluates a day
                start, end = _hhmm(start, "Shift start"), _hhmm(end, "Shift end")
            try:
                order = int(request.form.get("order") or 5)
            except ValueError as exc:
                raise ImportError_("The order must be a whole number.") from exc
            repo.save_shift_type({"code": code, "name": request.form.get("name") or code, "kind": kind,
                                  "start_time": start, "end_time": end, "sort_order": order})
            auth.audit("shift_type_saved", "shift_type", code, dict(request.form))
        elif a == "holiday_add":
            repo.add_holiday(request.form.get("date", ""), request.form.get("name", "Holiday"))
            auth.audit("holiday_added", "holiday", request.form.get("date"))
        elif a == "holiday_remove":
            repo.remove_holiday(request.form.get("date", ""))
            auth.audit("holiday_removed", "holiday", request.form.get("date"))
        elif a == "leave_type":
            repo.save_leave_type({"code": (request.form.get("code") or "").strip().lower(), "name": request.form.get("name") or "",
                                  "counts_against_balance": bool(request.form.get("counts"))})
            auth.audit("leave_type_saved", "leave_type", request.form.get("code"))
        flash("Saved.", "ok")
        return redirect(url_for("att_admin.settings"))
    st = settings_svc.load(repo)
    return render_template("attendance/admin_settings.html", st=st, shifts=repo.list_shift_types(), holidays=repo.list_holidays(),
                           leave_types=repo.list_leave_types(),
                           team_members={t: repo.directory.team_manager_candidates(t) for t in _team_names(repo)},
                           team_manager={t: repo.directory.team_manager_of(t) for t in _team_names(repo)},
                           allowance_rules=settings_svc.allowance_rules(st),
                           ot=settings_svc.overtime_rules(st),
                           allowance_kinds=[(k, al.ENGLISH[k], k in al.SHIFT_KINDS) for k in al.KINDS],
                           team_pct_text="; ".join(f"{k}={v:g}" for k, v in st["min_staffing_pct_team"].items()),
                           shift_pct_text="; ".join(f"{k}={v:g}" for k, v in st["min_staffing_pct_shift"].items()),
                           default_shift=next((s for s in repo.list_shift_types() if s["code"] == st["default_shift_code"]), None))


@bp.route("/admin/scoring", methods=["GET", "POST"])
@auth.admin_required
def scoring_rules():
    repo = auth.repo()
    if request.method == "POST":
        sc.save_new_version(repo, g.att_user, request.form, request.form.get("effective_from", ""), request.form.get("note"))
        flash("New scoring-rule version saved. Earlier versions are kept and still apply to periods before the effective date.", "ok")
        return redirect(url_for("att_admin.scoring_rules"))
    rules, version = rules_for(repo, auth.today())
    return render_template("attendance/admin_scoring.html", form=sc.to_form(rules), version=version, versions=repo.scoring_rule_versions(),
                           rules_text=scoring.describe_rules(rules), kinds=[(k, scoring.LABELS[k]) for k in scoring.ORDER],
                           configured=scoring.is_configured(rules), today=auth.today(), presets=sc.PRESETS)


@bp.post("/admin/scoring/preview")
@auth.admin_required
def scoring_preview():
    """The score of an invented month under the rules as they are typed on the page (nothing is saved)."""
    f = request.form
    try:
        rules = sc.parse_rules(f)
        sample = {"late": f.get("sample_late"), "early": f.get("sample_early"), "absences": f.get("sample_absences"), "missing": f.get("sample_missing"),
                  "rejected": f.get("sample_rejected"), "days": f.get("sample_days"), "consecutive": bool(f.get("sample_consecutive"))}
        return jsonify(sc.preview(rules, sample))
    except ImportError_ as exc:
        return jsonify(error=str(exc)), 400
    except ValueError:
        return jsonify(error="Enter whole numbers in the example."), 400


@bp.get("/admin/audit")
@auth.admin_required
def audit_log():
    action = request.args.get("action", "").strip() or None
    rows = auth.repo().list_audit(action=action, limit=int(request.args.get("limit", 300)))
    return render_template("attendance/admin_audit.html", rows=rows, action=action or "", json=json)
