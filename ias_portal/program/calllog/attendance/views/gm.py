"""Today's Workforce Overview (landing page), reports + exports, and the JSON API for the same data."""
from datetime import date, timedelta

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .. import auth
from ..services import access, analytics_service as an, pdf_export, reports_service as rs
from . import month_bounds, parse_date

bp = Blueprint("att_gm", __name__)

REPORT_GROUPS = [
    ("Month-end", "check-circle", ["review", "approvals", "summary", "scores", "score_details", "ranking"]),
    ("Attendance", "user-check", ["lateness", "permits", "hours", "short_hours", "short_hours_days", "punches", "planned_actual", "rest", "daily"]),
    ("Teams & coverage", "users", ["teams", "departments", "coverage"]),
    ("Leave & time off", "plane", ["leave", "comp"]),
    ("Changes & trends", "history", ["changes", "trends"]),
]


def _filters():
    f = {k: request.args.get(k, "").strip() for k in ("department", "team", "manager", "shift", "status", "flag", "q")}
    return {k: v for k, v in f.items() if v}


@bp.get("/")
@auth.login_required
def index():
    return redirect(url_for("att_planning.day"))


@bp.get("/today")
@auth.login_required
def today():
    """The old overview page no longer exists: the dashboard is the Day view and Team availability. Old links land on the Day view."""
    return redirect(url_for("att_planning.day", date=request.args.get("date") or None))


@bp.get("/api/overview")
@auth.login_required
def api_overview():
    day = parse_date(request.args.get("date"), auth.today())
    ov = an.overview(auth.repo(), g.att_user, day, auth.now(), _filters())
    return jsonify(date=day, kpi=ov["kpi"], status_counts=ov["status_counts"], rows=ov["rows"],
                   short_teams=ov["short_teams"], short_staffed=ov["short_staffed"], data_loaded=ov["data_loaded"], shift_stats=ov["shift_stats"], attention=ov["attention"])


def _report_args():
    today = auth.now().date()
    d1, d2 = month_bounds(today.year, today.month)
    d1 = parse_date(request.args.get("from"), d1)
    d2 = parse_date(request.args.get("to"), d2)
    if d2 < d1:
        d1, d2 = d2, d1
    kind = request.args.get("kind", "summary")
    if kind not in rs.KINDS:
        kind = "summary"
    return kind, d1, d2, request.args.get("team", "").strip() or None


def _presets():
    t = auth.now().date()
    first = t.replace(day=1)
    prev = first - timedelta(days=1)
    q0 = date(t.year, 3 * ((t.month - 1) // 3) + 1, 1)
    return [("Last 7 days", (t - timedelta(days=6)).isoformat(), t.isoformat()), ("Last 30 days", (t - timedelta(days=29)).isoformat(), t.isoformat()),
            ("This month", *month_bounds(t.year, t.month)), ("Last month", *month_bounds(prev.year, prev.month)),
            ("This quarter", q0.isoformat(), t.isoformat()), ("Year to date", date(t.year, 1, 1).isoformat(), t.isoformat())]


@bp.get("/reports")
@auth.login_required
def reports():
    kind, d1, d2, team = _report_args()
    rep = rs.build(auth.repo(), g.att_user, kind, d1, d2, auth.now(), team)
    teams = sorted({t for e in access.visible_employees(auth.repo(), g.att_user) for t in e["teams"]})
    return render_template("attendance/reports.html", rep=rep, kinds=rs.KINDS, groups=REPORT_GROUPS, kind=kind, d1=d1, d2=d2, team=team or "",
                           teams=teams, presets=_presets(), pdf_ok=pdf_export.available(current_app.config["SETTINGS"].get("pdf_font")))


@bp.get("/analytics")
@auth.login_required
def analytics_page():
    """Analytics: the picture of a period - indicators and charts. The tables behind them are in Reports (each card links to its report)."""
    from ..services import analytics_overview
    kind, d1, d2, team = _report_args()
    data = analytics_overview.build(auth.repo(), g.att_user, d1, d2, auth.now(), team)
    teams = sorted({t for e in access.visible_employees(auth.repo(), g.att_user) for t in e["teams"]})
    return render_template("attendance/analytics.html", a=data, d1=d1, d2=d2, team=team or "", teams=teams, presets=_presets())


@bp.get("/export/report.xlsx")
@auth.login_required
def export_report():
    kind, d1, d2, team = _report_args()
    rep = rs.build(auth.repo(), g.att_user, kind, d1, d2, auth.now(), team)
    auth.audit("report_exported", "report", kind, {"from": d1, "to": d2, "team": team, "rows": len(rep["rows"]), "format": "xlsx"})
    return Response(rs.to_xlsx(rep, d1, d2), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_{d1}_{d2}.xlsx"'})


@bp.get("/export/report.pdf")
@auth.login_required
def export_report_pdf():
    kind, d1, d2, team = _report_args()
    font = current_app.config["SETTINGS"].get("pdf_font")
    if not pdf_export.available(font):
        flash("PDF export needs the optional packages in requirements-pdf.txt (and an Arabic-capable font). "
              "Use Print / Save as PDF in the meantime.", "warn")
        return redirect(url_for("att_gm.reports", kind=kind, team=team or "", **{"from": d1, "to": d2}))
    rep = rs.build(auth.repo(), g.att_user, kind, d1, d2, auth.now(), team)
    auth.audit("report_exported", "report", kind, {"from": d1, "to": d2, "team": team, "rows": len(rep["rows"]), "format": "pdf"})
    data = pdf_export.render(rep, d1, d2, subtitle=f"team {team}" if team else "", configured_font=font)
    return Response(data, mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{kind}_{d1}_{d2}.pdf"'})
