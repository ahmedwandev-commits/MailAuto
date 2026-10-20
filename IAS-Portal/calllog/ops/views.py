"""HTML pages of the Operations Hub: /hub /my /calls /email /sla /itsm /system (docs/HUB-DESIGN.md, sections 3 and 8).

The pages are thin shells: the data comes from ``/api/ops`` and is drawn by ``static/*.js`` (strict CSP: no inline script or style).
This module only decides WHO may open WHICH page and what the shell needs to know:

* anonymous            -> the sign-in page
* employee             -> /my (and the Call Log page /dashboard); every other Hub page sends them back to /my
* manager              -> /hub /calls (their team's calls) /email /sla /itsm; /system sends them to /hub
* administrator        -> everything; /calls is the existing full Call Log dashboard (/admin)
* /my                  -> everybody; an administrator or manager must say whose page with ``?as=<user id>`` (read-only), otherwise /hub
"""
from functools import wraps

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from .. import auth
from ..storage import norm_uid
from . import api as ops_api, overlay

bp = Blueprint("hub_pages", __name__)


def home_for(user):
    """Where a person starts: the Admin dashboard for admin / manager, My work for an employee."""
    return "/hub" if user["role"] in auth.OPS_ROLES else "/my"


def page(*roles):
    """Page gate. Anonymous -> sign in; a role that may not see the page -> that person's own start page (never a bare error)."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.user:
                return redirect(url_for("pages.login"))
            if roles and g.user["role"] not in roles:
                return redirect(home_for(g.user))
            return view(*args, **kwargs)
        return wrapper
    return decorator


def employees_for_picker(scope=None):
    """Active employees for the "Find an employee" search (id, name, department), by name. ``scope`` keeps a manager's
    list to their own team(s) (a tuple of team names); an administrator passes ``None`` and sees every employee.
    Always test ``scope is None`` - an empty scope means *nothing*, not *everything*."""
    users = [u for u in auth.store().users().values()
             if u.get("role") == "user" and u.get("active", True)
             and (scope is None or any(overlay.team_in(scope, t) for t in overlay.user_teams(u)))]
    users.sort(key=lambda u: (str(u.get("name") or "").casefold(), u["user_id"]))
    return [{"user_id": u["user_id"], "name": u.get("name") or u["user_id"], "department": u.get("department", "")} for u in users]


def home_first_paint():
    """What Home shows first - the overview and today's timeline - computed here, with the very functions the page polls afterwards,
    and carried in the page itself. The dashboard appears at once instead of after two round trips, and a browser that opens it
    at 08:00 makes its first request only after a random pause (docs/HUB-DESIGN.md section 14). None when it cannot be computed:
    the page then fetches it like any later poll and shows the error in its own words."""
    try:
        overview = ops_api.overview().get_json()
        events = ops_api.events().get_json()["events"][:60]
    except Exception:
        current_app.logger.exception("Home: the first paint could not be computed; the page will ask for it")
        return None
    return {"overview": overview, "events": events}


@bp.get("/hub")
@page(*auth.OPS_ROLES)
def hub():
    # a manager searches within their own team(s); an administrator (scope None) searches every employee
    return render_template("hub.html", employees=employees_for_picker(auth.team_scope(g.user)),
                           today=auth.today(), seed=home_first_paint())


@bp.get("/calls")
@page(*auth.OPS_ROLES)
def calls():
    if g.user["role"] == "admin":
        return redirect("/admin#overview")         # the full Call Log dashboard
    return render_template("calls.html")            # a manager: the calls of their own team(s)


@bp.get("/email")
@page(*auth.OPS_ROLES)
def email():
    return render_template("email.html")


@bp.get("/sla")
@page(*auth.OPS_ROLES)
def sla():
    return render_template("sla.html")


@bp.get("/itsm")
@page(*auth.OPS_ROLES)
def itsm():
    return render_template("itsm.html")


@bp.get("/auto-assign")
@page(*auth.OPS_ROLES)
def auto_assign():
    return render_template("auto_assign.html")


@bp.get("/system")
@page("admin")
def system():
    return render_template("system.html")


@bp.get("/my")
@page()
def my():
    wanted = norm_uid(request.args.get("as"))
    if g.user["role"] not in auth.OPS_ROLES:
        if wanted and wanted != g.user["user_id"]:
            return redirect("/my")                      # an employee only ever opens their own page
        return render_template("my.html", view_as=None)
    if not wanted:
        return redirect("/hub")
    target = auth.store().get_user(wanted)
    if not target or not overlay.may_view_person(g.user, target):         # outside the manager's teams = not found
        return render_template("message.html", title="Person not found",
                               message="There is no account with that user ID. Go back to the Home page and choose from the list."), 404
    return render_template("my.html", view_as={"user_id": target["user_id"], "name": target.get("name") or target["user_id"]})
