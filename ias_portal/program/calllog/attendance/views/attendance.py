"""HR attendance import and punch corrections / reports / absence reviews."""
from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..importers import templates
from ..services import attendance_service as ats
from ..services.schedule_service import ImportError_

bp = Blueprint("att_attendance", __name__)


@bp.route("/hr/import", methods=["GET", "POST"])
@auth.admin_required
def import_upload():
    repo = auth.repo()
    if request.method == "GET":
        return render_template("attendance/attendance_import.html", imports=repo.list_attendance_imports()[:15])
    f = request.files.get("file")
    if not f or not f.filename:
        raise ImportError_("Choose the Excel file to upload.")
    import_id = ats.start_import(repo, g.att_user, f.read(), f.filename)
    if request.form.get("review"):
        return redirect(url_for("att_attendance.import_preview", import_id=import_id))
    res = ats.import_directly(repo, g.att_user, import_id)
    if res is None:                                     # the columns need mapping: the review page offers it
        flash("The file was read but its columns need your attention before it can be imported.", "warn")
        return redirect(url_for("att_attendance.import_preview", import_id=import_id))
    skipped = res["rejected_unknown_employee"]
    flash(f"Imported at once: {res['inserted']} new day-records; {res.get('updated', 0)} stored days completed / corrected by this file; {res['existing_kept']} already existed (unchanged); "
          f"{res['rejected']} rejected rows" + (f"; {skipped} row(s) skipped because {len(res['unknown_numbers'])} employee number(s) have no portal account "
                                                f"({', '.join(map(str, res['unknown_numbers'][:8]))}{' …' if len(res['unknown_numbers']) > 8 else ''})" if skipped else "") + "."
          + (f" {res['missing_punch_people']} employee(s) with a forgotten check-in / check-out and their managers were told."
             if res.get("missing_punch_people") else ""),
          "warn" if skipped else "ok")
    if any(u["candidates"] or u["employee_id"] for u in res.get("unlinked") or []):
        flash("Some employee numbers of the file look like portal people under another number: link them below.", "warn")
        return redirect(url_for("att_attendance.links"))            # their name matches somebody: link them to that person
    return redirect(url_for("att_attendance.history"))


@bp.route("/hr/links", methods=["GET", "POST"])
@auth.admin_required
def links():
    """HR numbers <-> portal accounts: a person whose number in the HR file is not their portal User ID («خالد فكري» in the
    file, «خالد محمد فكري» on the portal) is linked once; their days are then loaded from every stored file."""
    repo = auth.repo()
    if request.method == "POST":
        done = 0
        for key, value in request.form.items():
            if key.startswith("link:") and value:
                done += 1
                added = ats.link_hr_number(repo, g.att_user, int(key[5:]), None if value == "none" else value)
                flash(f"HR number {key[5:]}: " + ("unlinked." if value == "none" else f"linked ({added} day-record(s) added)."), "ok")
        if not done:
            flash("Nothing chosen.", "warn")
        return redirect(url_for("att_attendance.links"))
    saved = ats.hr_links(repo)
    names = {e["id"]: e["name"] for e in repo.list_employees(active_only=False)}
    unlinked = {}
    for imp in repo.list_attendance_imports()[:10]:
        full = repo.get_attendance_import(imp["id"]) or {}
        if full.get("parsed") and not full["parsed"].get("error"):
            for u in ats.unlinked_numbers(repo, full["parsed"]):
                unlinked.setdefault(u["hr_number"], u)
    return render_template("attendance/hr_links.html", saved=saved, names=names, unlinked=list(unlinked.values()),
                           employees=sorted(repo.list_employees(), key=lambda e: e["name"]))


@bp.get("/hr/import/<int:import_id>")
@auth.admin_required
def import_preview(import_id):
    pv = ats.build_preview(auth.repo(), import_id)
    if not pv:
        raise ImportError_("Import not found.")
    return render_template("attendance/attendance_preview.html", pv=pv)


@bp.post("/hr/import/<int:import_id>/confirm")
@auth.admin_required
def import_confirm(import_id):
    repo = auth.repo()
    for key, value in request.form.items():                    # the preview's "Portal account" choices for unknown HR numbers
        if key.startswith("link:") and value and value != "none":
            ats.link_hr_number(repo, g.att_user, int(key[5:]), value)
    res = ats.confirm_import(repo, g.att_user, import_id, bool(request.form.get("create_unknown")))
    flash(f"Imported {res['inserted']} new day-records; {res.get('updated', 0)} stored days completed / corrected by this file; {res['existing_kept']} already existed (unchanged); "
          f"{res['rejected'] + res['rejected_unknown_employee']} rejected."
          + (f" {res['missing_punch_people']} employee(s) with a forgotten check-in / check-out and their managers were told."
             if res.get("missing_punch_people") else ""), "ok")
    return redirect(url_for("att_attendance.history"))


@bp.post("/hr/import/<int:import_id>/cancel")
@auth.admin_required
def import_cancel(import_id):
    ats.cancel_import(auth.repo(), g.att_user, import_id)
    flash("Import cancelled. Nothing was changed.", "ok")
    return redirect(url_for("att_attendance.import_upload"))


@bp.get("/hr/history")
@auth.admin_required
def history():
    return redirect(url_for("att_planning.history"))


@bp.post("/hr/import/<int:import_id>/map")
@auth.admin_required
def import_map(import_id):
    override = {k[4:]: v for k, v in request.form.items() if k.startswith("map_") and v.strip() != ""}
    ats.remap_columns(auth.repo(), g.att_user, import_id, override, request.form.get("header_row"))
    flash("Column mapping applied. Review the preview again.", "ok")
    return redirect(url_for("att_attendance.import_preview", import_id=import_id))


@bp.get("/templates/attendance.xlsx")
@auth.login_required
def attendance_template():
    return Response(templates.attendance_template(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="attendance-template.xlsx"'})


@bp.get("/templates/schedule.xlsx")
@auth.login_required
def schedule_template():
    t = auth.now()
    try:
        y, m = int(request.args.get("year", t.year)), int(request.args.get("month", t.month))
    except ValueError:
        y, m = t.year, t.month
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        y, m = t.year, t.month
    return Response(templates.schedule_template(y, m), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="schedule-template-{y}-{m:02d}.xlsx"'})


# ---------------------------------------------------------------- per-employee actions (return to the profile)
def _back(emp_id):
    return redirect(url_for("att_people.profile", emp_id=emp_id, **{k: v for k, v in {
        "from": request.form.get("back_from"), "to": request.form.get("back_to")}.items() if v}))


@bp.post("/hr/correct")
@auth.writer_required
def correct():
    emp = int(request.form.get("emp", 0))
    day, reason, mode = request.form.get("date", ""), request.form.get("reason"), request.form.get("mode", "")
    if mode in ("set", "clear"):
        # one dialog for the day: set the check-in / check-out (e.g. "mark present" with the shift's times) or remove both
        done = 0
        for field in ("in_time", "out_time"):
            value = "" if mode == "clear" else (request.form.get(field) or "").strip()
            if mode == "clear" or value:
                ats.correct_punch(auth.repo(), g.att_user, emp, day, field, value, reason, allow_clear=True)
                done += 1
        if not done:
            raise ImportError_("Enter a check-in or a check-out time.")
        flash("Attendance removed for that day." if mode == "clear" else "Attendance saved with its reason and audit entry.", "ok")
    else:
        ats.correct_punch(auth.repo(), g.att_user, emp, day, request.form.get("field", ""), request.form.get("value", ""), reason)
        flash("Correction saved with its reason and audit entry.", "ok")
    nxt = auth.safe_next("")
    return redirect(nxt) if nxt else _back(emp)


@bp.post("/hr/punch-report")
@auth.writer_required
def punch_report():
    emp = int(request.form.get("emp", 0))
    ats.report_forgotten_punch(auth.repo(), g.att_user, emp, request.form.get("date", ""), request.form.get("kind", ""),
                               request.form.get("reason"))
    flash("Forgotten-punch report recorded (pending verification).", "ok")
    return _back(emp)


@bp.post("/hr/punch-report/<int:report_id>/<decision>")
@auth.writer_required
def punch_decide(report_id, decision):
    ats.decide_punch_report(auth.repo(), g.att_user, report_id, decision)
    flash("Report " + decision + ".", "ok")
    return _back(int(request.form.get("emp", 0)))


@bp.post("/hr/absence-review")
@auth.writer_required
def absence_review():
    emp = int(request.form.get("emp", 0))
    ats.review_absence(auth.repo(), g.att_user, emp, request.form.get("date", ""), request.form.get("decision", ""),
                       request.form.get("reason"))
    flash("Absence review saved.", "ok")
    return _back(emp)


@bp.post("/hr/permit-review")
@auth.writer_required
def permit_review():
    emp = int(request.form.get("emp", 0))
    ats.review_permit(auth.repo(), g.att_user, emp, request.form.get("date", ""), request.form.get("kind", ""),
                      request.form.get("decision", ""), request.form.get("reason"))
    flash("Permit review saved.", "ok")
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return _back(emp)