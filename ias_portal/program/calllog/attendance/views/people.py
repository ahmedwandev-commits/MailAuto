"""Employees, attendance profile + score explanation, leave and compensatory days."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .. import auth
from ..services import access, analytics_service as an, leave_service as ls
from ..services.schedule_service import ImportError_
from . import month_bounds, parse_date

bp = Blueprint("att_people", __name__)


def _period():
    t = auth.now()
    d1, d2 = month_bounds(t.year, t.month)
    d1, d2 = parse_date(request.values.get("from"), d1), parse_date(request.values.get("to"), d2)
    return (d2, d1) if d2 < d1 else (d1, d2)


def _presets():
    """Quick period choices for the profile and score pages."""
    from datetime import timedelta
    today = auth.now().date()
    first = today.replace(day=1)
    last_prev = first - timedelta(days=1)
    return [("This month", *month_bounds(today.year, today.month)), ("Last month", *month_bounds(last_prev.year, last_prev.month)),
            ("Last 7 days", (today - timedelta(days=6)).isoformat(), today.isoformat()),
            ("Last 30 days", (today - timedelta(days=29)).isoformat(), today.isoformat())]


@bp.get("/employees")
@auth.login_required
def employees():
    q = request.args.get("q", "").strip().lower()
    team = request.args.get("team", "")
    repo = auth.repo()
    emps = access.visible_employees(repo, g.att_user)
    teams = sorted({t for e in emps for t in e["teams"]})
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    if q:
        emps = [e for e in emps if q in f"{e['id']} {e['name']} {e['department']}".lower()]
    today = {r["employee_id"]: r for r in an.overview(repo, g.att_user, auth.today(), auth.now())["all_rows"]}
    return render_template("attendance/employees.html", emps=emps, teams=teams, q=q, team=team, today=today, today_iso=auth.today())


@bp.get("/employee/<int:emp_id>")
@auth.login_required
def profile(emp_id):
    d1, d2 = _period()
    repo = auth.repo()
    rep = an.employee_report(repo, g.att_user, emp_id, d1, d2, auth.now())
    year = int(d2[:4])
    lv = ls.leave_totals(repo, g.att_user, emp_id, d1, d2)
    bal = ls.balances(repo, g.att_user, year, [emp_id])[emp_id]
    comp = ls.comp_balances(repo, g.att_user, [emp_id])[emp_id]
    comp_rows = [c for c in repo.comp_days([emp_id])]
    in_period = lambda d: bool(d) and d1 <= d <= d2  # noqa: E731
    rest = {"planned_off": sum(1 for d in rep["days"] if "OFF" in d["planned_codes"]),
            "worked_on_off": rep["summary"]["worked_on_rest_day"],
            "comp_earned": sum(c["days"] for c in comp_rows if in_period(c["earned_date"])),
            "comp_taken": sum(c["days"] for c in comp_rows if in_period(c["taken_date"]))}
    mgrs = an.managers_by_team(repo)
    manager = ", ".join(dict.fromkeys(m for t in rep["employee"]["teams"] for m in mgrs.get(t.strip().lower(), [])))
    return render_template("attendance/profile.html", rep=rep, d1=d1, d2=d2, leave=lv, bal=bal, comp=comp, rest=rest, manager=manager,
                           punch_reports=repo.punch_reports(d1, d2, [emp_id]), types=repo.list_leave_types(), year=year,
                           presets=_presets(), can_edit=g.att_user["role"] in ("admin", "manager"))

@bp.get("/employee/<int:emp_id>/score")
@auth.login_required
def score(emp_id):
    d1, d2 = _period()
    rep = an.employee_report(auth.repo(), g.att_user, emp_id, d1, d2, auth.now())
    return render_template("attendance/score.html", rep=rep, d1=d1, d2=d2, presets=_presets())


@bp.get("/api/employee/<int:emp_id>/summary")
@auth.login_required
def api_summary(emp_id):
    d1, d2 = _period()
    rep = an.employee_report(auth.repo(), g.att_user, emp_id, d1, d2, auth.now())
    return jsonify(employee=rep["employee"], summary=rep["summary"], score=rep["score"], rules_version=rep["rules_version"],
                   days=rep["days"])


# ---------------------------------------------------------------- leave
@bp.get("/leave")
@auth.login_required
def leave():
    from ..services import planning_service as ps
    repo = auth.repo()
    ids = access.visible_ids(repo, g.att_user)
    t = auth.now()
    try:
        year, month = int(request.args.get("year", t.year)), int(request.args.get("month", t.month))
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = t.year, t.month
    status = request.args.get("status", "")
    team = request.args.get("team", "")
    reqs = repo.leave_requests(ids, status=status or None)
    emps = {e["id"]: e for e in access.visible_employees(repo, g.att_user)}
    if team:
        reqs = [r for r in reqs if (emps.get(r["employee_id"]) or {}).get("team") == team]
    cal = ps.leave_calendar(repo, g.att_user, year, month, team or None)
    return render_template("attendance/leave.html", reqs=reqs, emps=emps, types=repo.list_leave_types(), year=year, month=month, status=status,
                           team=team, teams=sorted({t for e in emps.values() for t in e["teams"]}), cal=cal,
                           bal=ls.balances(repo, g.att_user, year, ids), today=auth.today(), tab=request.args.get("tab", "calendar"),
                           can_edit=g.att_user["role"] in ("admin", "manager"))


@bp.post("/leave/new")
@auth.writer_required
def leave_new():
    ls.request_leave(auth.repo(), g.att_user, int(request.form.get("emp", 0)), request.form.get("type", ""),
                     request.form.get("from", ""), request.form.get("to", ""), request.form.get("reason"),
                     request.form.get("days") or None)
    flash("Leave recorded.", "ok")
    return redirect(auth.safe_next(url_for("att_people.leave")))


@bp.post("/leave/<int:req_id>/cancel")
@auth.writer_required
def leave_cancel(req_id):
    ls.cancel_leave(auth.repo(), g.att_user, req_id, request.form.get("reason"))
    flash("Leave cancelled.", "ok")
    return redirect(auth.safe_next(url_for("att_people.leave")))


@bp.post("/leave/entitlement")
@auth.admin_required
def leave_entitlement():
    emp = int(request.form.get("emp", 0))
    access.require_employee(auth.repo(), g.att_user, emp)
    try:
        ent, opening = float(request.form.get("entitlement", "")), float(request.form.get("opening") or 0)
        year = int(request.form.get("year"))
    except ValueError as exc:
        raise ImportError_("Enter numbers for the year, entitlement and opening balance.") from exc
    auth.repo().set_entitlement(emp, year, request.form.get("type", ""), ent, opening)
    auth.audit("leave_entitlement_set", "employee", emp, {"year": year, "type": request.form.get("type"), "entitlement": ent,
                                                          "opening": opening})
    flash("Entitlement saved.", "ok")
    return redirect(url_for("att_people.leave", year=year))


# ---------------------------------------------------------------- compensatory days
@bp.get("/comp")
@auth.login_required
def comp():
    repo = auth.repo()
    ids = access.visible_ids(repo, g.att_user)
    emps = {e["id"]: e for e in access.visible_employees(repo, g.att_user)}
    return render_template("attendance/comp.html", rows=repo.comp_days(ids), emps=emps, bal=ls.comp_balances(repo, g.att_user, ids),
                           can_edit=g.att_user["role"] in ("admin", "manager"), today=auth.today())

@bp.post("/comp/new")
@auth.writer_required
def comp_new():
    ls.earn_comp(auth.repo(), g.att_user, int(request.form.get("emp", 0)), request.form.get("earned", ""), request.form.get("days"),
                 request.form.get("reason"), request.form.get("planned"))
    flash("Compensatory day recorded.", "ok")
    return redirect(url_for("att_people.comp"))


@bp.post("/comp/<int:comp_id>/update")
@auth.writer_required
def comp_update(comp_id):
    ls.update_comp(auth.repo(), g.att_user, comp_id, request.form.get("planned"), request.form.get("taken"))
    flash("Updated.", "ok")
    return redirect(url_for("att_people.comp"))
