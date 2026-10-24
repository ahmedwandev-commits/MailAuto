# -*- coding: utf-8 -*-
"""Analysis across every module: how much work arrives, on which days and at which hours, and how that changes.

Every module is turned into the same shape: ``{day: {"g": {group: {"n": count, "h": [24 counts]}}, "src": ...}}`` where
a group is the set of teams the items belong to (``"Flex Cube"``, or ``"Flex Cube\\x1fCard Management"`` for an e-mail
sent to two teams). ``series`` turns that into what the Analysis page draws - daily trend, hour-of-day profile, weekday x
hour heat map, peak hour / peak weekday / busiest day, per team - for any set of teams, counting an item ONCE even when
it belongs to several of the chosen teams.

The e-mail module needs its own long-term store, because a scan holds only the current day: ``fold_email`` counts each
mail ONCE on the day (and at the hour) it was received into ``ops/analytics/email_intake.json`` (kept ``KEEP_DAYS``).
Days from before that store existed come from the e-mail history points (the day's total only, no hours). Calls are
folded per month (the Call Log keeps one file per employee per day - years of them), tickets come from the SLA status
history and change requests from the ITSM recording. Pure functions over plain dicts.
"""
import hashlib
from datetime import date, datetime, timedelta

from .engines.email_rules import RATING_KEYS, score_of as _score     # Excellent / Very Good / Good / Needs Improvement

KEEP_DAYS = 730                 # two years of e-mail intake
SEEN_DAYS = 35                  # ids of counted mails are remembered this long (a carried-over mail is seen again)
MAX_RANGE_DAYS = 731
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UNCLASSIFIED = "Unclassified"
NO_TEAM = "(no team)"
SEP = "\x1f"


def _day(text):
    s = str(text or "").strip().replace(" ", "T")
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else ""


def _hour(text):
    s = str(text or "").strip().replace(" ", "T")
    if "T" in s:
        s = s.split("T", 1)[1]
    try:
        h = int(s[:2])
    except (TypeError, ValueError):
        return None
    return h if 0 <= h <= 23 else None


def group_key(teams):
    names = sorted(dict.fromkeys(str(t) for t in teams or () if t))
    return SEP.join(names) if names else NO_TEAM


def group_teams(key):
    return [t for t in str(key or "").split(SEP) if t] or [NO_TEAM]


def add(days, day, hour=None, teams=(), n=1):
    """Count ``n`` items on ``day`` (at ``hour`` when known) for the group of ``teams``."""
    if not day:
        return
    d = days.setdefault(day, {"g": {}})
    g = d["g"].setdefault(group_key(teams), {"n": 0, "h": [0] * 24})
    g["n"] += n
    if hour is not None:
        if g.get("h") is None:
            g["h"] = [0] * 24
        g["h"][hour] += n


# ------------------------------------------------------------------------------------------- e-mail intake (kept)
def _mail_key(row):
    raw = str(row.get("message_id") or row.get("entry_id") or row.get("row_id") or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] if raw else ""


def mail_teams(row):
    out = [t for t in [row.get("team")] + list(row.get("all_teams") or []) if t]
    return list(dict.fromkeys(out)) or [UNCLASSIFIED]


def fold_email(doc, rows, now):
    """Count the mails of one scan into the intake store ``doc`` ({"days", "seen"}): each mail once, on the day and at
    the hour it was RECEIVED, for its teams (a mail split per team is still one mail). A mail already counted is
    skipped, so folding the same scan twice, or the next scan, changes nothing. Returns how many mails were added."""
    days = doc.setdefault("days", {})
    seen = doc.setdefault("seen", {})
    by_mail = {}
    for row in rows or []:
        key, day = _mail_key(row), _day(row.get("received_time"))
        if not key or not day:
            continue
        item = by_mail.setdefault(key, {"day": day, "hour": _hour(row.get("received_time")), "teams": []})
        item["teams"] += [t for t in mail_teams(row) if t not in item["teams"]]
    added = 0
    for key, item in by_mail.items():
        mine = seen.setdefault(item["day"], [])
        if key in mine:
            continue
        mine.append(key)
        if days.get(item["day"], {}).get("src") == "history":     # live data replaces an approximate backfill
            days[item["day"]] = {"g": {}}
        add(days, item["day"], item["hour"], item["teams"])
        days[item["day"]]["src"] = "scan"
        added += 1
    today = (now or datetime.now()).date()
    cut_seen = (today - timedelta(days=SEEN_DAYS)).isoformat()
    cut_keep = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for d in [d for d in seen if d < cut_seen]:
        del seen[d]
    for d in [d for d in days if d < cut_keep]:
        del days[d]
    return added


def backfill_email(doc, history_days):
    """Days the intake store has not seen, filled from the e-mail history (``{day: day document}``): the day's
    business e-mails from its last scan point (per team set when the point carries groups); no hours."""
    days = doc.setdefault("days", {})
    added = 0
    for day, hist in (history_days or {}).items():
        if day in days or not isinstance(hist, dict):
            continue
        points = [p for p in hist.get("points") or [] if isinstance(p, dict)]
        last = points[-1] if points else {}
        groups = {}
        for grp in last.get("groups") or []:
            n = int(grp.get("business_emails") or 0)
            if n:
                key = group_key(grp.get("teams") or [UNCLASSIFIED])
                groups[key] = {"n": groups.get(key, {}).get("n", 0) + n, "h": None}
        total = int(((hist.get("final") or {}).get("business_emails")) or last.get("business_emails") or 0)
        if not groups and total:
            groups[NO_TEAM] = {"n": total, "h": None}
        if groups:
            days[day] = {"g": groups, "src": "history"}
            added += 1
    return added


# ------------------------------------------------------------------------------------------- the other modules
def calls_day(records, teams_of_user):
    """One day of the Call Log -> {"g": ...} by the hour each call was logged, for the employee's teams."""
    days = {}
    for r in records or []:
        add(days, "x", _hour(r.get("time")), teams_of_user(r.get("user_id")))
    return days.get("x") or {"g": {}}


def stamp_days(records, field, team_of, fallback=None, keep=None):
    """Records (SLA history requests, ITSM change requests) -> day counts by one of their time stamps."""
    days = {}
    for r in (records or {}).values() if isinstance(records, dict) else records or []:
        if keep and not keep(r):
            continue
        stamp = r.get(field) or (r.get(fallback) if fallback else "")
        add(days, _day(stamp), _hour(stamp), team_of(r))
    return days


# ------------------------------------------------------------------------------------------- stores (no Flask)
EMAIL_STORE = "email_intake"


def fold_email_snapshot(store, now=None):
    """Fold the latest e-mail scan into the intake store (once per scan: the collector after each scan, the web as a
    safety net) and, the first time, fill the earlier days from the e-mail history. Returns how many mails were added."""
    snap = store.read_snapshot("email")
    if not snap:
        return 0
    stamp = snap.get("generated_at") or ""
    current = store.read_analytics(EMAIL_STORE)
    if current.get("last_folded") == stamp and current.get("backfilled"):
        return 0
    out = {"added": 0}

    def change(doc):
        if doc.get("last_folded") != stamp:
            out["added"] = fold_email(doc, (snap.get("data") or {}).get("rows") or [], now or datetime.now())
            doc["last_folded"] = stamp
        if not doc.get("backfilled"):
            history = {}
            for day in store.history_days("email"):
                try:
                    history[day] = store.read_history("email", day)
                except Exception:                           # noqa: BLE001 - one bad day file never stops the fold
                    continue
            backfill_email(doc, history)
            doc["backfilled"] = (now or datetime.now()).isoformat(timespec="seconds")
    store.update_analytics(EMAIL_STORE, change)
    return out["added"]


def calls_store_name(day):
    return f"calls_{day[:4]}_{day[5:7]}"


def mark_calls_dirty(store, day):
    """A call of a past ``day`` was edited or deleted: that day is counted again at the next read."""
    if not _day(day):
        return

    def change(doc):
        dirty = doc.setdefault("dirty", [])
        if day not in dirty:
            dirty.append(day)
    store.update_analytics(calls_store_name(day), change)


TODAY_REFRESH_SECONDS = 60


def calls_days(store, call_store, d_from, d_to, teams_of_user, now=None):
    """{day: DayDoc} of the Call Log over [d_from, d_to]. Days are counted once and kept per month in the analysis
    store (the Call Log has one file per employee per day, years of them); today is counted again every 5 minutes,
    an edited past day when it is marked dirty."""
    now = now or datetime.now()
    today = now.date().isoformat()
    with_data = set(call_store.days_with_data(d_from, min(d_to, today)))
    out = {}
    months = {}
    for day in date_range(d_from, min(d_to, today)):
        months.setdefault(calls_store_name(day), []).append(day)
    for name, days in months.items():
        doc = store.read_analytics(name)
        known = doc.get("days") or {}
        dirty = set(doc.get("dirty") or [])
        stale_today = (today in days and (doc.get("today_at") or "") < (now - timedelta(seconds=TODAY_REFRESH_SECONDS))
                       .isoformat(timespec="seconds"))
        todo = [d for d in days if d not in known or d in dirty or (d == today and stale_today)]
        fresh = {d: (calls_day(call_store.day_all(d), teams_of_user) if d in with_data else {"g": {}}) for d in todo}
        if fresh:
            def change(d, fresh=fresh):
                d.setdefault("days", {}).update(fresh)
                d["dirty"] = [x for x in d.get("dirty") or [] if x not in fresh]
                if today in fresh:
                    d["today_at"] = now.isoformat(timespec="seconds")
            doc = store.update_analytics(name, change)
            known = doc.get("days") or {}
        out.update({d: known[d] for d in days if d in known})
    return out


# ------------------------------------------------------------------------------------------- one series
def date_range(d_from, d_to):
    a, b = date.fromisoformat(d_from), date.fromisoformat(d_to)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def previous_range(d_from, d_to):
    """The period of the same length just before (for the change %)."""
    a, b = date.fromisoformat(d_from), date.fromisoformat(d_to)
    n = (b - a).days + 1
    return (a - timedelta(days=n)).isoformat(), (a - timedelta(days=1)).isoformat()


def _groups(doc, keep):
    for key, g in ((doc or {}).get("g") or {}).items():
        names = group_teams(key)
        if keep is None or any(keep(t) for t in names):
            yield names, g


def day_total(doc, keep=None):
    return sum(int(g.get("n") or 0) for _, g in _groups(doc, keep))


def series(days, d_from, d_to, keep=None):
    """What the page draws for one module over [d_from, d_to]. ``keep(team)`` -> bool narrows to some teams (None =
    every team); an item of several kept teams counts once in the totals and once for each team in ``by_team``."""
    span = date_range(d_from, d_to)
    daily = []
    hours = [0] * 24
    heat = [[0] * 24 for _ in range(7)]
    weekday_total, weekday_days = [0] * 7, [0] * 7
    by_team, hourly_days, approx = {}, 0, 0
    for d in span:
        doc = days.get(d)
        wd = date.fromisoformat(d).weekday()
        n = 0
        any_hours = False
        for names, g in _groups(doc, keep):
            c = int(g.get("n") or 0)
            n += c
            for t in names:
                if keep is None or keep(t):
                    by_team[t] = by_team.get(t, 0) + c
            hrs = g.get("h")
            if hrs is None:
                continue
            any_hours = any_hours or any(hrs)
            for h, v in enumerate(hrs):
                hours[h] += v
                heat[wd][h] += v
        daily.append({"date": d, "n": n, "missing": doc is None})
        weekday_days[wd] += 1
        weekday_total[wd] += n
        hourly_days += any_hours
        approx += bool(doc and doc.get("src") == "history")
    total = sum(x["n"] for x in daily)
    with_data = [x for x in daily if not x["missing"]]
    busy = max(with_data, key=lambda x: x["n"], default=None)
    quiet = min((x for x in with_data if x["n"]), key=lambda x: x["n"], default=None)
    peak_h = max(range(24), key=lambda h: hours[h]) if any(hours) else None
    avg_wd = [round(weekday_total[i] / weekday_days[i], 1) if weekday_days[i] else 0 for i in range(7)]
    peak_wd = max(range(7), key=lambda i: avg_wd[i]) if any(avg_wd) else None
    slot = max(((wd, h) for wd in range(7) for h in range(24)), key=lambda x: heat[x[0]][x[1]]) if any(hours) else None
    return {"total": total, "days": len(span), "data_days": len(with_data), "approximate_days": approx,
            "avg_per_day": round(total / len(with_data), 1) if with_data else 0,
            "daily": daily, "hours": hours, "heatmap": heat, "weekday_avg": avg_wd, "hourly_days": hourly_days,
            "peak_hour": {"hour": peak_h, "n": hours[peak_h], "share": round(100.0 * hours[peak_h] / max(1, sum(hours)), 1)}
            if peak_h is not None else None,
            "peak_weekday": {"weekday": WEEKDAYS[peak_wd], "avg": avg_wd[peak_wd]} if peak_wd is not None else None,
            "busiest_day": {"date": busy["date"], "n": busy["n"]} if busy and busy["n"] else None,
            "quietest_day": {"date": quiet["date"], "n": quiet["n"]} if quiet else None,
            "busiest_slot": {"weekday": WEEKDAYS[slot[0]], "hour": slot[1], "n": heat[slot[0]][slot[1]]} if slot else None,
            "by_team": sorted(({"team": t, "n": c} for t, c in by_team.items()), key=lambda x: -x["n"])}


def change(current, previous):
    """The change % of a number against the previous period (None when there is nothing to compare with)."""
    current, previous = float(current or 0), float(previous or 0)
    return round((current - previous) / previous * 100.0, 1) if previous else None


# ------------------------------------------------------------------------------------------- quality reviews
COUNT_KEYS = tuple(RATING_KEYS.values())             # excellent, very_good, good, needs (Needs Improvement)


def quality_stats(entries, d_from, d_to, keep=None, received_by_team=None, roles=None, received_total=None):
    """Who rated the replies and how much of each team's mail was rated, over [d_from, d_to].

    ``entries`` = the quality reviews ({message id: review}); a review counts on the day its mail was RECEIVED (else
    the day it was rated) for the team that wrote the reply (``reply_team``, else the mail's team). ``keep(team)`` ->
    bool narrows to some teams. ``received_by_team`` = {team: e-mails received in the period} for the coverage %, and
    ``received_total`` the mails of all those teams counted once (a mail to two teams is one mail). ``roles`` =
    {reviewer user id: role}. A rating withdrawn after an appeal counts for the reviewer's activity but not as a rated
    mail."""
    blank = lambda: dict.fromkeys(COUNT_KEYS, 0)                     # noqa: E731
    reviewers, by_team, people = {}, {}, {}
    daily = {d: 0 for d in date_range(d_from, d_to)}
    total = withdrawn = appeals = waiting = 0
    for entry in (entries or {}).values():
        if not isinstance(entry, dict) or entry.get("rating") not in RATING_KEYS:
            continue
        day = _day(entry.get("received")) or _day(entry.get("reviewed_at"))
        if not day or not d_from <= day <= d_to:
            continue
        team = str(entry.get("reply_team") or entry.get("team") or NO_TEAM)
        if keep is not None and not keep(team):
            continue
        key = RATING_KEYS[entry["rating"]]
        rid = str(entry.get("reviewed_by_id") or entry.get("reviewed_by") or "?")
        r = reviewers.setdefault(rid, {"user_id": str(entry.get("reviewed_by_id") or ""),
                                       "name": str(entry.get("reviewed_by") or entry.get("reviewed_by_id") or "?"),
                                       "role": (roles or {}).get(str(entry.get("reviewed_by_id") or ""), ""),
                                       "ratings": 0, "withdrawn": 0, "teams": set(), "last_at": "", **blank()})
        r["ratings"] += 1
        r[key] += 1
        r["teams"].add(team)
        r["last_at"] = max(r["last_at"], str(entry.get("reviewed_at") or ""))
        if entry.get("appeal"):
            appeals += 1
            waiting += not entry.get("appeal_decision")
        if entry.get("rating_withdrawn"):
            r["withdrawn"] += 1
            withdrawn += 1
            continue
        total += 1
        daily[day] = daily.get(day, 0) + 1
        t = by_team.setdefault(team, {"rated": 0, "reviewers": set(), **blank()})
        t["rated"] += 1
        t[key] += 1
        t["reviewers"].add(rid)
        who = str(entry.get("replied_by_email") or entry.get("replied_by") or "?").lower()
        p = people.setdefault(who, {"name": str(entry.get("replied_by") or entry.get("replied_by_email") or "?"),
                                    "email": str(entry.get("replied_by_email") or ""), "team": team, "rated": 0, **blank()})
        p["rated"] += 1
        p[key] += 1
    received_by_team = received_by_team or {}
    team_rows = []
    for name in sorted(set(by_team) | {t for t, n in received_by_team.items() if n and (keep is None or keep(t))}):
        t = by_team.get(name) or {"rated": 0, "reviewers": set(), **blank()}
        received = int(received_by_team.get(name) or 0)
        team_rows.append({"team": name, "received": received, "rated": t["rated"],
                          "coverage_pct": round(100.0 * t["rated"] / received, 1) if received else None,
                          "reviewers": len(t["reviewers"]), "score": _score(t),
                          **{k: t[k] for k in COUNT_KEYS}})
    team_rows.sort(key=lambda x: (-x["rated"], -x["received"], x["team"]))
    reviewer_rows = sorted(({**r, "teams": sorted(r["teams"])} for r in reviewers.values()),
                           key=lambda x: (-x["ratings"], x["name"].casefold()))
    people_rows = sorted(({**p, "score": _score(p)} for p in people.values()),
                         key=lambda x: (-x["rated"], x["name"].casefold()))
    counts = {k: sum(t[k] for t in team_rows) for k in COUNT_KEYS}
    if received_total is None:
        received_total = sum(t["received"] for t in team_rows)
    return {"rated": total, **counts, "score": _score(counts), "withdrawn": withdrawn, "appeals": appeals,
            "appeals_waiting": waiting, "received": received_total,
            "coverage_pct": round(100.0 * total / received_total, 1) if received_total else None,
            "reviewers": reviewer_rows, "teams": team_rows, "people": people_rows,
            "daily": [{"date": d, "n": n} for d, n in sorted(daily.items())]}
