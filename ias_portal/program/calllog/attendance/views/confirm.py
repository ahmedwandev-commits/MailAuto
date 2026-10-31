"""Workforce › Allowance confirmation: the month's shift allowance, confirmed by the employees, their team managers and the
General Manager (``services/allowance_confirm.py``). An administrator sends the month; a manager sees and confirms their own
team(s); the General Manager sees every team and confirms the sector."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..services import allowance_confirm as ac

bp = Blueprint("att_confirm", __name__)


def _month():
    return ac.month_of(request.values.get("month")) or ac.default_month(auth.today())


@bp.get("/allowance/confirm")
@auth.roles_required("admin", "gm", "manager")
def index():
    repo = auth.repo()
    month = _month()
    st = ac.status(repo, month)
    me = g.att_user
    if me["role"] == "manager":
        mine = {t.strip().lower() for t in me.get("teams") or []}
        st["teams"] = [t for t in st["teams"] if t["team"].strip().lower() in mine]
    team = request.args.get("team", "")
    months = sorted(ac.rounds(repo), reverse=True)
    return render_template("attendance/allowance_confirm.html", st=st, month=month, months=months, team=team, me=me)


def _back(month):
    return redirect(url_for("att_confirm.index", month=month))


@bp.post("/allowance/confirm/start")
@auth.roles_required("admin")
def start():
    res = ac.start(auth.repo(), g.att_user, request.form.get("month"))
    flash(f"Sent for confirmation: {res['employees']} employee(s) and the managers of {res['teams']} team(s) "
          f"({res['mails']} e-mail(s) queued for the collector's Outlook).", "ok")
    return _back(res["month"])


@bp.post("/allowance/confirm/member")
@auth.roles_required("admin", "manager")
def member():
    month = _month()
    emp = ac.confirm_member(auth.repo(), g.att_user, month, int(request.form.get("emp") or 0))
    flash(f"{emp['name']}: confirmed on their behalf.", "ok")
    return _back(month)


@bp.post("/allowance/confirm/team")
@auth.roles_required("admin", "manager")
def team():
    month = _month()
    res = ac.confirm_team(auth.repo(), g.att_user, month, request.form.get("team", ""))
    flash(f"{res['team']} confirmed ({res['people']} people)." +
          (" Every team is confirmed: the General Manager was asked to confirm the sector." if res["all_teams_confirmed"] else ""), "ok")
    return _back(month)


@bp.post("/allowance/confirm/gm")
@auth.roles_required("gm")
def gm():
    month = _month()
    ac.confirm_gm(auth.repo(), g.att_user, month)
    flash("The sector's shift allowance is confirmed. The administrative affairs were told.", "ok")
    return _back(month)
