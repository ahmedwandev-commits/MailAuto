"""Auto-assign: route pending work to a team's employees by the manager's rules.

Two targets share one config file (``ops/auto_assign.json``, see OpsStore.read_auto_assign):
  * EMAIL     - internal: distribute the team's PENDING e-mails among the chosen employees (this module does it, the
                web app applies it by writing normal assignment overrides so a distributed mail looks assigned).
  * TICKETING - the SSO/portal RPA (a CDP engine) assigns portal requests with the SAME rules.

Config shape::

    {
      "applications": [{"name": "Flexcube", "team": "Flex Cube"}, ...],   # admin: app -> team; a team sees only its apps
      "teams": {
        "<team>": {
          "email":     {"enabled": bool, "rules": [RULE, ...]},
          "ticketing": {"enabled": bool, "rules": [RULE, ...]}
        }
      }
    }
    RULE = {"employees": ["a@x", ...], "applications": ["Flexcube", ...], "from": "HH:MM", "to": "HH:MM"}
      employees    - who the work goes to (must be in the team); multi-select.
      applications - which apps this rule covers ([] = every app of the team); for e-mail, matched against a mail's
                     detected systems; multi-select.
      from / to    - the daily window the rule is active ("" = always; a window may wrap past midnight).

Everything here is a pure function over plain dicts (no Flask, no files), so the engine and the web app share it and it
is easy to test.
"""
import re

from . import overlay, schemas

TARGETS = ("email", "ticketing")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_INTERVAL_MINUTES = 10          # how often auto-assign runs for a team, unless the manager changes it
MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES = 1, 1440


def default_config():
    return {"applications": [], "teams": {}}


def clean_interval(value):
    """How often (minutes) a team's auto-assign runs: a whole number clamped to [1, 1440], else the default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, n))


# ---------------------------------------------------------------------- reading the config
def team_applications(config, team):
    """The application names an admin assigned to ``team`` (a team sees only its own), in order, de-duplicated."""
    key, out, seen = overlay._key(team), [], set()
    for app in (config or {}).get("applications") or []:
        name = str(app.get("name") or "").strip()
        if name and overlay._key(app.get("team")) == key and overlay._key(name) not in seen:
            seen.add(overlay._key(name))
            out.append(name)
    return out


def team_block(config, team, kind):
    """The ``email`` / ``ticketing`` block of a team's config, with defaults filled in."""
    block = (((config or {}).get("teams") or {}).get(team) or {}).get(kind) or {}
    return {"enabled": bool(block.get("enabled")), "interval_minutes": clean_interval(block.get("interval_minutes")),
            "rules": list(block.get("rules") or [])}


# ---------------------------------------------------------------------- rules and time windows
def _minutes(text):
    m = _TIME_RE.match(str(text or ""))
    return int(text[:2]) * 60 + int(text[3:]) if m else None


def rule_active(rule, now):
    """Is ``rule`` active at ``now`` (a datetime)? An empty / invalid from or to means 'always'."""
    start, end = _minutes((rule or {}).get("from")), _minutes((rule or {}).get("to"))
    if start is None or end is None:
        return True
    minute = now.hour * 60 + now.minute
    return start <= minute <= end if start <= end else (minute >= start or minute <= end)


def active_rules(block, now):
    return [r for r in (block or {}).get("rules") or [] if rule_active(r, now)]


def _apps_match(rule_apps, detected_systems, canon=None):
    """Does an e-mail's detected systems overlap the rule's applications? ([] apps = matches every mail.) ``canon``
    (an application name -> its canonical name, e.g. email_rules' canonical_application) is applied to BOTH sides, so
    the portal's 'Flex Cube' / 'صناديق الإستثمار FCIS' match a rule that says 'Flexcube'."""
    same = canon or (lambda name: name)
    wanted = {overlay._key(same(a)) for a in rule_apps or [] if str(a or "").strip()} - {""}
    if not wanted:
        return True
    have = {overlay._key(same(s)) for s in str(detected_systems or "").split(";") if s.strip()} - {""}
    return bool(wanted & have)


# ---------------------------------------------------------------------- e-mail distribution (internal, pure)
def plan_email(block, rows, now, members, load=None):
    """Which PENDING, still-unassigned e-mails to hand to which employee.

    ``block`` = a team's ``email`` config; ``rows`` = that team's overlaid e-mail rows; ``now`` = datetime;
    ``members`` = {email: name} of the team's active employees; ``load`` = {email: count} already carried (for balance).
    Returns ``[{"message_id", "email", "name"}]`` - one per mail an active rule covers, balanced across that rule's
    employees (fewest-loaded first, then alphabetical). A mail already assigned, or matched by no rule, is skipped.
    """
    if not block.get("enabled"):
        return []
    rules = active_rules(block, now)
    if not rules:
        return []
    carried = {overlay.normalize_email(k): v for k, v in (load or {}).items()}
    out, taken = [], set()
    pending = sorted((r for r in rows if r.get("status") == schemas.EMAIL_PENDING
                      and not overlay.normalize_email(r.get("assigned_to_email")) and r.get("message_id")),
                     key=lambda r: str(r.get("received_time") or ""))
    for row in pending:
        mid = row.get("message_id")
        if mid in taken:
            continue
        rule = next((r for r in rules if _apps_match(r.get("applications"), row.get("detected_systems"))), None)
        if rule is None:
            continue
        pool = [e for e in (overlay.normalize_email(x) for x in rule.get("employees") or []) if e in members]
        if not pool:
            continue
        email = min(pool, key=lambda e: (carried.get(e, 0), e))
        carried[email] = carried.get(email, 0) + 1
        taken.add(mid)
        out.append({"message_id": mid, "email": email, "name": members.get(email, "")})
    return out


# ---------------------------------------------------------------------- ticketing distribution (the portal RPA, pure)
def plan_ticketing(block, requests, now, members, load=None, canon=None):
    """Which NEW portal requests to hand to which security employee, by the manager's ticketing rules.

    The native CDP assign engine reads the "assign new requests" page into ``requests`` - each a dict with a stable
    ``id`` (the grid row id the engine will tick) and a ``system`` (the request's application, matched against a rule's
    applications). ``members`` maps a team member's e-mail to ``{"name", "portal_id"}``; ``portal_id`` is that person's
    value in the portal's الموظفين drop-down (set per user in admin). ``load`` is an optional {portal_id: count} already
    carried, for balance. ``canon`` names an application the way the rules do (see ``_apps_match``).

    Returns ``[{"id", "portal_id", "email", "name"}]`` - one per request an active rule covers, balanced across that
    rule's employees that have a portal id (fewest-loaded first, then portal id). A request matched by no rule, or whose
    rule names no portal-mapped employee, is skipped (left for a human). Pure: no browser, no files, easy to test."""
    if not block.get("enabled"):
        return []
    rules = active_rules(block, now)
    if not rules:
        return []
    member_by_email = {overlay.normalize_email(e): m for e, m in (members or {}).items()}
    carried = dict(load or {})
    out, taken = [], set()
    for req in requests or []:
        rid = req.get("id")
        if rid is None or rid in taken:
            continue
        rule = next((r for r in rules if _apps_match(r.get("applications"), req.get("system"), canon)), None)
        if rule is None:
            continue
        pool = []
        for raw in rule.get("employees") or []:
            m = member_by_email.get(overlay.normalize_email(raw))
            pid = str((m or {}).get("portal_id") or "").strip()
            if pid and (pid, raw) not in pool:
                pool.append((pid, overlay.normalize_email(raw)))
        if not pool:
            continue
        pid, email = min(pool, key=lambda pe: (carried.get(pe[0], 0), pe[0]))
        carried[pid] = carried.get(pid, 0) + 1
        taken.add(rid)
        out.append({"id": rid, "portal_id": pid, "email": email,
                    "name": (member_by_email.get(email) or {}).get("name", "")})
    return out


def group_by_employee(plan):
    """``plan`` (plan_ticketing output) -> ordered ``[{"portal_id", "name", "email", "ids": [...]}]``: the request ids
    to tick for each security employee, the busiest employee first (so one Execute per employee covers their share)."""
    groups = {}
    for item in plan or []:
        g = groups.setdefault(item["portal_id"], {"portal_id": item["portal_id"], "name": item.get("name", ""),
                                                   "email": item.get("email", ""), "ids": []})
        g["ids"].append(item["id"])
    return sorted(groups.values(), key=lambda g: (-len(g["ids"]), g["portal_id"]))


# ---------------------------------------------------------------------- validation / normalisation (for the API)
def clean_time(text):
    text = str(text or "").strip()
    return text if (not text or _TIME_RE.match(text)) else ""


def clean_rule(rule, allowed_apps, members):
    """A normalised rule, or None when it names no usable (in-team) employee. Unknown employees / apps are dropped."""
    rule = rule if isinstance(rule, dict) else {}
    emps = []
    for raw in rule.get("employees") or []:
        e = overlay.normalize_email(raw)
        if e and e in members and e not in emps:
            emps.append(e)
    if not emps:
        return None
    appkeys = {overlay._key(a) for a in allowed_apps}
    apps = []
    for raw in rule.get("applications") or []:
        name = str(raw or "").strip()
        if name and overlay._key(name) in appkeys and name not in apps:
            apps.append(name)
    return {"employees": emps, "applications": apps, "from": clean_time(rule.get("from")), "to": clean_time(rule.get("to"))}


def clean_block(block, allowed_apps, members):
    """A normalised ``{enabled, interval_minutes, rules}`` block (invalid rules dropped)."""
    block = block if isinstance(block, dict) else {}
    rules = [r for r in (clean_rule(x, allowed_apps, members) for x in block.get("rules") or []) if r]
    return {"enabled": bool(block.get("enabled")), "interval_minutes": clean_interval(block.get("interval_minutes")), "rules": rules}


def clean_applications(apps, teams):
    """Normalise the admin's application list: each needs a name and a team that exists; names are unique (first wins)."""
    teamset = {overlay._key(t) for t in teams}
    out, seen = [], set()
    for app in apps or []:
        if not isinstance(app, dict):
            continue
        name, team = str(app.get("name") or "").strip(), str(app.get("team") or "").strip()
        if name and overlay._key(name) not in seen and overlay._key(team) in teamset:
            seen.add(overlay._key(name))
            out.append({"name": name, "team": team})
    return out
