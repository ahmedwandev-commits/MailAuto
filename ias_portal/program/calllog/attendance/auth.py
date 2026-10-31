"""Request helpers of the attendance module. Sign-in, sessions, CSRF and the password rules are the PORTAL's (calllog/auth.py,
calllog/views.py): this file only turns the portal's signed-in user into an attendance user (``g.att_user``) and offers the
same decorators the rest of the portal uses."""
from functools import wraps

from flask import current_app, flash, g, jsonify, redirect, request, url_for

from . import services

PREFIX = "/attendance"


def repo():
    return current_app.extensions["att_repo"]


def directory():
    return repo().directory


def now():
    return current_app.config["CLOCK"]()


def today():
    return now().date().isoformat()


def wants_json():
    return "/api/" in request.path


def audit(action, entity=None, entity_id=None, detail=None, actor=None):
    services.audit(repo(), actor if actor is not None else g.get("att_user"), action, entity, entity_id, detail, request.remote_addr)


def csrf_token():
    from calllog import auth as hub_auth
    return hub_auth.csrf_token()


def safe_next(default):
    """The ``next`` field of a form when it is a path inside this portal, otherwise ``default``."""
    nxt = request.form.get("next", "") or request.args.get("next", "")
    return nxt if nxt.startswith("/attendance/") and not nxt.startswith("//") else default


def _deny(status):
    if wants_json() or "/export/" in request.path:
        return jsonify(error="Please sign in." if status == 401 else "You do not have access to this."), status
    if status == 401:
        return redirect(url_for("pages.login"))
    flash("You do not have access to that page.", "error")
    return redirect(url_for("att_gm.index"))


def login_required(view):
    @wraps(view)
    def wrapper(*a, **k):
        return view(*a, **k) if g.att_user else _deny(401)
    return wrapper


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*a, **k):
            if not g.att_user:
                return _deny(401)
            if g.att_user["role"] not in roles:
                return _deny(403)
            return view(*a, **k)
        return wrapper
    return decorator


def allowance_required(view):
    """The shift-allowance export: administrators and the accounts the administrator assigned."""
    @wraps(view)
    def wrapper(*a, **k):
        if not g.att_user:
            return _deny(401)
        if not g.att_user.get("can_allowance"):
            return _deny(403)
        return view(*a, **k)
    return wrapper


admin_required = roles_required("admin")
writer_required = roles_required("admin", "manager")
viewer_roles = ("admin", "gm")          # roles that may open the import history (managers have no import screens)
