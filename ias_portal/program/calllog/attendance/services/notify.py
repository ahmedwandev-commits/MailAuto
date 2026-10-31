"""Bell notifications of the Attendance module (the portal's own bell, calllog/ops/notifications.py) - the owner's rule:

* a new (or replaced) monthly schedule -> every employee in it ("your November 2026 schedule is out", opens My schedule) and
  the managers of their teams (opens the month's calendar);
* a shift changed (weekly plan, monthly calendar, Changes) -> that employee.

A notification never breaks the action that caused it: every failure here is swallowed."""
import logging
from datetime import date

from flask import current_app

log = logging.getLogger(__name__)

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]
SHIFT_WORDS = {"M": "Morning", "E": "Evening", "N": "Night", "D": "Normal day", "SB": "Stand-by", "OFF": "Rest day"}


def _ops():
    """The portal's operations store (the bell), or None outside a running portal (a script, a test)."""
    try:
        return current_app.extensions.get("ops")
    except RuntimeError:
        return None


def _codes_text(entries):
    codes = [e.get("shift_code") if isinstance(e, dict) else e for e in entries or []]
    return " + ".join(SHIFT_WORDS.get(c, str(c)) for c in codes if c) or "nothing"


def schedule_published(repo, user, year, month, employee_ids, replaced=False):
    """Tell the employees of a new monthly schedule and the managers of their teams."""
    try:
        from calllog.ops import notifications, schemas
        store = _ops()
        if store is None:
            return []
        label = f"{MONTHS[month - 1]} {year}"
        ids = sorted({str(i) for i in employee_ids or ()})
        word = "updated" if replaced else "out"
        actor = (user or {}).get("name") or (user or {}).get("user_id") or ""
        told = notifications.notify_people(store, ids, schemas.NOTIFY_SCHEDULE, f"Your {label} schedule is {word}",
                                           "Open My schedule to see your shifts.", "/attendance/my", actor)
        teams = set()
        for i in ids:
            acct = repo.directory.get_user(i) or {}
            teams.update(acct.get("teams") or [])
        managers = [u["user_id"] for u in repo.directory.list_users()
                    if u.get("hub_role") == "manager" and u.get("active", True) and teams & set(u.get("teams") or [])
                    and u["user_id"] not in ids]
        told += notifications.notify_people(store, managers, schemas.NOTIFY_SCHEDULE,
                                            f"The {label} schedule of your team is {word}",
                                            f"{len(ids)} employee(s) in it - open the monthly calendar.",
                                            f"/attendance/schedule?year={year}&month={month}", actor)
        return told
    except Exception:                                       # noqa: BLE001 - never breaks the import
        log.exception("attendance schedule notification failed")
        return []


def missing_punches(repo, user, by_emp):
    """After an HR attendance file is imported (owner's rule, v5.16): every employee with a forgotten check-in or check-out on
    a working day is told - bell + e-mail - and the direct manager(s) of their team get one list of their people. ``by_emp``:
    {employee_id: [(date, 'in'|'out')]} (only the days this file added). Returns how many people were told."""
    if not by_emp:
        return 0
    try:
        from . import allowance_confirm as ac
        actor = (user or {}).get("name") or (user or {}).get("user_id") or ""
        word = {"in": ("check-in", "بصمة حضور"), "out": ("check-out", "بصمة انصراف")}
        per_manager = {}
        users = repo.directory.list_users()                  # once for the whole file, not once per person
        for emp_id, days in sorted(by_emp.items()):
            emp = repo.get_employee(int(emp_id))
            if not emp:
                continue
            days = sorted(days)
            lines_en = [f"{date.fromisoformat(d).strftime('%a %d/%m/%Y')}: no {word[k][0]}" for d, k in days]
            lines_ar = [f"{date.fromisoformat(d).strftime('%d/%m/%Y')}: لا توجد {word[k][1]}" for d, k in days]
            body = ac._mail_html("لديك نسيان بصمة حضور أو انصراف", "You have a forgotten check-in or check-out",
                                 ["حسب ملف الحضور والانصراف الأخير لديك نسيان بصمة في الأيام التالية:", *lines_ar,
                                  "برجاء عمل اللازم (إذن / تصحيح البصمة) والتواصل مع مديرك."],
                                 ["The latest attendance file shows a forgotten punch on these days:", *lines_en,
                                  "Please get it fixed (permit / punch correction) and tell your manager."])
            ac._queue(repo, [emp["id"]], "Forgotten check-in / check-out · نسيان بصمة حضور أو انصراف", body, "missing_punch")
            ac._bell([emp["id"]], f"Forgotten punch on {len(days)} day(s)", "; ".join(lines_en[:4]) + (" …" if len(days) > 4 else ""),
                     "/attendance/my", actor)
            for t in emp["teams"]:
                for m in ac._team_managers(repo, t, users):
                    if str(m["user_id"]) != str(emp["id"]):
                        per_manager.setdefault(m["user_id"], []).append((emp, days))
        for manager_id, people in per_manager.items():
            rows = [(f"{e['name']} ({e['id']})", ", ".join(f"{date.fromisoformat(d).strftime('%d/%m')} {word[k][0]}" for d, k in days))
                    for e, days in people]
            body = ac._mail_html("موظفون لديهم نسيان بصمة حضور أو انصراف", "People of your team with a forgotten check-in or check-out",
                                 [f"حسب ملف الحضور والانصراف الأخير، {len(people)} من فريقك لديهم نسيان بصمة:"],
                                 [f"The latest attendance file shows {len(people)} of your people with a forgotten punch:"], rows)
            ac._queue(repo, [manager_id], f"{len(people)} of your team forgot a check-in / check-out · نسيان بصمة في فريقك", body,
                      "missing_punch_manager")
            ac._bell([manager_id], f"{len(people)} of your team forgot a check-in / check-out",
                     ", ".join(e["name"] for e, _ in people[:5]) + (" …" if len(people) > 5 else ""), "/attendance/planning/day", actor)
        return len(by_emp)
    except Exception:                                       # noqa: BLE001 - never breaks the import
        log.exception("missing punch notification failed")
        return 0


def shift_changed(repo, user, employee_id, work_date, old, new, reason=""):
    """Tell one employee that a manager changed their shift of ``work_date``."""
    try:
        from calllog.ops import notifications, schemas
        store = _ops()
        if store is None or str(employee_id) == str((user or {}).get("user_id")):
            return []
        before, after = _codes_text(old), _codes_text(new)
        if before == after:
            return []
        day = date.fromisoformat(work_date).strftime("%a %d %b")
        actor = (user or {}).get("name") or (user or {}).get("user_id") or ""
        return notifications.notify_people(store, [employee_id], schemas.NOTIFY_SHIFT, f"Your shift on {day} changed",
                                           f"{before} → {after}" + (f" · {reason}" if reason else ""), "/attendance/my", actor)
    except Exception:                                       # noqa: BLE001 - never breaks the change
        log.exception("attendance shift notification failed")
        return []
