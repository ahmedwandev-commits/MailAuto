"""Leave requests, balances and compensatory days. Balances need entitlement data entered by an administrator -
without it the balance is reported as unavailable, never guessed."""
from datetime import date

from ..repo import ConflictError
from . import access, audit
from .schedule_service import ImportError_


def _days(d1, d2):
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1


def request_leave(repo, user, employee_id, leave_type, date_from, date_to, reason, days=None):
    """Record leave. An administrator or a manager (within their teams) records it directly: it counts at once, there is no approval
    step. The audit entry carries who, when, the employee, type, dates and reason."""
    emp = access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    if leave_type not in {t["code"] for t in repo.list_leave_types()}:
        raise ImportError_("Unknown leave type.")
    try:
        n = _days(date_from, date_to)
        # stored as YYYY-MM-DD: the overlap check and every period filter compare the dates as text
        date_from, date_to = date.fromisoformat(date_from).isoformat(), date.fromisoformat(date_to).isoformat()
    except (TypeError, ValueError) as exc:
        raise ImportError_("Enter valid dates.") from exc
    if n < 1:
        raise ImportError_("The end date is before the start date.")
    try:
        days = float(days) if days else float(n)
    except (TypeError, ValueError) as exc:
        raise ImportError_("Enter the number of days as a number.") from exc
    if not 0 < days <= n:                  # a balance must never be charged more (or less than nothing) for the dates recorded
        raise ImportError_(f"The number of days must be more than 0 and at most {n:g} for these dates.")
    try:
        rid = repo.create_leave_request({"employee_id": employee_id, "leave_type": leave_type, "date_from": date_from,
                                         "date_to": date_to, "days": days, "status": "approved",
                                         "reason": (reason or "").strip(), "created_by": user["user_id"]})
    except ConflictError as exc:
        raise ImportError_(str(exc)) from exc
    repo.decide_leave_request(rid, "approved", user["user_id"])
    audit(repo, user, "leave_recorded", "leave_request", rid,
          {"employee_id": employee_id, "employee": emp["name"], "previous": None,
           "new": {"type": leave_type, "from": date_from, "to": date_to, "days": days, "status": "approved"},
           "reason": (reason or "").strip()})
    return rid


def cancel_leave(repo, user, request_id, reason=""):
    """Cancel a leave record (direct; the previous status and the reason are kept in the audit trail)."""
    if not access.can_write(user):
        raise access.Forbidden("Not allowed.")
    req = next((r for r in repo.leave_requests() if r["id"] == request_id), None)
    if not req:
        raise ImportError_("Leave record not found.")
    access.require_employee(repo, user, req["employee_id"])
    if req["status"] == "cancelled":
        raise ImportError_("That leave is already cancelled.")
    repo.decide_leave_request(request_id, "cancelled", user["user_id"])
    audit(repo, user, "leave_cancelled", "leave_request", request_id,
          {"employee_id": req["employee_id"], "previous": {"status": req["status"], "type": req["leave_type"], "from": req["date_from"], "to": req["date_to"]},
           "new": {"status": "cancelled"}, "reason": (reason or "").strip()})

def balances(repo, user, year, employee_ids=None):
    """Per employee: {type: {entitlement, opening, taken, pending, remaining|None}}. Taken = approved requests only (a request
    is one row, so a day can never be deducted twice); remaining is None when no entitlement was entered."""
    ids = access.visible_ids(repo, user) if employee_ids is None else employee_ids
    types = repo.list_leave_types()
    ent = {(e["employee_id"], e["leave_type"]): e for e in repo.entitlements(year, ids)}
    reqs = repo.leave_requests(ids, f"{year}-01-01", f"{year}-12-31")
    out = {}
    for emp in ids:
        per = {}
        for t in types:
            e = ent.get((emp, t["code"]))
            taken = pend = 0.0
            for r in reqs:
                if r["employee_id"] == emp and r["leave_type"] == t["code"] and r["date_from"][:4] == str(year):
                    if r["status"] == "approved":
                        taken += r["days"]
                    elif r["status"] == "pending":
                        pend += r["days"]
            per[t["code"]] = {"entitlement": e["entitlement"] if e else None, "opening": e["opening_balance"] if e else None,
                              "taken": taken, "pending": pend, "counts": t["counts_against_balance"],
                              "remaining": (e["entitlement"] + e["opening_balance"] - taken) if e else None}
        out[emp] = per
    return out


def leave_totals(repo, user, employee_id, d1, d2):
    """Leave-day totals per type and request counts by status for the period (used by the employee profile)."""
    access.require_employee(repo, user, employee_id)
    reqs = repo.leave_requests([employee_id], d1, d2)
    by_type, status = {}, {"approved": 0, "pending": 0, "rejected": 0, "cancelled": 0}
    for r in reqs:
        status[r["status"]] += 1
        if r["status"] == "approved":
            lo, hi = max(r["date_from"], d1), min(r["date_to"], d2)
            by_type[r["leave_type"]] = by_type.get(r["leave_type"], 0) + max(0, _days(lo, hi))
    return {"by_type": by_type, "total": sum(by_type.values()), "status": status}


# ---------------------------------------------------------------- compensatory days
def earn_comp(repo, user, employee_id, earned_date, days, reason, planned_take_date=None):
    access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    if not (reason or "").strip():
        raise ImportError_("The reason the day was earned is required.")
    try:
        date.fromisoformat(earned_date)
        d = float(days or 1)
    except ValueError as exc:
        raise ImportError_("Enter a valid date and number of days.") from exc
    cid = repo.add_comp_day({"employee_id": employee_id, "earned_date": earned_date, "days": d, "reason": reason.strip(),
                             "planned_take_date": planned_take_date or None, "created_by": user["user_id"]})
    audit(repo, user, "comp_day_earned", "comp_day", cid, {"employee_id": employee_id, "days": d, "reason": reason.strip()})
    return cid


def update_comp(repo, user, comp_id, planned_take_date=None, taken_date=None):
    row = next((c for c in repo.comp_days() if c["id"] == comp_id), None)
    if not row:
        raise ImportError_("Not found.")
    access.require_employee(repo, user, row["employee_id"])
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    patch = {}
    if planned_take_date is not None:
        patch["planned_take_date"] = planned_take_date or None
    if taken_date is not None:
        patch["taken_date"] = taken_date or None
    repo.update_comp_day(comp_id, patch)
    audit(repo, user, "comp_day_updated", "comp_day", comp_id, patch)


def comp_balances(repo, user, employee_ids=None):
    ids = access.visible_ids(repo, user) if employee_ids is None else employee_ids
    out = {i: {"earned": 0.0, "taken": 0.0, "planned_open": 0.0, "remaining": 0.0} for i in ids}
    for c in repo.comp_days(ids):
        b = out[c["employee_id"]]
        b["earned"] += c["days"]
        if c["taken_date"]:
            b["taken"] += c["days"]
        elif c["planned_take_date"]:
            b["planned_open"] += c["days"]
    for b in out.values():
        b["remaining"] = b["earned"] - b["taken"]
    return out
