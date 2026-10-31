"""Monthly shifts: a manager sees the people of their team with the shifts the system counts for the month, and approves them at the end of
the month; the approved shifts reach the allowance sheet of the administrator (see ``services/shifts_service.py``)."""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..services import access, shifts_service as svc
from ..services.schedule_service import ImportError_

bp = Blueprint("att_shifts", __name__)


def _month(value):
    try:
        y, m = (int(x) for x in (value or "").split("-"))
        date(y, m, 1)
        return f"{y}-{m:02d}"
    except (TypeError, ValueError):
        t = auth.now()
        return f"{t.year}-{t.month:02d}"


def _teams(repo, user):
    return sorted({t for e in access.visible_employees(repo, user) for t in e["teams"]}, key=str.lower)


@bp.route("/monthly-shifts", methods=["GET", "POST"])
@auth.roles_required("admin", "manager", "gm")
def index():
    repo = auth.repo()
    teams = _teams(repo, g.att_user)
    month = _month(request.values.get("month"))
    team = request.values.get("team") or (teams[0] if teams else "")
    if team and team not in teams:
        raise access.Forbidden("That team is not in your scope.")
    if request.method == "POST":
        if not team:
            raise ImportError_("Choose a team.")
        if request.form.get("action") == "approve":
            rec = svc.approve(repo, g.att_user, month, team)
            flash(f"{team}: the shifts of {month} are approved ({rec['employees']} people, {rec['days']} counted days). They are in the allowance sheet now.", "ok")
        elif request.form.get("action") == "reopen":
            svc.reopen(repo, g.att_user, month, team)
            flash(f"{team}: the approval of {month} was withdrawn.", "ok")
        return redirect(url_for("att_shifts.index", month=month, team=team))
    mv = svc.month_view(repo, g.att_user, month, team) if team else None
    y, m = int(month[:4]), int(month[5:])
    prev_m, next_m = (f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"), (f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}")
    return render_template("attendance/monthly_shifts.html", mv=mv, month=month, team=team, teams=teams, prev_m=prev_m, next_m=next_m,
                           can_approve=g.att_user["role"] == "manager", no_teams=(g.att_user["role"] == "manager" and not teams))
