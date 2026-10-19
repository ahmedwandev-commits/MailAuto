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


def _apps_match(rule_apps, detected_systems):
    """Does an e-mail's detected systems overlap the rule's applications? ([] apps = matches every mail.)"""
    wanted = {overlay._key(a) for a in rule_apps or []} - {""}
    if not wanted:
        return True
    have = {overlay._key(s) for s in str(detected_systems or "").split(";")} - {""}
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
