"""My attendance (v5.17): the signed-in person's month in three tabs - Attendance (check-in / check-out, hours, overtime of each day),
Shift allowance (the expected shifts and meals, as counts - no money) and Overtime (weighted hours, the weekly deduction, the cap and the %).
Managers, the General Manager and the administrators also get a Team tab: the same numbers for every person they may see.
The employee number is always the signed-in account's own - never a request parameter."""
from flask import Blueprint, g, render_template, request

from .. import auth
from ..services import access, overtime_service as ots

bp = Blueprint("att_myatt", __name__)
TABS = ("attendance", "allowance", "overtime", "team")


@bp.get("/my/attendance")
@auth.login_required
def index():
    repo = auth.repo()
    user = g.att_user
    today = auth.today()
    month = ots.month_of(request.args.get("month"), today)
    can_team = user["role"] in ("admin", "gm", "manager")
    mine = bool(user.get("is_employee"))
    tab = request.args.get("tab") or ("attendance" if mine else "team")
    if tab not in TABS or (tab == "team" and not can_team) or (tab != "team" and not mine):
        tab = "attendance" if mine else ("team" if can_team else "attendance")
    rep, team_rows = None, []
    if mine and tab != "team":
        emp = repo.get_employee(int(user["user_id"]))
        rep = ots.build(repo, [emp], month, today)[emp["id"]] if emp else None
    if tab == "team" and can_team:
        emps = access.visible_employees(repo, user)
        team = request.args.get("team") or ""
        if team:
            emps = [e for e in emps if access.has_team(e, team)]
        reps = ots.build(repo, emps, month, today) if emps else {}
        team_rows = sorted(reps.values(), key=lambda r: (-r["overtime"]["capped"], r["employee"]["name"]))
    y, m = (int(x) for x in month.split("-"))
    prev_m = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    next_m = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
    teams = sorted({t for e in access.visible_employees(repo, user) for t in e["teams"]}, key=str.lower) if can_team else []
    return render_template("attendance/my_attendance.html", rep=rep, tab=tab, mine=mine, can_team=can_team, team_rows=team_rows,
                           month=month, prev_m=prev_m, next_m=next_m, label=ots.MONTH_NAMES[m - 1] + f" {y}", today=today,
                           teams=teams, team=request.args.get("team") or "")
