"""Workforce planning screens: weekly plan, day view, the manager's team availability dashboard, combined import history."""
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..services import access, planning_service as ps, schedule_service, settings as settings_svc
from . import parse_date

bp = Blueprint("att_planning", __name__)


def _teams(repo, user):
    return sorted({t for e in access.visible_employees(repo, user) for t in e["teams"]})


def _default_team(user, teams):
    mine = [t for t in teams if access.in_scope(user, t)]
    return mine[0] if user["role"] == "manager" and len(mine) == 1 else ""


@bp.get("/planning/week")
@auth.login_required
def week():
    repo = auth.repo()
    st = settings_svc.load(repo)
    start = ps.week_start_for(parse_date(request.args.get("start"), auth.today()), st["work_week_start"])
    teams = _teams(repo, g.att_user)
    team = request.args.get("team")
    team = _default_team(g.att_user, teams) if team is None else team
    wv = ps.week_view(repo, g.att_user, start, team or None)
    d0 = date.fromisoformat(start)
    return render_template("attendance/week.html", wv=wv, start=start, team=team, teams=teams, today=auth.today(),
                           prev=(d0 - timedelta(days=7)).isoformat(), nxt=(d0 + timedelta(days=7)).isoformat(),
                           can_edit=g.att_user["role"] in ("admin", "manager"))


@bp.post("/planning/week/save")
@auth.writer_required
def week_save():
    start, team = request.form.get("start", ""), request.form.get("team", "")
    date.fromisoformat(start)
    wanted = {}
    for key, val in request.form.items():
        if key.startswith("cell_"):
            _, emp, d = key.split("_", 2)
            wanted[(int(emp), d)] = val.strip().upper()
    res = ps.save_week(auth.repo(), g.att_user, start, team, wanted, request.form.get("reason"))
    if not res["changed"]:
        flash("Nothing changed.", "ok")
    else:
        flash(f"{res['changed']} change(s) saved." + (" Warnings: " + " ".join(res["warnings"][:6]) if res["warnings"] else ""),
              "warn" if res["warnings"] else "ok")
    return redirect(url_for("att_planning.week", start=start, team=team))


@bp.get("/planning/day")
@auth.login_required
def day():
    """The dashboard of the General Manager and of the managers: who is where on a day, per team (with the team manager), per shift
    and per person. A manager only ever gets the teams on the account."""
    repo = auth.repo()
    d = parse_date(request.args.get("date"), auth.today())
    teams = _teams(repo, g.att_user)
    team = request.args.get("team")
    if team is None:
        team = teams[0] if g.att_user["role"] == "manager" and len(teams) == 1 else ""
    if team and team not in teams:
        raise access.Forbidden("That team is not in your scope.")
    dv = ps.day_view(repo, g.att_user, d, auth.now(), team or None)
    # the Today dashboard also carries the day's counters and the teams (one line each, a click opens the Team dashboard)
    td = ps.team_dashboard(repo, g.att_user, team, d, auth.now())
    ts = ps.team_summary(repo, td["rows"], td["shifts"], team or None, d, auth.now())
    attention = [r for r in td["rows"] if (r.get("presence") or {}).get("key") == "missing"
                 or r.get("late_permit") == "required" or r.get("early_permit") == "required"]
    dd = date.fromisoformat(d)
    return render_template("attendance/day.html", dv=dv, td=td, ts=ts, attention=attention,
                           day=d, team=team, teams=teams, today=auth.today(), is_today=(d == auth.today()),
                           prev=(dd - timedelta(days=1)).isoformat(), nxt=(dd + timedelta(days=1)).isoformat(),
                           stale=schedule_service.stale_months(repo) if g.att_user["role"] == "admin" else [],
                           can_edit=g.att_user["role"] in ("admin", "manager"),
                           no_teams=(g.att_user["role"] == "manager" and not teams))


@bp.get("/modify")
@auth.writer_required
def modify():
    """Modification screen (administrator and managers): change an employee's shift for a day and manage leave, for the teams in scope."""
    repo = auth.repo()
    d = parse_date(request.args.get("date"), auth.today())
    teams = _teams(repo, g.att_user)
    team = request.args.get("team")
    if team is None:
        team = teams[0] if len(teams) == 1 or (teams and g.att_user["role"] == "manager") else ""
    if team and team not in teams:
        raise access.Forbidden("That team is not in your scope.")
    mv = ps.modify_view(repo, g.att_user, team, d, auth.now())
    dd = date.fromisoformat(d)
    return render_template("attendance/modify.html", mv=mv, day=d, team=team, teams=teams, today=auth.today(),
                           prev=(dd - timedelta(days=1)).isoformat(), nxt=(dd + timedelta(days=1)).isoformat(),
                           no_teams=(g.att_user["role"] == "manager" and not teams))


@bp.get("/team")
@auth.login_required
def team():
    """Team availability: everyone of the team with the approved plan, presence indicator and exceptions (managers: own team, the
    General Manager and the administrator: any team)."""
    repo = auth.repo()
    teams = _teams(repo, g.att_user)
    wanted = request.args.get("team")
    team_name = wanted if wanted is not None else (teams[0] if teams and g.att_user["role"] == "manager" else "")
    if team_name and team_name not in teams:
        raise access.Forbidden("That team is not in your scope.")
    d = parse_date(request.args.get("date"), auth.today())
    if not team_name and teams:                       # every team at once is the Today dashboard; this page is one team's
        return redirect(url_for("att_planning.day", date=d))
    td = ps.team_dashboard(repo, g.att_user, team_name, d, auth.now())
    ts = ps.team_summary(repo, td["rows"], td["shifts"], team_name or None, d, auth.now())
    dd = date.fromisoformat(d)
    return render_template("attendance/team.html", td=td, ts=ts, day=d, team=team_name, teams=teams, today=auth.today(), is_today=(d == auth.today()),
                           prev=(dd - timedelta(days=1)).isoformat(), nxt=(dd + timedelta(days=1)).isoformat(),
                           can_edit=g.att_user["role"] in ("admin", "manager"), no_teams=(g.att_user["role"] == "manager" and not teams))


@bp.get("/imports/history")
@auth.roles_required("admin", "gm")
def history():
    repo = auth.repo()
    return render_template("attendance/history.html", schedule=repo.list_schedule_imports(), attendance=repo.list_attendance_imports())
