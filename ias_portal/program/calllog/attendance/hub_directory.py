"""The portal's accounts ARE the attendance module's employees and users: nothing is copied, nothing is duplicated.

* employee  = an active portal account with a numeric User ID (the HR employee number) and the role "user"
              (managers too when the administrator ticks "Include managers" in the attendance settings);
* teams     = the ONE Workforce team of that account (``att_team``: the administrator's choice, else the first team on the account;
              never "Unclassified", which is an e-mail inbox) - the Calls / E-mail modules keep every team of the account;
* access    = administrator -> admin; a portal account the administrator made General Manager -> gm; manager -> manager
              (own teams only); every other account has no attendance access.

The only attendance-specific data kept about an account is the General Manager assignment (``roles.json`` in the attendance folder).
"""
import copy

from ..ops import overlay
from ..ops.schemas import ALL_TEAMS_MARK
from .repo.base import ConflictError

GM = "gm"
ALLOWANCE = "allowance"
ATT_ADMIN = "att_admin"       # (older way) roles.json list of Attendance administrators
ATT_ROLE = "attendance"       # the portal role of the Attendance administrator: every attendance right, attendance only, no employee
EMPLOYEE = "employee"


class HubDirectory:
    manages_employees = False       # employees and teams are edited on the portal's own Users page
    manages_users = False

    def __init__(self, store, repo=None):
        self.store = store
        self.repo = repo

    # ---------------------------------------------------------------- roles
    def _gm_ids(self):
        return set(self.repo._get("roles.json", {}).get(GM, []))

    def _allowance_ids(self):
        return set(self.repo._get("roles.json", {}).get(ALLOWANCE, []))

    def _att_admin_ids(self):
        return set(self.repo._get("roles.json", {}).get(ATT_ADMIN, []))

    def is_att_admin(self, user):
        """An Attendance administrator: the portal role "attendance" (not an employee: never on the roster) - every attendance
        right and nothing else of the portal. An account marked under the older way (a "user" + roles.json) counts too."""
        if not user:
            return False
        return user.get("role") == ATT_ROLE or (user.get("role") == "user" and user["user_id"] in self._att_admin_ids())

    def set_att_admin(self, user_id, on):
        """Grant / withdraw the Attendance administrator role. Returns the previous state."""
        with self.repo.transaction():
            roles = self.repo._mut("roles.json", {})
            ids = list(roles.get(ATT_ADMIN, []))
            before = user_id in ids
            if on and not before:
                ids.append(user_id)
            if not on:
                ids = [i for i in ids if i != user_id]
            roles[ATT_ADMIN] = sorted(ids)
        return before

    def can_allowance(self, user):
        """May this account open the shift-allowance export? Administrators (also of Attendance) always; anyone else only when
        the administrator assigned it."""
        return bool(user) and (user.get("role") == "admin" or self.is_att_admin(user) or user["user_id"] in self._allowance_ids())

    def set_allowance(self, user_id, on):
        """Give / withdraw the shift-allowance export. Returns the previous state."""
        with self.repo.transaction():
            roles = self.repo._mut("roles.json", {})
            ids = list(roles.get(ALLOWANCE, []))
            before = user_id in ids
            if on and not before:
                ids.append(user_id)
            if not on:
                ids = [i for i in ids if i != user_id]
            roles[ALLOWANCE] = sorted(ids)
        return before

    def role_of(self, user):
        if not user:
            return None
        if user.get("role") == "admin" or self.is_att_admin(user):
            return "admin"                                        # inside Attendance the Attendance administrator is an admin
        if user["user_id"] in self._gm_ids():
            return GM
        if user.get("role") == "manager":
            # owner (v5.18): only the managers the administrator gave the team's schedule have the team's Workforce pages (or the
            # only manager of the team). Another manager of the team is an employee here (own schedule and attendance);
            # a manager set to every team keeps every team.
            if overlay.user_teams(user) == [ALL_TEAMS_MARK] or user["user_id"] in self.team_managers_of(self.att_team(user)):
                return "manager"
            return EMPLOYEE if self._is_employee(user) else None
        if user["user_id"] in self._allowance_ids():
            return ALLOWANCE                                      # an account with only the allowance export (+ its own schedule)
        return EMPLOYEE if self._is_employee(user) else None      # an employee: only their own schedule

    def set_gm(self, user_id, on):
        """Grant / withdraw the General Manager role. Returns the previous state."""
        with self.repo.transaction():
            roles = self.repo._mut("roles.json", {})
            ids = list(roles.get(GM, []))
            before = user_id in ids
            if on and not before:
                ids.append(user_id)
            if not on:
                ids = [i for i in ids if i != user_id]
            roles[GM] = sorted(ids)
        return before

    def gm_candidates(self):
        """Active portal accounts that could be made General Manager (everyone except administrators)."""
        return [self._view(u) for u in self.store.users().values() if u.get("active", True) and u.get("role") != "admin"]

    # ---------------------------------------------------------------- users (portal accounts seen through attendance roles)
    # ---------------------------------------------------------------- the ONE Workforce team of a person (owner, v5.17)
    # An account may carry several teams and "Unclassified" for the E-mail module; in Workforce every person - employee or manager -
    # belongs to exactly one team: the one the administrator chose (Settings › Staff & teams), else the first team on the account.
    # "Unclassified" is an e-mail inbox, never a Workforce team. The all-teams marker stays (a manager who must see every team).
    def team_choices(self, u):
        """The teams on the account that can be a Workforce team (no all-teams marker, no "Unclassified")."""
        return [t for t in overlay.user_teams(u) if t != ALL_TEAMS_MARK and t.strip().casefold() != overlay.UNCLASSIFIED.casefold()]

    def att_team(self, u):
        """The person's single Workforce team ('' = none)."""
        choices = self.team_choices(u)
        chosen = (self.repo._get("settings.json", {}).get("attendance_team_of") or {}).get(str(u.get("user_id")))
        if chosen and any(chosen.strip().casefold() == c.strip().casefold() for c in choices):
            return next(c for c in choices if chosen.strip().casefold() == c.strip().casefold())
        return choices[0] if choices else ""

    def team_managers_all(self):
        """{team (lower case): [account ids]} - the managers who run each team in Workforce (its schedule, approvals, pages).
        The administrator gives it (Admin › Users "Manager + the team's schedule", or Settings › Team managers), to one or
        several managers of a team (owner, v5.18). A team with a single manager has that one until the administrator chooses."""
        users = [u for u in self.store.users().values() if u.get("active", True) and u.get("role") == "manager"
                 and overlay.user_teams(u) != [ALL_TEAMS_MARK]]
        by_team = {}
        for u in users:
            t = self.att_team(u).strip().lower()
            if t:
                by_team.setdefault(t, []).append(u["user_id"])
        chosen = self._chosen_managers()
        # the lists of older versions ("Team availability" showed several names) are NOT a choice of who runs the team: until
        # the administrator saves the managers once in this version, only the first of such a list counts (as in v5.17)
        legacy = not self.repo._get("settings.json", {}).get("team_managers_chosen")
        out = {}
        for t, ids in by_team.items():
            picks = [i for i in (chosen.get(t) or []) if i in ids]
            if legacy:
                picks = picks[:1]
            if not picks and len(ids) == 1 and t not in chosen:
                picks = ids[:]                               # the only manager of the team, until the administrator chooses
            if picks:
                out[t] = picks
        return out

    def _chosen_managers(self):
        return {str(k).strip().lower(): list(v or []) for k, v in (self.repo._get("settings.json", {}).get("team_managers") or {}).items()}

    def team_managers_of(self, team):
        """The account ids of the managers who run ``team`` in Workforce ([] = nobody chosen yet)."""
        return self.team_managers_all().get((team or "").strip().lower(), [])

    def schedule_right(self, u):
        """(team, holds): the Workforce team of a manager of ONE team, and whether this manager runs it (the team's schedule,
        approvals and pages). ("", False) for anybody else (an employee, an administrator, a manager of every team)."""
        if not u or u.get("role") != "manager" or overlay.user_teams(u) == [ALL_TEAMS_MARK]:
            return "", False
        team = self.att_team(u)
        return team, bool(team) and u["user_id"] in self.team_managers_of(team)

    def set_team_manager(self, user_id, on):
        """Admin › Users: give a manager their team's schedule, or take it away. Other managers of the team keep theirs
        (several may have it). Returns the team's holders before the change."""
        u = self.store.get_user(user_id)
        team, holds = self.schedule_right(u)
        if not team or holds == bool(on):
            return None
        working = self.team_managers_all()                  # who runs every team today (an older list: its first manager)
        before = working.get(team.strip().lower(), [])
        after = [i for i in before if i != user_id] + ([user_id] if on else [])
        with self.repo.transaction():
            st = self.repo._mut("settings.json", {})
            tm = {k: v for k, v in (st.get("team_managers") or {}).items() if k.strip().lower() != team.strip().lower()}
            if not st.get("team_managers_chosen"):           # the first choice in this version: keep the others as they work today
                tm = {k: working.get(k.strip().lower(), []) for k in tm}
            tm[team] = after                                  # [] = nobody (an explicit choice)
            st["team_managers"] = tm
            st["team_managers_chosen"] = True
        return before

    def team_manager_candidates(self, team):
        """The portal managers whose Workforce team is ``team`` (the administrator picks one of them)."""
        t = (team or "").strip().lower()
        return sorted((self._view(u) for u in self.store.users().values() if u.get("active", True) and u.get("role") == "manager"
                       and overlay.user_teams(u) != [ALL_TEAMS_MARK] and self.att_team(u).strip().lower() == t),
                      key=lambda v: v["name"])

    def att_teams(self, u):
        """The Workforce scope of an account: [ALL] for the all-teams marker, else its one team (or none)."""
        if overlay.user_teams(u) == [ALL_TEAMS_MARK]:
            return [ALL_TEAMS_MARK]
        t = self.att_team(u)
        return [t] if t else []

    def _view(self, u):
        teams = self.att_teams(u)
        return {"user_id": u["user_id"], "name": u.get("name") or u["user_id"], "role": self.role_of(u), "hub_role": u.get("role"),
                "teams": teams, "active": u.get("active", True), "must_change_password": bool(u.get("must_change_password")),
                "department": u.get("department", ""), "can_allowance": self.can_allowance(u),
                "att_admin": self.is_att_admin(u),
                "is_employee": self._is_employee(u) and u.get("active", True)}

    def first_logins(self, day):
        """{user_id: 'HH:MM:SS'} - the first successful portal sign-in of every account on ``day`` (from the portal's audit log).
        Used as the "arrived" mark of today until the HR attendance file of the day is imported."""
        out = {}
        for e in self.store.read_audit(day, day):
            if e.get("action") == "login" and e.get("actor"):
                out.setdefault(str(e["actor"]), str(e["ts"])[11:19])
        return out

    def team_members(self, team):
        """Every active portal account on ``team`` (any role): the people the administrator picks the team manager(s) from."""
        t = (team or "").strip().lower()
        rows = [u for u in self.store.users().values() if u.get("active", True) and u.get("role") != "admin"
                and self.att_team(u).strip().lower() == t]
        return sorted((self._view(u) for u in rows), key=lambda v: (v["hub_role"] != "manager", v["name"]))

    def get_user(self, user_id):
        u = self.store.get_user(user_id)
        return self._view(u) if u else None

    def list_users(self):
        """Portal accounts that have an attendance role (an employee's own-schedule access is not a role worth listing)."""
        return sorted((v for v in map(self._view, self.store.users().values()) if v["role"] and v["role"] != EMPLOYEE),
                      key=lambda v: v["user_id"])

    def create_user(self, user):
        raise ConflictError("Accounts are created on the portal's Users page.")

    def update_user(self, user_id, patch):
        raise ConflictError("Accounts are changed on the portal's Users page.")

    # ---------------------------------------------------------------- employees
    def _include_managers(self):
        return bool(self.repo._get("settings.json", {}).get("roster_include_managers", True))

    def _is_employee(self, u):
        uid = u.get("user_id", "")
        if not uid.isdigit():
            return False
        # an administrative-affairs account is under a team too (owner, v5.17): on the roster like an employee, still seeing only Workforce
        return u.get("role") in ("user", ATT_ROLE) or (u.get("role") == "manager" and self._include_managers())

    def _emp(self, u):
        t1 = self.att_team(u)
        teams = [t1] if t1 else []                              # one Workforce team per person (owner, v5.17)
        return {"id": int(u["user_id"]), "name": u.get("name") or u["user_id"], "department": u.get("department", ""), "sector": "",
                "team": teams[0] if teams else "", "teams": teams, "active": bool(u.get("active", True)),
                "created_at": u.get("created_at")}

    def get_employee(self, employee_id):
        u = self.store.get_user(str(employee_id))
        return self._emp(u) if u and self._is_employee(u) else None

    def list_employees(self, active_only=True):
        rows = [self._emp(u) for u in self.store.users().values() if self._is_employee(u)]
        return sorted((e for e in rows if e["active"] or not active_only), key=lambda e: e["id"])

    def add_employees_if_missing(self, employees):
        return 0            # people come from the portal's accounts; an unknown HR number is reported and skipped, never created

    def update_employee(self, employee_id, patch):
        raise ConflictError("Teams and names are edited on the portal's Users page.")


class HubAudit:
    """Attendance actions go into the portal's own audit log (same files, same viewer) with the action prefixed ``attendance.``."""
    PREFIX = "attendance."

    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    def write(self, rec):
        details = {**(rec.get("detail") or {})}
        if rec.get("entity"):
            details["entity"] = rec["entity"]
        if rec.get("entity_id") is not None:
            details["entity_id"] = rec["entity_id"]
        self.store.audit({"ts": rec["ts"].replace("T", " "), "actor": rec.get("user_id"), "actor_name": rec.get("username"),
                          "action": self.PREFIX + rec["action"], "target": rec.get("entity_id"), "ip": rec.get("ip"), "details": details})

    def read(self, action=None, entity=None, limit=500, days=366):
        from datetime import timedelta
        today = self.clock().date()
        rows = self.store.read_audit((today - timedelta(days=days)).isoformat(), today.isoformat())
        out = []
        for r in reversed(rows):
            if not str(r.get("action", "")).startswith(self.PREFIX):
                continue
            act = r["action"][len(self.PREFIX):]
            d = copy.deepcopy(r.get("details") or {})
            ent, eid = d.pop("entity", None), d.pop("entity_id", None)
            if (action and act != action) or (entity and ent != entity):
                continue
            out.append({"ts": r["ts"], "user_id": r.get("actor"), "username": r.get("actor_name"), "action": act, "entity": ent,
                        "entity_id": None if eid is None else str(eid), "detail": d, "ip": r.get("ip")})
            if len(out) >= limit:
                break
        return out
