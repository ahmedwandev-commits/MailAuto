"""The user guide (v5.17): one page inside the portal that shows each person only the parts of their role - an employee their own
pages, a team manager their team's work, the General Manager the reviews and confirmations, the administrative affairs and the
administrators everything they run. The numbers of the overtime and allowance rules are the ones set in Settings."""
from flask import Blueprint, g, render_template

from .. import auth
from ..services import settings as settings_svc

bp = Blueprint("att_guide", __name__)


def audience(user):
    """Who is reading: the flags the guide's sections are shown by."""
    hub = user.get("hub_role")
    affairs = bool(user.get("att_admin"))
    admin = hub == "admin"
    return {"employee": bool(user.get("is_employee")), "manager": user["role"] == "manager", "gm": user["role"] == "gm",
            "affairs": affairs, "admin": admin, "office": affairs or admin,          # those who run the module
            "lead": user["role"] in ("admin", "gm", "manager"),                     # anybody who sees teams
            "writer": user["role"] in ("admin", "manager"), "exporter": bool(user.get("can_allowance"))}


@bp.get("/guide")
@auth.login_required
def index():
    st = settings_svc.load(auth.repo())
    return render_template("attendance/guide.html", who=audience(g.att_user), ot=settings_svc.overtime_rules(st))
