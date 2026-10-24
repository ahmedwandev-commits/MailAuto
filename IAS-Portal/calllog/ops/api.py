"""Operations Hub JSON API, mounted at ``/api/ops`` (see docs/HUB-DESIGN.md, section 7).

What lives here: the two dashboards (``/shared`` for everybody, ``/my`` for the signed-in person), the module answers
(snapshot + collector health + e-mail overrides applied), the e-mail actions (assign, no-action, team, quality,
classification rules), jobs ("Run now"), settings, reports, history, events and the application health.

Rules this file keeps:
* the web app never talks to Outlook or the portals - it reads snapshots and writes override files;
* an employee only ever gets numbers (``/shared``), the rows that are theirs and the e-mails of their own teams
  (``/my``) - never a mail of the "Unclassified" bucket unless it was handed to them by name;
* a manager acts only on the e-mails of their own teams (an account can hold several teams);
* every write is validated, answered as JSON and written to the audit log (``ops_*`` actions);
* scraped / e-mail text is returned as data only - the pages show it with textContent.
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

from flask import Blueprint, current_app, g, jsonify, request, send_file

from .. import auth, overview as calls_overview, stats as call_stats
from ..api import public_user
from ..queries import ApiError, decorate, json_body, parse_day, reasons_by_key
from ..storage import STATUSES, StorageError, norm_uid
from . import analytics, auto_assign, health, itsm_records, mailbox, notifications, overlay, schemas, sla_history, sla_records, teams
from . import settings as hub_settings
from .engines import common
from .engines.common import norm_arabic
from .store import now_iso

_TICKETING_LOCK = threading.Lock()                 # one browser-driven ticketing auto-assign run at a time per process

bp = Blueprint("ops_api", __name__)

RATINGS = ("Excellent", "Good", "Needs Improvement")
DECISIONS = ("Accepted", "Rejected")
# Pre-set reasons offered when rating a reply (from the old e-mail dashboard) so ratings stay analysable.
QUALITY_REASONS = ["Incomplete answer", "Wrong information", "Tone / wording", "Too slow", "Missing follow-up",
                   "Not escalated properly"]
MANAGER_JOBS = ("email", "sla", "itsm")
MODULE_LABELS = {"email": "E-mail", "sla": "Ticketing", "itsm": "ITSM"}
MODULE_LINKS = {"email": "/email", "sla": "/sla", "itsm": "/itsm"}
_SECRET_KEYS = {"password", "passwd", "pwd", "secret", "token", "api_key", "apikey"}
_MAX_LIST = 100
_MAX_ASSIGNED = 300          # assigned mails listed on My work (the page shows them 10 per page)


# ----------------------------------------------------------------------------- shared helpers
def ops():
    return current_app.extensions["ops"]


def hub_cfg():
    """The Hub settings (cached for the request; ``forget_settings`` after a change)."""
    cached = g.get("_hub_settings")
    if cached is None:
        cached = g._hub_settings = hub_settings.load(ops())
    return cached


def forget_settings():
    g.pop("_hub_settings", None)


def read_snapshot(module):
    """(snapshot or None, problem text or None). A damaged file is reported to the page instead of failing it."""
    try:
        return ops().read_snapshot(module), None
    except StorageError as exc:
        return None, str(exc)[:300]


def clean_text(value, field, maxlen, required=False, minlen=0, multiline=False):
    text = str(value or "").strip() if multiline else " ".join(str(value or "").split())
    if required and not text:
        raise ApiError(f"{field} is required.")
    if text and len(text) < minlen:
        raise ApiError(f"{field} is too short (at least {minlen} characters).")
    if len(text) > maxlen:
        raise ApiError(f"{field} is too long (max {maxlen} characters).")
    return text


def optional_body():
    """The JSON object of a request that may also carry its arguments in the query string (DELETE)."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def clean_message_id(value):
    text = str(value or "").strip()
    if not text or len(text) > 500 or any(ord(c) < 32 for c in text):
        raise ApiError("message_id is required.")
    return text


def minutes_since(stamp, now=None):
    try:
        return max(0, int(((now or auth.now()) - datetime.fromisoformat(str(stamp))).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _fmt_threshold(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60} h" if minutes and minutes % 60 == 0 else f"{minutes} min"


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


# ----------------------------------------------------------------------------- collector state
def job_states():
    cached = g.get("_job_states")
    if cached is None:
        cached = g._job_states = health.job_states(ops(), hub_cfg(), auth.now())
    return cached


def viewer_is_admin():
    return (g.get("user") or {}).get("role") == "admin"


def _mask_collector_health(meta):
    """Only an administrator sees collector problems. A manager still sees the data, but the offline / failed / stale
    alarms and the error text are hidden (managers act on e-mails and tickets; they do not monitor the collectors)."""
    def clean(state):
        s = dict(state)
        s["online"], s["last_error"] = True, None
        if s.get("last_status") == "error":
            s["last_status"] = "ok"
        return s
    return {**meta, "online": True, "stale": False, "last_error": None,
            "last_status": "ok" if meta.get("last_status") == "error" else meta.get("last_status"),
            "jobs": {job: clean(st) for job, st in (meta.get("jobs") or {}).items()}}


def module_meta(module, snap, problem=None, mask=None):
    """Freshness of one module: is its collector alive (``online``), is the data too old (``stale``). ``mask`` hides the
    collector errors/offline for non-admins (default); pass ``mask=False`` where the raw status is needed (the strip)."""
    cfg, now = hub_cfg(), auth.now()
    jobs = {job: state for job, state in job_states().items() if schemas.JOB_MODULE[job] == module}
    main = jobs[module]
    interval = health.module_interval(cfg, module)
    age = minutes_since(snap.get("generated_at"), now) if snap else None
    active = hub_settings.is_active(cfg, module, now)
    enabled = bool((cfg.get(module) or {}).get("enabled", True))
    # outside the working hours / days nothing is scheduled, so old data is expected and not a fault
    stale = bool(enabled and active and snap and age is not None and age > 3 * max(1, interval))
    meta = {"module": module, "enabled": enabled, "interval_minutes": interval, "online": main["online"],
            "stale": stale, "active_now": active, "age_minutes": age,
            "generated_at": snap.get("generated_at") if snap else None, "date": snap.get("date") if snap else None,
            "running": main.get("running", False), "host": main.get("host", ""), "last_status": main.get("last_status"),
            "last_error": main.get("last_error"), "last_ok_at": main.get("last_ok_at"),
            "last_run_at": main.get("last_run_at"), "next_run_at": main.get("next_run_at"),
            "last_summary": main.get("last_summary", ""), "jobs": jobs, "problem": problem,
            "server_time": now.isoformat(timespec="seconds")}
    if mask is None:
        mask = not viewer_is_admin()
    return _mask_collector_health(meta) if mask else meta


# ----------------------------------------------------------------------------- overrides
def read_overrides():
    return {name: ops().overrides(name) for name in schemas.OVERRIDES}


def overrides_rev(ovr):
    """Short fingerprint of the override lists (pages send it back as ``rev`` to learn that someone acted)."""
    blob = json.dumps(ovr, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _latest_override_stamp(ovr):
    latest = ""
    for entries in ovr.values():
        for entry in entries.values():
            for field in ("marked_at", "reviewed_at", "appeal_at", "decided_at", "done_at"):
                stamp = str(entry.get(field) or "").replace(" ", "T")
                latest = max(latest, stamp)
    return latest


_CACHE_LIMIT = 96


def _view_cache():
    return current_app.extensions.setdefault("ops_view_cache", {})


def _remember(cache, key, snap, value):
    """Store ``value`` for ``key`` (the last item of a key is the 5-second bucket, so old entries can be dropped)."""
    if len(cache) >= _CACHE_LIMIT:
        for old in [k for k in cache if k[-1] != key[-1]]:
            del cache[old]
        if len(cache) >= _CACHE_LIMIT:
            cache.clear()
    cache[key] = (snap, value)


def current_overrides():
    """(override lists, their fingerprint) for a read-only request - read once per request."""
    cached = g.get("_overrides")
    if cached is None:
        ovr = read_overrides()
        cached = g._overrides = (ovr, overrides_rev(ovr))
    return cached


def overlaid_rows(snap, ovr=None, rev=None):
    """All rows of the e-mail snapshot with the overrides applied (every team, shared by every scope below). Callers
    must treat the list and its rows as read-only."""
    if ovr is None:
        ovr, rev = current_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    now = auth.now()
    key = ("rows", snap.get("generated_at"), rev, int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:                # same parsed file, not just the same timestamp
        return hit[1]
    rows = overlay.apply_overrides((snap.get("data") or {}).get("rows") or [], ovr, now)
    _remember(cache, key, snap, rows)
    return rows


def email_view(snap, scope_team=None, ovr=None, hide_unclassified=False, rev=None):
    """The e-mail snapshot data with overrides applied, narrowed to a scope (None = every team, else a team name or a
    tuple of names) and recounted.

    Many pages ask for the same thing every few seconds, so the result is shared for a moment (and until the snapshot
    or an override changes). Callers must treat it as read-only."""
    ovr = ovr if ovr is not None else read_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    scope = overlay.as_scope(scope_team)
    now = auth.now()
    key = ("view", snap.get("generated_at"), rev, overlay.scope_key(scope), bool(hide_unclassified),
           int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:
        return hit[1]
    view = overlay.view_email_data(snap.get("data") or {}, ovr, scope, now, rows=overlaid_rows(snap, ovr, rev),
                                   hide_unclassified=hide_unclassified,
                                   person_teams=person_teams() if scope is not None else None)
    _remember(cache, key, snap, view)
    return view


def current_email_view(scope_team=None):
    """(view or None, snapshot or None) for the latest e-mail snapshot."""
    snap, _ = read_snapshot("email")
    return (email_view(snap, scope_team) if snap else None), snap


def team_mail(snap, scope, ovr=None, rev=None):
    """(items, summary) of the e-mails of ``scope`` for the 'my team' list (see ``overlay.team_mail``), computed once
    per snapshot version, overrides version and team set and shared by everybody with the same teams. Read-only."""
    if ovr is None:
        ovr, rev = current_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    now = auth.now()
    key = ("team_mail", snap.get("generated_at"), rev, overlay.scope_key(scope), int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:
        return hit[1]
    value = overlay.team_mail(overlaid_rows(snap, ovr, rev), scope,
                              person_teams=person_teams() if scope is not None else None)
    _remember(cache, key, snap, value)
    return value


# ----------------------------------------------------------------------------- headline numbers
EMAIL_HEADLINE = ("business_emails", "replied", "pending", "no_action_needed", "needed_reply", "reply_rate",
                  "sla_breached", "sla_compliance_rate", "escalated_count", "carryover_count", "carryover_oldest_days",
                  "assigned_count", "assigned_done_count", "average_response")


def email_headline(stats):
    return {k: stats.get(k, 0) for k in EMAIL_HEADLINE}


def sla_since(r):
    """The clock of an open request: its last assignment to the security user holding it when the scan matched it to
    the SLA history (``assigned_at``), else the day it was sent to the team (``sent_at``)."""
    return (r or {}).get("assigned_at") or (r or {}).get("sent_at")


def sla_oldest_now(requests):
    """Minutes the oldest open request has been waiting NOW (from its last assignment, else its sent date), the way the
    Live tickets tab counts it - not the age frozen at the last scan. None when no request has a date (pages show
    "—")."""
    now, ages = auth.now(), []
    for r in requests or []:
        try:
            sent = datetime.fromisoformat(str(sla_since(r) or "")[:19])
        except ValueError:
            continue
        ages.append(max(0, int((now - sent).total_seconds() // 60)))
    return max(ages) if ages else None


def sla_headline(data):
    totals = data.get("totals") or {}
    out = {k: totals.get(k, 0) for k in ("open", "within", "reminder", "breached", "no_date")}
    out["oldest_minutes"] = sla_oldest_now(data["requests"]) if "requests" in data else totals.get("oldest_minutes")
    return out


# ----------------------------------------------------------------------------- one team rule for SLA and ITSM data
def team_resolver():
    """Which team an SLA request / an ITSM role belongs to (calllog/ops/teams.py), with the current settings, accounts
    and application list - built once per request."""
    if "team_resolver" not in g:
        g.team_resolver = teams.Resolver(hub_cfg(), auth.store().users(), _aa_config().get("applications") or [])
    return g.team_resolver


def sla_people():
    """{the portal's employee number (EmpNo in the ALL_*.csv export): the person's security-user name} from the
    accounts (their portal ID, else their user ID), so an export line names its handler the way the live scan does."""
    if "sla_people" not in g:
        users = auth.store().users()
        people = {}
        for u in (users.values() if isinstance(users, dict) else users):
            name = str(u.get("security_name") or u.get("name") or "").strip()
            for ident in (u.get("user_id"), u.get("portal_id")):        # the portal ID wins over the user ID
                if str(ident or "").strip() and name:
                    people[str(ident).strip()] = name
        g.sla_people = people
    return g.sla_people


def sla_system_header():
    """The request column that names the application (the SLA-analysis setting, «النظام» by default)."""
    cfg = {**sla_records.default_config(), **(ops().read_sla_records().get("config") or {})}
    return str((cfg.get("fields") or {}).get("system") or teams.SLA_SYSTEM_HEADER)


def scoped_data(module, data, scope):
    """A module snapshot's ``data`` as ``scope`` may see it: only the SLA requests / ITSM roles of those teams, with
    every total recomputed (e-mail has its own view, ``email_view``). None = every team, unchanged."""
    if scope is None or data is None:
        return data
    if module == "sla":
        return teams.scope_sla(data, scope, team_resolver(), sla_system_header())
    if module == "itsm":
        return teams.scope_itsm(data, scope, team_resolver())
    return data


def calls_totals(day, scope=None):
    """Team calls of one day from the Call Log storage (counts only): every employee's for ``scope`` None, else only
    the calls of the employees of those teams (the same numbers as the manager's "My team's calls" page)."""
    if scope is not None:
        team = build_team_calls(day, scope)
        return {"date": day, **team["totals"], "_hours": team["per_hour"]}
    data = calls_overview.merged(day, day)
    t = data["totals"]
    users = auth.store().users()
    employees = sum(1 for u in users.values() if u.get("role") == "user" and u.get("active", True))
    return {"date": day, "total": t["total"], **{s: t[s] for s in STATUSES}, "other": t.get("other", 0),
            "active_employees": sum(1 for u in data["users"].values() if u["total"] > 0), "employees": employees,
            "_hours": call_stats.chart_buckets(data["rollups"], "hour", day, day)}


def _public_calls(totals):
    return {k: v for k, v in totals.items() if not k.startswith("_")}


def shared_scope(user, viewer=None):
    """The teams the 'Team status' strip counts e-mails of for ``user``: None = every team. When somebody else (a
    manager) looks at another person's page, only what both of them may see."""
    scope = overlay.scope_for_user(user)
    if viewer is not None:
        scope = overlay.intersect_scopes(scope, overlay.scope_for_user(viewer))
    return scope


def build_shared(user=None, viewer=None):
    """Section 3.1: counts only - never a subject, a customer or a name. The ONE function behind ``/shared``,
    ``/my`` (its ``shared`` block) and Home, so every dashboard shows the same numbers for the same viewer.

    The e-mail numbers are scoped to the person: an administrator (and a manager of every team) gets all rows,
    everybody else the rows of their own teams - an employee never counts a mail of the "Unclassified" bucket. The
    block says so with ``scope`` ('all' | 'teams') and ``teams``. Calls, security requests and ITSM are sector-wide
    counts on an employee's own page, but a MANAGER of some teams - on Home, on the strip, or looking at an employee's
    page - only ever counts their own teams (the calls of their employees, the requests and ITSM roles of their teams
    by calllog/ops/teams.py)."""
    user = user if user is not None else g.user
    scope = shared_scope(user, viewer)
    hide = user.get("role") == "user"
    looker = viewer if viewer is not None else user
    counts = overlay.scope_for_user(looker) if looker.get("role") == "manager" else None    # calls, SLA, ITSM
    snaps = {m: read_snapshot(m) for m in schemas.MODULES}
    modules, updates = {}, []
    ovr, rev = current_overrides()
    for module in schemas.MODULES:
        snap, problem = snaps[module]
        meta = module_meta(module, snap, problem, mask=False)      # the strip shows the true collector freshness to everyone
        block = {"available": bool(snap), "online": meta["online"], "stale": meta["stale"], "enabled": meta["enabled"],
                 "updated_at": meta["generated_at"], "date": meta["date"]}
        if module == "email":
            block.update(scope="all" if scope is None else "teams", teams=list(scope or ()))
        if snap:
            updates.append(snap["generated_at"])
            data = snap.get("data") or {}
            if module == "email":
                block.update(email_headline(email_view(snap, scope, ovr, hide, rev)["stats"]))
            elif module == "sla":
                block.update(sla_headline(scoped_data("sla", data, counts)))
            else:
                block["totals"] = dict(scoped_data("itsm", data, counts).get("totals") or {})
        modules[module] = block
    enabled = [b for b in modules.values() if b["enabled"]]
    return {"generated_at": auth.now().isoformat(timespec="seconds"),
            "calls": {**_public_calls(calls_totals(auth.today(), counts)), "scope": "all" if counts is None else "teams"},
            "email": modules["email"], "sla": modules["sla"], "itsm": modules["itsm"],
            "collector": {"online": bool(enabled) and all(b["online"] for b in enabled),
                          "last_update": max(updates) if updates else None,
                          "modules": {m: {"online": b["online"], "stale": b["stale"], "updated_at": b["updated_at"]}
                                      for m, b in modules.items()}}}


# ----------------------------------------------------------------------------- team calls (manager / admin)
def team_employee_ids(scope):
    """Active employees (role 'user') whose team(s) are inside ``scope`` (None = every employee)."""
    ids = set()
    for uid, u in auth.store().users().items():
        if u.get("role") != "user" or not u.get("active", True):
            continue
        if scope is None or any(overlay.team_in(scope, t) for t in overlay.user_teams(u)):
            ids.add(uid)
    return ids


def build_team_calls(day, scope):
    """One day of the calls logged by the employees of ``scope`` (a manager's team, or everybody for ``None``): the totals,
    the per-hour chart, the reason breakdown, a per-employee table and the call records themselves. Counts only come from
    the employees' own day files, so a manager never sees another team's calls."""
    users = auth.store().users()
    reasons = reasons_by_key()
    ids = team_employee_ids(scope)
    per = {uid: auth.store().day_records(day, uid) for uid in ids}
    recs = [r for rows in per.values() for r in rows]
    decorated = [decorate(r, users, reasons) for r in recs]
    decorated.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    summ = call_stats.summarize(recs, reasons)
    employees = []
    for uid in ids:
        u = users.get(uid, {})
        s = call_stats.summarize(per.get(uid, []), reasons)
        employees.append({"user_id": uid, "name": u.get("name") or uid, "department": u.get("department", ""),
                          "teams": overlay.user_teams(u), "total": s["total"], **{st: s[st] for st in STATUSES}})
    employees.sort(key=lambda e: (-e["total"], str(e["name"]).casefold()))
    by_reason = [{"key": k, "label_ar": reasons.get(k, {}).get("label_ar", k),
                  "label_en": reasons.get(k, {}).get("label_en", k), "count": n}
                 for k, n in sorted(summ["by_reason"].items(), key=lambda kv: -kv[1])]
    return {"date": day, "scope": overlay.scope_label(scope), "scope_teams": None if scope is None else list(scope),
            "totals": {"total": summ["total"], **{st: summ[st] for st in STATUSES}, "other": summ["other"],
                       "active_employees": sum(1 for e in employees if e["total"]), "employees": len(employees)},
            "per_hour": call_stats.buckets(recs, reasons, "hour"), "by_reason": by_reason,
            "first_call": summ["first_call"], "last_call": summ["last_call"],
            "employees": employees, "records": decorated}


@bp.get("/team-calls")
@auth.ops_view_required
def team_calls_api():
    """The calls of the signed-in manager's team(s) for one day (admin: every team): the team's own detailed call log.
    Its totals are the numbers the manager's Home and strip show for today (``calls_totals``). No date = the server's
    today."""
    day = parse_day(request.args.get("date"), auth.today())
    if day > auth.today():
        raise ApiError("That date is in the future.")
    return jsonify(**build_team_calls(day, auth.team_scope()), server_time=now_iso(auth.now()))


@bp.get("/shared")
@auth.login_required
def shared():
    return jsonify(build_shared())


# ----------------------------------------------------------------------------- my work (section 3.2)
def _day_summary(uid, day, reasons):
    recs = auth.store().day_records(day, uid)
    s = call_stats.summarize(recs, reasons)
    return recs, {"date": day, "total": s["total"], **{k: s[k] for k in STATUSES}, "other": s["other"],
                  "first_call": s["first_call"], "last_call": s["last_call"], "_by_reason": s["by_reason"]}


def my_calls(uid, day):
    reasons = reasons_by_key()
    today = date.fromisoformat(day)
    daily, today_recs = [], []
    for back in range(6, -1, -1):
        d = (today - timedelta(days=back)).isoformat()
        recs, summary = _day_summary(uid, d, reasons)
        if back == 0:
            today_recs = recs
        daily.append(summary)
    by_reason = [{"key": k, "label_ar": reasons.get(k, {}).get("label_ar", k),
                  "label_en": reasons.get(k, {}).get("label_en", k), "count": n}
                 for k, n in sorted(daily[-1]["_by_reason"].items(), key=lambda kv: -kv[1])]
    daily = [{k: v for k, v in d.items() if not k.startswith("_")} for d in daily]
    last7 = {k: sum(d[k] for d in daily) for k in ("total", *STATUSES, "other")}
    return {"today": daily[-1], "yesterday": daily[-2], "last7": last7, "daily": daily,
            "per_hour_today": call_stats.buckets(today_recs, reasons, "hour"), "by_reason_today": by_reason}


def _my_email_row(row):
    """Only what the person needs about a mail that is theirs."""
    return {"message_id": row.get("message_id", ""), "subject": row.get("subject", ""),
            "customer_name": row.get("customer_name", ""), "team": row.get("team", ""),
            "received_time": row.get("received_time"), "status": row.get("status", ""),
            "sla_status": row.get("sla_status", ""), "escalated": bool(row.get("escalated")),
            "waiting": row.get("current_waiting_time", "") or row.get("first_response_time", ""),
            "first_reply_time": row.get("first_reply_time"), "response_minutes": row.get("response_minutes"),
            "assigned_by": row.get("assigned_by", ""), "assigned_at": row.get("assigned_at", ""),
            "note": row.get("assigned_note", ""), "quality_rating": row.get("quality_rating", ""),
            "is_carryover": bool(row.get("is_carryover")),
            "assignment_done": bool(row.get("assignment_done")), "done_at": row.get("done_at", ""),
            "done_by": row.get("done_by", ""), "done_note": row.get("done_note", ""),
            "done_auto": bool(row.get("done_auto")), "mail_copy": row.get("mail_copy", "")}


def _team_summary(teams, all_teams=False):
    return {"teams": list(teams), "all_teams": all_teams, "pending": 0, "replied": 0, "no_action": 0, "sla_breached": 0,
            "escalated": 0, "reply_rate": 0.0, "listed": 0, "more": 0}


def _one_row_per_mail(rows):
    """A mail the collector split into one row per team is still ONE assignment: keep a single row (a pending one wins)."""
    kept, at = [], {}
    for row in rows:
        key = row.get("message_id") or id(row)
        if key not in at:
            at[key] = len(kept)
            kept.append(row)
        elif row.get("status") == schemas.EMAIL_PENDING and kept[at[key]].get("status") != schemas.EMAIL_PENDING:
            kept[at[key]] = row
    return kept


def my_email(user, viewer=None):
    """The e-mail part of /my: the mails that are the person's by name (``assigned`` / ``replied``) and the e-mails of
    their own teams (``team_emails`` + ``team_summary``; never the "Unclassified" bucket).

    ``viewer`` = the manager / administrator looking at somebody else's page: the manager only sees what both of them
    may see."""
    email = overlay.normalize_email(user.get("email"))
    own_scope = overlay.scope_for_user(user)
    scope = overlay.intersect_scopes(own_scope, overlay.scope_for_user(viewer)) if viewer is not None else own_scope
    hint = ""
    if scope is not None and not scope:
        hint = "no_team" if not own_scope else "out_of_scope"
    empty = {"configured": bool(email), "available": False, "email": email,
             "assigned": {"pending": 0, "done": 0, "other": 0, "items": []},
             "replied": {"count": 0, "average_first_reply_minutes": None, "sla_met": 0, "sla_breached": 0, "items": []},
             "team_emails": [], "team_summary": _team_summary(scope or (), scope is None), "team_emails_hint": hint}
    snap, _ = read_snapshot("email")
    if not snap:
        return empty
    ovr, rev = current_overrides()
    out = {**empty, "available": True, "date": snap.get("date"), "updated_at": snap.get("generated_at")}
    if scope is None or scope:
        items, summary = team_mail(snap, scope, ovr, rev)
        out["team_emails"], out["team_summary"] = items, {**summary, "teams": list(summary["teams"])}
    if not email:
        return out
    rows = overlaid_rows(snap, ovr, rev)
    if viewer is not None:                        # a manager looking at someone else sees only rows of their own teams
        rows = overlay.scope_rows(rows, viewer)
    mine_assigned, mine_replied = _one_row_per_mail(overlay.assigned_rows(rows, email)), overlay.replied_rows(rows, email)
    open_rows = [r for r in mine_assigned if not r.get("assignment_done") and r.get("status") == schemas.EMAIL_PENDING]
    done_rows = [r for r in mine_assigned if r.get("assignment_done")]
    listed = {id(r) for r in open_rows} | {id(r) for r in done_rows}
    other_rows = [r for r in mine_assigned if id(r) not in listed]        # closed by somebody else / no action needed
    newest = lambda r: (str(r.get("assigned_at") or ""), str(r.get("received_time") or ""))    # noqa: E731
    ordered = [r for group in (open_rows, done_rows, other_rows) for r in sorted(group, key=newest, reverse=True)]
    times = [m for m in (overlay.response_minutes(r) for r in mine_replied) if m is not None]
    mine_replied.sort(key=lambda r: str(r.get("first_reply_time") or ""), reverse=True)
    own = viewer is None and email == overlay.normalize_email(user.get("email"))   # acting needs the person's own page
    marks = ovr.get("no_action") or {}

    def item(r):
        one = _my_email_row(r)
        pending = r.get("status") == schemas.EMAIL_PENDING and not r.get("assignment_done")
        one["no_action_reason"] = r.get("no_action_reason", "") if r.get("status") == schemas.EMAIL_NO_ACTION else ""
        one["can"] = {"no_action": own and pending, "reassign": own and pending,
                      "undo_no_action": own and r.get("status") == schemas.EMAIL_NO_ACTION
                      and str((marks.get(r.get("message_id")) or {}).get("marked_by_id") or "") == str(user.get("user_id"))}
        return one
    out["assigned"] = {"pending": len(open_rows), "done": len(done_rows), "other": len(other_rows),
                       "items": [item(r) for r in ordered[:_MAX_ASSIGNED]]}
    out["replied"] = {"count": len(mine_replied),
                      "average_first_reply_minutes": round(sum(times) / len(times), 1) if times else None,
                      "sla_met": sum(1 for r in mine_replied if r.get("sla_status") == "Met"),
                      "sla_breached": sum(1 for r in mine_replied if r.get("sla_status") == "Breached"),
                      "items": [_my_email_row(r) for r in mine_replied[:_MAX_LIST]]}
    return out


def _my_review(message_id, entry):
    open_appeal = bool(entry.get("appeal")) and not entry.get("appeal_decision")
    return {"message_id": message_id, "subject": entry.get("subject", ""), "rating": entry.get("rating", ""),
            "note": entry.get("note", ""), "reasons": entry.get("reasons", ""),
            "reviewed_by": entry.get("reviewed_by", ""), "reviewed_at": entry.get("reviewed_at", ""),
            "appeal": entry.get("appeal", ""), "appeal_at": entry.get("appeal_at", ""),
            "appeal_decision": entry.get("appeal_decision", ""), "decided_at": entry.get("decided_at", ""),
            "rating_withdrawn": bool(entry.get("rating_withdrawn")),
            "can_appeal": not (entry.get("appeal") or entry.get("appeal_decision") or entry.get("rating_withdrawn")),
            "appeal_open": open_appeal}


def my_quality(user, viewer=None):
    """The ratings of the person's replies. ``viewer`` = a manager looking at somebody else's page: only the ratings of
    mails of the viewer's own teams."""
    email = overlay.normalize_email(user.get("email"))
    if not email:
        return {"configured": False, "reviews": [], "counts": {}}
    mine = [(mid, e) for mid, e in ops().overrides("quality").items()
            if overlay.normalize_email(e.get("replied_by_email")) == email]
    scope = overlay.scope_for_user(viewer) if viewer is not None else None
    if scope is not None:
        snap, _ = read_snapshot("email")
        visible = {r.get("message_id") for r in overlay.rows_in_scope(((snap or {}).get("data") or {}).get("rows") or [],
                                                                      scope)}
        mine = [(mid, e) for mid, e in mine if overlay.team_in(scope, e.get("team")) or mid in visible]
    mine.sort(key=lambda kv: str(kv[1].get("reviewed_at") or ""), reverse=True)
    counted = [e for _, e in mine if not e.get("rating_withdrawn")]
    return {"configured": True,
            "counts": {"reviewed": len(counted),
                       "excellent": sum(1 for e in counted if e.get("rating") == "Excellent"),
                       "good": sum(1 for e in counted if e.get("rating") == "Good"),
                       "needs_improvement": sum(1 for e in counted if e.get("rating") == "Needs Improvement"),
                       "appeals": sum(1 for _, e in mine if e.get("appeal")),
                       "withdrawn": sum(1 for _, e in mine if e.get("rating_withdrawn"))},
            "reviews": [_my_review(mid, e) for mid, e in mine[:50]]}


def my_sla(user):
    name = str(user.get("security_name") or "").strip()
    totals = {"open": 0, "within": 0, "reminder": 0, "breached": 0, "no_date": 0, "oldest_minutes": 0}
    empty = {"configured": bool(name), "available": False, "security_name": name, "totals": totals, "requests": []}
    if not name:
        return empty
    snap, _ = read_snapshot("sla")
    if not snap:
        return empty
    wanted = norm_arabic(name)
    mine = [r for r in (snap.get("data") or {}).get("requests") or []
            if isinstance(r, dict) and norm_arabic(r.get("security_user")) == wanted]
    for r in mine:
        totals["open"] += 1
        if r.get("status") in schemas.SLA_STATUSES:
            totals[r["status"]] += 1
    totals["oldest_minutes"] = sla_oldest_now(mine)                # by the clock, like the Live tab and Home
    return {**empty, "available": True, "updated_at": snap.get("generated_at"),
            "thresholds": (snap.get("data") or {}).get("thresholds") or {}, "totals": totals,
            "requests": [{"key": r.get("key", ""), "sent_at": r.get("sent_at"), "assigned_at": r.get("assigned_at"),
                          "age_from": r.get("age_from") or "sent", "age_minutes": r.get("age_minutes"),
                          "status": r.get("status"), "fields": r.get("fields") or {}}
                         for r in mine[:_MAX_LIST]]}


MY_SLA_DAYS = 7


def my_sla_history(user, day):
    """The person's own SLA from the portal's status history, linked by THEIR employee number («رقم المستخدم» = the
    account's portal ID, else its user ID; else their security name): today's executions and confirmations with the
    exact clocks, the last ``MY_SLA_DAYS`` days, and the requests they hold right now. Third Party is left out."""
    numbers = {str(user.get(k) or "").strip() for k in ("portal_id", "user_id")} - {""}
    name = sla_history._norm(user.get("security_name"))
    store = ops()
    state = store.read_sla_history_state() or {}
    if not state.get("last_history") and not store.sla_closed_days():
        return {"available": False}
    # every employee's page polls every 30 s: one answer per person and minute (and per recorded scan)
    memo = current_app.extensions.setdefault("ops_my_sla_history", {})
    key = (user.get("user_id"), tuple(sorted(numbers)), name, day, state.get("last_history") or "",
           int(time.monotonic() // 60))
    if key in memo:
        return memo[key]
    if len(memo) > 400:
        memo.clear()
    memo[key] = value = _my_sla_history(user, day, numbers, name, store, state)
    return value


def _my_sla_history(user, day, numbers, name, store, state):
    cfg = sla_records.clean_config((store.read_sla_records() or {}).get("config"))
    excluded = teams.sla_exclusion(hub_cfg().get("sla") or {}, team_resolver()) or (lambda system: False)
    start = (date.fromisoformat(day) - timedelta(days=MY_SLA_DAYS - 1)).isoformat()
    records = dict(state.get("open") or {})
    for d in store.sla_closed_days():
        if start <= d <= day:
            records.update(store.read_sla_closed_day(d))
    records = {k: r for k, r in records.items() if not excluded(r.get("system"))}
    is_me = lambda no, who: (no and str(no).strip() in numbers) or (name and sla_history._norm(who) == name)  # noqa: E731
    executed = {k: r for k, r in records.items() if is_me(r.get("executor_no"), r.get("executor"))}
    confirmed = {k: r for k, r in records.items() if is_me(r.get("checker_no"), r.get("checker"))}
    names = {sla_history._norm(r.get("executor")) for r in executed.values() if r.get("executor")} | ({name} - {""})
    today = {k: r for k, r in executed.items() if str(r.get("executed_at") or "")[:10] == day}
    a = sla_history.analyze(today, cfg)
    per_day = []
    for i in range(MY_SLA_DAYS):
        d = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        one = sla_history.analyze({k: r for k, r in executed.items() if str(r.get("executed_at") or "")[:10] == d}, cfg)
        per_day.append({"date": d, "executed": one["employee_clock"]["executed"],
                        "not_executed": one["employee_clock"]["not_executed"],
                        "median_minutes": one["employee_clock"]["median_minutes"]})
    now = auth.now().isoformat(timespec="seconds")
    held = sla_history.right_now(state.get("grid") or {}, state.get("open") or {}, state.get("index") or {}, cfg, now,
                                 lambda g: sla_history._norm(g.get("holder")) in names and not excluded(g.get("system")))
    return {"available": True, "numbers": sorted(numbers), "sla_minutes": cfg["sla_minutes"],
            "today": {"executed": a["employee_clock"]["executed"], "not_executed": a["employee_clock"]["not_executed"],
                      "median_minutes": a["employee_clock"]["median_minutes"],
                      "average_minutes": a["employee_clock"]["average_minutes"],
                      "confirmed_by_me": sum(1 for r in confirmed.values() if str(r.get("confirmed_at") or "")[:10] == day)},
            "days": per_day, "now": held["now"], "open_now": held["open_now"][:_MAX_LIST]}


def build_my(user, viewer=None):
    day = auth.today()
    return {"user": public_user(user), "read_only": viewer is not None, "viewer": viewer["user_id"] if viewer else None,
            "date": day, "server_time": auth.now().isoformat(timespec="seconds"),
            "all_teams": [t for t in hub_cfg()["email"]["teams"] if t.casefold() != "unclassified"],
            "calls": my_calls(user["user_id"], day), "email": my_email(user, viewer), "quality": my_quality(user, viewer),
            "sla": my_sla(user), "sla_history": my_sla_history(user, day), "shared": build_shared(user, viewer),
            "no_action_reasons": list(NO_ACTION_REASONS)}


@bp.get("/my")
@auth.login_required
def my():
    target, viewer = g.user, None
    wanted = norm_uid(request.args.get("as"))
    if wanted and wanted != g.user["user_id"]:
        if g.user["role"] not in auth.OPS_ROLES:
            raise ApiError("Only administrators and managers can open another person's page.", 403)
        target = auth.store().get_user(wanted)
        if not target or not overlay.may_view_person(g.user, target):     # outside the manager's teams = not found
            raise ApiError("User not found.", 404)
        viewer = g.user
        auth.audit("ops_view_as", target=wanted)
    return jsonify(build_my(target, viewer))


# ----------------------------------------------------------------------------- overview (Home)
def _attention(level, module, text, link=None):
    return {"level": level, "module": module, "text": text, "link": link or MODULE_LINKS.get(module, "/hub")}


def build_attention(email_stats, email_rows, sla_data, itsm_data, metas):
    items = []
    if sla_data is not None:
        totals, limits = sla_data.get("totals") or {}, sla_data.get("thresholds") or {}
        # a state ("open past"), not the timeline's event ("passed"); each item opens exactly its requests
        if totals.get("breached"):
            items.append(_attention("danger", "sla", f"{_plural(totals['breached'], 'ticket')} open past the "
                                                      f"{_fmt_threshold(limits.get('breach_minutes', 120))} SLA",
                                    "/sla?status=breached"))
        if totals.get("reminder"):
            items.append(_attention("warn", "sla", f"{_plural(totals['reminder'], 'ticket')} open past the "
                                                     f"reminder time ({_fmt_threshold(limits.get('warning_minutes', 60))})",
                                    "/sla?status=reminder"))
        if totals.get("no_date"):
            items.append(_attention("info", "sla", f"{_plural(totals['no_date'], 'ticket')} without a sent date",
                                    "/sla?status=no_date"))
    if email_stats is not None:
        if email_stats.get("escalated_count"):
            items.append(_attention("danger", "email", f"{_plural(email_stats['escalated_count'], 'e-mail')} waiting past "
                                                         f"the SLA with no team reply", "/email?escalated=1"))
        late = (email_stats.get("sla_breached") or 0) - (email_stats.get("escalated_count") or 0)
        if late > 0:
            items.append(_attention("warn", "email", f"{_plural(late, 'e-mail')} answered after the SLA"))
        if email_stats.get("carryover_count"):
            items.append(_attention("warn", "email", f"{_plural(email_stats['carryover_count'], 'e-mail')} carried over "
                                                       f"from earlier days (oldest {email_stats.get('carryover_oldest_days', 0)} days)",
                                    "/email?carryover=1"))
        unclassified = sum(1 for r in email_rows if str(r.get("team") or "").casefold() == "unclassified")
        if unclassified:
            items.append(_attention("info", "email", f"{_plural(unclassified, 'e-mail')} not classified to a team",
                                    "/email#unclassified"))
        appeals = sum(1 for r in email_rows if r.get("quality_appeal") and not r.get("appeal_decision"))
        if appeals:
            items.append(_attention("info", "email", f"{_plural(appeals, 'quality appeal')} waiting for a decision",
                                    "/email#quality"))
    if itsm_data is not None:
        for category, n in (itsm_data.get("totals") or {}).items():
            if n and "rollback" in str(category).lower():
                items.append(_attention("warn", "itsm", f"{_plural(n, 'rollback request')} waiting"))
        failed = [r for r in itsm_data.get("roles") or [] if r.get("error")]
        if viewer_is_admin():                                   # the collector's error text: administrators only
            for role in failed:
                items.append(_attention("danger", "itsm", f"ITSM role '{str(role.get('role'))[:60]}' could not be read: "
                                                            f"{str(role['error'])[:120]}"))
        elif failed:                                            # a manager's totals would look complete otherwise
            names = ", ".join(str(r.get("role"))[:60] for r in failed[:3])
            items.append(_attention("warn", "itsm", f"{_plural(len(failed), 'ITSM role')} of your team could not be read "
                                                    f"in the last scan ({names}): its requests are missing from the totals"))
    for module, meta in metas.items():
        label = MODULE_LABELS[module]
        if not meta["enabled"]:
            continue
        has_hb = meta["jobs"][module].get("present")
        if not has_hb and not meta["generated_at"]:
            items.append(_attention("info", module, f"{label}: no data yet - the collector has not run", "/system"))
            continue
        if has_hb and not meta["online"] and meta["active_now"]:
            items.append(_attention("danger", module, f"{label} collector is offline (last seen "
                                                        f"{meta['jobs'][module].get('age_minutes')} min ago)", "/system"))
        elif meta["stale"]:
            items.append(_attention("warn", module, f"{label} data is {meta['age_minutes']} minutes old", "/system"))
        for job, state in meta["jobs"].items():
            if state.get("last_status") == "error":
                what = "scan" if job == module else "daily report"
                items.append(_attention("danger", module, f"The last {label} {what} failed: "
                                                            f"{state.get('last_error') or 'unknown error'}", "/system"))
    order = {"danger": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["level"]])


def _assigned_today_by_team(rows, day):
    """Per team: how many e-mails were assigned to an employee today (auto-assign and manual), and how many of those
    the Auto-assign engine did - the dashboard's "Assigned today" number. ``day`` is the server's date (YYYY-MM-DD)."""
    total, auto = {}, {}
    for r in rows or ():
        if not (r.get("assigned_to_email") or r.get("assigned_to_name")):
            continue
        if str(r.get("assigned_at") or "")[:10] != day:
            continue
        team = r.get("team") or ""
        total[team] = total.get(team, 0) + 1
        if str(r.get("assigned_by") or "") == "Auto-assign":
            auto[team] = auto.get(team, 0) + 1
    return total, auto


def _team_table(stats, rows=None, day=None):
    out = [{"team": team, **{k: t.get(k, 0) for k in ("business_emails", "replied", "pending", "no_action_needed",
                                                      "escalated_count", "carryover_count", "sla_breached_count")}}
           for team, t in (stats.get("team_stats") or {}).items() if t.get("business_emails")]
    total_assigned, auto_assigned = _assigned_today_by_team(rows, day) if day else ({}, {})
    for row in out:
        row["assigned_today"] = total_assigned.get(row["team"], 0)
        row["auto_assigned_today"] = auto_assigned.get(row["team"], 0)
    out.sort(key=lambda r: (-r["pending"], -r["business_emails"], r["team"]))
    return out


@bp.get("/overview")
@auth.ops_view_required
def overview():
    day = auth.today()
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    snaps = {m: read_snapshot(m) for m in schemas.MODULES}
    metas = {m: module_meta(m, *snaps[m]) for m in schemas.MODULES}
    scope = auth.team_scope()
    today_calls = calls_totals(day, scope)
    scope_text = overlay.scope_label(scope)
    ovr, rev = current_overrides()

    email_stats = email_rows = None
    email_block = {"available": False, "scope": scope_text}
    if snaps["email"][0]:
        view = email_view(snaps["email"][0], scope, ovr, rev=rev)
        email_stats, email_rows = view["stats"], view["rows"]
        email_block = {"available": True, "scope": scope_text, **email_headline(email_stats),
                       "quality": email_stats.get("quality", {}), "teams": _team_table(email_stats, email_rows, day),
                       "unclassified": sum(1 for r in email_rows if str(r.get("team") or "").casefold() == "unclassified"),
                       "updated_at": snaps["email"][0]["generated_at"]}
    sla_data = scoped_data("sla", (snaps["sla"][0] or {}).get("data"), scope)
    sla_block = {"available": False}
    if sla_data is not None:
        sla_block = {"available": True, **sla_headline(sla_data), "thresholds": sla_data.get("thresholds") or {},
                     "top_users": (sla_data.get("by_user") or [])[:5], "updated_at": snaps["sla"][0]["generated_at"]}
    itsm_data = scoped_data("itsm", (snaps["itsm"][0] or {}).get("data"), scope)
    itsm_block = {"available": False}
    if itsm_data is not None:
        changes = itsm_data.get("changes") or {}
        itsm_block = {"available": True, "categories": itsm_data.get("categories") or [],
                      "totals": itsm_data.get("totals") or {},
                      "role_errors": [{"role": r.get("role"), "error": str(r.get("error"))[:160] if viewer_is_admin() else ""}
                                      for r in itsm_data.get("roles") or [] if r.get("error")],   # the reason: admins only
                      "changes": {"new": len(changes.get("new") or []), "closed": len(changes.get("closed") or [])},
                      "updated_at": snaps["itsm"][0]["generated_at"]}
    yesterday_calls = calls_totals(yesterday, scope)
    attention = build_attention(email_stats, email_rows or [], sla_data, itsm_data, metas)
    if scope is not None and snaps["email"][0]:            # a manager also files the Unclassified mails: tell them
        waiting = sum(1 for r in overlaid_rows(snaps["email"][0], ovr, rev)
                      if overlay.is_unclassified(r) and r.get("status") == schemas.EMAIL_PENDING)
        email_block["unclassified"] = waiting
        if waiting:
            attention.append(_attention("info", "email", f"{_plural(waiting, 'e-mail')} not classified to a team",
                                        "/email#unclassified"))
    return jsonify(
        server_time=auth.now().isoformat(timespec="seconds"), date=day, scope=scope_text,
        scope_teams=None if scope is None else list(scope), shared=build_shared(),
        calls={"today": _public_calls(today_calls), "yesterday": _public_calls(yesterday_calls),
               "per_hour_today": today_calls["_hours"]},
        email=email_block, sla=sla_block, itsm=itsm_block, attention=attention,
        collectors=(job_states() if viewer_is_admin() else {}))


# ----------------------------------------------------------------------------- module answers
def people_directory(scope_team=None):
    """[{email, name, team}] of the people an e-mail can be assigned to: every active account with a mailbox (named and
    teamed from the account, linked to them by their employee account), then the Settings team-member addresses that
    have no account. ``scope_team`` = None (everybody) or the team names to keep."""
    scope = overlay.as_scope(scope_team)
    email_cfg = hub_cfg()["email"]
    seen, out = set(), []
    users = auth.store().users()
    for u in (users.values() if isinstance(users, dict) else users or []):
        addr = overlay.normalize_email(u.get("email"))
        if not addr or addr in seen or not u.get("active", True) or u.get("role") not in ("user", "manager"):
            continue
        mine = [t for t in overlay.user_teams(u) if t != schemas.ALL_TEAMS_MARK and not overlay.is_unclassified({"team": t})]
        if scope is not None and not any(overlay.team_in(scope, t) for t in mine):
            continue
        seen.add(addr)
        out.append({"email": addr, "name": u.get("name") or overlay.display_name_from_email(addr),
                    "team": mine[0] if mine else "", "user_id": u.get("user_id", "")})
    pairs = [(team, addr) for team, addrs in (email_cfg.get("team_members_by_team") or {}).items() for addr in addrs]
    pairs += [("", addr) for addr in email_cfg.get("team_members") or []]
    for team, addr in pairs:
        addr = overlay.normalize_email(addr)
        if not addr or addr in seen:
            continue
        if scope is not None and not overlay.team_in(scope, team):
            continue
        seen.add(addr)
        out.append({"email": addr, "name": overlay.display_name_from_email(addr), "team": team})
    out.sort(key=lambda p: (p["name"].casefold(), p["email"]))
    return out


def person_teams():
    """{mailbox address: [team, ...]} of every active account and every Settings team-member address - whose team a
    reply belongs to (overlay.reply_in_scope). Built once per request."""
    cached = g.get("_person_teams")
    if cached is not None:
        return cached
    out = {}
    users = auth.store().users()
    for u in (users.values() if isinstance(users, dict) else users or []):
        addr = overlay.normalize_email(u.get("email"))
        if addr and u.get("active", True):
            for t in overlay.user_teams(u):
                if t != schemas.ALL_TEAMS_MARK and not overlay.is_unclassified({"team": t}) and t not in out.get(addr, []):
                    out.setdefault(addr, []).append(t)
    for team, addrs in (hub_cfg()["email"].get("team_members_by_team") or {}).items():
        for addr in addrs or []:
            addr = overlay.normalize_email(addr)
            if addr and team and team not in out.get(addr, []):
                out.setdefault(addr, []).append(team)
    g._person_teams = out
    return out


def email_options(scope_team=None):
    """Lists for the e-mail page. ``scope`` = the teams text of a manager ('Flex Cube' or 'Flex Cube, Card Management'),
    None for every team; ``scope_teams`` = the same as a list (None for every team)."""
    scope = overlay.as_scope(scope_team)
    email_cfg = hub_cfg()["email"]
    cls = email_cfg["classification"]
    rules = {k: v for k, v in cls["system_team_map"].items() if overlay.team_in(scope, v)}
    # a manager may hand a mail to a member of ANY team (their own team listed first by the page's grouping)
    return {"teams": list(email_cfg["teams"]), "quality_reasons": list(QUALITY_REASONS), "ratings": list(RATINGS),
            "people": people_directory(None), "unclassified_team": cls["unclassified_team"],
            "system_team_map": rules, "scope": overlay.scope_label(scope),
            "scope_teams": None if scope is None else list(scope)}


@bp.get("/<any(email,sla,itsm):module>")
@auth.ops_view_required
def module_answer(module):
    snap, problem = read_snapshot(module)
    meta = module_meta(module, snap, problem)
    is_email = module == "email"
    scope = auth.team_scope()
    options = email_options(scope) if is_email else None
    if snap is None:
        return jsonify(snapshot=None, meta=meta, **({"options": options} if is_email else {}))
    ovr, rev = current_overrides() if is_email else ({}, "")
    meta["rev"] = rev
    if request.args.get("since") == snap["generated_at"]:
        sent_rev = request.args.get("rev")
        same = (sent_rev == rev) if sent_rev is not None else (
            not is_email or _latest_override_stamp(ovr) < snap["generated_at"].replace(" ", "T"))
        if same:
            return jsonify(unchanged=True, meta=meta)
    out = dict(snap)
    if is_email:
        out["data"] = email_view(snap, scope, ovr, rev=rev)
        out["options"] = options
        if scope is not None:              # a manager also handles the Unclassified mails (their KPIs stay their own)
            out["unclassified"] = overlay.hide_other_team_replies(
                [r for r in overlaid_rows(snap, ovr, rev) if overlay.is_unclassified(r)], scope, person_teams())
    else:
        out["data"] = scoped_data(module, snap.get("data"), scope)
    out["meta"] = meta
    return jsonify(out)


def _thin(points, limit=300):
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    picked = [points[int(i * step)] for i in range(limit - 1)]
    return picked + [points[-1]]


@bp.get("/<any(email,sla,itsm):module>/history")
@auth.ops_view_required
def module_history(module):
    try:
        days = max(1, min(int(request.args.get("days", 14)), 60))
    except ValueError:
        raise ApiError("days must be a number.")
    scope = auth.team_scope()
    keys = None
    if scope is not None and module == "sla":              # the breach marks of this manager's own requests only
        snap, _ = read_snapshot("sla")
        keys = {r.get("key") for r in (scoped_data("sla", (snap or {}).get("data"), scope) or {}).get("requests") or []}
    out = []
    for day in ops().history_days(module)[-days:]:
        doc = {**(ops().read_history(module, day) or {}), "day": day}
        if scope is not None:
            mine = teams.scope_history_day(module, doc, scope, team_resolver(), sla_system_header())
            if keys is not None:
                mine["first_breach"] = {k: v for k, v in (doc.get("first_breach") or {}).items() if k in keys}
            doc = mine
        out.append({**doc, "points": _thin(list(doc.get("points") or []))})
    return jsonify(module=module, days=out)


@bp.get("/sla-analysis")
@auth.ops_view_required
def sla_analysis_get():
    """The LIVE SLA analysis of security requests: each request's lifecycle is recorded across scans/feeds and split
    into the employee clock (assignment -> execution, by مستخدم السرية) and the manager clock (execution ->
    confirmation, by مراجع السرية). Read-only, limited to the viewer's teams like every page, shown as a tab on the
    Ticketing page (admins + managers)."""
    now = auth.now().isoformat(timespec="seconds")
    try:
        apply_sla_scan()                                    # the Live tickets recording stays current too
    except Exception:                                       # recording must never break the page
        current_app.logger.exception("SLA scan recording failed")
    scope = auth.team_scope()                               # a manager sees only their team(s); admin / all-teams -> everything
    filt = sla_filter_args(request.args)
    # 70 people at once: one answer per view, rebuilt when the collector records a scan or after SLA_VIEW_TTL seconds
    state = ops().read_sla_history_state() or {}
    memo_key = (overlay.scope_label(scope), tuple(sorted(filt.items())), state.get("last_history") or "",
                json.dumps(hub_cfg().get("sla") or {}, sort_keys=True, default=str),
                json.dumps((ops().read_sla_records() or {}).get("config") or {}, sort_keys=True, default=str))
    memo = current_app.extensions.setdefault("ops_sla_view", {})
    hit = memo.get(memo_key)
    if hit and time.monotonic() - hit[0] < SLA_VIEW_TTL:
        return jsonify(hit[1])
    view = sla_history_view(scope, filt["date_from"], filt["date_to"])
    if not view["records"] and not view["grid"]:            # nothing recorded yet: say why
        return jsonify(available=False, server_time=now, scope=overlay.scope_label(scope), recorder=view["recorder"],
                       scan_interval=health.module_interval(hub_cfg(), "sla"))
    records, cfg = view["records"], view["cfg"]
    options = sla_history.filter_options(records)           # the employees/checkers/teams/date-range the viewer may pick
    period = sla_history.analyze(sla_history.filter_records(records, filt), cfg, now=now,
                                 period=(filt["date_from"], filt["date_to"]))
    # "right now" is never date-bounded: what the latest inquiry grid lists as open / awaiting, read or not
    person = sla_history._norm(filt["executor"])
    grid_text = {k: sla_history._norm(filt[k]) for k in ("team", "system", "request_type") if filt.get(k)}

    def keep(g):
        return ((not person or sla_history._norm(g.get("holder")) == person)
                and all(sla_history._norm(g.get(k)) == v for k, v in grid_text.items()))
    period.update(sla_history.right_now(view["grid"], view["open"], view["index"], cfg, now, keep))
    # the portal's own «الاستفسار عن الطلبات» rows of the period's request dates, next to what the history holds
    period["portal"] = {**sla_history.portal_comparison(view["grid"], view["index"], filt["date_from"], filt["date_to"],
                                                        cfg),
                        "last_read": view["recorder"]["last_read"]}
    answer = dict(available=True, server_time=now, scope=overlay.scope_label(scope), unmapped=view["unmapped"],
                  scan_interval=health.module_interval(hub_cfg(), "sla"), recorder=view["recorder"],
                  left_out=view["left_out"], options=options, filter={k: (v or "") for k, v in filt.items()}, **period)
    if len(memo) > 64:
        memo.clear()
    memo[memo_key] = (time.monotonic(), answer)
    return jsonify(answer)


SLA_VIEW_TTL = 30.0               # seconds one SLA Analysis answer is reused (the "waiting" ages move by this at most)
SLA_FILTER_KEYS = ("executor", "checker", "team", "system", "branch", "request_type", "outcome", "returned")


def sla_filter_args(args):
    """The SLA Analysis filter from the query string: the period (from / to) and the text filters (sla_history.
    filter_records)."""
    filt = {"date_from": parse_day(args.get("from")) if args.get("from") else "",
            "date_to": parse_day(args.get("to")) if args.get("to") else ""}
    for key in SLA_FILTER_KEYS:
        filt[key] = str(args.get(key) or "").strip()[:150]
    if filt["outcome"] and filt["outcome"] not in sla_history.OUTCOMES:
        raise ApiError("Unknown outcome.")
    return filt


def sla_period(scope, filt):
    """(view, the records of the filtered period) for one viewer - the SLA Analysis and its export read the same."""
    view = sla_history_view(scope, filt["date_from"], filt["date_to"])
    return view, sla_history.filter_records(view["records"], filt)


@bp.get("/sla-analysis/request")
@auth.ops_view_required
def sla_analysis_request():
    """One request of the SLA Analysis: its stamps, the time of each stage against its target and its whole timeline
    («الحالة التاريخية للطلب»: who did what, when). Limited like the analysis (the viewer's teams, Third Party left out);
    anything else is the same 404."""
    rid = str(request.args.get("id") or "").strip()[:60]
    if not rid:
        raise ApiError("Which request?")
    state = ops().read_sla_history_state() or {}
    day = parse_day(request.args.get("day")) if request.args.get("day") else ""
    if not day:
        day = next((v.get("day") for v in (state.get("index") or {}).values() if v.get("id") == rid and v.get("day")), "")
    view = sla_history_view(auth.team_scope(), day or "9999-12-31", day or "9999-12-31")
    rec = view["records"].get(rid)
    if rec is None:
        raise ApiError("That request is not available.", 404)
    cfg = view["cfg"]
    targets = sla_history.stage_targets(cfg)
    mins = sla_history.stage_minutes(rec)
    stages = [{"key": k, "label": label, "minutes": round(mins[k], 1) if mins[k] is not None else None,
               "target": targets.get(k), "rag": sla_history.rag(mins[k], targets.get(k))}
              for k, label, *_ in sla_history.STAGES]
    keep = ("id", "request_date", "branch", "requester", "requester_no", "system", "team", "request_type", "grid_status",
            "registered_at", "branch_confirmed_at", "region_approved_at", "region_by", "pool_at", "first_assigned_at",
            "assigned_at", "assigner", "executed_at", "executor", "exec_outcome", "confirmed_at", "checker",
            "final_outcome", "rejected_at", "rejected_by", "returned", "reassigned", "transferred", "held_minutes",
            "on_hold", "holder", "last_status", "last_seen")
    trail = [{"at": t[0], "step": t[1], "status": t[2], "who": t[3], "number": t[4], "note": t[5]}
             for t in rec.get("trail") or [] if isinstance(t, (list, tuple)) and len(t) >= 6]
    return jsonify(request={k: rec.get(k) for k in keep}, stage=sla_history.stage_of(rec),
                   outcome=sla_history.outcome_of(rec), stages=stages, trail=trail, sla_minutes=targets["sla"])


# ----------------------------------------------------------------------------- Analysis: every module, over time
ANALYTICS_TTL = 60.0              # one answer per view and minute: 70 people at once read the same numbers
ANALYTICS_DEFAULT_DAYS = 30
ANALYTICS_LABELS = {"calls": ("Calls", "Calls logged", None),
                    "email": ("E-mails", "E-mails received", None),
                    "tickets": ("Tickets", "Assigned to the security team", "Confirmed (closed)"),
                    "crs": ("Change requests", "New on the ITSM board", "Completed")}


def apply_email_intake():
    """Fold the latest e-mail scan into the long-term intake counts (the collector does it after each scan; this is
    the safety net for a scan written by an older collector). Never raises."""
    try:
        return analytics.fold_email_snapshot(ops(), auth.now())
    except Exception:                                       # noqa: BLE001 - the analysis must never break a page
        current_app.logger.exception("E-mail intake fold failed")
        return 0


def _analytics_users():
    users = auth.store().users()
    return users if isinstance(users, dict) else {u.get("user_id"): u for u in users or []}


def _analytics_module_days(module, d_from, d_to):
    """{day: DayDoc} of one module over [d_from, d_to], every team (the scope is applied by ``series``)."""
    if module == "email":
        apply_email_intake()
        days = ops().read_analytics(analytics.EMAIL_STORE).get("days") or {}
        return {d: v for d, v in days.items() if d_from <= d <= d_to}
    if module == "calls":
        users = _analytics_users()

        def teams_of_user(uid):
            u = users.get(str(uid or "")) or {}
            return [t for t in overlay.user_teams(u) if teams._real_team(t)] or [analytics.NO_TEAM]
        return analytics.calls_days(ops(), auth.store(), d_from, d_to, teams_of_user, auth.now())
    if module == "tickets":
        view = sla_history_view(None, d_from, "")           # every request closed from d_from on, and the open ones
        recs = view["records"]
        team_of = lambda r: [r.get("team") or analytics.NO_TEAM]          # noqa: E731
        arrived = analytics.stamp_days(recs, "first_assigned_at", team_of, fallback="assigned_at")
        closed = analytics.stamp_days(recs, "confirmed_at", team_of)
        cut = lambda days: {d: v for d, v in days.items() if d_from <= d <= d_to}   # noqa: E731
        return cut(arrived), cut(closed)
    if module == "crs":
        recs = (ops().read_itsm_records() or {}).get("records") or {}
        resolver = team_resolver()
        team_of = lambda r: [resolver.itsm_team(r.get("role")) or analytics.NO_TEAM]   # noqa: E731
        new = analytics.stamp_days(recs, "first_seen", team_of)
        done = analytics.stamp_days(recs, "completed_at", team_of, keep=lambda r: r.get("conclusion") == "completed")
        cut = lambda days: {d: v for d, v in days.items() if d_from <= d <= d_to}   # noqa: E731
        return cut(new), cut(done)
    raise ApiError("Unknown module.", 404)


@bp.get("/analytics")
@auth.ops_view_required
def analytics_get():
    """The Analysis page: calls, e-mails, tickets and change requests over a period - the daily trend, the hour of the
    day, the weekday x hour heat map, the peaks, by team, and the change against the previous period of the same
    length. A manager sees only their own team(s); ``team`` narrows to one of them."""
    today = auth.today()
    d_to = parse_day(request.args.get("to"), today)
    d_from = parse_day(request.args.get("from"),
                       (date.fromisoformat(d_to) - timedelta(days=ANALYTICS_DEFAULT_DAYS - 1)).isoformat())
    d_to = min(d_to, today)                                 # a browser a day ahead asks for "tomorrow": up to today
    if d_from > d_to:
        raise ApiError("'From' date is after 'To' date.")
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days >= analytics.MAX_RANGE_DAYS:
        raise ApiError("Choose at most two years.")
    scope = auth.team_scope()
    team = str(request.args.get("team") or "").strip()
    if team and scope is not None and not overlay.team_in(scope, team):
        raise ApiError("You can only look at your own team(s).", 403)
    if team:
        keep = (lambda t, team=team: overlay._key(t) == overlay._key(team))
    elif scope is not None:
        keep = (lambda t: teams.in_scope(scope, t))
    else:
        keep = None
    stamps = (ops().read_analytics(analytics.EMAIL_STORE).get("last_folded") or "",
              (ops().read_sla_history_state() or {}).get("last_history") or "",
              (ops().read_itsm_records() or {}).get("last_generated") or "")
    memo_key = (overlay.scope_label(scope), d_from, d_to, team, stamps, int(time.monotonic() // ANALYTICS_TTL))
    memo = current_app.extensions.setdefault("ops_analytics_view", {})
    if memo_key in memo:
        answer = memo[memo_key]
        return jsonify({**answer, "quality": _analytics_quality(d_from, d_to, keep, answer["modules"].get("email"))})
    p_from, p_to = analytics.previous_range(d_from, d_to)
    modules = {}
    for module, (label, in_label, done_label) in ANALYTICS_LABELS.items():
        try:
            now_days = _analytics_module_days(module, d_from, d_to)
            prev_days = _analytics_module_days(module, p_from, p_to)
        except ApiError:
            raise
        except Exception:                                   # noqa: BLE001 - one module never hides the others
            current_app.logger.exception("Analysis of %s failed", module)
            modules[module] = {"label": label, "available": False}
            continue
        arrived, done = (now_days if isinstance(now_days, tuple) else (now_days, None))
        p_arrived, p_done = (prev_days if isinstance(prev_days, tuple) else (prev_days, None))
        current = analytics.series(arrived, d_from, d_to, keep)
        before = analytics.series(p_arrived, p_from, p_to, keep)
        entry = {"label": label, "in_label": in_label, "available": True, **current,
                 "previous": {"from": p_from, "to": p_to, "total": before["total"], "avg_per_day": before["avg_per_day"]},
                 "change_pct": analytics.change(current["total"], before["total"]),
                 "avg_change_pct": analytics.change(current["avg_per_day"], before["avg_per_day"])}
        if done is not None:
            closed = analytics.series(done, d_from, d_to, keep)
            entry["done"] = {"label": done_label, "total": closed["total"], "daily": closed["daily"],
                             "hours": closed["hours"], "peak_hour": closed["peak_hour"],
                             "previous_total": analytics.series(p_done, p_from, p_to, keep)["total"]}
            # hour by hour, each team: what arrived and what was finished (the page's "arrived vs finished" table)
            names = [t["team"] for t in current["by_team"][:15]]
            names += [t["team"] for t in closed["by_team"] if t["team"] not in names][:max(0, 15 - len(names))]
            entry["hours_by_team"] = [
                {"team": name,
                 "arrived": analytics.series(arrived, d_from, d_to, lambda t, n=name: t == n)["hours"],
                 "done": analytics.series(done, d_from, d_to, lambda t, n=name: t == n)["hours"]} for name in names]
        modules[module] = entry
    real = [t for t in hub_cfg()["email"]["teams"] if teams._real_team(t)]
    answer = {"from": d_from, "to": d_to, "today": today, "team": team, "scope": overlay.scope_label(scope),
              "server_time": auth.now().isoformat(timespec="seconds"),
              "previous": {"from": p_from, "to": p_to},
              "options": {"teams": [t for t in real if scope is None or overlay.team_in(scope, t)]},
              "modules": modules}
    if len(memo) > 64:
        memo.clear()
    memo[memo_key] = answer
    return jsonify({**answer, "quality": _analytics_quality(d_from, d_to, keep, modules.get("email"))})


def _analytics_quality(d_from, d_to, keep, email_entry):
    """The quality-review statistics of the period (analytics.quality_stats): every reviewer (manager / administrator)
    with how many replies they rated, and per team the e-mails received against the ones rated. Read fresh on every
    call (one pass over the reviews), so a rating shows at once."""
    try:
        email_entry = email_entry if (email_entry or {}).get("available") else {}
        # per team: the real teams only (days of the older history know only the day's total - "(no team)")
        by_team = {x["team"]: x["n"] for x in email_entry.get("by_team") or []
                   if x["team"] != analytics.NO_TEAM and teams._real_team(x["team"])}
        roles = {str(uid): u.get("role", "") for uid, u in _analytics_users().items()}
        received = int(email_entry.get("total") or 0) if email_entry else None     # every mail of the scope, once
        return {"available": True, **analytics.quality_stats(ops().overrides("quality"), d_from, d_to, keep, by_team,
                                                              roles, received_total=received)}
    except Exception:                                       # noqa: BLE001 - one block never hides the page
        current_app.logger.exception("Quality statistics failed")
        return {"available": False}


def sla_history_people():
    """The accounts by the number the portal shows in its status history («رقم المستخدم»: the account's portal ID,
    else its user ID) and by its security name - so every step of a request is linked to the person who did it."""
    if "sla_history_people" not in g:
        users = auth.store().users()
        users = list(users.values()) if isinstance(users, dict) else list(users or [])
        by_no, by_name = {}, {}
        for field in ("portal_id", "user_id"):              # the portal ID wins over the user ID
            for u in users:
                ident = str(u.get(field) or "").strip()
                if ident:
                    by_no.setdefault(ident, u)
        for u in users:
            name = sla_history._norm(u.get("security_name"))
            if name:
                by_name.setdefault(name, u)
        g.sla_history_people = (by_no, by_name)
    return g.sla_history_people


def sla_history_account(number="", name=""):
    """The account of one person of the status history: by their number first, else by their security name."""
    by_no, by_name = sla_history_people()
    return by_no.get(str(number or "").strip()) or by_name.get(sla_history._norm(name))


SLA_CLOSED_REACH_DAYS = 14       # closing-day files read past the period's end: requests assigned in it, closed later


def sla_history_view(scope, date_from="", date_to=""):
    """The SLA history a viewer may see: {records (closed in the period's days + every open one), open, index, grid,
    cfg, recorder, left_out, unmapped}. Every record gets its team from the PERSON who executed it (or holds it) - their
    account, found by the employee number of the status history - else from the application; Third Party (Settings ›
    Ticketing › Left out of the SLA) is removed; a manager keeps only their own team(s)."""
    store = ops()
    state = store.read_sla_history_state() or {}
    cfg = sla_records.clean_config((store.read_sla_records() or {}).get("config"))
    sla_cfg = hub_cfg().get("sla") or {}
    resolver = team_resolver()
    excluded = teams.sla_exclusion(sla_cfg, resolver) or (lambda system: False)

    def team_of(system, number="", name=""):
        acct = sla_history_account(number, name) if (number or name) else None
        mine = [t for t in overlay.user_teams(acct or {}) if teams._real_team(t)]
        return (mine[0] if mine else "") or resolver.sla_team(system, name) or teams.NO_TEAM

    def link(r):
        person_no = r.get("executor_no") if r.get("executor_no") or r.get("executor") else ""
        team = team_of(r.get("system"), person_no, r.get("executor") or r.get("holder"))
        acct = sla_history_account(r.get("executor_no"), r.get("executor"))
        return {**r, "team": team, "teams": [team], "executor_uid": (acct or {}).get("user_id", "")}

    days = store.sla_closed_days()
    if date_from:
        days = [d for d in days if d >= date_from]
    if date_to:                 # the period is the ASSIGNMENT day: a request closed up to two weeks later belongs to it
        reach = min(date.fromisoformat(date_to) + timedelta(days=SLA_CLOSED_REACH_DAYS), auth.now().date()).isoformat()
        days = [d for d in days if d <= max(reach, date_to)]
    records = {}
    for day in days:
        records.update(store.read_sla_closed_day(day))
    open_recs = state.get("open") or {}
    records.update(open_recs)
    records = {k: link(r) for k, r in records.items() if not excluded(r.get("system"))}
    unmapped = sum(1 for r in records.values() if r["team"] == teams.NO_TEAM)
    # the grid names the security user holding a request but not their number: the number they act under in the
    # history (any request they executed or confirmed) links them, else their account's security name does
    number_of = {}
    for r in records.values():
        for name, no in ((r.get("executor"), r.get("executor_no")), (r.get("checker"), r.get("checker_no"))):
            if name and no:
                number_of.setdefault(sla_history._norm(name), no)
    grid = {}
    for key, row in (state.get("grid") or {}).items():
        if excluded(row.get("system")):
            continue
        holder = row.get("holder") or ""
        grid[key] = {**row, "team": team_of(row.get("system"), number_of.get(sla_history._norm(holder), ""), holder)}
    if scope is not None:
        records = {k: r for k, r in records.items() if teams.in_scope(scope, r["team"])}
        grid = {k: r for k, r in grid.items() if teams.in_scope(scope, r["team"])}
    return {"records": records, "open": {k: r for k, r in open_recs.items() if k in records},
            "index": state.get("index") or {}, "grid": grid, "cfg": cfg, "unmapped": unmapped,
            "recorder": {"enabled": bool(sla_cfg.get("history_enabled", True)), "last_read": state.get("last_history") or "",
                         "stats": state.get("history_stats") or {}, "error": state.get("history_error") or {},
                         "days": int(sla_cfg.get("history_days") or 3), "first_day": (store.sla_closed_days() or [""])[0]},
            "left_out": {"teams": list(sla_cfg.get("exclude_teams") or []),
                         "applications": list(sla_cfg.get("exclude_applications") or [])}}


def live_sla_view():
    """What the analysis's "right now" strip borrows from the Live tickets tab, so both count the same requests the same
    way: its breach time (the latest scan's own threshold, else the setting) and each listed request's status."""
    snap, _ = read_snapshot("sla")
    data = (snap or {}).get("data") or {}
    limits = data.get("thresholds") or {}
    return {"live_breach_minutes": int(limits.get("breach_minutes") or (hub_cfg().get("sla") or {}).get("breach_minutes") or 120),
            "live_status": {str(r.get("key")): r.get("status") for r in data.get("requests") or [] if r.get("key")}}


@bp.get("/sla-config")
@auth.ops_view_required
def sla_config_get():
    """The SLA-recording config (SLA target, status labels, column mapping, feed path). Managers see it; admins edit it."""
    doc = ops().read_sla_records()
    records = doc.get("records") or {}
    scope = auth.team_scope()
    if scope is not None:                                    # a manager's count is their own team's, like the analysis
        resolver = team_resolver()
        people = sla_people()

        def mine(r):                                         # the analysis's rule: any of the request's applications
            who = r.get("assignee") or people.get(str(r.get("executor") or "").strip(), r.get("executor"))
            return any(teams.in_scope(scope, resolver.sla_team(sys_, who)) for sys_ in (r.get("systems") or [r.get("system")]))
        records = [r for r in records.values() if mine(r)]
    return jsonify(config=sla_records.clean_config(doc.get("config")), is_admin=viewer_is_admin(), recorded=len(records))


@bp.put("/sla-config")
@auth.admin_required
def sla_config_put():
    """Save the SLA-recording config (administrators only); the recording and the analysis use it from the next scan."""
    cfg = sla_records.clean_config(json_body())

    def change(doc):
        doc["config"] = cfg
    ops().update_sla_records(change)
    auth.audit("ops_sla_config", details={"sla_minutes": cfg["sla_minutes"]})
    return jsonify(ok=True, config=cfg)


def _sla_system_team():
    """System -> team for the SLA recording, taken from the admin's auto-assign application list (else Open System)."""
    return sla_records.system_team_map(_aa_config())


def apply_sla_scan(now=None, force=False):
    """Fold the latest LIVE SLA scan into the live SLA recording (sla_records.fold_latest_scan). The collector does this
    as it writes each scan; the web pages call it too, as a safety net for scans written by an older collector. Returns
    how many requests are open in the folded scan (0 when it was folded already). (The SLA Analysis itself reads the
    portal's status history, recorded by the collector: nothing is uploaded or imported.)"""
    return sla_records.fold_latest_scan(ops(), _sla_system_team(), force=force)


# ----------------------------------------------------------------- live ITSM change-request recording (exec + conclusion)
def apply_itsm_scan():
    """Fold the latest ITSM snapshot into the live CR recording (itsm_records.fold_latest_scan): each change order's
    EXECUTION TIME and CONCLUSION. The collector does this as it writes each scan; the web pages call it too, as a safety
    net. Returns how many NEW change orders were added."""
    return itsm_records.fold_latest_scan(ops(), bool((hub_cfg().get("itsm") or {}).get("complete_when_missing")))


@bp.get("/itsm-analysis")
@auth.ops_view_required
def itsm_analysis_get():
    """The LIVE ITSM change-request analysis: each CR with its EXECUTION TIME (when it was first seen as
    'Implementation Completed') and its CONCLUSION (completed / rollback / pending), filterable by date (default today
    on the page), role and conclusion. Folds the latest snapshot first so it is always current."""
    now = auth.now().isoformat(timespec="seconds")
    try:
        apply_itsm_scan()
    except Exception:                                        # recording must never break the page
        current_app.logger.exception("ITSM scan recording failed")
    records = ops().read_itsm_records().get("records") or {}
    scope = auth.team_scope()                                # a manager sees only the roles of their team(s)
    if scope is not None:
        resolver = team_resolver()
        records = {k: r for k, r in records.items() if teams.in_scope(scope, resolver.itsm_team(r.get("role")))}
    if not records:
        return jsonify(available=False, server_time=now)
    options = itsm_records.filter_options(records)
    filt = {"date_from": request.args.get("from", ""), "date_to": request.args.get("to", ""),
            "role": request.args.get("role", ""), "conclusion": request.args.get("conclusion", "")}
    filtered = itsm_records.filter_records(records, filt)
    return jsonify(available=True, server_time=now, options=options, filter={k: (v or "") for k, v in filt.items()},
                   scoreboard=itsm_listed_vs_counted(scope), **itsm_records.analyze(filtered))


def itsm_listed_vs_counted(scope):
    """{"counted", "listed"}: the waiting requests the latest scoreboard COUNTS (what the Scoreboard tab and Home show)
    and the rows the portal LISTED (all the Change requests tab can record) - a paged or unreadable list makes them
    differ, and the tab says so instead of showing a smaller number with no word."""
    snap, _ = read_snapshot("itsm")
    data = scoped_data("itsm", (snap or {}).get("data"), scope) or {}
    counted = listed = 0
    for role in data.get("roles") or []:
        for category, cell in (role.get("categories") or {}).items():
            if re.search(r"complet|closed|done", str(category), re.I) or not isinstance(cell, dict):
                continue                                      # finished work is not waiting (OPS.isDoneCategory)
            counted += int(cell.get("count") or 0)
            listed += len(cell.get("rows") or [])
    return {"counted": counted, "listed": min(listed, counted)}


@bp.get("/events")
@auth.ops_view_required
def events():
    day = parse_day(request.args.get("day"), auth.today())
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        raise ApiError("limit must be a number.")
    items = ops().read_events(day, limit)
    scope = auth.team_scope()
    if scope is not None:
        items = [e for e in (_scope_event(e, scope) for e in items) if e]
    return jsonify(day=day, events=items)


def _event_item_team(module, item):
    if module == "email":
        return item.get("team")
    if module == "sla":
        return team_resolver().sla_team(item.get("system", ""), item.get("user", ""))
    if module == "itsm":
        return team_resolver().itsm_team(item.get("role", ""))
    return ""


def _scope_event(event, scope):
    """An event as a team manager may see it: only the items, and the count, of their own teams (the collector stores
    per-group counts on the event: ``groups`` [{teams | system+user | role, n}]). An older event without them is split
    by its items when they are all listed; one that names more than it lists cannot be split and is not shown."""
    module = event.get("module")
    if module not in ("email", "sla", "itsm"):
        return event
    resolver = team_resolver()
    groups = event.get("groups")
    items = [i for i in event.get("items") or [] if isinstance(i, dict)]
    mine = [i for i in items if teams.in_scope(scope, _event_item_team(module, i))]
    if isinstance(groups, list):
        def team_of(grp):
            if module == "email":
                return next((t for t in grp.get("teams") or [] if teams.in_scope(scope, t)), "")
            if module == "sla":
                return resolver.sla_team(grp.get("system", ""), grp.get("user", ""))
            return resolver.itsm_team(grp.get("role", ""))
        count = sum(int(grp.get("n") or 0) for grp in groups if teams.in_scope(scope, team_of(grp)))
        if not count:
            return None
        out = {k: v for k, v in event.items() if k != "groups"}
        out.update(count=count, text=teams.recount_text(event.get("text", ""), count))
        if items:
            out["items"] = mine
        return out
    head = str(event.get("text") or "").split(" ", 1)[0]
    complete = items and int(event.get("count") or len(items)) == len(items) and (not head.isdigit() or int(head) == len(items))
    if not complete or not mine:
        return None
    return {**event, "items": mine, "count": len(mine), "text": teams.recount_text(event.get("text", ""), len(mine))}


# ----------------------------------------------------------------------------- e-mail actions
def raw_rows(message_id):
    snap, _ = read_snapshot("email")
    return [r for r in ((snap or {}).get("data") or {}).get("rows") or [] if r.get("message_id") == message_id]


def mail_rows(message_id, ovr=None):
    """The rows of one e-mail as people see them (overrides applied); [] when it is not in the latest scan."""
    raw = raw_rows(message_id)
    return overlay.apply_overrides(raw, ovr if ovr is not None else read_overrides(), auth.now()) if raw else []


def current_rows(message_id):
    """The e-mail's rows as people see them now (overrides applied). 404 when it is not in the latest scan; 403 when
    it belongs to another team than the manager's."""
    rows = mail_rows(message_id)
    if not rows:
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    scope = act_scope()                      # a manager also acts on the Unclassified mails
    if scope is not None and not any(overlay.row_in_scope(r, scope) for r in rows):
        raise ApiError("That e-mail belongs to another team.", 403)
    return rows


def act_scope():
    """The e-mails the signed-in person may open and act on (overlay.email_act_scope): a manager's teams plus the
    Unclassified mails, which they may read, file under a team, assign or mark as needing no action."""
    return overlay.email_act_scope(g.user)


def _stamp():
    return now_iso(auth.now())


def _who():
    return g.user.get("name") or g.user["user_id"]


def _subject(rows):
    return str(rows[0].get("subject") or "")[:200] if rows else ""


def _clean_team(value):
    team = str(value or "").strip()
    if team not in hub_cfg()["email"]["teams"]:
        raise ApiError("Choose one of the teams in the list.")
    return team


# fields of an assignment the assignee's own actions set; a new note for the SAME person must not undo them
_KEPT_WHEN_SAME_PERSON = ("done_at", "done_by", "done_by_id", "done_note", "replied_notified")


def _row_team(rows, entry=None):
    """The team the assignment is filed under: the mail's team (for a mail sent to several teams, the first one the signed-in
    manager looks after, so they can still handle it when it has left the scan)."""
    scope = act_scope()
    row = next((r for r in rows if overlay.row_in_scope(r, scope)), rows[0] if rows else {})
    return str(overlay.team_in_scope(row, scope) or row.get("team") or (entry or {}).get("team") or "")[:80]


def _notify(kind, entry, rows, message_id, old_entry=None, detail=""):
    """In-app notifications of one event (docs/FEATURES-V2.md B4). A failure here is logged and never undoes the action."""
    try:
        subject = _subject(rows) or str((entry or {}).get("subject") or "")[:200]
        return notifications.notify(ops(), auth.store().users(), kind, g.user, entry, rows, subject, message_id,
                                    auth.now(), old_entry, detail=detail)
    except Exception as exc:                                    # noqa: BLE001
        current_app.logger.warning("notification %s failed: %s: %s", kind, type(exc).__name__, exc)
        return []


@bp.post("/email/assign")
@auth.ops_act_required
def email_assign():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    address = overlay.normalize_email(body.get("assigned_to_email"))
    if not overlay.valid_email(address):
        raise ApiError("Enter a valid e-mail address to assign to.")
    name = clean_text(body.get("assigned_to_name"), "Name", 80) or overlay.display_name_from_email(address)
    note = clean_text(body.get("note"), "Note", 300, multiline=True)
    rows = current_rows(message_id)
    team = _row_team(rows)
    seen = {}

    def change(old):
        seen["old"] = old
        same = bool(old and old.get("assigned_to_email") == address)
        new = {"assigned_to_email": address, "assigned_to_name": name, "note": note, "marked_by": _who(),
               "marked_by_id": g.user["user_id"], "marked_at": _stamp(), "subject": _subject(rows), "team": team,
               "notified": bool(same and old.get("notified") and old.get("note") == note)}
        if same:
            new.update({k: old[k] for k in _KEPT_WHEN_SAME_PERSON if old.get(k)})
        return new
    entry = ops().update_override("assignments", message_id, change)
    old = seen.get("old")
    auth.audit("ops_assign", target=message_id, details={"to": address, "name": name, "note": note,
                                                          "subject": _subject(rows)[:80]})
    if not old or old.get("assigned_to_email") != address:       # another person than before: tell them (and the one it was taken from)
        if old:
            _notify(schemas.NOTIFY_UNASSIGNED, old, rows, message_id, old)
        _notify(schemas.NOTIFY_ASSIGNED, entry, rows, message_id)
    return jsonify(ok=True, assignment=entry, rev=overrides_rev(read_overrides()))


def manager_rows(message_id, entry):
    """The rows of a mail for a manager / administrator action: 404 when nothing is known about it, 403 when it belongs to
    another team. A mail that has left the latest scan can still be handled while its assignment remembers its team."""
    rows = mail_rows(message_id)
    scope = act_scope()
    if rows:
        if scope is not None and not any(overlay.row_in_scope(r, scope) for r in rows):
            raise ApiError("That e-mail belongs to another team.", 403)
    elif entry:
        if scope is not None and not overlay.team_in(scope, entry.get("team")):
            raise ApiError("That e-mail belongs to another team.", 403)
    else:
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    return rows


@bp.delete("/email/assign")
@auth.ops_act_required
def email_unassign():
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    entry = ops().overrides("assignments").get(message_id)
    rows = manager_rows(message_id, entry)
    if not entry:
        raise ApiError("That e-mail is not assigned.", 404)
    taken = {}

    def remove(old):
        taken["old"] = old
        return None
    ops().update_override("assignments", message_id, remove)
    old = taken.get("old") or entry
    auth.audit("ops_unassign", target=message_id, details={"subject": (_subject(rows) or str(old.get("subject") or ""))[:80]})
    _notify(schemas.NOTIFY_UNASSIGNED, old, rows, message_id, old)
    return jsonify(ok=True, rev=overrides_rev(read_overrides()))


@bp.post("/email/no-action")
@auth.ops_act_required
def email_no_action():
    message_id = clean_message_id(json_body().get("message_id"))
    rows = current_rows(message_id)
    if all(r.get("status") == schemas.EMAIL_REPLIED for r in rows):
        raise ApiError("That e-mail already has a reply, so it cannot be marked as needing no action.", 409)
    entry = ops().set_override("no_action", message_id, {
        "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(), "subject": _subject(rows)})
    auth.audit("ops_no_action", target=message_id, details={"subject": _subject(rows)[:80]})
    return jsonify(ok=True, mark=entry, rev=overrides_rev(read_overrides()))


@bp.delete("/email/no-action")
@auth.ops_act_required
def email_no_action_undo():
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    if not ops().overrides("no_action").get(message_id):
        raise ApiError("That e-mail is not marked as needing no action.", 404)
    scope = act_scope()
    raw = raw_rows(message_id)       # the overlay hides marked carry-over mails, so look at the snapshot itself
    if scope is not None and not any(overlay.row_in_scope(r, scope) for r in raw):
        raise ApiError("That e-mail belongs to another team.", 403)
    ops().delete_override("no_action", message_id)
    auth.audit("ops_no_action_undo", target=message_id, details={"subject": _subject(raw)[:80]})
    return jsonify(ok=True, rev=overrides_rev(read_overrides()))


@bp.post("/email/team")
@auth.login_required
def email_team():
    """Move an e-mail to another team. A manager / administrator may re-file any mail of a team they look after to ANY
    team; an employee may re-file a mail that is assigned to THEM (so a person who sees a mail landed in the wrong team
    can route it on). The destination is any real team."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    team = _clean_team(body.get("team"))
    if g.user["role"] in auth.OPS_ROLES:
        rows = current_rows(message_id)                 # manager / admin: a mail of a team they look after
        via = "manager"
    else:
        entry, rows, via = assignment_target(message_id)   # employee: only a mail assigned to them
        if via != "assignee":
            raise not_available()
    previous = rows[0].get("team", "")
    entry = ops().set_override("team", message_id, {
        "team": team, "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(),
        "subject": _subject(rows)})
    auth.audit("ops_team", target=message_id, details={"from": previous, "to": team, "by": via,
                                                       "subject": _subject(rows)[:80]})
    return jsonify(ok=True, team=entry, rev=overrides_rev(read_overrides()))


def _clean_reasons(value):
    if value in (None, ""):
        return ""
    items = value if isinstance(value, list) else [p.strip() for p in str(value).split(",")]
    out = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item not in QUALITY_REASONS:
            raise ApiError(f"'{item[:40]}' is not one of the quality reasons.")
        if item not in out:
            out.append(item)
    return ", ".join(out)


@bp.post("/email/quality")
@auth.ops_act_required
def email_quality():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    rating = str(body.get("rating") or "").strip()
    if rating not in RATINGS:
        raise ApiError("Rating must be Excellent, Good or Needs Improvement.")
    note = clean_text(body.get("note"), "Note", 500, multiline=True)
    reasons = _clean_reasons(body.get("reasons"))
    rows = current_rows(message_id)
    row = next((r for r in rows if r.get("status") == schemas.EMAIL_REPLIED), None)
    if row is None:
        raise ApiError("Only e-mails that have a reply can be rated.", 409)
    if row.get("reply_by_manager"):
        raise ApiError("Replies written by managers are not quality-reviewed.", 409)
    replier = overlay.normalize_email(row.get("first_reply_email"))
    if replier and replier == overlay.normalize_email(g.user.get("email")):
        raise ApiError("You cannot rate your own reply.", 403)
    if not overlay.reply_in_scope(row, overlay.scope_for_user(g.user), person_teams()):
        raise ApiError("That reply was written by another team - only its own managers rate it.", 403)
    reply_team = (overlay.reply_teams(row, person_teams()) or [""])[0]

    def change(old):
        old = old or {}
        return {"rating": rating, "note": note, "reasons": reasons, "replied_by": row.get("first_reply_by", ""),
                "replied_by_email": replier, "reviewed_by": _who(), "reviewed_by_id": g.user["user_id"],
                "reviewed_at": _stamp(), "subject": _subject(rows), "team": _row_team([row]),
                "reply_team": reply_team, "received": str(row.get("received_time") or "")[:19],
                "feedback_sent": False,
                # an appeal is kept as history; a new rating has not been decided on yet
                "appeal": old.get("appeal", ""), "appeal_by": old.get("appeal_by", ""), "appeal_at": old.get("appeal_at", ""),
                "appeal_decision": "", "decided_by": "", "decided_at": "", "rating_withdrawn": False}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality", target=message_id, details={"rating": rating, "reasons": reasons, "replied_by": replier,
                                                           "subject": _subject(rows)[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


@bp.post("/email/quality/appeal")
@auth.login_required
def email_quality_appeal():
    """The person who wrote the rated reply asks for the rating to be looked at again."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    text = clean_text(body.get("appeal"), "Appeal", 500, required=True, minlen=3, multiline=True)
    mine = overlay.normalize_email(g.user.get("email"))
    review = ops().overrides("quality").get(message_id)
    if not review or not mine or overlay.normalize_email(review.get("replied_by_email")) != mine:
        # the same answer whether or not somebody else's rating exists, so nothing can be probed
        raise ApiError("There is no rating of your own reply for that e-mail.", 404)
    if review.get("rating_withdrawn"):
        raise ApiError("That rating was already withdrawn.", 409)
    if review.get("appeal_decision"):
        raise ApiError("That appeal was already decided.", 409)

    def change(old):
        return {**old, "appeal": text, "appeal_by": _who(), "appeal_at": _stamp()}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality_appeal", target=message_id, details={"subject": str(review.get("subject") or "")[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


@bp.post("/email/quality/decision")
@auth.ops_act_required
def email_quality_decision():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    decision = str(body.get("decision") or "").strip().capitalize()
    if decision not in DECISIONS:
        raise ApiError("Decision must be Accepted or Rejected.")
    review = ops().overrides("quality").get(message_id)
    if not review:
        raise ApiError("There is no rating for that e-mail.", 404)
    if not review.get("appeal"):
        raise ApiError("There is no appeal to decide.", 409)
    team = review.get("team") or next((r.get("team") for r in raw_rows(message_id)), "")
    scope = auth.team_scope()
    rows = mail_rows(message_id)
    if not auth.may_act_on_team(team) and not any(overlay.row_in_scope(r, scope) for r in rows):
        raise ApiError("That e-mail belongs to another team.", 403)
    # the appeal is about a reply: only the managers of the team that wrote it decide (as only they rated it)
    replied = next((r for r in rows if r.get("status") == schemas.EMAIL_REPLIED), None)
    reply_team = review.get("reply_team") or ((overlay.reply_teams(replied, person_teams()) or [""])[0] if replied else "")
    if reply_team and scope is not None and not overlay.team_in(scope, reply_team):
        raise ApiError("That reply was written by another team - only its own managers decide the appeal.", 403)

    def change(old):
        return {**old, "appeal_decision": decision, "decided_by": _who(), "decided_at": _stamp(),
                "rating_withdrawn": decision == "Accepted"}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality_decision", target=message_id, details={"decision": decision,
                                                                    "subject": str(review.get("subject") or "")[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


# ----------------------------------------------------------------------------- assigned mail: open, download, Done
# docs/FEATURES-V2.md B2 / B3. THE rule of this section: a person gets a mail only when the assignment names them (any role),
# or when they are a manager of its team, or an administrator. Everything else - a mail that does not exist, a mail that
# is somebody else's, an "Unclassified" mail of no one - gets the SAME answer, so ids cannot be probed.
def not_available():
    return ApiError("That e-mail is not available.", 404)


def is_assignee(user, entry):
    mine = overlay.normalize_email((user or {}).get("email"))
    return bool(entry) and bool(mine) and overlay.normalize_email(entry.get("assigned_to_email")) == mine


def may_open_mail(user, message_id, entry, rows):
    """The access rule for opening, downloading, marking done and reopening one e-mail (see above)."""
    if is_assignee(user, entry):
        return True
    if (user or {}).get("role") not in auth.OPS_ROLES:
        return False                                            # an employee: only what is assigned to them
    scope = overlay.email_act_scope(user)                       # a manager: their teams and the Unclassified mails
    if rows:
        return scope is None or any(overlay.row_in_scope(r, scope) for r in rows)
    if entry:                                                   # the mail left the scan: the assignment remembers its team
        return scope is None or overlay.team_in(scope, entry.get("team"))
    # nothing in the scan and no assignment: only someone who sees every team, and only if a saved copy exists
    return scope is None and mailbox.preview_path(ops().mail_dir, mailbox.mail_key(message_id)).is_file()


def mail_access(message_id, route):
    """(assignment entry or None, overlaid rows) when the signed-in person may handle ``message_id``; otherwise the one
    404 for 'not found' and 'not yours' alike (and an audit line, so probing leaves a trace)."""
    ovr = read_overrides()
    entry = ovr["assignments"].get(message_id)
    rows = mail_rows(message_id, ovr)
    if not may_open_mail(g.user, message_id, entry, rows):
        auth.audit("ops_mail_denied", target=message_id[:200], details={"route": route})
        raise not_available()
    return entry, rows


def read_only_view():
    """True when the page is somebody else's (``?as=<user>``): looking is allowed, acting is not."""
    wanted = norm_uid(request.args.get("as") or optional_body().get("as"))
    return bool(wanted) and wanted != g.user["user_id"]


def mail_state(message_id, entry, rows):
    """(key, preview or None, has_msg, state) of the saved copy of one mail."""
    key = mailbox.mail_key(message_id)
    preview, has_msg = mailbox.load_copy(ops().mail_dir, key)
    scope = hub_cfg()["email"]["mail_copies"]["scope"]
    wanted = scope != "off" and (bool(entry) or (scope == "assigned_and_pending" and any(
        r.get("status") == schemas.EMAIL_PENDING for r in rows)))
    if preview and has_msg:
        state = schemas.MAIL_STATE_READY
    elif preview and not int(preview.get("msg_bytes") or 0):
        state = schemas.MAIL_STATE_TOO_LARGE
    else:
        state = schemas.MAIL_STATE_PREPARING if wanted else schemas.MAIL_STATE_UNAVAILABLE
    return key, preview, has_msg, state


def assignment_view(entry, row=None):
    """What a person sees of an assignment (no internal ids)."""
    if not entry:
        return None
    manual = bool(entry.get("done_at"))
    auto = bool(row and row.get("done_auto")) and not manual
    return {"assigned_to_email": overlay.normalize_email(entry.get("assigned_to_email")),
            "assigned_to_name": entry.get("assigned_to_name", ""), "assigned_by": entry.get("marked_by", ""),
            "assigned_at": entry.get("marked_at", ""), "note": entry.get("note", ""),
            "done": manual or auto, "done_auto": auto,
            "done_at": entry.get("done_at", "") if manual else (row.get("done_at", "") if auto else ""),
            "done_by": entry.get("done_by", "") if manual else "", "done_note": entry.get("done_note", "") if manual else ""}


def _mail_block(rows):
    if not rows:
        return None
    row = next((r for r in rows if r.get("status") == schemas.EMAIL_PENDING), rows[0])
    block = {k: row.get(k, "") for k in ("status", "sla_status", "sla_minutes", "team", "received_time",
                                          "current_waiting_time", "first_response_time", "first_reply_by",
                                          "first_reply_time", "detected_systems", "customer_name")}
    block.update(escalated=bool(row.get("escalated")), is_carryover=bool(row.get("is_carryover")))
    return block


@bp.get("/email/message")
@auth.login_required
def email_message():
    message_id = clean_message_id(request.args.get("message_id"))
    entry, rows = mail_access(message_id, "message")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    row = rows[0] if rows else {}
    view = assignment_view(entry, next((r for r in rows if r.get("assigned_to_email")), row))
    out = {"message_id": message_id, "state": state, "has_msg": has_msg, "source": "copy" if preview else "snapshot"}
    if preview:
        out.update({k: preview.get(k) for k in ("subject", "from_name", "from_email", "to", "cc", "received", "body_text",
                                                 "truncated", "attachments", "saved_at", "msg_bytes")})
    else:                                  # no saved copy yet: what the scan knows (the first 300 characters of the text)
        out.update(subject=row.get("subject") or (entry or {}).get("subject", ""), from_name=row.get("customer_name", ""),
                   from_email=row.get("customer_email", ""), to=[], cc=[], received=row.get("received_time"),
                   body_text=row.get("body_preview", ""), truncated=bool(row.get("body_preview")), attachments=[],
                   saved_at=None, msg_bytes=0)
    acting = not read_only_view()
    handler = is_assignee(g.user, entry) or g.user["role"] in auth.OPS_ROLES
    # The team member's reply, read from the scan (not the saved .msg, which is the customer's original). The manager
    # reads the original e-mail and this reply side by side to do the quality review - but only a reply of their own
    # team(s): another team's reply shows which team answered, never the person, the text or its rating.
    reply_row = next((r for r in rows if r.get("first_reply_time") or r.get("reply_count")), row)
    mine = overlay.normalize_email(g.user.get("email"))
    own_reply = bool(mine) and overlay.normalize_email(reply_row.get("first_reply_email")) == mine
    hidden = (g.user["role"] in auth.OPS_ROLES and not own_reply and not is_assignee(g.user, entry)
              and not overlay.reply_in_scope(reply_row, overlay.scope_for_user(g.user), person_teams()))
    block = _mail_block(rows)
    if hidden and block:
        block["first_reply_by"] = ""
    out.update(mail=block, assignment=view, can={
        "download": state == schemas.MAIL_STATE_READY, "prepare": state == schemas.MAIL_STATE_PREPARING,
        "done": bool(entry) and acting and handler and not (view or {}).get("done"),
        "reopen": bool(entry) and acting and handler and bool(entry.get("done_at"))})
    if hidden:
        out["reply"] = {"hidden": True, "team": (overlay.reply_teams(reply_row, person_teams()) or [""])[0],
                        "at": reply_row.get("first_reply_time"), "count": reply_row.get("reply_count", 0) or 0,
                        "response_minutes": reply_row.get("response_minutes")}
    elif reply_row.get("first_reply_time") or reply_row.get("reply_count"):
        if reply_row.get("quality_rating") and (g.user["role"] in auth.OPS_ROLES or own_reply):
            out["quality"] = {"rating": reply_row.get("quality_rating", ""), "reasons": reply_row.get("quality_reasons", ""),
                              "note": reply_row.get("quality_note", ""),
                              "reviewed_by": reply_row.get("quality_reviewed_by", ""),
                              "reviewed_at": reply_row.get("quality_reviewed_at", ""),
                              "appeal": reply_row.get("quality_appeal", ""),
                              "appeal_decision": reply_row.get("appeal_decision", ""),
                              "withdrawn": bool(reply_row.get("rating_withdrawn"))}
        out["reply"] = {
            "by": reply_row.get("first_reply_by", ""),
            "email": reply_row.get("first_reply_email", ""),
            "at": reply_row.get("first_reply_time"),
            "text": reply_row.get("reply_body_preview", ""),
            "count": reply_row.get("reply_count", 0) or 0,
            "by_manager": bool(reply_row.get("reply_by_manager")),
            "response_minutes": reply_row.get("response_minutes"),
            "last_by": reply_row.get("last_reply_by", ""),
            "last_at": reply_row.get("last_reply_time"),
        }
    auth.audit("ops_mail_open", target=message_id, details={"state": state, "subject": str(out.get("subject") or "")[:80]})
    return jsonify(out)


@bp.get("/email/message/download")
@auth.login_required
def email_message_download():
    message_id = clean_message_id(request.args.get("message_id"))
    entry, rows = mail_access(message_id, "download")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    if state != schemas.MAIL_STATE_READY:
        raise ApiError("The copy of this e-mail is " + ("too large to keep - open it in Outlook."
                       if state == schemas.MAIL_STATE_TOO_LARGE else "not ready yet."), 404)
    name, ascii_name = mailbox.download_name(preview.get("subject"))
    try:
        resp = send_file(mailbox.msg_path(ops().mail_dir, key), mimetype=mailbox.MSG_MIME, as_attachment=True,
                         download_name=ascii_name, conditional=False, etag=False, max_age=0)
    except OSError:                                               # housekeeping removed it a moment ago
        raise ApiError("The copy of this e-mail is not ready yet.", 404)
    resp.headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    auth.audit("ops_mail_download", target=message_id, details={"subject": str(preview.get("subject") or "")[:80],
                                                                  "bytes": preview.get("msg_bytes")})
    return resp


@bp.post("/email/message/prepare")
@auth.login_required
def email_message_prepare():
    """'Prepare now': ask the collector for an e-mail scan so the copy of a mail that has none yet is saved."""
    message_id = clean_message_id(json_body().get("message_id"))
    entry, rows = mail_access(message_id, "prepare")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    if state != schemas.MAIL_STATE_PREPARING:
        return jsonify(ok=True, queued=False, state=state)
    if ops().pending_run_requests("email"):
        return jsonify(ok=True, queued=True, already_pending=True, state=state)
    name = ops().add_run_request("email", g.user["user_id"])
    auth.audit("ops_mail_prepare", target=message_id, details={"request": name})
    return jsonify(ok=True, queued=True, already_pending=False, state=state)


def assignment_target(message_id):
    """(entry, rows, via) for Done / Reopen: the person must be the assignee or a manager / administrator of the mail's team.
    Anybody else gets the one 'not available' answer; an in-scope manager on a mail nobody holds gets 409."""
    if read_only_view():
        raise ApiError("This is somebody else's page - it is read-only.", 403)
    entry, rows = mail_access(message_id, "done")
    if not entry:
        raise ApiError("That e-mail is not assigned.", 409)
    return entry, rows, ("assignee" if is_assignee(g.user, entry) else "manager")


@bp.post("/email/done")
@auth.login_required
def email_done():
    """The assignee (or a manager / administrator of the mail's team) says the assigned e-mail is handled."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    note = clean_text(body.get("note"), "Note", schemas.DONE_NOTE_CHARS, multiline=True)
    entry, rows, via = assignment_target(message_id)
    state = {}

    def change(old):
        if not old or (via == "assignee" and not is_assignee(g.user, old)):
            state["gone"] = True                                  # unassigned / given to somebody else a moment ago
            return old
        if old.get("done_at"):
            state["already"] = True
            return old
        state["changed"] = True
        return {**old, "done_at": _stamp(), "done_by": _who(), "done_by_id": g.user["user_id"], "done_note": note}
    updated = ops().update_override("assignments", message_id, change)
    if state.get("gone"):
        raise not_available()
    if state.get("changed"):
        auth.audit("ops_done", target=message_id, details={"by": via, "note": note[:80],
                                                           "subject": (_subject(rows) or str(updated.get("subject") or ""))[:80]})
        _notify(schemas.NOTIFY_DONE, updated, rows, message_id)
    return jsonify(ok=True, already=bool(state.get("already")), assignment=assignment_view(updated),
                   rev=overrides_rev(read_overrides()))


@bp.delete("/email/done")
@auth.login_required
def email_reopen():
    """Take the Done mark back (the assignee, or a manager / administrator of the mail's team)."""
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    entry, rows, via = assignment_target(message_id)
    if not entry.get("done_at"):
        if any(r.get("done_auto") for r in rows):
            raise ApiError("The assignee answered that e-mail in Outlook, so it stays done.", 409)
        return jsonify(ok=True, already=True, assignment=assignment_view(entry), rev=overrides_rev(read_overrides()))
    state = {}

    def change(old):
        if not old or (via == "assignee" and not is_assignee(g.user, old)):
            state["gone"] = True
            return old
        if not old.get("done_at"):
            state["already"] = True
            return old
        state["changed"] = True
        return {k: v for k, v in old.items() if k not in ("done_at", "done_by", "done_by_id", "done_note")}
    updated = ops().update_override("assignments", message_id, change)
    if state.get("gone"):
        raise not_available()
    if state.get("changed"):
        auth.audit("ops_reopen", target=message_id, details={"by": via,
                                                             "subject": (_subject(rows) or str(updated.get("subject") or ""))[:80]})
        _notify(schemas.NOTIFY_REOPENED, updated, rows, message_id)
    return jsonify(ok=True, already=bool(state.get("already")), assignment=assignment_view(updated),
                   rev=overrides_rev(read_overrides()))


# ----------------------------------------------------------------- the employee's own actions on a mail assigned to them
NO_ACTION_REASONS = ["For information only", "Duplicate of another e-mail", "Already handled another way",
                     "Automatic reply / out of office", "Sent to us by mistake", "Other"]


def assignee_target(message_id, route):
    """(entry, rows) when the signed-in person is the ASSIGNEE of the mail, on their own page; the single 'not
    available' 404 otherwise (a manager uses the /email/* actions instead)."""
    if read_only_view():
        raise ApiError("This is somebody else's page - it is read-only.", 403)
    entry, rows = mail_access(message_id, route)
    if not is_assignee(g.user, entry):
        raise not_available()
    return entry, rows


def _mail_is_open(rows, entry):
    return any(r.get("status") == schemas.EMAIL_PENDING for r in rows or []) and not (entry or {}).get("done_at")


def teammates_for_mail(rows, entry):
    """The active employees of the mail's team(s) - or, for an Unclassified mail, of the employee's own teams."""
    teams = overlay.mail_teams(rows, entry) or [t for t in overlay.user_teams(g.user) if t != schemas.ALL_TEAMS_MARK]
    return overlay.teammates(auth.store().users(), teams, g.user.get("email"))


@bp.get("/my/email/teammates")
@auth.login_required
def my_email_teammates():
    """Who the assignee may hand the mail on to: active employees of its team (names only, never addresses)."""
    message_id = clean_message_id(request.args.get("message_id"))
    entry, rows = assignee_target(message_id, "teammates")
    return jsonify(teammates=[{"user_id": p["user_id"], "name": p["name"], "teams": p["teams"]}
                              for p in teammates_for_mail(rows, entry)])


@bp.post("/my/email/reassign")
@auth.login_required
def my_email_reassign():
    """The assignee hands an OPEN mail to a teammate; the assignment then follows the teammate (who is told), and the
    assigner and the team's managers are told who has it now. The employee no longer holds it."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    to_uid = norm_uid(body.get("to_user_id"))
    note = clean_text(body.get("note"), "Note", 300, multiline=True)
    entry, rows = assignee_target(message_id, "reassign")
    if not _mail_is_open(rows, entry):
        raise ApiError("Only an open e-mail can be handed on (it was answered, closed or marked done).", 409)
    target = next((p for p in teammates_for_mail(rows, entry) if p["user_id"] == to_uid), None)
    if target is None:
        raise ApiError("Choose a teammate from the list.")
    state = {}

    def change(old):
        if not old or not is_assignee(g.user, old):
            state["gone"] = True                       # somebody re-assigned it a moment ago
            return old
        state["old"] = dict(old)
        hand = {"from": g.user.get("email", ""), "from_name": _who(), "to": target["email"], "at": _stamp()}
        return {"assigned_to_email": target["email"], "assigned_to_name": target["name"], "note": note,
                "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(),
                "subject": _subject(rows) or old.get("subject", ""), "team": old.get("team") or _row_team(rows),
                "notified": False, "by": "assignee",
                "handovers": (list(old.get("handovers") or []) + [hand])[-5:],
                "first_marked_by": old.get("first_marked_by") or old.get("marked_by", ""),
                "first_marked_by_id": old.get("first_marked_by_id") or old.get("marked_by_id", "")}
    updated = ops().update_override("assignments", message_id, change)
    if state.get("gone"):
        raise not_available()
    auth.audit("ops_reassign", target=message_id, details={"from": g.user.get("email", ""), "to": target["email"],
                                                            "by": "assignee", "note": note[:80],
                                                            "subject": _subject(rows)[:80]})
    _notify(schemas.NOTIFY_ASSIGNED, updated, rows, message_id)
    _notify(schemas.NOTIFY_REASSIGNED, updated, rows, message_id, state.get("old"), detail=f"to {target['name']}")
    return jsonify(ok=True, to=target["name"], rev=overrides_rev(read_overrides()))


@bp.post("/my/email/no-action")
@auth.login_required
def my_email_no_action():
    """The assignee marks an OPEN mail as needing no reply, with a reason (and a note for "Other")."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    reason = str(body.get("reason") or "").strip()
    if reason not in NO_ACTION_REASONS:
        raise ApiError("Choose why no reply is needed.")
    note = clean_text(body.get("note"), "Note", 300, multiline=True)
    if reason == "Other" and len(note) < 3:
        raise ApiError("Say in a few words why no reply is needed.")
    entry, rows = assignee_target(message_id, "no_action")
    if not _mail_is_open(rows, entry):
        raise ApiError("Only an open e-mail can be marked as needing no reply.", 409)
    mark = ops().set_override("no_action", message_id, {
        "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(), "subject": _subject(rows),
        "reason": reason, "note": note, "by": "assignee", "team": (entry or {}).get("team") or _row_team(rows)})
    auth.audit("ops_no_action", target=message_id, details={"by": "assignee", "reason": reason, "note": note[:80],
                                                             "subject": _subject(rows)[:80]})
    _notify(schemas.NOTIFY_NO_ACTION, entry, rows, message_id, detail=reason)
    return jsonify(ok=True, mark=mark, rev=overrides_rev(read_overrides()))


@bp.delete("/my/email/no-action")
@auth.login_required
def my_email_no_action_undo():
    """The assignee takes back THEIR OWN "no reply needed" mark (never a manager's)."""
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    assignee_target(message_id, "no_action_undo")
    mark = ops().overrides("no_action").get(message_id)
    if not mark or str(mark.get("marked_by_id") or "") != g.user["user_id"]:
        raise not_available()
    ops().delete_override("no_action", message_id)
    auth.audit("ops_no_action_undo", target=message_id,
               details={"by": "assignee", "subject": str(mark.get("subject") or "")[:80]})
    return jsonify(ok=True, rev=overrides_rev(read_overrides()))


# ----------------------------------------------------------------------------- notifications (the bell)
def sweep_replied():
    """Tell the assigner and the team's managers - once - when an assignee answered an assigned e-mail in Outlook.

    The collector only writes snapshots, so the web app notices: when the snapshot or the assignments changed it looks for
    rows that are Replied by their assignee and whose assignment has not been announced yet (``replied_notified``). The
    flag is set under the assignments lock, so two requests at once (or two servers) announce a reply only once."""
    snap, _ = read_snapshot("email")
    if not snap:
        return
    assignments = ops().overrides("assignments")
    memo = current_app.extensions.setdefault("ops_replied_sweep", {})
    if memo.get("stamp") == snap.get("generated_at") and memo.get("assignments") is assignments:
        return
    memo["stamp"], memo["assignments"] = snap.get("generated_at"), assignments
    found = {}
    for row in (snap.get("data") or {}).get("rows") or []:
        entry = assignments.get(row.get("message_id")) if isinstance(row, dict) else None
        if entry and not entry.get("replied_notified") and row.get("status") == schemas.EMAIL_REPLIED \
                and overlay.replied_by_address(row, overlay.normalize_email(entry.get("assigned_to_email"))):
            found.setdefault(row["message_id"], row)
    for message_id, row in found.items():
        flipped = {}

        def flip(old, flipped=flipped):
            if old and not old.get("replied_notified"):
                flipped["entry"] = {**old, "replied_notified": _stamp()}
                return flipped["entry"]
            return old
        ops().update_override("assignments", message_id, flip)
        if flipped:
            entry = flipped["entry"]
            assignee = next((u for u in auth.store().users().values()
                             if overlay.normalize_email(u.get("email")) == overlay.normalize_email(entry.get("assigned_to_email"))
                             and u.get("active", True)), None)
            rows = overlay.apply_overrides([row], read_overrides(), auth.now())
            try:
                notifications.notify(ops(), auth.store().users(), schemas.NOTIFY_REPLIED, None, entry, rows,
                                     row.get("subject", ""), message_id, auth.now(),
                                     actor_name=(assignee or {}).get("name") or entry.get("assigned_to_name", ""))
            except Exception as exc:                          # noqa: BLE001
                current_app.logger.warning("notification replied failed: %s: %s", type(exc).__name__, exc)


@bp.get("/notifications")
@auth.login_required
def notifications_get():
    """The signed-in person's bell: ``{unread, items, ts}`` or, when ``since`` is still current, ``{unchanged, unread}``.
    The person comes from the session only - there is no way to ask for somebody else's."""
    try:
        sweep_replied()
    except Exception as exc:                                  # noqa: BLE001 - the bell must keep answering
        current_app.logger.warning("replied sweep failed: %s: %s", type(exc).__name__, exc)
    return jsonify(notifications.poll(ops(), g.user["user_id"], request.args.get("since")))


@bp.post("/notifications/read")
@auth.login_required
def notifications_read():
    body = json_body()
    ids = body.get("ids")
    if ids != "all":
        if not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids):
            raise ApiError('ids must be a list of notification ids or "all".')
    result = notifications.mark_read(ops(), g.user["user_id"], ids, auth.now())
    if result["changed"]:
        auth.audit("ops_notifications_read", details={"count": result["changed"], "all": ids == "all"})
    return jsonify(ok=True, unread=result["unread"], ts=result["ts"])


# ----------------------------------------------------------------------------- classification rules
def _rule_keyword(body):
    keyword = " ".join(str(body.get("keyword") or "").split())
    if not 2 <= len(keyword) <= 80:
        raise ApiError("The keyword must be 2 to 80 characters.")
    return keyword


def _find_rule(rules, keyword):
    return next((k for k in rules if k.casefold() == keyword.casefold()), None)


def _save_rules(rules):
    try:
        saved = hub_settings.patch(ops(), {"email": {"classification": {"system_team_map": rules}}})
    except hub_settings.SettingsError as exc:
        raise ApiError("; ".join(f"{e['path']}: {e['message']}" for e in exc.errors[:3]), 400)
    forget_settings()
    return saved["email"]["classification"]["system_team_map"]


@bp.post("/email/classification/rule")
@auth.ops_act_required
def classification_rule_add():
    body = json_body()
    keyword = _rule_keyword(body)
    team = _clean_team(body.get("team"))
    if team.casefold() == "unclassified":
        raise ApiError("A rule must point to a real team, not to Unclassified.")
    if not auth.may_act_on_team(team):
        raise ApiError("You can only create rules for your own team.", 403)
    rules = dict(hub_cfg()["email"]["classification"]["system_team_map"])
    existing = _find_rule(rules, keyword)
    if existing:
        if not auth.may_act_on_team(rules[existing]):
            raise ApiError("That keyword already belongs to another team's rule.", 403)
        del rules[existing]
    rules[keyword] = team
    saved = _save_rules(rules)
    auth.audit("ops_classification_rule", target=keyword, details={"op": "add", "team": team})
    return jsonify(ok=True, rule={"keyword": keyword, "team": team}, system_team_map=saved)


@bp.delete("/email/classification/rule")
@auth.ops_act_required
def classification_rule_remove():
    body = optional_body()
    keyword = " ".join(str(body.get("keyword") or request.args.get("keyword") or "").split())
    if not keyword:
        raise ApiError("Say which keyword to remove.")
    rules = dict(hub_cfg()["email"]["classification"]["system_team_map"])
    existing = _find_rule(rules, keyword)
    if not existing:
        raise ApiError("There is no such rule.", 404)
    if not auth.may_act_on_team(rules[existing]):
        raise ApiError("You can only remove rules of your own team.", 403)
    team = rules.pop(existing)
    saved = _save_rules(rules)
    auth.audit("ops_classification_rule", target=existing, details={"op": "remove", "team": team})
    return jsonify(ok=True, system_team_map=saved)


# -------------------------------------------------------------- learned classifications (pending admin approval)
@bp.get("/email/classification/suggestions")
@auth.ops_view_required
def classification_suggestions():
    """The learned classifications still waiting for approval: an Unclassified mail a team member answered, with the
    team to file it under and a keyword to learn from it. Pending ones are derived from the latest scan; approved and
    rejected ones (stored) are never proposed again. A manager sees only suggestions for their own team(s)."""
    snap, _ = read_snapshot("email")
    scope = auth.team_scope()
    decided = ops().read_classify_suggestions()
    team_ov = read_overrides().get("team") or {}
    out = []
    for r in (overlaid_rows(snap) if snap else []):
        team = r.get("suggested_team")
        mid = r.get("message_id") or r.get("row_id")
        if not team or not mid:
            continue
        if r.get("classified_by") != "none" or mid in team_ov:          # already classified, or already moved
            continue
        if (decided.get(mid) or {}).get("status") in ("approved", "rejected"):
            continue
        if scope is not None and not overlay.team_in(scope, team):       # not this manager's team
            continue
        words = [w.strip() for w in str(r.get("classification_hint") or "").split(";") if w.strip()]
        out.append({"message_id": mid, "subject": r.get("subject", ""), "suggested_team": team,
                    "reply_by": r.get("first_reply_by", ""), "reply_at": r.get("first_reply_time"),
                    "received": r.get("received_time"), "customer": r.get("customer_name", ""),
                    "keyword": words[0] if words else "", "candidates": words})
    out.sort(key=lambda s: str(s.get("received") or ""), reverse=True)
    return jsonify(suggestions=out, scope=overlay.scope_label(scope))


@bp.post("/email/classification/suggestion")
@auth.ops_act_required
def classification_suggestion_approve():
    """Approve a learned classification: file THIS mail under the team now, and learn a keyword -> team rule so the
    next mail like it classifies itself."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    keyword = _rule_keyword(body)
    team = _clean_team(body.get("team"))
    if team.casefold() == "unclassified":
        raise ApiError("Pick a real team to file this e-mail under, not Unclassified.")
    if not auth.may_act_on_team(team):
        raise ApiError("You can only approve classifications for your own team.", 403)
    ovr = read_overrides()
    rows = mail_rows(message_id, ovr)       # the mail is Unclassified now, so its own team is out of a manager's scope:
    if not rows:                            # authorise by the TARGET team (may_act_on_team above), not the mail's team
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    # only a suggestion still pending (the same filter as the list) may be approved: never a classified mail
    pending = [r for r in rows if r.get("suggested_team") and r.get("classified_by") == "none"]
    if not pending or message_id in (ovr.get("team") or {}):
        raise ApiError("There is no pending classification suggestion for that e-mail. Reload the page.", 404)
    scope = auth.team_scope()
    if scope is not None and not any(overlay.team_in(scope, r["suggested_team"]) for r in pending):
        raise ApiError("You can only approve classifications for your own team.", 403)
    previous = rows[0].get("team", "")
    # 1) learn the keyword -> team rule (added to system_team_map; it wins over the built-in map)
    rules = dict(hub_cfg()["email"]["classification"]["system_team_map"])
    existing = _find_rule(rules, keyword)
    if existing:
        if not auth.may_act_on_team(rules[existing]):
            raise ApiError("That keyword already belongs to another team's rule.", 403)
        del rules[existing]
    rules[keyword] = team
    saved = _save_rules(rules)
    # 2) file this mail under the team now
    ops().set_override("team", message_id, {"team": team, "marked_by": _who(), "marked_by_id": g.user["user_id"],
                                            "marked_at": _stamp(), "subject": _subject(rows)})
    # 3) remember the decision so the suggestion is not shown again
    ops().update_classify_suggestions(lambda d: {**d, message_id: {
        "status": "approved", "team": team, "keyword": keyword, "decided_by": _who(), "decided_at": _stamp()}})
    auth.audit("ops_classification_learn", target=message_id,
               details={"op": "approve", "from": previous, "team": team, "keyword": keyword})
    return jsonify(ok=True, rule={"keyword": keyword, "team": team}, system_team_map=saved,
                   rev=overrides_rev(read_overrides()))


@bp.delete("/email/classification/suggestion")
@auth.ops_act_required
def classification_suggestion_reject():
    """Dismiss a learned classification without applying it: remembered as rejected, not proposed again."""
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    rows = mail_rows(message_id)
    if not rows:
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    team = rows[0].get("suggested_team") or ""
    if not team:
        raise ApiError("There is no suggestion for that e-mail.", 404)
    if not auth.may_act_on_team(team):
        raise ApiError("You can only dismiss suggestions for your own team.", 403)
    ops().update_classify_suggestions(lambda d: {**d, message_id: {
        "status": "rejected", "decided_by": _who(), "decided_at": _stamp()}})
    auth.audit("ops_classification_learn", target=message_id, details={"op": "reject", "team": team})
    return jsonify(ok=True)


# ----------------------------------------------------------------------------- auto-assign (docs/HUB-DESIGN auto-assign)
def _aa_config():
    return ops().read_auto_assign()


def _aa_team_members(team):
    """{email: name} of the active employees of ``team`` (an auto-assign rule may only name these)."""
    return {e: m["name"] for e, m in _aa_team_members_full(team).items()}


def _aa_team_members_full(team):
    """{email: {"name", "portal_id"}} of the active employees of ``team``. ``portal_id`` is the value the person has in
    the portal's الموظفين drop-down (set per user in admin); the ticketing auto-assign can only hand work to a member
    who has one."""
    out = {}
    for u in auth.store().users().values():
        email = overlay.normalize_email(u.get("email"))
        if u.get("role") == "user" and u.get("active", True) and email \
                and any(overlay._same_team(t, team) for t in overlay.user_teams(u)):
            out[email] = {"name": u.get("name") or u["user_id"], "portal_id": str(u.get("portal_id") or "").strip()}
    return out


def _aa_viewer_teams():
    """The teams the viewer may configure: an administrator every real team, a manager only their own."""
    teams = [t for t in hub_cfg()["email"]["teams"] if t.casefold() != "unclassified"]
    if viewer_is_admin():
        return teams
    scope = auth.team_scope()
    return teams if scope is None else [t for t in teams if overlay.team_in(scope, t)]


@bp.get("/auto-assign")
@auth.ops_view_required
def auto_assign_get():
    """The auto-assign config scoped to the viewer: each of their teams' applications, employees and email/ticketing rules."""
    cfg = _aa_config()
    out = {}
    for team in _aa_viewer_teams():
        members = _aa_team_members_full(team)
        out[team] = {"applications": auto_assign.team_applications(cfg, team),
                     "members": [{"email": e, "name": m["name"], "portal_id": m["portal_id"]}
                                 for e, m in sorted(members.items(), key=lambda kv: str(kv[1]["name"]).casefold())],
                     "email": auto_assign.team_block(cfg, team, "email"),
                     "ticketing": auto_assign.team_block(cfg, team, "ticketing")}
    return jsonify(teams=out, is_admin=viewer_is_admin(), server_time=auth.now().isoformat(timespec="seconds"),
                   all_teams=[t for t in hub_cfg()["email"]["teams"] if t.casefold() != "unclassified"],
                   applications=(cfg.get("applications") or []) if viewer_is_admin() else [])


@bp.put("/auto-assign/team")
@auth.ops_act_required
def auto_assign_put_team():
    body = json_body()
    team = _clean_team(body.get("team"))
    if not auth.may_act_on_team(team):
        raise ApiError("You can only configure auto-assign for your own team.", 403)
    kind = body.get("kind")
    if kind not in auto_assign.TARGETS:
        raise ApiError("kind must be 'email' or 'ticketing'.")
    members = _aa_team_members(team)
    # e-mail auto-assign has no application filter (every pending mail of the team goes to its members)
    apps = auto_assign.team_applications(_aa_config(), team) if kind == "ticketing" else []
    block = auto_assign.clean_block({"enabled": body.get("enabled"), "interval_minutes": body.get("interval_minutes"),
                                     "rules": body.get("rules")}, apps, members)

    def change(doc):
        doc.setdefault("teams", {}).setdefault(team, {})[kind] = block
    ops().update_auto_assign(change)
    auth.audit("ops_auto_assign_config", target=team,
               details={"kind": kind, "enabled": block["enabled"], "rules": len(block["rules"])})
    assigned = apply_email_auto_assign(only_teams=[team], actor=g.user, force=True) if kind == "email" else 0
    return jsonify(ok=True, block=block, assigned=assigned)


@bp.post("/auto-assign/apps")
@auth.admin_required
def auto_assign_add_app():
    body = json_body()
    name = clean_text(body.get("name"), "Application name", 80, required=True)
    team = _clean_team(body.get("team"))

    def change(doc):
        apps = [a for a in doc.get("applications") or [] if overlay._key(a.get("name")) != overlay._key(name)]
        apps.append({"name": name, "team": team})
        doc["applications"] = auto_assign.clean_applications(apps, hub_cfg()["email"]["teams"])
    saved = ops().update_auto_assign(change)
    auth.audit("ops_auto_assign_app", target=name, details={"op": "add", "team": team})
    return jsonify(ok=True, applications=saved.get("applications") or [])


@bp.delete("/auto-assign/apps")
@auth.admin_required
def auto_assign_del_app():
    body = optional_body()
    name = " ".join(str(body.get("name") or request.args.get("name") or "").split())
    if not name:
        raise ApiError("Say which application to remove.")

    def change(doc):
        doc["applications"] = [a for a in doc.get("applications") or [] if overlay._key(a.get("name")) != overlay._key(name)]
    saved = ops().update_auto_assign(change)
    auth.audit("ops_auto_assign_app", target=name, details={"op": "remove"})
    return jsonify(ok=True, applications=saved.get("applications") or [])


@bp.post("/auto-assign/apply")
@auth.ops_act_required
def auto_assign_apply():
    """Distribute pending e-mails now (a manager: their own team; an administrator: every team)."""
    return jsonify(ok=True, assigned=apply_email_auto_assign(only_teams=_aa_viewer_teams(), actor=g.user, force=True))


# ----------------------------------------------------------- ticketing auto-assign (the portal RPA, manager-triggered)
def _assign_settings(settings):
    """The config the assign engine runs with: the saved ``assign`` block, with the portal LOGIN basics taken from the
    ``sla`` block so an admin configures the SSO portal once (same site, same login). The package / page settings are
    NOT shared: assigning new requests lives in the supervisor package, not the SLA collector's Re-Assign package."""
    assign = dict(settings.get("assign") or {})
    sla = settings.get("sla") or {}
    for key in ("base_url", "login_path", "login_button_text", "selectors", "timeout_seconds", "browser_executable"):
        if sla.get(key) not in (None, "", {}):
            assign[key] = sla[key]
    return assign


def _ticketing_run_state():
    return current_app.extensions.setdefault("ops_ticketing_run", {"running": False})


TICKETING_LOOP_UNIT_SECONDS = 60          # one "minute" of the keep-running interval (tests shorten it)
TICKETING_LOOP_MAX_HOURS = 12             # a loop never outlives a working day
TICKETING_LOOP_MAX_FAILURES = 3           # this many failed runs in a row end the loop


@bp.get("/auto-assign/run-ticketing")
@auth.ops_act_required
def auto_assign_run_ticketing_status():
    """The live status of the current/last manual ticketing auto-assign run (polled by the page while it runs). A
    manager sees only a run of their own team(s)."""
    st = current_app.extensions.get("ops_ticketing_run") or {}
    scope = auth.team_scope()
    if scope is not None and not teams.in_scope(scope, st.get("team")):
        st = {}
    return jsonify(running=bool(st.get("running")), done=bool(st.get("done")), team=st.get("team", ""),
                   started_at=st.get("started_at", ""), log=list(st.get("log") or [])[-200:],
                   summary=st.get("summary", ""), assigned=int(st.get("assigned") or 0),
                   by_employee=st.get("by_employee") or {}, error=st.get("error", ""), dry_run=bool(st.get("dry_run")),
                   repeat=bool(st.get("repeat")), runs=int(st.get("runs") or 0), next_run_at=st.get("next_run_at", ""),
                   interval_minutes=int(st.get("interval_minutes") or 0), stopping=bool(st.get("stop")))


@bp.post("/auto-assign/run-ticketing/stop")
@auth.ops_act_required
def auto_assign_run_ticketing_stop():
    """Stop a "keep running" ticketing loop after its current run (the manager of its team, or an administrator)."""
    st = current_app.extensions.get("ops_ticketing_run") or {}
    if not st.get("running"):
        raise ApiError("No ticketing auto-assign is running.")
    if not auth.may_act_on_team(st.get("team")):
        raise ApiError("You can only stop your own team's auto-assign.", 403)
    st["stop"] = True
    auth.audit("ops_auto_assign_stop", target=st.get("team"), details={"kind": "ticketing"})
    return jsonify(ok=True)


@bp.post("/auto-assign/run-ticketing")
@auth.ops_act_required
def auto_assign_run_ticketing():
    """Run the ticketing auto-assign now for one team, with the MANAGER's portal credentials typed for this run only
    (never stored). The engine drives the portal in a background thread; the page polls the GET above for progress.

    ``repeat``: keep running, like the bank's stand-alone bot - a run every ``interval_minutes`` (the team's setting)
    until the manager presses Stop, the working hours end, the password is refused, or 3 runs in a row fail. The
    password then stays in this loop's memory only: never in the status, the log, the audit or a file, and a server
    restart forgets it (the manager presses Run now again)."""
    body = json_body()
    team = _clean_team(body.get("team"))
    if not auth.may_act_on_team(team):
        raise ApiError("You can only run auto-assign for your own team.", 403)
    username = clean_text(body.get("username"), "Portal username", 60, required=True)
    password = str(body.get("password") or "")
    if not password:
        raise ApiError("Type your portal password for this run.")
    dry_run = bool(body.get("dry_run"))
    repeat = bool(body.get("repeat")) and not dry_run           # a dry run shows one pass; it never loops
    block = auto_assign.team_block(_aa_config(), team, "ticketing")
    if not block.get("enabled") or not block.get("rules"):
        raise ApiError("Ticketing auto-assign is off for this team, or has no rules. Enable it and save rules first.")
    members = _aa_team_members_full(team)
    if not any(m.get("portal_id") for m in members.values()):
        raise ApiError("No employee of this team has a portal number yet. Set “Number in the security portal” in "
                       "Admin › Users for the people who should receive requests.")
    assign_settings = _assign_settings(hub_cfg())
    hub = hub_cfg()
    interval = max(1, int(block.get("interval_minutes") or auto_assign.DEFAULT_INTERVAL_MINUTES))
    store, data_dir, clock = ops(), str(ops().root.parent), current_app.config["CLOCK"]
    app = current_app._get_current_object()
    actor = g.user["user_id"]

    with _TICKETING_LOCK:
        st = _ticketing_run_state()
        if st.get("running"):
            raise ApiError("A ticketing auto-assign run is already in progress. Wait for it to finish.", 409)
        st.clear()
        st.update({"running": True, "team": team, "actor": actor, "started_at": now_iso(clock()),
                   "log": [], "done": False, "summary": "", "assigned": 0, "by_employee": {}, "error": "",
                   "dry_run": dry_run, "repeat": repeat, "runs": 0, "next_run_at": "", "interval_minutes": interval,
                   "stop": False})
    auth.audit("ops_auto_assign_run", target=team, details={"kind": "ticketing", "dry_run": dry_run, "repeat": repeat})
    teams_arg = {team: {"block": block, "members": members}}

    def worker():
        from .engines import assign_engine

        def log(message):
            state = app.extensions.get("ops_ticketing_run")
            if state is not None:
                state.setdefault("log", []).append(f"{clock().strftime('%H:%M:%S')} {message}")
                del state["log"][:-300]

        def once():
            ctx = common.JobContext(job="assign", settings=assign_settings, general={}, store=store, now=clock, log=log,
                                    debug=False, dry_run=dry_run, headed=False, data_dir=data_dir,
                                    options={"teams": teams_arg, "username": username, "password": password,
                                             "email_settings": hub.get("email") or {}})
            try:
                return assign_engine.run(ctx)
            except Exception as exc:                              # noqa: BLE001 - the thread must always settle the state
                return common.JobResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        state = app.extensions.get("ops_ticketing_run") or {}
        started, failures, total, by_emp = time.monotonic(), 0, 0, {}
        while True:
            result = once()
            data = result.snapshot_data or {}
            total += int(data.get("assigned") or 0)
            for name, n in (data.get("by_employee") or {}).items():
                by_emp[name] = by_emp.get(name, 0) + int(n or 0)
            failures = 0 if result.ok else failures + 1
            state.update({"runs": int(state.get("runs") or 0) + 1, "assigned": total, "by_employee": by_emp,
                          "summary": result.summary or "", "error": result.error or ""})
            why = ""
            if not repeat:
                break
            if state.get("stop"):
                why = "stopped by the manager"
            elif result.error_kind == "auth":
                why = "the portal refused the login - press Run now and type the password again"
            elif failures >= TICKETING_LOOP_MAX_FAILURES:
                why = f"{failures} runs in a row failed"
            elif not hub_settings.is_active(hub, "sla", clock()):
                why = "the working hours are over"
            elif time.monotonic() - started > TICKETING_LOOP_MAX_HOURS * 3600:
                why = f"it ran {TICKETING_LOOP_MAX_HOURS} hours"
            if why:
                log(f"keep running ended: {why}")
                state["summary"] = f"{state['runs']} run(s), assigned {total} in all - {why}"
                break
            state["next_run_at"] = now_iso(clock() + timedelta(minutes=interval))
            log(f"next run in {interval} min (keep running until stopped)")
            end = time.monotonic() + interval * TICKETING_LOOP_UNIT_SECONDS
            while time.monotonic() < end and not state.get("stop"):
                time.sleep(min(1.0, TICKETING_LOOP_UNIT_SECONDS / 4))
            if state.get("stop"):
                log("keep running ended: stopped by the manager")
                state["summary"] = f"{state['runs']} run(s), assigned {total} in all - stopped by the manager"
                break
        state.update({"running": False, "done": True, "next_run_at": ""})

    threading.Thread(target=worker, name="ticketing-assign", daemon=True).start()
    return jsonify(ok=True, started=True)


def apply_email_auto_assign(only_teams=None, actor=None, force=False):
    """Hand the pending, still-unassigned e-mails of every auto-assign-enabled team to its employees, by the rules.

    Writes normal assignment overrides so a distributed mail looks assigned everywhere, and skips an already-assigned
    one, so the background refresher may call it often (a short time throttle keeps the no-op case cheap)."""
    snap, _ = read_snapshot("email")
    if not snap:
        return 0
    memo = current_app.extensions.setdefault("ops_auto_assign", {"at": 0.0, "team_at": {}})
    team_at = memo.setdefault("team_at", {})
    if not force and (time.monotonic() - memo.get("at", 0.0)) < 10:      # cheap no-op guard between refreshes
        return 0
    memo["at"] = time.monotonic()
    cfg = ops().read_auto_assign()
    ovr, rev = current_overrides()
    rows = overlaid_rows(snap, ovr, rev)
    now = auth.now()
    load = {}
    for r in rows:
        e = overlay.normalize_email(r.get("assigned_to_email"))
        if e:
            load[e] = load.get(e, 0) + 1
    teams = only_teams if only_teams is not None else [t for t in hub_cfg()["email"]["teams"] if t.casefold() != "unclassified"]
    assigned = 0
    for team in teams:
        block = auto_assign.team_block(cfg, team, "email")
        members = _aa_team_members(team)
        if not block.get("enabled") or not members:
            continue
        if not force:                                   # run a team no more often than the interval the manager set
            last = team_at.get(team, 0.0)
            if last and (time.monotonic() - last) < block.get("interval_minutes", auto_assign.DEFAULT_INTERVAL_MINUTES) * 60:
                continue
        team_at[team] = time.monotonic()
        team_rows = [r for r in rows if overlay.row_in_team(r, team)]
        for item in auto_assign.plan_email(block, team_rows, now, members, load):
            mid, email, name = item["message_id"], item["email"], item["name"]
            row = next((r for r in team_rows if r.get("message_id") == mid), None)
            subject = str((row or {}).get("subject") or "")[:200]
            done = {}

            def change(old, email=email, name=name, subject=subject, team=team, done=done):
                if old and overlay.normalize_email(old.get("assigned_to_email")):
                    return old                                  # assigned by someone in the meantime
                done["new"] = True
                return {"assigned_to_email": email, "assigned_to_name": name, "note": "", "marked_by": "Auto-assign",
                        "marked_by_id": "auto", "marked_at": now_iso(now), "subject": subject, "team": team, "notified": False}
            entry = ops().update_override("assignments", mid, change)
            if done.get("new"):
                assigned += 1
                load[email] = load.get(email, 0) + 1
                try:
                    notifications.notify(ops(), auth.store().users(), schemas.NOTIFY_ASSIGNED, actor, entry,
                                         [row] if row else [], subject, mid, now)
                except Exception as exc:                        # noqa: BLE001 - a bell failure never undoes an assignment
                    current_app.logger.warning("auto-assign notify failed: %s", exc)
    if assigned:
        auth.audit("ops_auto_assign_apply", details={"assigned": assigned, "teams": teams[:20]}, actor=actor)
    return assigned


# ----------------------------------------------------------------------------- jobs
def _sanitize_info(value):
    """Defence in depth: whatever the collector wrote, no secret leaves the server."""
    if isinstance(value, dict):
        return {k: _sanitize_info(v) for k, v in value.items() if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_sanitize_info(v) for v in value]
    return value


def collector_info():
    try:
        info = ops().read_state("collector_info", {})
    except (StorageError, ValueError):
        info = {}
    return _sanitize_info(info) if isinstance(info, dict) else {}


@bp.get("/jobs")
@auth.admin_required
def jobs():
    pending = ops().pending_run_requests()
    return jsonify(jobs=job_states(), pending=pending, collector_info=collector_info(),
                   server_time=auth.now().isoformat(timespec="seconds"))


def _known_job(job):
    if not schemas.valid_job(job):
        raise ApiError("Unknown job.", 404)
    return job


@bp.post("/jobs/<job>/run")
@auth.admin_required
def job_run(job):
    """Run now / refresh a collector job — administrators only (managers act on e-mails and tickets, they do not run scans)."""
    _known_job(job)
    if ops().pending_run_requests(job):
        return jsonify(queued=True, already_pending=True)
    name = ops().add_run_request(job, g.user["user_id"])
    auth.audit("ops_run_job", target=job, details={"request": name})
    return jsonify(queued=True, already_pending=False, request=name)


@bp.get("/jobs/<job>/log")
@auth.admin_required
def job_log(job):
    _known_job(job)
    try:
        lines = int(request.args.get("lines", 200))
    except ValueError:
        raise ApiError("lines must be a number.")
    return jsonify(job=job, lines=ops().tail_log(job, lines))


# ----------------------------------------------------------------------------- settings
def _secret_paths(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            sub = f"{path}.{key}" if path else str(key)
            # sla.selectors.password is the CSS selector of the password box, not a password
            if str(key).lower() in _SECRET_KEYS and not path.endswith("selectors"):
                found.append(sub)
            found += _secret_paths(item, sub)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += _secret_paths(item, f"{path}[{i}]")
    return found


def _changed_paths(old, new, prefix=""):
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            out += _changed_paths(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else str(key))
        return out
    return [] if old == new else [prefix]


@bp.get("/settings")
@auth.admin_required
def settings_get():
    from .engines import email_rules
    info = collector_info()
    return jsonify(settings=hub_cfg(), pinned_keys=list(info.get("pinned_keys") or []), collector_info=info,
                   weekdays=hub_settings.WEEKDAYS, builtin_system_team_map=email_rules.builtin_system_team_map())


@bp.put("/settings")
@auth.admin_required
def settings_put():
    body = json_body()
    doc = body["settings"] if isinstance(body.get("settings"), dict) else body
    secrets_found = _secret_paths(doc)
    if secrets_found:
        message = "Passwords are never stored in the settings - put them in collector_config.json on the collector PC."
        return jsonify(error=message, errors=[{"path": p, "message": message} for p in secrets_found]), 400
    before = hub_cfg()
    try:
        saved = hub_settings.save(ops(), doc)
    except hub_settings.SettingsError as exc:
        return jsonify(error="Some settings are not valid: " + "; ".join(
            f"{e['path']}: {e['message']}" for e in exc.errors[:3]), errors=exc.errors), 400
    forget_settings()
    changed = _changed_paths(before, saved)
    auth.audit("ops_settings_saved", details={"changed": changed[:40], "count": len(changed)})
    return jsonify(ok=True, settings=saved, changed=changed)


# ----------------------------------------------------------------------------- reports, health
@bp.get("/reports")
@auth.ops_view_required
def reports():
    module = request.args.get("module") or None
    if module and not schemas.valid_module(module):
        raise ApiError("Unknown module.", 404)
    # the collector's report files cover every team: an administrator and a manager of every team only
    items = ops().list_reports(module) if auth.team_scope() is None else []
    return jsonify(reports=[{**r, "url": f"/export/ops/report/{r['module']}/{r['name']}"} for r in items])


@bp.get("/health")
@auth.admin_required
def health_report():
    return jsonify(health.collect(current_app.config["SETTINGS"]["data_dir"], ops(), auth.store(), hub_cfg(), auth.now()))
