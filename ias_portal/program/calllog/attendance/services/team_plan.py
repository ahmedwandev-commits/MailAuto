"""Workforce › Team schedule (owner's rule, v5.16): the second way to make a month's schedule - the first is the Excel upload,
which stays as it was.

1. a team manager builds the month of their team: for every working day (Sunday-Thursday: every day that is not a weekly rest
   weekday of Settings) they drag their people into Morning or Evening, then send it to the General Manager;
2. the General Manager reviews every team - may move people between Morning / Evening - and approves it, or sends it back
   with a note;
3. the administrative affairs only (the Attendance administrators; a portal administrator counts too) add the night shifts,
   the days off and the official holidays, and publish the month once every team that used this page is approved: the
   schedule becomes the official one (attendance, the shift allowance and My schedule follow it) and one e-mail with the
   whole sector's schedule goes to the To / Cc of Settings › Team schedule.

Leaves are taken from the leave records (an employee on leave cannot be placed that day). On a working day an employee of a
published team who has no shift gets a rest day ("OFF"), so the default shift never marks them absent. Teams that did not
use this page keep the schedule they uploaded."""
import calendar
import html
import logging
from datetime import date

from . import access, audit, settings as settings_svc
from .access import Forbidden
from .allowance_confirm import _bell, _mail_html, _queue_to, label, month_of
from .schedule_service import ImportError_

log = logging.getLogger(__name__)
FILE = "team_plans.json"
# "PL" = planned leave: the manager may put any number of their people on it any day (owner); published as a rest day
# "D" = the Normal day 08:00-16:00 (owner): placed like a shift, but it is not one (no shift allowance).
# "CD" comp day («بدل راحة»): the manager puts any number of people on it; published as a rest day (never absent) with a
# note, shown as CD. Leaves are not placed here: the manager records them on Planning › Leave (owner) and the page shows them.
MANAGER_CODES = ("M", "D", "E", "CD")           # what a manager and the General Manager place
AFFAIRS_CODES = ("N", "OFF")                    # what only the administrative affairs place
CODES = ("M", "D", "E", "N", "OFF", "CD")       # one place a day: the first of these wins
NAMES = {"M": "Morning", "D": "Normal", "E": "Evening", "N": "Night", "OFF": "Day off", "CD": "Comp day"}
NAMES_AR = {"M": "صباحي", "D": "عادي", "E": "مسائي", "N": "ليلي", "OFF": "إجازة", "CD": "بدل راحة"}
REST_NOTES = {"CD": "Comp day"}                 # the note of the rest day it is published as
STATUS = ("draft", "submitted", "returned", "approved")


# ------------------------------------------------------------------------------------------------ helpers
def is_affairs(user):
    """The administrative affairs: inside Attendance the Attendance administrator (and a portal administrator) is "admin"."""
    return user["role"] == "admin"


def _manages(user, team):
    return is_affairs(user) or user["role"] == "gm" or (
        user["role"] == "manager" and team.strip().lower() in [t.strip().lower() for t in user.get("teams") or []])


def work_days(repo, month):
    """The month's working days (Sunday-Thursday by default: every weekday that is not a weekly rest weekday)."""
    rest = set(settings_svc.load(repo).get("weekly_rest_weekdays") or [4, 5])
    y, m = int(month[:4]), int(month[5:7])
    return [date(y, m, d).isoformat() for d in range(1, calendar.monthrange(y, m)[1] + 1) if date(y, m, d).weekday() not in rest]


def plans(repo):
    return repo._get(FILE, {})


def _plan(repo, month, team):
    return ((plans(repo).get(month) or {}).get("teams") or {}).get(team) or {}


def _team_emps(repo, team):
    return [e for e in repo.list_employees() if access.has_team(e, team)]


def teams_for(repo, user):
    """The teams this user plans or reviews: a manager their own, the General Manager / affairs every team with people."""
    every = sorted({t for e in repo.list_employees() for t in e["teams"]}, key=str.lower)
    return [t for t in every if _manages(user, t)]


def _is_manager(repo, employee_id):
    acct = repo.directory.store.get_user(str(employee_id)) if hasattr(repo.directory, "store") else None
    return bool(acct) and acct.get("role") == "manager"


def _leaves(repo, ids, d1, d2):
    """{(employee_id, date): leave type} of the approved leaves in the range."""
    out = {}
    for r in repo.leave_requests(ids, d1, d2):
        if r.get("status") not in ("approved", "Approved"):
            continue
        d = date.fromisoformat(max(r["date_from"], d1))
        while d.isoformat() <= min(r["date_to"], d2):
            out[(r["employee_id"], d.isoformat())] = r.get("leave_type") or r.get("type") or "Leave"
            d = date.fromordinal(d.toordinal() + 1)
    return out


def _holidays(repo, month):
    return {h["date"]: h.get("name") or "Official holiday" for h in repo.list_holidays() if h["date"][:7] == month}


# ------------------------------------------------------------------------------------------------ the page
def board(repo, user, month, team):
    """Everything the drag-and-drop page needs for one team and month."""
    if not _manages(user, team):
        raise Forbidden("You can plan only your own team(s).")
    days = work_days(repo, month)
    emps = _team_emps(repo, team)
    ids = [e["id"] for e in emps]
    plan = _plan(repo, month, team)
    pub = (plans(repo).get(month) or {}).get("published") or {}
    status = plan.get("status", "draft") if plan else "new"
    # approved by the General Manager = official: only the administrative affairs change it then (it stays official)
    locked = status == "approved"
    can_shift = is_affairs(user) or (not locked and (
        (user["role"] == "manager" and status in ("new", "draft", "returned")) or
        (user["role"] == "gm" and status in ("new", "draft", "submitted", "returned"))))
    return {
        "month": month, "label": label(month), "team": team, "days": days,
        "weekday": {d: date.fromisoformat(d).strftime("%a") for d in days},
        # a manager is scheduled like everybody (owner) - but paid the shift allowance for the morning shift only
        "employees": [{"id": e["id"], "name": e["name"], "manager": _is_manager(repo, e["id"])} for e in emps],
        "cells": plan.get("cells") or {}, "status": status, "plan": plan,
        "leaves": {f"{k[0]}|{k[1]}": v for k, v in _leaves(repo, ids, days[0], days[-1]).items()} if days else {},
        "holidays": _holidays(repo, month), "published": pub,
        "can": {"shift": can_shift, "affairs": is_affairs(user),
                "submit": can_shift and status in ("new", "draft", "returned") and user["role"] in ("manager", "admin"),
                "review": user["role"] in ("gm", "admin") and status == "submitted" and not locked},
    }


def overview(repo, user, month):
    """Every team of the user with the state of its plan, and whether the month can be published."""
    rows = []
    for t in teams_for(repo, user):
        p = _plan(repo, month, t)
        rows.append({"team": t, "status": p.get("status", "new") if p else "new", "people": len(_team_emps(repo, t)),
                     "by_name": p.get("updated_by_name"), "note": p.get("note", ""), "at": p.get("updated_at")})
    used = [r for r in rows if r["status"] != "new"]
    return {"month": month, "label": label(month), "teams": rows,
            "ready": bool(used) and all(r["status"] == "approved" for r in used),
            "published": (plans(repo).get(month) or {}).get("published") or {}}


# ------------------------------------------------------------------------------------------------ actions
def _save_plan(repo, month, team, change):
    with repo.transaction():
        data = repo._mut(FILE, {})
        rec = data.setdefault(month, {}).setdefault("teams", {}).setdefault(team, {"status": "draft", "cells": {}})
        return change(rec, data[month])


def save(repo, user, month, team, cells):
    """Keep the plan of a team. ``cells`` = {date: {code: [employee ids]}}. A manager / the General Manager change Morning and
    Evening; only the administrative affairs change Night and Day off (whatever else is sent for those is ignored)."""
    month = month_of(month) or ""
    b = board(repo, user, month, team)
    if not b["can"]["shift"]:
        raise Forbidden("This plan cannot be changed now (sent, approved or published).")
    days, ids = set(b["days"]), {e["id"] for e in b["employees"]}
    leaves, holidays = b["leaves"], b["holidays"]
    mine = CODES if b["can"]["affairs"] else MANAGER_CODES
    old = b["cells"]
    clean, problems = {}, []
    for d in sorted(days):
        day = {}
        for code in CODES:
            if code not in mine:                       # kept as the affairs left it
                day[code] = [i for i in (old.get(d) or {}).get(code, []) if i in ids]
                continue
            seen = []
            for raw in ((cells or {}).get(d) or {}).get(code, []) or []:
                try:
                    i = int(raw)
                except (TypeError, ValueError):
                    continue
                if i not in ids or i in seen:
                    continue
                if d in holidays:
                    problems.append(f"{d} is an official holiday")
                    continue
                if f"{i}|{d}" in leaves:
                    problems.append(f"{next(e['name'] for e in b['employees'] if e['id'] == i)} is on leave on {d}")
                    continue
                seen.append(i)
            day[code] = seen
        if not b["can"]["affairs"]:                    # a night / day off the affairs gave stays: nobody else moves that person
            kept = {i for c in AFFAIRS_CODES for i in day[c]}
            for c in MANAGER_CODES:
                day[c] = [i for i in day[c] if i not in kept]
        placed = [i for c in CODES for i in day[c]]
        if len(placed) != len(set(placed)):            # one shift a day: the first place wins (Morning, Evening, Night, Day off)
            taken, fixed = set(), {}
            for c in CODES:
                fixed[c] = [i for i in day[c] if i not in taken]
                taken.update(fixed[c])
            day = fixed
        if any(day.values()):
            clean[d] = day
    now = repo._now()

    def change(rec, _m):
        rec["cells"] = clean
        status = rec.get("status") or "draft"
        if user["role"] == "manager" and status != "returned":
            status = "draft"                           # a manager's change is a draft until they send it again
        rec.update(status=status, updated_by=user["user_id"], updated_by_name=user.get("name"), updated_at=now)
        return status
    status = _save_plan(repo, month, team, change)
    audit(repo, user, "team_plan_saved", "team_plan", f"{month}|{team}", {"days": len(clean)})
    if status == "approved":                           # the affairs changed an official plan: the official schedule follows
        make_official(repo, user, month, [team])
    return {"days": len(clean), "problems": sorted(set(problems))[:10]}


def _gms(repo):
    return [u["user_id"] for u in repo.directory.list_users() if u.get("role") == "gm" and u.get("active", True)]


def _managers(repo, team):
    from .allowance_confirm import _team_managers
    return [m["user_id"] for m in _team_managers(repo, team)]


def submit(repo, user, month, team):
    month = month_of(month) or ""
    b = board(repo, user, month, team)
    if not b["can"]["submit"]:
        raise Forbidden("Only the team's manager sends the plan to the General Manager.")
    if not b["cells"]:
        raise ImportError_("Place your people on the days first, then save.")
    now = repo._now()

    def change(rec, _m):
        rec.update(status="submitted", submitted_by=user["user_id"], submitted_by_name=user.get("name"), submitted_at=now, note="")
    _save_plan(repo, month, team, change)
    en, ar = label(month)
    _bell(_gms(repo), f"{team}: the {en} schedule is waiting for your approval", f"Sent by {user.get('name') or user['user_id']}.",
          f"/attendance/team-plan?month={month}&team={team}", user.get("name") or user["user_id"])
    audit(repo, user, "team_plan_submitted", "team_plan", f"{month}|{team}")
    return {"team": team}


def review(repo, user, month, team, decision, note=""):
    """The General Manager (or the affairs) approves a sent plan, or sends it back to the manager with a note."""
    month = month_of(month) or ""
    b = board(repo, user, month, team)
    if not b["can"]["review"]:
        raise Forbidden("Only the General Manager approves a plan that was sent.")
    note = (note or "").strip()[:500]
    if decision == "return" and not note:
        raise ImportError_("Write what to change, so the manager knows.")
    now = repo._now()

    def change(rec, _m):
        rec.update(status="approved" if decision == "approve" else "returned", note=note if decision == "return" else "",
                   reviewed_by=user["user_id"], reviewed_by_name=user.get("name"), reviewed_at=now)
    _save_plan(repo, month, team, change)
    if decision == "approve":                          # approved = official at once (owner): calendar, My schedule, attendance
        make_official(repo, user, month, [team])
    en, _ar = label(month)
    word = "approved" if decision == "approve" else "sent back"
    _bell(_managers(repo, team), f"Your {en} schedule of {team} was {word}", note or "Workforce › Team schedule.",
          f"/attendance/team-plan?month={month}&team={team}", user.get("name") or user["user_id"])
    audit(repo, user, f"team_plan_{'approved' if decision == 'approve' else 'returned'}", "team_plan", f"{month}|{team}",
          {"note": note} if note else None)
    return {"team": team, "status": "approved" if decision == "approve" else "returned"}


def set_holiday(repo, user, day, name, on):
    """An official holiday (the administrative affairs only): nobody is placed on it; attendance treats it as a holiday."""
    if not is_affairs(user):
        raise Forbidden("Only the administrative affairs set the official holidays.")
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ImportError_("Choose a valid date.")
    if on:
        repo.add_holiday(day, (name or "").strip()[:80] or "Official holiday")
    else:
        repo.remove_holiday(day)
    audit(repo, user, "holiday_set" if on else "holiday_removed", "holiday", day, {"name": name} if on else None)
    month = day[:7]                                    # the official plans of that month follow the holiday
    approved = [t for t, r in ((plans(repo).get(month) or {}).get("teams") or {}).items() if r.get("status") == "approved"]
    if approved:
        make_official(repo, user, month, approved)


def _entries_of(repo, month, team, rec, holidays, days):
    """The schedule entries of one approved team: its shifts, and a rest day on every other working day."""
    out = []
    ids = [e["id"] for e in _team_emps(repo, team)]
    leaves = _leaves(repo, ids, days[0], days[-1]) if days else {}
    cells = rec.get("cells") or {}
    for d in days:
        placed = set()
        for code in CODES:
            for i in (cells.get(d) or {}).get(code, []):
                if i in ids and i not in placed and d not in holidays:
                    if code in REST_NOTES:               # a comp day: a rest day, never "absent"
                        out.append({"employee_id": i, "work_date": d, "shift_code": "OFF", "source": "team_plan",
                                    "note": f"{REST_NOTES[code]} · {team}"})
                    else:
                        out.append({"employee_id": i, "work_date": d, "shift_code": code, "source": "team_plan", "note": team})
                    placed.add(i)
        for i in ids:
            if i not in placed and d not in holidays and (i, d) not in leaves:
                out.append({"employee_id": i, "work_date": d, "shift_code": "OFF", "source": "team_plan", "note": team})
    return out, set(ids)


def make_official(repo, user, month, teams):
    """The approved plans of ``teams`` become the official schedule (owner: as soon as the General Manager approves): their
    people's days of the month are replaced in a new version of the month's schedule - everybody else keeps theirs - and
    they and their managers are told. Called again when the affairs change an approved plan or an official holiday."""
    y, m = int(month[:4]), int(month[5:7])
    days, holidays = work_days(repo, month), _holidays(repo, month)
    month_plans = (plans(repo).get(month) or {}).get("teams") or {}
    new, planned, done = [], set(), []
    for t in sorted(set(teams)):
        rec = month_plans.get(t) or {}
        if rec.get("status") != "approved":
            continue
        entries, ids = _entries_of(repo, month, t, rec, holidays, days)
        new += entries
        planned |= ids
        done.append(t)
    if not done:
        return None
    first, last = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    keep = [{k: e[k] for k in ("employee_id", "work_date", "shift_code", "stream", "note", "source", "flags") if k in e}
            for e in repo.schedule_entries(first, last) if e["employee_id"] not in planned]
    version = repo.commit_schedule_version_atomic(y, m, None, keep + new, user["user_id"], replace=True)
    now = repo._now()
    with repo.transaction():
        teams_rec = repo._mut(FILE, {}).setdefault(month, {}).setdefault("teams", {})
        for t in done:
            teams_rec.setdefault(t, {}).update(official_version=version["version_no"], official_at=now)
    audit(repo, user, "team_plan_official", "team_plan", month,
          {"teams": done, "version": version["version_no"], "people": len(planned)})
    from . import notify
    notify.schedule_published(repo, user, y, m, planned, replaced=True)
    return {"version": version["version_no"], "people": len(planned), "teams": done}


def publish(repo, user, month):
    """The administrative affairs send the e-mail of the month once every team that used this page is approved (the
    approved plans are already official; they are written once more so the e-mail and the schedule are the same)."""
    if not is_affairs(user):
        raise Forbidden("Only the administrative affairs send the schedule e-mail.")
    month = month_of(month) or ""
    ov = overview(repo, user, month)
    if not ov["ready"]:
        waiting = [r["team"] for r in ov["teams"] if r["status"] not in ("new", "approved")]
        raise ImportError_("Every team must be approved by the General Manager first" +
                           (f": still waiting - {', '.join(waiting)}." if waiting else "."))
    month_plans = (plans(repo).get(month) or {}).get("teams") or {}
    res = make_official(repo, user, month, [t for t, r in month_plans.items() if r.get("status") == "approved"]) or {}
    now = repo._now()
    with repo.transaction():
        repo._mut(FILE, {}).setdefault(month, {})["published"] = {"at": now, "by": user["user_id"], "by_name": user.get("name"),
                                                                  "version": res.get("version")}
    audit(repo, user, "team_plan_mailed", "team_plan", month, {"version": res.get("version")})
    mails = _mail(repo, month, month_plans, _holidays(repo, month), work_days(repo, month))
    return {"version": res.get("version"), "people": res.get("people", 0), "mails": mails}


# ------------------------------------------------------------------------------------------------ the e-mail
def mail_html(repo, month, month_plans, holidays, days):
    esc = html.escape
    en, ar = label(month)
    names = {e["id"]: e["name"] for e in repo.list_employees()}
    parts = []
    for team, rec in sorted(month_plans.items(), key=lambda kv: kv[0].lower()):
        if rec.get("status") != "approved":
            continue
        cells = rec.get("cells") or {}
        rows = []
        for d in days:
            day = date.fromisoformat(d)
            if d in holidays:
                body = (f'<td colspan="5" style="padding:6px 8px;border:1px solid #e5e7eb;color:#b45309;font-weight:700">'
                        f'{esc(holidays[d])} / عطلة رسمية</td>')
            else:
                body = "".join(f'<td style="padding:6px 8px;border:1px solid #e5e7eb;vertical-align:top">'
                               f'{"<br>".join(esc(names.get(i, str(i))) for i in (cells.get(d) or {}).get(c, [])) or "—"}</td>'
                               for c in ("M", "D", "E", "N", "CD"))
            rows.append(f'<tr><td style="padding:6px 8px;border:1px solid #e5e7eb;white-space:nowrap;font-weight:600">'
                        f'{day.strftime("%a %d/%m")}</td>{body}</tr>')
        head = "".join(f'<th style="padding:6px 8px;border:1px solid #e5e7eb;background:#004B32;color:#fff;text-align:left">{h}</th>'
                       for h in ("Day / اليوم", "Morning / صباحي", "Normal 08-16 / عادي", "Evening / مسائي", "Night / ليلي",
                                 "Comp day / بدل راحة"))
        parts.append(f'<h3 style="margin:18px 0 6px;color:#004B32" dir="auto">{esc(team)}</h3>'
                     f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;width:100%">'
                     f'<tr>{head}</tr>{"".join(rows)}</table>')
    intro = _mail_html(f"جدول ورديات {ar} المعتمد", f"The approved shift schedule of {en}",
                       ["تم اعتماد جدول الورديات من المدير العام ونشره من الشئون الإدارية. الجدول بالأسفل وعلى البورتال."],
                       ["The shift schedule was approved by the General Manager and published by the administrative affairs. "
                        "It is below and on the IAS Portal (My schedule)."])
    foot = '<p style="color:#6b7280'                  # the teams go in the body, just before its footer line
    return intro.replace(foot, "".join(parts) + foot, 1) if foot in intro else intro + "".join(parts)


def _mail(repo, month, month_plans, holidays, days):
    st = settings_svc.load(repo)
    to = [a for a in st.get("team_plan_to") or [] if "@" in a]
    cc = [a for a in st.get("team_plan_cc") or [] if "@" in a]
    if not to and not cc:
        return 0
    en, ar = label(month)
    return _queue_to(to or cc, f"The approved {en} shift schedule · جدول ورديات {ar} المعتمد",
                     mail_html(repo, month, month_plans, holidays, days), "team_plan", repo, cc=cc if to else [])
