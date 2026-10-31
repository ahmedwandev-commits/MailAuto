"""Monthly schedule: import (upload -> preview -> confirm), history, calendar, direct (audited) manual changes."""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..services import access, schedule_service as ss
from ..services.schedule_service import ImportError_

bp = Blueprint("att_schedule", __name__)


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _ym():
    t = auth.now()
    return _int(request.args.get("year"), t.year), _int(request.args.get("month"), t.month)


@bp.route("/schedule/import", methods=["GET", "POST"])
@auth.admin_required
def import_upload():
    repo = auth.repo()
    teams = sorted({t for e in access.visible_employees(repo, g.att_user) for t in e["teams"]} |
                   {t for t in g.att_user.get("teams", []) if t != access.ALL})
    if request.method == "GET":
        t = auth.now()
        return render_template("attendance/schedule_import.html", teams=teams, year=t.year, month=t.month,
                               imports=repo.list_schedule_imports()[:15])
    f = request.files.get("file")
    if not f or not f.filename:
        raise ImportError_("Choose the Excel file to upload.")
    import_id = ss.start_import(repo, g.att_user, f.read(), f.filename, _int(request.form.get("year"), 0),
                                _int(request.form.get("month"), 0), request.form.get("team", ""))
    if request.form.get("review"):                      # optional: look at the matching first
        return redirect(url_for("att_schedule.import_preview", import_id=import_id))
    res = ss.import_directly(repo, g.att_user, import_id)
    if res is None:                                     # something needs a person: the review page explains what
        flash("The file was read but needs your attention before it can be imported.", "warn")
        return redirect(url_for("att_schedule.import_preview", import_id=import_id))
    a = res["auto"]
    msg = (f"Imported at once as version {res['version_no']}: {res['entries']} assignments, {res['conflicts']} scheduling warning(s)."
           + (f" It replaced version {a['replaced_version']} (kept in the history)." if a["replaced_version"] else "")
           + (f" Date correction(s) applied: {', '.join(a['date_corrections'])}." if a["date_corrections"] else "")
           + (f" Names matched by suggestion: {len(a['suggestions_used'])}." if a["suggestions_used"] else "")
           + (f" Names skipped (no employee found): {', '.join(a['names_skipped'][:8])}" + (" …" if len(a["names_skipped"]) > 8 else "") + "." if a["names_skipped"] else ""))
    flash(msg, "warn" if a["names_skipped"] else "ok")
    return redirect(url_for("att_schedule.month", year=res["year"], month=res["month"]))


@bp.get("/schedule/import/<int:import_id>")
@auth.admin_required
def import_preview(import_id):
    pv = ss.build_preview(auth.repo(), import_id)
    if not pv:
        raise ImportError_("Import not found.")
    return render_template("attendance/schedule_preview.html", pv=pv)


@bp.post("/schedule/import/<int:import_id>/confirm")
@auth.admin_required
def import_confirm(import_id):
    resolutions = {k[4:]: v for k, v in request.form.items() if k.startswith("map:") and v}
    res = ss.confirm_import(auth.repo(), g.att_user, import_id, resolutions,
                            bool(request.form.get("accept_dates")), bool(request.form.get("replace")),
                            bool(request.form.get("skip_unresolved")), bool(request.form.get("infer_rest")))
    imp = auth.repo().get_schedule_import(import_id)
    flash(f"Imported version {res['version_no']}: {res['entries']} assignments, {res['inferred_rest_days']} inferred rest days, "
          f"{res['conflicts']} scheduling warning(s).", "ok")
    return redirect(url_for("att_schedule.month", year=imp["year"], month=imp["month"]))


@bp.post("/schedule/import/<int:import_id>/cancel")
@auth.admin_required
def import_cancel(import_id):
    ss.cancel_import(auth.repo(), g.att_user, import_id)
    flash("Import cancelled. Nothing was changed.", "ok")
    return redirect(url_for("att_schedule.import_upload"))


@bp.route("/schedule/names", methods=["GET", "POST"])
@auth.admin_required
def names():
    """Name mapping: the schedule has first names only; the administrator links every name to a portal account once, and it is
    remembered for every later import."""
    repo = auth.repo()
    if request.method == "POST":
        if request.form.get("action") == "include_managers":
            repo.save_settings({"roster_include_managers": True})
            auth.audit("settings_changed", "settings", "roster_include_managers", {"previous": False, "new": True})
            flash("Portal managers are on the attendance roster now: their names can be linked below.", "ok")
            return redirect(url_for("att_schedule.names"))
        if request.form.get("action") == "add_missing":
            try:
                year, mo = (int(x) for x in request.form.get("month", "").split("-"))
            except ValueError as exc:
                raise ImportError_("Choose a month.") from exc
            n = ss.add_missing_names(repo, g.att_user, year, mo)
            flash(f"{n} shift day(s) were added to the {year}-{mo:02d} schedule. Nothing that was already planned was changed.", "ok")
            return redirect(request.form.get("next") or url_for("att_schedule.names"))
        if request.form.get("action") == "reapply":
            ym = request.form.get("month", "")
            try:
                year, mo = (int(x) for x in ym.split("-"))
            except ValueError as exc:
                raise ImportError_("Choose a month.") from exc
            res = ss.reapply_month(repo, g.att_user, year, mo)
            flash(f"The {year}-{mo:02d} schedule was rebuilt with the name mapping as version {res['version_no']} "
                  "(the previous version is kept in the history).", "ok")
            return redirect(url_for("att_schedule.names"))
        choices = {k[4:]: v for k, v in request.form.items() if k.startswith("map:") and v}
        saved, cleared = ss.save_name_mapping(repo, g.att_user, choices)
        flash(f"Saved {saved} name(s)" + (f", forgot {cleared}" if cleared else "") +
              ". They apply to every next import; use “Rebuild” to apply them to a month that is already imported.", "ok")
        return redirect(url_for("att_schedule.names"))
    return render_template("attendance/schedule_names.html", nm=ss.name_mapping(repo))


@bp.get("/schedule/history")
@auth.roles_required("admin", "gm")
def history():
    return redirect(url_for("att_planning.history"))


@bp.get("/schedule")
@auth.login_required
def month():
    year, mo = _ym()
    team = request.args.get("team", "")
    view = request.args.get("view", "matrix")
    mv = ss.month_view(auth.repo(), g.att_user, year, mo, team or None)
    from ..services import settings as settings_svc
    start_wd = settings_svc.load(auth.repo())["work_week_start"]
    first = date(year, mo, 1)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return render_template("attendance/schedule_month.html", mv=mv, year=year, month=mo, team=team, view=view, today=auth.today(),
                           pad=(first.weekday() - start_wd) % 7, dayheads=[names[(start_wd + i) % 7] for i in range(7)],
                           changes=auth.repo().list_schedule_changes(year, mo)[:30],
                           can_edit=g.att_user["role"] in ("admin", "manager"))


@bp.route("/schedule/edit", methods=["GET", "POST"])
@auth.writer_required
def edit():
    repo = auth.repo()
    if request.method == "GET":
        emp = access.require_employee(repo, g.att_user, _int(request.args.get("emp"), 0))
        d = request.args.get("date", "")
        date.fromisoformat(d)
        cur = [e["shift_code"] for e in repo.schedule_entries(d, d, [emp["id"]])]
        return render_template("attendance/schedule_edit.html", emp=emp, d=d, cur=cur, shifts=ss.shifts_by_code(repo))
    emp_id, d = _int(request.form.get("emp"), 0), request.form.get("date", "")
    res = ss.edit_day(repo, g.att_user, emp_id, d, request.form.getlist("codes"), request.form.get("reason"))
    flash(f"{res['emp']['name']} on {d}: {res['old']} → {res['new']}." + (" Warning: " + " ".join(res["warnings"]) if res["warnings"] else ""),
          "warn" if res["warnings"] else "ok")
    dd = date.fromisoformat(d)
    return redirect(auth.safe_next(url_for("att_schedule.month", year=dd.year, month=dd.month)))
