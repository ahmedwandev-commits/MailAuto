"""Who may see / change which employees. The ONLY place team scoping is decided (enforced server-side on every request).

User shape (see identity.py): {'user_id','name','role','teams'}. 'admin' and 'gm' see every team; a 'manager' sees the teams on
the account (``__all__`` = every team; an empty list = nothing)."""

ALL = "__all__"


class Forbidden(Exception):
    pass


def team_scope(user):
    """None = every team, otherwise a set of lower-cased team names (possibly empty = nothing)."""
    if user["role"] in ("admin", "gm"):
        return None
    teams = user.get("teams") or []
    if ALL in teams:
        return None
    return {t.strip().lower() for t in teams if t.strip()}


def in_scope(user, team):
    sc = team_scope(user)
    return sc is None or (team or "").strip().lower() in sc


def emp_teams(emp):
    """The teams of an employee (a portal user may belong to several; the standalone store keeps one)."""
    return list(emp.get("teams") or ([emp["team"]] if emp.get("team") else []))


def shift_teams(teams, mark, known=()):
    """The team a planned shift counts for. A person is in ONE Workforce team (v5.17), but a name written «محمد صلاح (MFA)» in the schedule
    counts for the MFA team for that shift - whatever the person's own team is (``known`` = the team names of the roster, to find it)."""
    if (mark or "").strip().upper() == "MFA":
        mine = [t for t in teams if "mfa" in t.lower()]
        if mine:
            return mine
        found = sorted(t for t in known if "mfa" in str(t).lower())
        if found:
            return found[:1]
    return list(teams)


def has_team(emp, team):
    return (team or "").strip().lower() in {t.strip().lower() for t in emp_teams(emp)}


def can_see(user, emp):
    """An employee is visible when the user sees every team, or shares at least one team with them."""
    sc = team_scope(user)
    return sc is None or any(t.strip().lower() in sc for t in emp_teams(emp))


def visible_employees(repo, user, active_only=True):
    emps = repo.list_employees(active_only)
    return [e for e in emps if can_see(user, e)]


def visible_ids(repo, user, active_only=True):
    return [e["id"] for e in visible_employees(repo, user, active_only)]


def require_employee(repo, user, employee_id):
    e = repo.get_employee(employee_id)
    if not e or not can_see(user, e):
        raise Forbidden("You do not have access to this employee.")
    return e

def can_write(user):
    """admin and manager may change data; the general manager is read-only."""
    return user["role"] in ("admin", "manager")


ROLE_SUMMARY = {
    "admin": "Configures the module and has every permission across all teams.",
    "gm": "General Manager: sees every team, reports and exports - strictly read-only.",
    "manager": "Plans and manages only the teams on the account; cannot configure the module.",
}

# (capability, admin, gm, manager). True = yes, False = no, str = qualified. This table drives the "Roles & permissions" page and
# tests/test_roles.py checks every row against the real routes, so the page can never promise more or less than the server enforces.
ROLE_MATRIX = [
    ("See the Day view: teams, their managers, who is present, shifts and leave", "All teams", "All teams", "Own teams"),
    ("See Team availability: who is expected, presence, permit exceptions", "All teams", "All teams", "Own teams"),
    ("Open employee profiles and score explanations", "All teams", "All teams", "Own teams"),
    ("Run reports and export Excel / PDF", "All teams", "All teams", "Own teams"),
    ("Edit the weekly plan and the monthly calendar", "All teams", False, "Own teams"),
    ("Import the monthly shift schedule", True, False, False),
    ("Modification screen: change an employee's shift and manage leave", "All teams", False, "Own teams"),
    ("Import HR attendance files", True, False, False),
    ("Record leave and compensatory days (direct, no approval step)", "All teams", False, "Own teams"),
    ("Correct punches, review absences, record forgotten punches", "All teams", False, "Own teams"),
    ("Review attendance permits (اذن حضور / اذن انصراف)", "All teams", False, "Own teams"),
    ("Set leave entitlements", True, False, False),
    ("Settings: default shift, permit and staffing thresholds, scoring rules, holidays, shift types", True, False, False),
    ("Staff & teams, accounts, roles, audit log", True, False, False),
]
