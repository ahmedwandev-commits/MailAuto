"""Workforce › Team schedule: a manager builds the month of their team by drag and drop, the General Manager approves it, the
administrative affairs add nights / days off / official holidays and publish it (``services/team_plan.py``). The Excel
upload (Schedule › Import) stays the other way."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .. import auth
from ..services import allowance_confirm as ac, team_plan as tp

bp = Blueprint("att_plan", __name__)


def _month():
    """The month to plan: the one asked, else next month (a schedule is made before its month)."""
    asked = ac.month_of(request.values.get("month"))
    if asked:
        return asked
    y, m = (int(x) for x in auth.today()[:7].split("-"))
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def _months_around(month):
    y, m = int(month[:4]), int(month[5:7])
    out = []
    for step in (-2, -1, 0, 1, 2, 3):
        mm = m + step
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        out.append(f"{yy:04d}-{mm:02d}")
    return out


@bp.get("/team-plan")
@auth.roles_required("admin", "gm", "manager")
def index():
    repo, me, month = auth.repo(), g.att_user, _month()
    team = request.args.get("team", "")
    ov = tp.overview(repo, me, month)
    if not team and me["role"] == "manager" and len(ov["teams"]) == 1:
        team = ov["teams"][0]["team"]                     # a manager of one team goes straight to it
    if team:
        return render_template("attendance/team_plan.html", b=tp.board(repo, me, month, team), ov=ov, me=me,
                               months=_months_around(month), affairs=tp.is_affairs(me))
    return render_template("attendance/team_plan_overview.html", ov=ov, me=me, months=_months_around(month),
                           affairs=tp.is_affairs(me), holidays=tp._holidays(repo, month), days=tp.work_days(repo, month))


def _back(month, team=""):
    return redirect(url_for("att_plan.index", month=month, team=team) if team else url_for("att_plan.index", month=month))


@bp.post("/team-plan/save")
@auth.roles_required("admin", "gm", "manager")
def save():
    body = request.get_json(silent=True) or {}
    res = tp.save(auth.repo(), g.att_user, body.get("month"), str(body.get("team") or ""), body.get("cells") or {})
    return jsonify(ok=True, **res)


@bp.post("/team-plan/submit")
@auth.roles_required("admin", "manager")
def submit():
    month, team = _month(), request.form.get("team", "")
    tp.submit(auth.repo(), g.att_user, month, team)
    flash(f"{team}: sent to the General Manager for approval.", "ok")
    return _back(month, team)


@bp.post("/team-plan/review")
@auth.roles_required("admin", "gm")
def review():
    month, team = _month(), request.form.get("team", "")
    res = tp.review(auth.repo(), g.att_user, month, team, request.form.get("decision"), request.form.get("note", ""))
    flash(f"{team}: approved - it is the official schedule now (monthly schedule, My schedule, attendance)."
          if res["status"] == "approved" else f"{team}: sent back to the manager.", "ok")
    return _back(month)


@bp.post("/team-plan/holiday")
@auth.roles_required("admin")
def holiday():
    month = _month()
    on = request.form.get("on") == "1"
    tp.set_holiday(auth.repo(), g.att_user, request.form.get("date", ""), request.form.get("name", ""), on)
    flash("Official holiday saved." if on else "Official holiday removed.", "ok")
    return _back(month, request.form.get("team", ""))


@bp.post("/team-plan/publish")
@auth.roles_required("admin")
def publish():
    month = _month()
    res = tp.publish(auth.repo(), g.att_user, month)
    flash(f"The approved schedule of {res['people']} people is official. "
          + (f"{res['mails']} e-mail queued for the collector's Outlook." if res["mails"] else
             "No e-mail: add the To / Cc in Attendance settings › Team schedule."), "ok")
    return _back(month)
