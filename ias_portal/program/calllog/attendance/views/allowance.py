"""Shift-allowance export: an account the administrator assigned sees the HR attendance of the employees (as shifts) and downloads it in
the form of the template «نموذج ورديات»."""
from datetime import date, timedelta

from flask import Blueprint, Response, g, render_template, request

from .. import auth
from ..services import allowance_service as svc

bp = Blueprint("att_allowance", __name__)


def _month_of(day):
    first = day.replace(day=1)
    return first.isoformat(), ((first + timedelta(days=32)).replace(day=1) - timedelta(days=1)).isoformat()


def _period():
    """The period of the request; the default is the whole current month (what the managers approve and the employees confirm)."""
    d1, d2 = _month_of(date.fromisoformat(auth.today()))
    return request.args.get("from") or d1, request.args.get("to") or d2


@bp.get("/allowance")
@auth.allowance_required
def index():
    repo = auth.repo()
    d1, d2 = _period()
    team = request.args.get("team", "")
    can_pending = g.att_user["role"] == "admin"                       # the administrator sees every shift at once (marked approved / not yet)
    pending = can_pending
    rep = svc.build(repo, d1, d2, team or None, pending)
    teams = sorted({t for e in repo.list_employees() for t in e["teams"]}, key=str.lower)
    this_m = _month_of(date.fromisoformat(auth.today()))
    last_m = _month_of(date.fromisoformat(this_m[0]) - timedelta(days=1))
    return render_template("attendance/allowance.html", rep=rep, teams=teams, team=team, d1=d1, d2=d2, can_pending=can_pending,
                           months=(("This month", *this_m), ("Last month", *last_m)),
                           include_pending=pending, approvals=rep["approvals"])


@bp.get("/allowance/export/xlsx")
@auth.allowance_required
def export():
    d1, d2 = _period()
    team = request.args.get("team", "")
    pending = g.att_user["role"] == "admin"
    rep = svc.build(auth.repo(), d1, d2, team or None, pending)
    auth.audit("allowance_exported", "allowance", f"{d1}..{d2}", {"team": team, "employees": len(rep["rows"]), "days": len(rep["days"]),
                                                                  "includes_unapproved": pending, "hidden_days": rep["hidden_days"]})
    name = f"shift-allowance-{d1}-to-{d2}.xlsx"
    return Response(svc.workbook(rep), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
