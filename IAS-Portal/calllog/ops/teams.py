"""Which team a security request (SLA) or an ITSM role belongs to - ONE rule for every page, export, chart, timeline item
and Home number, so a manager of one team sees only that team everywhere and the numbers on every dashboard add up.

A security request belongs to the team of its APPLICATION «النظام» - never to the team of the person who holds or
executed it (the owner's rule):
  1. the administrator's application list (Auto-Assign > Applications: name -> team);
  2. the e-mail classification keywords (built-in map + the administrator's rules) found in the application name;
  otherwise no team: only administrators and managers of every team see it, and the SLA Analysis lists the application
  under "Applications with no team" for the administrator to give it one.
  A team merged into another for the SLA (``sla.merge_teams``: MFA Team -> Open System by default) counts as that
  team - in every SLA page, export and number - and a manager of the merged team sees the team it is merged into
  (``Resolver.sla_scope``). E-mails, ITSM and the accounts keep their own teams.

An ITSM role (a CA SDM role such as "IMP Card Management"):
  1. the administrator's map ``itsm.role_teams`` {role: team};
  2. a team whose name is in the role name ("IMP Card Management" -> Card Management, "IMP Open Systems" -> Open System);
  3. the e-mail classification keywords found in the role name ("IMP UAT I-Flex SMS" -> Flex Cube);
  otherwise no team.

Everything here is pure (no Flask, no files): ``Resolver`` gets the settings, the users and the application list.
"""
from .engines import email_rules
from .engines.common import norm_arabic
from . import overlay, schemas

NO_TEAM = "(no team)"           # what the pages show for a request / role no rule gives a team (admins only see it)


def _key(value):
    return norm_arabic(str(value or "")).strip()


def _real_team(name):
    return bool(name) and name != schemas.ALL_TEAMS_MARK and name.casefold() != overlay.UNCLASSIFIED.casefold()


def team_merges(settings):
    """{team (normalised): the team its security requests count under} from ``sla.merge_teams`` - the SLA only."""
    raw = ((settings or {}).get("sla") or {}).get("merge_teams") or {}
    if not isinstance(raw, dict):
        return {}
    return {_key(k).casefold(): str(v).strip() for k, v in raw.items()
            if _key(k) and _key(v) and _key(k).casefold() != _key(v).casefold()}


def merged_team(team, merges):
    """The team a security request of ``team`` counts under (itself unless it is merged into another)."""
    return (merges or {}).get(_key(team).casefold(), team) if team else team


def merged_scope(scope, merges):
    """``scope`` as it sees the SLA: a manager of a merged team also sees the team it is merged into. None = every
    team."""
    if scope is None or not merges:
        return scope
    extra = [merges[_key(t).casefold()] for t in scope if _key(t).casefold() in merges]
    return tuple(dict.fromkeys(list(scope) + extra))


class Resolver:
    def __init__(self, settings=None, users=None, applications=None):
        settings = settings or {}
        email = settings.get("email") or {}
        self.teams = [t for t in (email.get("teams") or []) if _real_team(t)]
        self.merges = team_merges(settings)
        self._apps = {_key(a.get("name")): str(a.get("team"))
                      for a in (applications or []) if a.get("name") and a.get("team")}
        self._roles = {_key(k): str(v) for k, v in ((settings.get("itsm") or {}).get("role_teams") or {}).items() if v}
        self._rules = email_rules.build_rules(email)
        self._cache = {}

    def _keywords(self, text):
        _, _, all_teams = self._rules.detect("", str(text or ""))
        return all_teams[0] if all_teams else ""

    def sla_team(self, system, security_user=""):
        """The team of one security request: its application's (``application_team``), whoever holds it - the
        security user is not used ("" = no team found)."""
        return self.application_team(system)

    def application_team(self, system):
        """The team an APPLICATION belongs to, whoever holds the request: the administrator's application list, else the
        classification keywords in its name ("" = none); a merged team as the team it is merged into."""
        key = ("app", _key(system))
        if key not in self._cache:
            team = self._apps.get(key[1], "") or (self._keywords(system) if key[1] else "")
            self._cache[key] = merged_team(team, self.merges)
        return self._cache[key]

    def sla_scope(self, scope):
        """``scope`` widened for the SLA (``merged_scope``)."""
        return merged_scope(scope, self.merges)

    def itsm_team(self, role):
        """The team of one ITSM role ("" = no team found)."""
        key = ("itsm", _key(role))
        if key not in self._cache:
            team = self._roles.get(key[1], "")
            if not team:
                low = key[1].lower()
                for name in sorted(self.teams, key=len, reverse=True):          # "Open System" in "IMP Open Systems"
                    if _key(name).lower() and _key(name).lower() in low:
                        team = name
                        break
            if not team and key[1]:
                team = self._keywords(role)
            self._cache[key] = team
        return self._cache[key]


def sla_exclusion(sla_settings, resolver):
    """``excluded(system) -> bool`` for the SLA and the SLA Analysis: an application in ``sla.exclude_applications``, or
    one whose team (``Resolver.application_team``) is in ``sla.exclude_teams`` (by default the Third Party team and its
    applications). None when nothing is excluded."""
    cfg = sla_settings or {}
    teams_out = {_key(t).casefold() for t in cfg.get("exclude_teams") or [] if _key(t)}
    apps_out = {_key(a).casefold() for a in cfg.get("exclude_applications") or [] if _key(a)}
    if not teams_out and not apps_out:
        return None

    def excluded(system):
        name = _key(system).casefold()
        if not name:
            return False
        if name in apps_out:
            return True
        team = resolver.application_team(system)
        return bool(team) and _key(team).casefold() in teams_out
    return excluded


def sla_people(users):
    """(by number, by security name): the accounts by the number the portal shows in its status history («رقم
    المستخدم»: the account's portal ID, else its user ID) and by their security name - so every step of a request is
    linked to the person who did it."""
    users = list(users.values()) if isinstance(users, dict) else list(users or [])
    by_no, by_name = {}, {}
    for field in ("portal_id", "user_id"):              # the portal ID wins over the user ID
        for u in users:
            ident = str(u.get(field) or "").strip()
            if ident:
                by_no.setdefault(ident, u)
    for u in users:
        name = norm_arabic(u.get("security_name"))
        if name:
            by_name.setdefault(name, u)
    return by_no, by_name


def sla_record_team(system, resolver):
    """The team of one SLA request in the SLA Analysis (and the daily summary, the performers, the day-by-day log):
    its application's (``Resolver.sla_team``) - never the team of the person who executed or holds it - else
    ``NO_TEAM``."""
    return resolver.sla_team(system) or NO_TEAM


def in_scope(scope, team):
    """Does a row of ``team`` belong to ``scope``? (None = every team; a row with no team only for None.)"""
    return scope is None or (bool(team) and team != NO_TEAM and overlay.team_in(scope, team))


# --------------------------------------------------------------------------- one module's data, limited to a scope
SLA_SYSTEM_HEADER = "النظام"


def sla_system(request, header=SLA_SYSTEM_HEADER):
    return str(((request or {}).get("fields") or {}).get(header) or "")


def sla_request_team(request, resolver, header=SLA_SYSTEM_HEADER):
    return resolver.sla_team(sla_system(request, header), (request or {}).get("security_user"))


def scope_sla(data, scope, resolver, header=SLA_SYSTEM_HEADER):
    """The SLA snapshot data limited to ``scope``: the requests of those teams, with the totals and the per-user table
    recomputed from them, so every tile, table and chart of a manager adds up. None = everything, unchanged."""
    if scope is None or not isinstance(data, dict):
        return data
    from .engines import sla_engine
    scope = resolver.sla_scope(scope)
    reqs = [r for r in data.get("requests") or [] if in_scope(scope, sla_request_team(r, resolver, header))]
    return {**data, "requests": reqs, "totals": sla_engine.totals_of(reqs), "by_user": sla_engine.summarize(reqs)}


def scope_itsm(data, scope, resolver):
    """The ITSM snapshot data limited to the roles of ``scope`` (totals and 'what changed' recomputed)."""
    if scope is None or not isinstance(data, dict):
        return data
    from .engines import itsm_engine
    keep = lambda role: in_scope(scope, resolver.itsm_team(role))      # noqa: E731
    roles = [r for r in data.get("roles") or [] if keep(r.get("role"))]
    changes = {k: [c for c in (v or []) if keep(c.get("role"))] for k, v in (data.get("changes") or {}).items()}
    return {**data, "roles": roles, "changes": changes,
            "totals": itsm_engine.compute_totals(roles, data.get("categories") or [])}


# --------------------------------------------------------------------------- history points, limited to a scope
SLA_COUNTS = ("open", "within", "reminder", "breached")
EMAIL_COUNTS = ("business_emails", "replied", "pending", "no_action_needed", "sla_breached", "escalated")


def _sum_groups(groups, keys):
    return {k: sum(int(g.get(k) or 0) for g in groups) for k in keys}


def scope_history_day(module, doc, scope, resolver, header=SLA_SYSTEM_HEADER):
    """One history day document (``points`` plus the module's extras) limited to ``scope``. A point is rebuilt from
    its per-group counts (``groups``, written by the collector since this version); an older point without them
    cannot be split by team and is left out for a manager. None = everything, unchanged."""
    if scope is None or not isinstance(doc, dict):
        return doc
    out = {"day": doc.get("day"), "points": []}
    sla_scope = resolver.sla_scope(scope) if module == "sla" else scope
    for point in doc.get("points") or []:
        if module == "itsm":
            per_role = {role: counts for role, counts in (point.get("per_role") or {}).items()
                        if in_scope(scope, resolver.itsm_team(role))}
            totals = {}
            for counts in per_role.values():
                for cat, n in (counts or {}).items():
                    totals[cat] = totals.get(cat, 0) + int(n or 0)
            out["points"].append({"ts": point.get("ts"), "totals": totals, "per_role": per_role})
            continue
        groups = point.get("groups")
        if not isinstance(groups, list):
            continue
        if module == "sla":
            mine = [g for g in groups if in_scope(sla_scope, resolver.sla_team(g.get("system"), g.get("user")))]
            out["points"].append({"ts": point.get("ts"), **_sum_groups(mine, SLA_COUNTS)})
        elif module == "email":
            mine = [g for g in groups if any(in_scope(scope, t) for t in g.get("teams") or [])]
            out["points"].append({"ts": point.get("ts"), **_sum_groups(mine, EMAIL_COUNTS)})
    if module == "email" and isinstance(doc.get("quality"), dict):
        out["quality"] = {name: q for name, q in doc["quality"].items() if in_scope(scope, (q or {}).get("team"))}
    if module == "email" and out["points"]:
        last = out["points"][-1]                         # the day's headline numbers, from the day's last scan of the scope
        out["final"] = {k: last.get(k, 0) for k in ("business_emails", "replied", "pending", "no_action_needed", "sla_breached")}
    return out


def recount_text(text, count):
    """An event text such as "3 tickets passed the 2 h SLA" with another count ("1 ticket passed the 2 h SLA")."""
    head, _, rest = str(text or "").partition(" ")
    if not head.isdigit():
        return text
    word, _, tail = rest.partition(" ")
    if word.endswith("s") and int(head) != 1 and count == 1:
        word = word[:-1]
    elif int(head) == 1 and count != 1 and word in ("ticket", "e-mail", "request"):
        word += "s"
    return " ".join(x for x in (str(count), word, tail) if x)


# --------------------------------------------------------------------------- per-group counts (written by the collector)
def sla_groups(requests, header=SLA_SYSTEM_HEADER):
    """The counts of SLA requests per (application, security user): what a history point needs so that the web can
    rebuild it for any team later (the team rule needs exactly these two)."""
    groups = {}
    for r in requests or []:
        key = (sla_system(r, header), str(r.get("security_user") or ""))
        g = groups.setdefault(key, {"system": key[0], "user": key[1], **{k: 0 for k in SLA_COUNTS}})
        g["open"] += 1
        if r.get("status") in SLA_COUNTS:
            g[r["status"]] += 1
    return list(groups.values())


def email_teams(row):
    """The teams of an e-mail row (its team first, then the other teams it was sent to)."""
    return list(dict.fromkeys(t for t in [row.get("team")] + list(row.get("all_teams") or []) if t))


def email_groups(rows):
    """The counts of an e-mail scan per set of teams, with the same formulas as ``overlay.recount``."""
    groups = {}
    for r in rows or []:
        names = email_teams(r)
        g = groups.setdefault(tuple(names), {"teams": names, **{k: 0 for k in EMAIL_COUNTS}})
        g["business_emails"] += 0 if r.get("is_carryover") else 1
        g["replied"] += r.get("status") == schemas.EMAIL_REPLIED
        g["pending"] += r.get("status") == schemas.EMAIL_PENDING
        g["no_action_needed"] += r.get("status") == schemas.EMAIL_NO_ACTION
        g["sla_breached"] += r.get("sla_status") == "Breached"
        g["escalated"] += bool(r.get("escalated"))
    return list(groups.values())
