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
    grid, left_grid = {}, {}                              # left_grid: the requests left out of the SLA, counted only
    for key, g in (state.get("grid") or {}).items():
        t = teams.sla_record_team(g.get("system"), resolver)
        if excluded(g.get("system")) or str(t).casefold() in out_teams:
            if teams.in_scope(scope, t):
                left_grid[key] = {**g, "team": t}
        elif shown(t):
            grid[key] = {**g, "team": t}
    left = (snap.get("data") or {}).get("left_out") if snap else None
    left_now = {t: int(n or 0) for t, n in ((left or {}).get("by_team") or {}).items() if teams.in_scope(scope, t)}
    left_teams = [str(t).strip() for t in sla_cfg.get("exclude_teams") or [] if str(t).strip() and teams.in_scope(scope, t)]
    return {"records": records, "grid": grid, "index": state.get("index") or {}, "cfg": cfg, "live": live,
            "resolver": resolver, "people": people, "number_of": number_of,
            "left_grid": left_grid, "left_now": left_now, "left_teams": left_teams,
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


def day_numbers(store, hub, users, day):
    """The day's security requests per person and per team ({"available", "sc", "by_person", "by_team"}), read ONCE for
    Best performers and the Best team (the SLA history is big: reading it twice doubles the page time)."""
    sc = sla_scope(store, hub, users, day, day, None)
    if not sc["available"]:
        return {"available": False, "sc": sc, "by_person": [], "by_team": []}
    window = str((store.read_sla_history_state() or {}).get("grid_from") or "")[:10]
    if window and day < window:                           # older than the inquiry: the numbers kept that day
        log = (store.read_history("sla", day) or {}).get("day_log") or {}
        return {"available": True, "sc": sc, "by_person": [fold_rejected(r) for r in log.get("by_person") or []],
                "by_team": log.get("by_team") or []}
    nums = sla_history.status_numbers(sc["grid"], sc["records"], sc["cfg"], day, day)
    return {"available": True, "sc": sc, "by_person": nums["by_person"], "by_team": nums["by_team"]}


def people_day(store, calls_store, hub, users, day, now, scope=None, top=None, numbers=None):
    """One day of each employee ``scope`` may see: [{user_id, name, team, calls, emails, executed, not_executed,
    rejected, tickets, total, exec_median_minutes, rank}] best first: ``total`` is the day's score in security
    requests (``WEIGHTS``: tickets handled + 2 per call + 3 per e-mail replied); ties by tickets, then calls.
    ``emails`` is None for every row when that day's e-mails are not known. Employees with nothing that day are left
    out."""
    accounts = _accounts_in(users, scope)
    calls = _calls(calls_store, day)
    emails = _emails(store, day, now)
    tickets = {}
    # the owner's rule: an employee's score counts EVERY request they handled (as the Best Performance e-mail does); the
    # scope only picks which employees are listed
    numbers = numbers or day_numbers(store, hub, users, day)
    sc = numbers["sc"]
    if numbers["available"]:                              # the day's requests each security user handled
        person_rows = numbers["by_person"]
        by_no, by_name = sc["people"]
        display = {sla_history._norm(u.get("name")): u for u in accounts.values() if u.get("name")}
        for row in person_rows:
            key = sla_history._norm(row["person"])
            acct = by_no.get(str(sc["number_of"].get(key) or "")) or by_name.get(key) or display.get(key)
            uid = str((acct or {}).get("user_id") or "")
            if uid in accounts:
                t = tickets.setdefault(uid, {"executed": 0, "not_executed": 0, "rejected": 0, "_m": []})
                for k in ("executed", "not_executed", "rejected"):
                    t[k] += int(row.get(k) or 0)
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


# ------------------------------------------------------------------------- the best team of the day (owner, v5.18)
EMAIL_SLA_OK, EMAIL_SLA_BAD = ("Met", "Within SLA"), ("Breached",)


def email_team_sla_of(rows, day):
    """{team: {"ok": n, "breached": n}} - the SLA of each team's e-mails of ``day`` (received or replied that day): a
    reply in time or a mail still inside its time is ok, a late reply or a mail waiting past it is breached."""
    out = {}
    for r in rows or []:
        team, st = str(r.get("team") or "").strip(), r.get("sla_status")
        if not teams._real_team(team) or st not in EMAIL_SLA_OK + EMAIL_SLA_BAD:
            continue
        if day not in (str(r.get("received_time") or "")[:10], str(r.get("first_reply_time") or "")[:10]):
            continue
        t = out.setdefault(team, {"ok": 0, "breached": 0})
        t["ok" if st in EMAIL_SLA_OK else "breached"] += 1
    return out


def _email_team_sla(store, day, now):
    """``email_team_sla_of`` for ``day``: today from the live e-mail view, an earlier day as the e-mail report kept it."""
    snap = store.read_snapshot("email")
    if snap and str(snap.get("date") or "")[:10] == day:
        ovr = {name: store.overrides(name) for name in schemas.OVERRIDES}
        view = overlay.view_email_data(snap.get("data") or {}, ovr, None, now)
        return email_team_sla_of(view.get("rows"), day)
    kept = (store.read_history("email", day) or {}).get("team_sla")
    return dict(kept) if isinstance(kept, dict) else {}


def _ticket_team_sla(store, hub, users, day, numbers=None):
    """{team: {"within": n, "timed": n, "median": minutes}} - the day's requests each team executed inside / outside
    the execution target (the SLA Analysis' numbers; a day older than the inquiry as it was kept)."""
    numbers = numbers or day_numbers(store, hub, users, day)
    out = {}
    for r in numbers["by_team"]:
        n = int(r.get("exec_n") or 0)
        if r.get("team") and n:
            out[r["team"]] = {"within": int(r.get("exec_within") or 0), "timed": n, "median": r.get("exec_median_minutes")}
    return out


def team_day(store, calls_store, hub, users, day, now, people=None, numbers=None):
    """The teams of ``day``, best first (owner, v5.18): a team of 2 that answered 100 calls beats a team of 100 that
    answered 200 - the work is counted PER PERSON - and a team that keeps its SLA beats one that does not:

    * ``work`` = the team's score in security requests (the same as Best performers: requests handled + 2 per call
      answered + 3 per e-mail replied), ``manpower`` = the team's active employees, ``per_person`` = work / manpower;
    * ``sla_pct`` = the share of the team's e-mails (``email_team_sla_of``) and requests (executed inside the target)
      of the day that kept their SLA; None when the team had neither;
    * ``score`` = per_person × sla_pct / 100 (per_person alone when nothing of the team had an SLA that day).

    Every team is listed (only totals, never a person). ``people`` = ``people_day`` rows already read (every team),
    ``numbers`` = ``day_numbers`` already read."""
    if people is None:
        numbers = numbers or day_numbers(store, hub, users, day)
        people = people_day(store, calls_store, hub, users, day, now, numbers=numbers)
    size = {}
    for u in _accounts_in(users, None).values():
        mine = [t for t in overlay.user_teams(u) if teams._real_team(t)]
        if mine:
            size[mine[0]] = size.get(mine[0], 0) + 1
    work = {}
    for p in people:
        if p.get("team") in size:
            w = work.setdefault(p["team"], {"calls": 0, "emails": 0, "tickets": 0, "work": 0})
            w["calls"] += p["calls"]
            w["emails"] += p.get("emails") or 0
            w["tickets"] += p["tickets"]
            w["work"] += p["total"]
    mail, tick = _email_team_sla(store, day, now), _ticket_team_sla(store, hub, users, day, numbers)
    key = lambda name: str(name or "").strip().casefold()                     # noqa: E731
    mail = {key(t): v for t, v in mail.items()}
    tick = {key(t): v for t, v in tick.items()}
    rows = []
    for team, n in size.items():
        w = work.get(team) or {"calls": 0, "emails": 0, "tickets": 0, "work": 0}
        if not w["work"]:
            continue
        m = mail.get(key(team)) or {"ok": 0, "breached": 0}
        t = tick.get(key(team)) or {"within": 0, "timed": 0, "median": None}
        measured = m["ok"] + m["breached"] + t["timed"]
        sla_pct = round(100.0 * (m["ok"] + t["within"]) / measured, 1) if measured else None
        per_person = round(w["work"] / n, 1)
        score = round(per_person * (sla_pct / 100.0 if sla_pct is not None else 1.0), 1)
        rows.append({"team": team, "manpower": n, **w, "per_person": per_person,
                     "email_sla_pct": round(100.0 * m["ok"] / (m["ok"] + m["breached"]), 1) if m["ok"] + m["breached"] else None,
                     "ticket_sla_pct": round(100.0 * t["within"] / t["timed"], 1) if t["timed"] else None,
                     "exec_median_minutes": t["median"], "sla_pct": sla_pct, "score": score})
    rows.sort(key=lambda r: (-r["score"], -(r["sla_pct"] if r["sla_pct"] is not None else -1), -r["per_person"], r["team"].casefold()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


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
        left = sla_history.status_numbers(sc["left_grid"], {}, sc["cfg"], d, d)
        log = {"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "totals": {k: nums[k] for k in COUNT_KEYS},
               "times": nums["times"], "by_team": nums["by_team"], "by_person": nums["by_person"],
               "left_out": sla_history.counts_only(left, sc["left_now"] if d == today else None)}
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
                            "_timed": [], **({"team": r.get("team")} if key == "person" else {})})
        for k in COUNT_KEYS:
            t[k] += int(r.get(k) or 0)
        t["now"] += int(r.get("now") or 0)               # only the window's rows carry the Live tickets
        n = int(r.get("exec_n") or 0)
        t["exec_n"] += n
        t["_sum"] += (r.get("exec_average_minutes") or 0) * n
        t["exec_within"] += int(r.get("exec_within") or 0)
        if n:
            t["_timed"].append(r)
    for t in out.values():
        s, n, timed = t.pop("_sum"), t["exec_n"], t.pop("_timed")
        # one row with times: its median stands; medians of several days cannot be added up
        median = timed[0].get("exec_median_minutes") if len(timed) == 1 else None
        t.update(exec_average_minutes=round(s / n, 1) if n else None, exec_median_minutes=median,
                 exec_within_pct=round(100.0 * t["exec_within"] / n, 1) if n else None)
    return list(out.values())


def _merged_rows(rows, merges):
    """Kept team / person rows with a merged team (``sla.merge_teams``) under the team it is merged into - a day kept
    before the merge counts like the days after it."""
    return [fold_rejected({**r, "team": teams.merged_team(r.get("team"), merges)} if merges else r) for r in rows or []]


def fold_rejected(row):
    """A day kept before the owner's rule "not executed is rejected": its not-executed requests count as rejected."""
    if not isinstance(row, dict) or not int(row.get("not_executed") or 0):
        return row
    return {**row, "rejected": int(row.get("rejected") or 0) + int(row["not_executed"]), "not_executed": 0}


def add_logged_status(glance, store, day_from, day_to, scope=None, merges=None, person=""):
    """Add to ``glance`` (``status_numbers``) the kept numbers of the days ``day_from``..``day_to`` (older than the
    inquiry window), as ``scope`` may see them (``merges`` = ``teams.team_merges`` of the settings); ``person`` = only
    that security user's kept rows."""
    teams_rows, people_rows = [], []
    scope = teams.merged_scope(scope, merges)
    want = sla_history._norm(person) if person else ""
    for day in store.history_days("sla"):
        if day < day_from or day > day_to:
            continue
        log = (store.read_history("sla", day) or {}).get("day_log") or {}
        teams_rows += [r for r in _merged_rows(log.get("by_team"), merges) if teams.in_scope(scope, r.get("team"))]
        people_rows += [r for r in _merged_rows(log.get("by_person"), merges) if teams.in_scope(scope, r.get("team"))]
    if want:                                              # one person: their own rows are the totals
        people_rows = [r for r in people_rows if sla_history._norm(r.get("person")) == want]
        teams_rows = list(people_rows)
    if not teams_rows:
        return glance
    for k in COUNT_KEYS:
        glance[k] = glance.get(k, 0) + sum(int(r.get(k) or 0) for r in teams_rows)
    untimed = glance.setdefault("untimed", {"came": 0, "done": 0, "confirmed": 0})   # kept days have no hours
    kept = lambda k: sum(int(r.get(k) or 0) for r in teams_rows)                        # noqa: E731
    untimed["came"] = untimed.get("came", 0) + kept("came")
    untimed["done"] = untimed.get("done", 0) + kept("executed") + kept("not_executed") + kept("rejected")
    untimed["confirmed"] = untimed.get("confirmed", 0) + kept("confirmed")
    glance["by_team"] = _sum_rows(list(glance.get("by_team") or []) + teams_rows, "team")
    glance["by_person"] = _sum_rows(list(glance.get("by_person") or []) + people_rows, "person")
    glance["logged_days"] = sorted({d for d in store.history_days("sla") if day_from <= d <= day_to})
    return glance


def add_logged_left_out(counts, store, day_from, day_to, scope=None, merges=None):
    """Add to ``counts`` (``sla_history.counts_only``: the requests left out of the SLA) the kept counts of the days
    ``day_from``..``day_to`` (older than the inquiry window), as ``scope`` may see them."""
    scope = teams.merged_scope(scope, merges)
    rows = []
    for day in store.history_days("sla"):
        if day_from <= day <= day_to:
            left = ((store.read_history("sla", day) or {}).get("day_log") or {}).get("left_out") or {}
            rows += [r for r in _merged_rows(left.get("by_team"), merges) if teams.in_scope(scope, r.get("team"))]
    if not rows:
        return counts
    keys = [k for k in sla_history.LEFT_OUT_KEYS if k != "now"]       # "now" is the Live tickets of today only
    by = {r["team"]: dict(r) for r in counts.get("by_team") or []}
    for r in rows:
        t = by.setdefault(r.get("team") or "—", {"team": r.get("team") or "—", **{k: 0 for k in sla_history.LEFT_OUT_KEYS}})
        for k in keys:
            t[k] += int(r.get(k) or 0)
    for k in keys:
        counts[k] = sum(t[k] for t in by.values())
    counts["by_team"] = sorted(by.values(), key=lambda r: (-r["came"], -r["now"], str(r["team"]).casefold()))
    return counts


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
            t, ex = fold_rejected(log.get("totals") or {}), (log.get("times") or {}).get("execution") or {}
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
