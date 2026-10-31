"""Attendance & Workforce module of the portal.

Mounted by ``calllog.app.create_app`` under ``/attendance``. It shares the portal's accounts (see ``hub_directory.py``), sessions,
CSRF protection, theme and audit log, and keeps its own data as JSON files in ``<data_dir>/attendance`` (see ``repo/json_repo.py``).
"""
from pathlib import Path

from flask import g, jsonify, redirect, render_template, request, url_for

from .hub_directory import HubAudit, HubDirectory
from .repo import JsonRepository

PREFIX = "/attendance"


def init_app(app):
    from . import auth
    from .services.access import Forbidden
    from .services.schedule_service import ImportError_
    from .views import (admin, allowance, attendance, confirm, gm, mine, people, planning, register_filters, schedule, shifts,
                        team_plan)

    store = app.extensions["store"]
    clock = app.config["CLOCK"]
    repo = JsonRepository(Path(store.root) / "attendance", clock, audit=HubAudit(store, clock))
    repo.directory = HubDirectory(store, repo)
    repo.migrate()
    app.extensions["att_repo"] = repo

    register_filters(app)
    for mod in (gm, planning, schedule, attendance, people, admin, allowance, mine, shifts, confirm, team_plan):
        app.register_blueprint(mod.bp, url_prefix=PREFIX)

    @app.before_request
    def att_user_and_gate():
        """Turn the portal's signed-in user into an attendance user; refuse everyone without an attendance role."""
        if not (request.blueprint or "").startswith("att_"):
            return None
        g.att_user = None
        if not g.get("user"):
            if auth.wants_json() or "/export/" in request.path:
                return jsonify(error="Please sign in."), 401
            return redirect(url_for("pages.login"))
        view = repo.directory.get_user(g.user["user_id"])
        if not (view and view["role"]):
            if auth.wants_json() or "/export/" in request.path:
                return jsonify(error="You do not have access to this."), 403
            return render_template("message.html", title="No access to Attendance",
                                   message="Your account does not have an attendance role. Ask an administrator."), 403
        if view["role"] in ("allowance", "employee"):
            # an employee opens only their own schedule; an account with the allowance export also that page. Nothing else.
            bp_name = request.blueprint or ""
            allowed = (bp_name == "att_mine" and view["is_employee"]) or (bp_name == "att_allowance" and view["can_allowance"])
            if not allowed:
                if request.path.rstrip("/") == PREFIX:                           # the portal's Attendance link
                    return redirect(url_for("att_mine.index" if view["is_employee"] else "att_allowance.index"))
                if auth.wants_json() or "/export/" in request.path:
                    return jsonify(error="You do not have access to this."), 403
                return render_template("message.html", title="No access to this page",
                                       message="Your account can only open your own schedule" +
                                               (" and the shift-allowance export." if view["can_allowance"] else ".")), 403
        g.att_user = view
        return None

    # what an Attendance administrator may still open outside /attendance: signing in / out, the password, the welcome, the
    # theme and the bell (their attendance notifications)
    OPEN_TO_ATT_ADMIN = ("/login", "/logout", "/account/", "/welcome", "/static/", "/healthz", "/api/ops/notifications")

    @app.before_request
    def att_admin_only_attendance():
        """The Attendance administrator (the sector's administrative affairs) sees the Attendance module and nothing else of
        the portal (owner's rule): every other page leads to Attendance, every other API answers 403."""
        user = g.get("user")
        path = request.path
        if not user or path.startswith(PREFIX) or path.startswith(OPEN_TO_ATT_ADMIN):
            return None
        if not repo.directory.is_att_admin(user):
            return None
        if path.startswith(("/api/", "/export/")):
            return jsonify(error="Your account can only use Attendance."), 403
        return redirect(PREFIX + "/planning/day")

    @app.context_processor
    def att_context():
        out = {"att_user": g.get("att_user"), "manages_users": False, "manages_employees": False, "att_prefix": PREFIX}
        user = g.get("user")
        role = repo.directory.role_of(user) if user else None
        out["att_can_open"] = bool(role)                                           # the portal's top bar shows the Attendance link
        out["att_role"] = role                                                     # "employee": the link is "My schedule"
        out["att_only"] = bool(user) and repo.directory.is_att_admin(user)          # Attendance administrator: that menu only
        out["att_can_allowance"] = bool(user) and repo.directory.can_allowance(user)
        out["att_is_employee"] = bool(user) and repo.directory._is_employee(user)
        return out

    @app.errorhandler(Forbidden)
    def att_forbidden(exc):
        if auth.wants_json():
            return jsonify(error=str(exc)), 403
        return render_template("attendance/message.html", title="Not allowed", message=str(exc)), 403

    @app.errorhandler(ImportError_)
    def att_user_error(exc):
        if auth.wants_json():
            return jsonify(error=str(exc)), 400
        return render_template("attendance/message.html", title="Cannot do that", message=str(exc)), 400
