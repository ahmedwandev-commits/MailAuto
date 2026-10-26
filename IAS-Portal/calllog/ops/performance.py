"""Who did what: one day of each employee's work - the calls they logged, the e-mails they replied to and the security
requests they handled (executed, not executed, rejected) - for the daily summary's "Best performers" (at each
checkpoint) and Insights › Analysis. Every number comes from the same place as the module's own page:

* calls - the Call Log (one logged call = one call answered);
* e-mails - today: the live e-mail view (every override applied), a reply counted on the day it was sent, by the
  replier's address; an earlier day: the per-person replies the e-mail report kept that day (``replies`` history);
* tickets - ``sla_history.period_numbers`` over the SLA history (the same counting as the SLA Analysis and the
  daily summary's tickets), each security user linked to their account by the number the status history shows,
  else by their security name.

A manager gets their own team(s) only (``scope``); Third Party is left out of the tickets as everywhere."""
from datetime import date, timedelta

from . import overlay, schemas, sla_history, sla_records, teams


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
    request's team is the team of the person who executed or holds it (their account), else its application's -
    the SLA Analysis' rule; Third Party left out."""
    sla_cfg = hub.get("sla") or {}
    resolver = _resolver(store, hub, users)
    excluded = teams.sla_exclusion(sla_cfg, resolver) or (lambda system: False)
    people = teams.sla_people(users)
    team_of = lambda system, number="", name="": teams.sla_person_team(system, number, name, people, resolver)  # noqa: E731
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
        number = r.get("executor_no") if r.get("executor_no") or r.get("executor") else ""
        t = team_of(r.get("system"), number, r.get("executor") or r.get("holder"))
        if teams.in_scope(scope, t):
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
        holder = g.get("holder") or ""
        t = team_of(g.get("system"), number_of.get(sla_history._norm(holder), ""), holder)
        if teams.in_scope(scope, t):
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
    rejected, tickets, total, exec_median_minutes, rank}] best first (the most work done: calls + e-mails replied +
    tickets handled; ties by tickets, then calls). ``emails`` is None for every row when that day's e-mails are not
    known. Employees with nothing that day are left out."""
    accounts = _accounts_in(users, scope)
    calls = _calls(calls_store, day)
    emails = _emails(store, day, now)
    tickets = {}
    sc = sla_scope(store, hub, users, day, day, scope)
    if sc["available"]:
        nums = sla_history.period_numbers(sc["records"], sc["grid"], sc["index"], sc["cfg"], day, day,
                                          now.strftime("%Y-%m-%dT%H:%M:%S"), live=sc["live"])
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
        total = c + (e or 0) + done
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
