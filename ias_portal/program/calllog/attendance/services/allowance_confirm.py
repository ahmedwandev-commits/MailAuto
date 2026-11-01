"""The monthly confirmation of the shift allowance (owner's rule, v5.16):

1. at the end of a month an administrator (portal or Attendance) presses "Send for confirmation": every employee of the month
   and every team manager is asked - bell + e-mail - to confirm on the portal;
2. an employee confirms their own days, or objects with a note (their team's managers are told at once);
3. a team manager confirms the team (on behalf of anybody who has not confirmed; an objection is settled by then) - the
   General Manager is told;
4. once every team is confirmed the General Manager confirms the whole sector;
5. then the administrative affairs (the Attendance administrators; the portal administrators when there are none) get an
   e-mail that every manager and the General Manager confirmed.

A reminder e-mail goes every working day at ``allowance_reminder_time`` to whoever has not confirmed (``queue_reminders``,
called by the collector). The e-mails are queued in ``calllog/ops/mail_outbox.py`` and sent by the collector's Outlook.
Managers are paid the morning shift and holidays only (``domain/allowance.py``). Amounts = days x the amount per day set in
Settings › Shift allowance."""
import calendar
import html
import logging
from datetime import date, datetime

from flask import current_app

from ..domain import allowance as al
from . import audit, settings as settings_svc
from .access import Forbidden
from .schedule_service import ImportError_

log = logging.getLogger(__name__)
FILE = "allowance_rounds.json"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]
MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
KIND_AR = {"morning": "صباحي", "evening": "مسائي", "night": "ليلي", "rest_day": "عطلة أسبوعية", "official_holiday": "عطلة رسمية"}


# ------------------------------------------------------------------------------------------------ helpers
def label(month):
    y, m = int(month[:4]), int(month[5:7])
    return f"{MONTHS[m - 1]} {y}", f"{MONTHS_AR[m - 1]} {y}"


def month_of(value):
    try:
        y, m = (int(x) for x in str(value).split("-")[:2])
        return f"{y:04d}-{m:02d}" if 2000 <= y <= 2100 and 1 <= m <= 12 else None
    except (TypeError, ValueError):
        return None


def default_month(today):
    """The month to send: this month from the 20th on (the end of the month), else the previous one."""
    d = date.fromisoformat(today)
    if d.day >= 20:
        return f"{d.year:04d}-{d.month:02d}"
    return f"{d.year - 1:04d}-12" if d.month == 1 else f"{d.year:04d}-{d.month - 1:02d}"


_STORE = {"ops": None}         # the collector passes its own operations store (no Flask app there)


def _ops():
    if _STORE["ops"] is not None:
        return _STORE["ops"]
    try:
        return current_app.extensions.get("ops")
    except RuntimeError:
        return None


def rounds(repo):
    return repo._get(FILE, {})


def get_round(repo, month):
    return rounds(repo).get(month)


def _now(repo):
    return repo.clock().isoformat(timespec="seconds") if hasattr(repo, "clock") else datetime.now().isoformat(timespec="seconds")


def _team_managers(repo, team, users=None):
    """The active managers of ``team``. ``users``: the directory's list, when the caller already has it (a page with 35
    teams reads it once, not 35 times)."""
    uid = repo.directory.team_manager_of(team)             # the ONE manager of the team (owner, v5.17)
    if not uid:
        return []
    return [u for u in (users if users is not None else repo.directory.list_users()) if u.get("user_id") == uid and u.get("active", True)]


def _gms(repo):
    return [u for u in repo.directory.list_users() if u.get("role") == "gm" and u.get("active", True)]


def _admin_affairs(repo):
    people = [u for u in repo.directory.list_users() if u.get("att_admin") and u.get("active", True)]
    return people or [u for u in repo.directory.list_users() if u.get("hub_role") == "admin" and u.get("active", True)]


def _email(repo, user_id):
    acct = repo.directory.store.get_user(str(user_id)) or {}
    return str(acct.get("email") or "").strip()


# ------------------------------------------------------------------------------------------------ the numbers
def figures(repo, month, employees=None):
    """{employee_id: {kind: count, ..., total, amount}} of ``month`` (every employee, or the given ones) + the rates."""
    from . import allowance_service
    y, m = int(month[:4]), int(month[5:7])
    d1, d2 = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    emps = employees if employees is not None else repo.list_employees()
    days, skipped, rules, st = allowance_service.compute(repo, d1, d2, emps)
    rates = {k: float(rules[k].get("rate") or 0) for k in al.KINDS}
    out = {e["id"]: {**{k: 0 for k in al.KINDS}, "total": 0, "amount": 0.0} for e in emps}
    for d in days:
        row = out.setdefault(d["employee_id"], {**{k: 0 for k in al.KINDS}, "total": 0, "amount": 0.0})
        row[d["kind"]] += 1
        row["total"] += 1
        row["amount"] += rates[d["kind"]]
    not_counted = {}
    for s in skipped:
        not_counted.setdefault(s["employee_id"], []).append(s)
    return out, rates, st.get("allowance_currency") or "EGP", not_counted


def money(value, currency):
    return f"{value:,.2f} {currency}" if value else ""


# ------------------------------------------------------------------------------------------------ status
def status(repo, month, user=None):
    """Everything a page needs: the round, every team with its people, what ``user`` may do."""
    rnd = get_round(repo, month)
    emps = repo.list_employees()
    nums, rates, currency, not_counted = figures(repo, month, emps)
    users = repo.directory.list_users()
    teams = {}
    for e in emps:
        rec = (rnd or {}).get("employees", {}).get(str(e["id"])) or {}
        person = {"id": e["id"], "name": e["name"], "teams": e["teams"], "figures": nums.get(e["id"]),
                  "status": rec.get("status", "pending"), "note": rec.get("note", ""), "at": rec.get("at"), "by_name": rec.get("by_name"),
                  "on_behalf": bool(rec.get("on_behalf")), "not_counted": not_counted.get(e["id"], [])}
        for t in e["teams"] or [NO_TEAM]:
            saved = {k: v for k, v in ((rnd or {}).get("teams", {}).get(t) or {"status": "pending"}).items() if k not in ("people", "amount")}
            teams.setdefault(t, {**saved, "team": t, "people": [], "managers": [m["name"] for m in _team_managers(repo, t, users)]})["people"].append(person)
    for t in teams.values():
        t["counts"] = {s: sum(1 for p in t["people"] if p["status"] == s) for s in ("pending", "confirmed", "objected")}
        t["amount"] = sum((p["figures"] or {}).get("amount", 0) for p in t["people"])
    team_list = sorted(teams.values(), key=lambda t: t["team"].lower())
    all_teams = bool(team_list) and all(t["status"] == "confirmed" for t in team_list)
    return {"month": month, "label": label(month), "round": rnd, "teams": team_list, "all_teams_confirmed": all_teams,
            "gm": (rnd or {}).get("gm") or {"status": "pending"}, "rates": rates, "currency": currency,
            # every person once: somebody on two teams is listed under both, but is paid once
            "total_amount": sum((nums.get(e["id"]) or {}).get("amount", 0) for e in emps),
            "kinds": [(k, al.ENGLISH[k]) for k in al.KINDS]}


# ------------------------------------------------------------------------------------------------ mails
def _mail_html(title_ar, title_en, lines_ar, lines_en, rows=None, currency="EGP"):
    esc = html.escape
    table = ""
    if rows:
        cells = "".join(f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e5e7eb">{esc(a)}</td>'
                        f'<td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:right"><b>{esc(str(b))}</b></td></tr>'
                        for a, b in rows)
        table = f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:12px 0;font-size:14px">{cells}</table>'
    ar = "".join(f'<p style="margin:6px 0">{esc(x)}</p>' for x in lines_ar)
    en = "".join(f'<p style="margin:6px 0">{esc(x)}</p>' for x in lines_en)
    return (f'<div style="font-family:Segoe UI,Tahoma,Arial,sans-serif;color:#111;max-width:640px">'
            f'<div style="background:#004B32;color:#fff;padding:14px 18px;border-bottom:3px solid #C5A059">'
            f'<div style="font-size:18px;font-weight:700" dir="rtl">{esc(title_ar)}</div>'
            f'<div style="font-size:14px;opacity:.9">{esc(title_en)}</div></div>'
            f'<div style="padding:14px 18px;border:1px solid #e5e7eb;border-top:0">'
            f'<div dir="rtl" style="text-align:right">{ar}</div>{table}<div dir="ltr">{en}</div>'
            f'<p style="color:#6b7280;font-size:12px;margin-top:14px">IAS Portal · National Bank of Egypt</p></div></div>')


def _queue(repo, user_ids, subject, html_body, kind):
    from calllog.ops import mail_outbox
    store = _ops()
    if store is None:
        return 0
    to = [_email(repo, u) for u in user_ids]
    try:
        return 1 if mail_outbox.add(store, to, subject, html_body, kind=kind, now=repo.clock()) else 0
    except Exception:                                                   # noqa: BLE001 - never breaks the action
        log.exception("allowance confirmation e-mail could not be queued")
        return 0


def _queue_to(to, subject, html_body, kind, repo, cc=()):
    """Queue one mail to plain addresses (the To / Cc a setting names)."""
    from calllog.ops import mail_outbox
    store = _ops()
    if store is None:
        return 0
    try:
        return 1 if mail_outbox.add(store, to, subject, html_body, kind=kind, cc=list(cc), now=repo.clock()) else 0
    except Exception:                                                   # noqa: BLE001 - never breaks the action
        log.exception("e-mail could not be queued")
        return 0


def _bell(user_ids, title, text, link, actor):
    try:
        from calllog.ops import notifications, schemas
        store = _ops()
        if store is not None and user_ids:
            notifications.notify_people(store, [str(u) for u in user_ids], getattr(schemas, "NOTIFY_SCHEDULE", "schedule"),
                                        title, text, link, actor)
    except Exception:                                                   # noqa: BLE001
        log.exception("allowance confirmation bell failed")


def _ask_employee(repo, month, emp, nums, currency, reminder=False):
    en, ar = label(month)
    f = nums or {}
    rows = [(f"{KIND_AR[k]} / {al.ENGLISH[k]}", f.get(k, 0)) for k in al.KINDS if f.get(k)]
    rows.append(("الإجمالي / Total days", f.get("total", 0)))
    if f.get("amount"):
        rows.append(("المبلغ / Amount", money(f["amount"], currency)))
    word_ar, word_en = ("تذكير: " if reminder else ""), ("Reminder: " if reminder else "")
    body = _mail_html(f"{word_ar}برجاء التعزيز على بدل ورديات {ar}", f"{word_en}Please confirm your {en} shift allowance",
                      ["برجاء التعزيز على بدل الورديات الخاص بك من خلال البورتال (جدولي › بدل الورديات).",
                       "لو فيه خطأ، اضغط «اعتراض» واكتب السبب، وهيوصل لمدير فريقك."],
                      ["Please confirm your shift allowance on the IAS Portal (My schedule › Shift allowance).",
                       "If something is wrong, press “Object” and write why; your team manager is told."], rows, currency)
    return body, f"{word_en}Confirm your {en} shift allowance · {word_ar}برجاء التعزيز على بدل ورديات {ar}"


def _ask_manager(month, team, reminder=False):
    en, ar = label(month)
    word_ar, word_en = ("تذكير: " if reminder else ""), ("Reminder: " if reminder else "")
    body = _mail_html(f"{word_ar}برجاء التعزيز على بدل ورديات فريق {team} - {ar}", f"{word_en}Please confirm the {en} shift allowance of {team}",
                      ["برجاء مراجعة بدل ورديات فريقك والتعزيز من خلال البورتال (القوى العاملة › تعزيز بدل الورديات).",
                       "تقدر تعزز بالنيابة عن أي موظف لم يعزز، وتراجع الاعتراضات قبل التعزيز."],
                      ["Please review your team's shift allowance and confirm it on the IAS Portal (Workforce › Allowance confirmation).",
                       "You can confirm on behalf of anybody who has not confirmed, and settle the objections first."])
    return body, f"{word_en}Confirm the {en} shift allowance of {team} · {word_ar}برجاء التعزيز على بدل ورديات {team}"


def _ask_gm(month, reminder=False):
    en, ar = label(month)
    word_ar, word_en = ("تذكير: " if reminder else ""), ("Reminder: " if reminder else "")
    body = _mail_html(f"{word_ar}برجاء التعزيز على بدل ورديات القطاع - {ar}", f"{word_en}Please confirm the {en} shift allowance of the sector",
                      ["كل مديري الفرق عززوا بدل ورديات فرقهم. برجاء التعزيز على بدل ورديات القطاع بالكامل من خلال البورتال."],
                      ["Every team manager confirmed their team. Please confirm the shift allowance of the whole sector on the IAS Portal "
                       "(Workforce › Allowance confirmation)."])
    return body, f"{word_en}Confirm the {en} shift allowance of the sector · {word_ar}برجاء التعزيز على بدل ورديات القطاع"


# ------------------------------------------------------------------------------------------------ actions
def _save(repo, month, change):
    with repo.transaction():
        data = repo._mut(FILE, {})
        rnd = data.get(month)
        out = change(rnd, data)
    return out


def start(repo, user, month):
    """Open the month's confirmation and ask everybody (an administrator). Sending it again re-asks whoever has not confirmed."""
    if user["role"] != "admin":
        raise Forbidden("Only an administrator sends the shift allowance for confirmation.")
    month = month_of(month)
    if not month:
        raise ImportError_("Choose the month (YYYY-MM).")
    emps = repo.list_employees()
    if not emps:
        raise ImportError_("There are no employees to ask.")
    now = _now(repo)

    def change(rnd, data):
        if rnd is None:
            data[month] = {"month": month, "started_by": user["user_id"], "started_by_name": user.get("name"), "started_at": now,
                           "employees": {}, "teams": {}, "gm": {"status": "pending"}, "done_notified_at": None, "reminded": []}
        else:
            rnd["resent_at"] = now
        return data[month]
    rnd = _save(repo, month, change)
    nums, rates, currency, _ = figures(repo, month, emps)
    en, ar = label(month)
    actor = user.get("name") or user["user_id"]
    pending_emps = [e for e in emps if (rnd["employees"].get(str(e["id"])) or {}).get("status") != "confirmed"]
    mails = 0
    for e in pending_emps:
        body, subject = _ask_employee(repo, month, e, nums.get(e["id"]), currency)
        mails += _queue(repo, [e["id"]], subject, body, "allowance_employee")
    _bell([e["id"] for e in pending_emps], f"Please confirm your {en} shift allowance", "Open My schedule › Shift allowance.",
          f"/attendance/my?allowance={month}", actor)
    teams = sorted({t for e in emps for t in e["teams"]}, key=str.lower)
    users = repo.directory.list_users()
    for t in teams:
        if (rnd["teams"].get(t) or {}).get("status") == "confirmed":
            continue
        managers = [m["user_id"] for m in _team_managers(repo, t, users)]
        body, subject = _ask_manager(month, t)
        mails += _queue(repo, managers, subject, body, "allowance_manager")
        _bell(managers, f"Please confirm the {en} shift allowance of {t}", "Workforce › Allowance confirmation.",
              f"/attendance/allowance/confirm?month={month}", actor)
    audit(repo, user, "allowance_round_resent" if rnd.get("resent_at") else "allowance_round_started", "allowance", month,
          {"employees": len(pending_emps), "teams": len(teams), "mails": mails})
    return {"month": month, "employees": len(pending_emps), "teams": len(teams), "mails": mails}


def _require_round(repo, month):
    rnd = get_round(repo, month)
    if not rnd:
        raise ImportError_("This month has not been sent for confirmation yet.")
    if (rnd.get("gm") or {}).get("status") == "confirmed":
        raise ImportError_("The General Manager already confirmed this month: it is closed.")
    return rnd


def employee_answer(repo, user, month, answer, note=""):
    """The employee confirms their own days, or objects with a note."""
    month = month_of(month) or ""
    _require_round(repo, month)
    emp = repo.get_employee(int(user["user_id"])) if str(user["user_id"]).isdigit() else None
    if not emp:
        raise Forbidden("Only an employee confirms their own shift allowance.")
    note = (note or "").strip()[:500]
    if answer not in ("confirm", "object"):
        raise ImportError_("Choose Confirm or Object.")
    if answer == "object" and not note:
        raise ImportError_("Write what is wrong, so your manager can check it.")
    nums, _, currency, _ = figures(repo, month, [emp])
    now = _now(repo)

    def change(rnd, data):
        team_done = [t for t in emp["teams"] if (rnd["teams"].get(t) or {}).get("status") == "confirmed"]
        if team_done:
            raise ImportError_(f"Your manager already confirmed {', '.join(team_done)} for this month.")
        rnd["employees"][str(emp["id"])] = {"status": "confirmed" if answer == "confirm" else "objected", "note": note, "at": now,
                                            "by": user["user_id"], "by_name": user.get("name"), "on_behalf": False,
                                            "figures": nums.get(emp["id"])}
    _save(repo, month, change)
    if answer == "object":
        en, ar = label(month)
        users = repo.directory.list_users()
        for t in emp["teams"]:
            managers = [m["user_id"] for m in _team_managers(repo, t, users)]
            body = _mail_html(f"اعتراض على بدل ورديات {ar}: {emp['name']}", f"Objection to the {en} shift allowance: {emp['name']}",
                              [f"اعترض {emp['name']} على بدل ورديات {ar}:", note, "برجاء المراجعة من خلال البورتال قبل تعزيز الفريق."],
                              [f"{emp['name']} objected to the {en} shift allowance:", note, "Please check it on the portal before confirming the team."])
            _queue(repo, managers, f"Objection: {emp['name']} · {en} shift allowance · اعتراض على بدل الورديات", body, "allowance_objection")
            _bell(managers, f"{emp['name']} objected to the {en} shift allowance", note, f"/attendance/allowance/confirm?month={month}&team={t}",
                  user.get("name") or user["user_id"])
    return answer


NO_TEAM = "(no team)"           # employees on no team: confirmed by an administrator


def _team_people(repo, team):
    if team == NO_TEAM:
        return [e for e in repo.list_employees() if not e["teams"]]
    return [e for e in repo.list_employees() if team.strip().lower() in [t.strip().lower() for t in e["teams"]]]


def _all_teams(repo):
    emps = repo.list_employees()
    return {t for e in emps for t in e["teams"]} | ({NO_TEAM} if any(not e["teams"] for e in emps) else set())


def _manages(user, team):
    """An administrator, or a manager of ``team`` (a manager with every team, «__all__», manages every team - as everywhere in Attendance)."""
    from .access import in_scope
    return user["role"] == "admin" or (team != NO_TEAM and in_scope({**user, "role": "manager"}, team))


def confirm_member(repo, user, month, employee_id):
    """A team manager confirms one person on their behalf (an objection settled)."""
    month = month_of(month) or ""
    _require_round(repo, month)
    emp = repo.get_employee(int(employee_id))
    if not emp or not (user["role"] in ("admin",) or (user["role"] == "manager" and any(_manages(user, t) for t in emp["teams"]))):
        raise Forbidden("You can confirm only the people of your own team(s).")
    nums, _, _, _ = figures(repo, month, [emp])
    now = _now(repo)

    def change(rnd, data):
        old = rnd["employees"].get(str(emp["id"])) or {}
        rnd["employees"][str(emp["id"])] = {"status": "confirmed", "note": old.get("note", ""), "at": now, "by": user["user_id"],
                                            "by_name": user.get("name"), "on_behalf": True, "figures": nums.get(emp["id"]),
                                            "objection": old.get("note") if old.get("status") == "objected" else None}
    _save(repo, month, change)
    return emp


def confirm_team(repo, user, month, team):
    """The team's manager confirms the team: everybody not confirmed yet is confirmed on their behalf. Tells the GM."""
    month = month_of(month) or ""
    _require_round(repo, month)
    if not (user["role"] == "admin" or (user["role"] == "manager" and _manages(user, team))):
        raise Forbidden("You can confirm only your own team(s).")
    people = _team_people(repo, team)
    if not people:
        raise ImportError_("This team has nobody to confirm.")
    nums, _, _, _ = figures(repo, month, people)
    now = _now(repo)

    def change(rnd, data):
        for e in people:
            rec = rnd["employees"].get(str(e["id"])) or {}
            if rec.get("status") != "confirmed":
                rnd["employees"][str(e["id"])] = {"status": "confirmed", "note": rec.get("note", ""), "at": now, "by": user["user_id"],
                                                  "by_name": user.get("name"), "on_behalf": True, "figures": nums.get(e["id"]),
                                                  "objection": rec.get("note") if rec.get("status") == "objected" else None}
        rnd["teams"][team] = {"status": "confirmed", "by": user["user_id"], "by_name": user.get("name"), "at": now,
                              "amount": sum((nums.get(e["id"]) or {}).get("amount", 0) for e in people), "people_count": len(people)}
        return all((rnd["teams"].get(t) or {}).get("status") == "confirmed" for t in _all_teams(repo))
    all_done = _save(repo, month, change)
    en, ar = label(month)
    gms = [g_["user_id"] for g_ in _gms(repo)]
    actor = user.get("name") or user["user_id"]
    _bell(gms, f"{team} confirmed the {en} shift allowance", "Every team: Workforce › Allowance confirmation.",
          f"/attendance/allowance/confirm?month={month}", actor)
    if all_done:
        body, subject = _ask_gm(month)
        _queue(repo, gms, subject, body, "allowance_gm")
        _bell(gms, f"Please confirm the {en} shift allowance of the sector", "Every team manager confirmed.",
              f"/attendance/allowance/confirm?month={month}", actor)
    return {"team": team, "people": len(people), "all_teams_confirmed": all_done}


def confirm_gm(repo, user, month):
    """The General Manager confirms the whole sector (every team confirmed first); the administrative affairs are told."""
    month = month_of(month) or ""
    _require_round(repo, month)
    if user["role"] != "gm":
        raise Forbidden("Only the General Manager confirms the sector.")
    st = status(repo, month)
    if not st["all_teams_confirmed"]:
        waiting = [t["team"] for t in st["teams"] if t["status"] != "confirmed"]
        raise ImportError_("Every team manager must confirm first. Waiting for: " + ", ".join(waiting) + ".")
    now = _now(repo)

    def change(rnd, data):
        rnd["gm"] = {"status": "confirmed", "by": user["user_id"], "by_name": user.get("name"), "at": now,
                     "amount": st["total_amount"]}
        rnd["done_notified_at"] = now
    _save(repo, month, change)
    en, ar = label(month)
    rows = [(f"{t['team']} - {t.get('by_name') or ''}", money(t["amount"], st["currency"]) or f"{len(t['people'])} people")
            for t in st["teams"]]
    rows.append(("المدير العام / General Manager", user.get("name") or user["user_id"]))
    if st["total_amount"]:
        rows.append(("الإجمالي / Total", money(st["total_amount"], st["currency"])))
    body = _mail_html(f"تم التعزيز على بدل ورديات {ar} بالكامل", f"The {en} shift allowance is fully confirmed",
                      ["كل مديري الفرق عززوا بدل ورديات فرقهم، والمدير العام عزز على بدل ورديات القطاع.",
                       "تقدر تنزل ملف بدل الورديات من البورتال (بدل الورديات › تصدير)."],
                      ["Every team manager confirmed their team and the General Manager confirmed the whole sector.",
                       "Download the shift-allowance file on the portal (Shift allowance › Export)."], rows, st["currency"])
    affairs = [u["user_id"] for u in _admin_affairs(repo)]
    _queue(repo, affairs, f"The {en} shift allowance is fully confirmed · تم التعزيز على بدل ورديات {ar}", body, "allowance_done")
    _bell(affairs, f"The {en} shift allowance is fully confirmed", "Every manager and the General Manager confirmed.",
          f"/attendance/allowance?from={month}-01", user.get("name") or user["user_id"])
    return st


# ------------------------------------------------------------------------------------------------ reminders (the collector)
def queue_reminders(repo, now, store=None):
    """Once a working day at the reminder time: remind whoever has not confirmed the open month(s). Returns the mails queued.
    ``store``: the collector's operations store (the outbox)."""
    _STORE["ops"] = store
    try:
        return _queue_reminders(repo, now)
    finally:
        _STORE["ops"] = None


def _queue_reminders(repo, now):
    st = settings_svc.load(repo)
    if not st.get("allowance_reminders", True):
        return 0
    if now.strftime("%H:%M") < str(st.get("allowance_reminder_time") or "10:00") or now.weekday() in (st.get("weekly_rest_weekdays") or []):
        return 0
    today = now.date().isoformat()
    mails = 0
    for month, rnd in sorted(rounds(repo).items()):
        if (rnd.get("gm") or {}).get("status") == "confirmed" or today in (rnd.get("reminded") or []):
            continue
        if rnd.get("started_at", "")[:10] == today:
            continue                                                    # asked today already

        def claim(r, data, today=today):
            """Take today's reminders under the file lock first: a second collector PC finds the day taken (never twice)."""
            if r is None or today in (r.get("reminded") or []):
                return False
            r.setdefault("reminded", []).append(today)
            r["reminded"] = r["reminded"][-60:]
            return True
        if not _save(repo, month, claim):
            continue
        info = status(repo, month)
        currency = info["currency"]
        waiting_people = {}
        users = repo.directory.list_users()
        for t in info["teams"]:
            if t["status"] == "confirmed":
                continue
            for p in t["people"]:
                if p["status"] == "pending":
                    waiting_people[p["id"]] = p
            managers = [m["user_id"] for m in _team_managers(repo, t["team"], users)]
            body, subject = _ask_manager(month, t["team"], reminder=True)
            mails += _queue(repo, managers, subject, body, "allowance_reminder")
        for p in waiting_people.values():
            body, subject = _ask_employee(repo, month, p, p["figures"], currency, reminder=True)
            mails += _queue(repo, [p["id"]], subject, body, "allowance_reminder")
        if info["all_teams_confirmed"]:
            body, subject = _ask_gm(month, reminder=True)
            mails += _queue(repo, [g_["user_id"] for g_ in _gms(repo)], subject, body, "allowance_reminder")
    return mails
