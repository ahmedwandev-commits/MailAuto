"""Who did what: one day of each employee's work - the calls they logged, the e-mails they replied to and the security
requests they handled (executed, not executed, rejected) - for Insights › Analysis' "Best performers", the Best
Performance e-mail (the top 3, engines/best_performer.py) and the day-by-day log. Every number comes from the same
place as the module's own page:

* calls - the Call Log (one logged call = one call answered);
* e-mails - today: the live e-mail view (every override applied), a reply counted on the day it was sent, by the
  replier's address; an earlier day: the per-person replies the e-mail report kept that day (``replies`` history);
* tickets - the day's requests (by «تاريخ الطلب») each security user executed, did not execute or rejected, as
  the portal's inquiry lists them (``sla_history.status_numbers``: the same counting as the SLA Analysis and the
  daily summary), linked to their account by the number the status history shows, else by their security name.

The day's score is counted in security requests (the owner's rule, ``WEIGHTS``): a call answered counts as 2
requests, an e-mail replied as 3, a request handled as 1.

A manager gets their own team(s) only (``scope``); Third Party is left out of the tickets as everywhere."""
from datetime import date, timedelta

from . import overlay, schemas, sla_history, sla_records, teams

WEIGHTS = {"calls": 2, "emails": 3, "tickets": 1}     # one call = 2 security requests, one e-mail replied = 3


def _resolver(store, hub, users):
    apps = []
    try:
        apps = (store.read_auto_assign() or {}).get("applications") or []
    except Exception:                                       # noqa: BLE001 - an unreadable list: no application teams
        pass
    return teams.Resolver(hub, users, apps)


def sla_scope(store, hub, users, day_from, day_to, scope=None):
    """The SLA history ``scope`` (None = every team) may see over ``day_from``..``day_to``: {records (closed on those
    days or up to two weeks later, and every open one), grid, index, cfg, live, resolver, people, available}. A
    request's team is its application's (``teams.sla_record_team``: never the team of the person who executed or
    holds it) - the SLA Analysis' rule; Third Party left out; a team merged into another (``sla.merge_teams``) as that
    team, and a scope of the merged team sees it whole."""
    sla_cfg = hub.get("sla") or {}
    resolver = _resolver(store, hub, users)
    scope = resolver.sla_scope(scope)
    excluded = teams.sla_exclusion(sla_cfg, resolver) or (lambda system: False)
    people = teams.sla_people(users)
    out_teams = {str(t).strip().casefold() for t in sla_cfg.get("exclude_teams") or [] if str(t).strip()}
    shown = lambda t: teams.in_scope(scope, t) and str(t).casefold() not in out_teams   # noqa: E731 - never Third Party
    live = []
    snap = store.read_snapshot("sla")
    if snap:
        live = [r for r in (snap.get("data") or {}).get("requests") or [] if isinstance(r, dict)
                and not excluded(teams.sla_system(r))
                and teams.in_scope(scope, resolver.sla_team(teams.sla_system(r), r.get("security_user")))]
    cfg = sla_records.clean_config((store.read_sla_records() or {}).get("config"))
    state = store.read_sla_history_state() or {}
    reach = (date.fromisoformat(day_to) + timedelta(days=14)).isoformat() if day_to else ""
    raw = {}
    for day in store.sla_closed_days():
        if day >= day_from and (not reach or day <= reach):
            raw.update(store.read_sla_closed_day(day) or {})
    raw.update(state.get("open") or {})
    records = {}
    for k, r in raw.items():
        r = sla_history.rederive(r, cfg)
        if excluded(r.get("system")):
            continue
        t = teams.sla_record_team(r.get("system"), resolver)
        if shown(t):
            records[k] = {**r, "team": t, "teams": [t]}
    number_of = {}
    for r in records.values():
        for name, no in ((r.get("executor"), r.get("executor_no")), (r.get("checker"), r.get("checker_no")),
                         (r.get("assigner"), r.get("assigner_no")), (r.get("rejected_by"), r.get("rejected_by_no"))):
            if name and no:
                number_of.setdefault(sla_history._norm(name), no)
    grid = {}
    for key, g in (state.get("grid") or {}).items():
        if excluded(g.get("system")):
            continue
        t = teams.sla_record_team(g.get("system"), resolver)
        if shown(t):
            grid[key] = {**g, "team": t}
    return {"records": records, "grid": grid, "index": state.get("index") or {}, "cfg": cfg, "live": live,
            "resolver": resolver, "people": people, "number_of": number_of,
            "available": bool(state.get("last_history") or raw), "last_history": state.get("last_history", "")}


def _accounts_in(users, scope):
    """The active employees (role "user") ``scope`` may see, by user ID."""
    out = {}
    for uid, u in (users or {}).items():
        if not u.get("active", True) or u.get("role") != "user":
            continue
        mine = [t for t in overlay.user_teams(u) if teams._real_team(t)]
        if scope is None or any(teams.in_scope(scope, t) for t in mine):
            out[str(uid)] = u
    return out


def _calls(calls_store, day):
    """{user ID: calls logged on ``day``} (``calls_store``: the Call Log ``Store``, read only)."""
    out = {}
    for r in calls_store.day_all(day):
        uid = str(r.get("user_id") or "")
        if uid:
            out[uid] = out.get(uid, 0) + 1
    return out


def email_replies_of(rows, day):
    """{replier address (lower case): e-mails replied on ``day``} from e-mail rows."""
    out = {}
    for r in rows or []:
        if r.get("status") != schemas.EMAIL_REPLIED or str(r.get("first_reply_time") or "")[:10] != day:
            continue
        who = str(r.get("first_reply_email") or "").strip().lower()
        if who:
            out[who] = out.get(who, 0) + 1
    return out


def _emails(store, day, now):
    """{address: replies on ``day``} - None when that day's e-mails are not known (no scan, no history)."""
    snap = store.read_snapshot("email")
    if snap and str(snap.get("date") or "")[:10] == day:
        ovr = {name: store.overrides(name) for name in schemas.OVERRIDES}
        view = overlay.view_email_data(snap.get("data") or {}, ovr, None, now)
        return email_replies_of(view.get("rows"), day)
    kept = (store.read_history("email", day) or {}).get("replies")
    return dict(kept) if isinstance(kept, dict) else None


def people_day(store, calls_store, hub, users, day, now, scope=None, top=None):
    """One day of each employee ``scope`` may see: [{user_id, name, team, calls, emails, executed, not_executed,
    rejected, tickets, total, exec_median_minutes, rank}] best first: ``total`` is the day's score in security
    requests (``WEIGHTS``: tickets handled + 2 per call + 3 per e-mail replied); ties by tickets, then calls.
    ``emails`` is None for every row when that day's e-mails are not known. Employees with nothing that day are left
    out."""
    accounts = _accounts_in(users, scope)
    calls = _calls(calls_store, day)
    emails = _emails(store, day, now)
    tickets = {}
    sc = sla_scope(store, hub, users, day, day, scope)
    if sc["available"]:                                   # the day's requests each security user handled
        nums = sla_history.status_numbers(sc["grid"], sc["records"], sc["cfg"], day, day)
        by_no, by_name = sc["people"]
        display = {sla_history._norm(u.get("name")): u for u in accounts.values() if u.get("name")}
        for row in nums["by_person"]:
            key = sla_history._norm(row["person"])
            acct = by_no.get(str(sc["number_of"].get(key) or "")) or by_name.get(key) or display.get(key)
            uid = str((acct or {}).get("user_id") or "")
            if uid in accounts:
                t = tickets.setdefault(uid, {"executed": 0, "not_executed": 0, "rejected": 0, "_m": []})
                for k in ("executed", "not_executed", "rejected"):
                    t[k] += row[k]
                if row.get("exec_median_minutes") is not None:
                    t["_m"].append(row["exec_median_minutes"])
    rows = []
    for uid, u in accounts.items():
        c = calls.get(uid, 0)
        e = (emails or {}).get(str(u.get("email") or "").strip().lower(), 0) if emails is not None else None
        t = tickets.get(uid) or {"executed": 0, "not_executed": 0, "rejected": 0, "_m": []}
        done = t["executed"] + t["not_executed"] + t["rejected"]
        total = c * WEIGHTS["calls"] + (e or 0) * WEIGHTS["emails"] + done * WEIGHTS["tickets"]
        if not total:
            continue
        mine = [x for x in overlay.user_teams(u) if teams._real_team(x)]
        rows.append({"user_id": uid, "name": u.get("name") or uid, "team": mine[0] if mine else "", "calls": c,
                     "emails": e, "executed": t["executed"], "not_executed": t["not_executed"],
                     "rejected": t["rejected"], "tickets": done, "total": total,
                     "exec_median_minutes": min(t["_m"]) if len(t["_m"]) == 1 else (
                         round(sum(t["_m"]) / len(t["_m"]), 1) if t["_m"] else None)})
    rows.sort(key=lambda r: (-r["total"], -r["tickets"], -r["calls"], r["name"].casefold()))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows[:top] if top else rows


# ------------------------------------------------------------------------- the day-by-day log (kept for good)
COUNT_KEYS = ("came", "open", "awaiting_executed", "awaiting_not", "confirmed_executed", "confirmed_not", "rejected",
              "executed", "not_executed", "waiting", "confirmed")


def save_day_log(store, calls_store, hub, users, now):
    """Keep, at every scan, the numbers of each day of the inquiry window (its requests by «تاريخ الطلب», where each
    stands now - ``sla_history.status_numbers``: every team's, each team, each security user, the times) in that
    day's SLA history file (``day_log``), and today's best performers: a day is kept as it last was while the inquiry
    still read it, so performance can be followed day by day. Returns today's log (None without an SLA history)."""
    today = now.date().isoformat()
    state = store.read_sla_history_state() or {}
    first = str(state.get("grid_from") or today)[:10]
    sc = sla_scope(store, hub, users, first, today, None)
    if not sc["available"]:
        return None
    live_team = lambda r: sc["resolver"].sla_team(teams.sla_system(r), r.get("security_user"))   # noqa: E731
    out, day = None, date.fromisoformat(min(first, today))
    while day.isoformat() <= today:
        d = day.isoformat()
        nums = sla_history.status_numbers(sc["grid"], sc["records"], sc["cfg"], d, d,
                                          live=sc["live"] if d == today else None, live_team=live_team)
        log = {"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "totals": {k: nums[k] for k in COUNT_KEYS},
               "times": nums["times"], "by_team": nums["by_team"], "by_person": nums["by_person"]}
        if d == today:
            log["totals"]["now"] = nums["now"]
            log["people"] = people_day(store, calls_store, hub, users, d, now, top=50)
            out = log
        else:                                             # an earlier day: its best performers stay as kept that day
            log["people"] = ((store.read_history("sla", d) or {}).get("day_log") or {}).get("people") or []
        store.update_history("sla", d, lambda doc, log=log: doc.update(day_log=log))
        day += timedelta(days=1)
    return out


def _sum_rows(rows, key):
    """Team or person rows of several days added up by ``key`` (counts exact, the execution average weighted)."""
    out = {}
    for r in rows:
        name = r.get(key)
        t = out.setdefault(sla_history._norm(name) if key == "person" else name,
                           {key: name, **{k: 0 for k in COUNT_KEYS}, "now": 0, "exec_n": 0, "_sum": 0.0, "exec_within": 0,
                            **({"team": r.get("team")} if key == "person" else {})})
        for k in COUNT_KEYS:
            t[k] += int(r.get(k) or 0)
        n = int(r.get("exec_n") or 0)
        t["exec_n"] += n
        t["_sum"] += (r.get("exec_average_minutes") or 0) * n
        t["exec_within"] += int(r.get("exec_within") or 0)
    for t in out.values():
        s, n = t.pop("_sum"), t["exec_n"]
        t.update(exec_average_minutes=round(s / n, 1) if n else None, exec_median_minutes=None,
                 exec_within_pct=round(100.0 * t["exec_within"] / n, 1) if n else None)
    return list(out.values())


def _merged_rows(rows, merges):
    """Kept team / person rows with a merged team (``sla.merge_teams``) under the team it is merged into - a day kept
    before the merge counts like the days after it."""
    return [{**r, "team": teams.merged_team(r.get("team"), merges)} if merges else r for r in rows or []]


def add_logged_status(glance, store, day_from, day_to, scope=None, merges=None):
    """Add to ``glance`` (``status_numbers``) the kept numbers of the days ``day_from``..``day_to`` (older than the
    inquiry window), as ``scope`` may see them (``merges`` = ``teams.team_merges`` of the settings)."""
    teams_rows, people_rows = [], []
    scope = teams.merged_scope(scope, merges)
    for day in store.history_days("sla"):
        if day < day_from or day > day_to:
            continue
        log = (store.read_history("sla", day) or {}).get("day_log") or {}
        teams_rows += [r for r in _merged_rows(log.get("by_team"), merges) if teams.in_scope(scope, r.get("team"))]
        people_rows += [r for r in _merged_rows(log.get("by_person"), merges) if teams.in_scope(scope, r.get("team"))]
    if not teams_rows:
        return glance
    for k in COUNT_KEYS:
        glance[k] = glance.get(k, 0) + sum(int(r.get(k) or 0) for r in teams_rows)
    glance["by_team"] = _sum_rows(list(glance.get("by_team") or []) + teams_rows, "team")
    glance["by_person"] = _sum_rows(list(glance.get("by_person") or []) + people_rows, "person")
    glance["logged_days"] = sorted({d for d in store.history_days("sla") if day_from <= d <= day_to})
    return glance


def _team_sum(rows, scope):
    """Totals and execution time of the team rows ``scope`` may see (exact: counts add up, the average is weighted)."""
    mine = [r for r in rows or [] if teams.in_scope(scope, r.get("team"))]
    out = {k: sum(int(r.get(k) or 0) for r in mine) for k in COUNT_KEYS}
    n = sum(int(r.get("exec_n") or 0) for r in mine)
    total = sum((r.get("exec_average_minutes") or 0) * int(r.get("exec_n") or 0) for r in mine)
    out.update(exec_n=n, exec_average_minutes=round(total / n, 1) if n else None,
               exec_within_pct=round(100.0 * sum(int(r.get("exec_within") or 0) for r in mine) / n, 1) if n else None)
    return out


def read_day_logs(store, day_from, day_to, scope=None, person="", merges=None):
    """The kept days between ``day_from`` and ``day_to`` (newest first) as ``scope`` may see them: [{day, at, came,
    executed, not_executed, rejected, waiting, confirmed, open, exec_average_minutes, exec_within_pct, best}] - every
    team's totals, a manager's own teams summed, or (``person``) one security user's own row. ``merges`` =
    ``teams.team_merges`` of the settings: a merged team's rows count as the team it is merged into."""
    want = sla_history._norm(person) if person else ""
    sla_scope = teams.merged_scope(scope, merges)
    out = []
    for day in reversed(store.history_days("sla")):
        if (day_from and day < day_from) or (day_to and day > day_to):
            continue
        log = (store.read_history("sla", day) or {}).get("day_log")
        if not isinstance(log, dict) or "came" not in (log.get("totals") or {}):
            continue                                      # an older kind of log: not comparable
        if want:
            row = next((r for r in _merged_rows(log.get("by_person"), merges)
                        if sla_history._norm(r.get("person")) == want), None)
            if row is None or (scope is not None and not teams.in_scope(sla_scope, row.get("team"))):
                continue
            nums = {k: row.get(k, 0) for k in COUNT_KEYS}
            nums.update(exec_n=row.get("exec_n", 0), exec_average_minutes=row.get("exec_average_minutes"),
                        exec_within_pct=row.get("exec_within_pct"))
        elif scope is None:
            t, ex = log.get("totals") or {}, (log.get("times") or {}).get("execution") or {}
            nums = {k: t.get(k, 0) for k in COUNT_KEYS}
            nums.update(exec_n=ex.get("n", 0), exec_average_minutes=ex.get("average_minutes"),
                        exec_within_pct=ex.get("within_pct"))
        else:
            nums = _team_sum(_merged_rows(log.get("by_team"), merges), sla_scope)
        best = next((p for p in log.get("people") or []
                     if scope is None or teams.in_scope(scope, p.get("team"))), None)
        out.append({"day": day, "at": log.get("at", ""), **nums,
                    "best": {"name": best["name"], "total": best["total"]} if best else None})
    return out
