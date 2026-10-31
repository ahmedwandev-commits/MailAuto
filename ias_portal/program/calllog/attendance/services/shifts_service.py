"""Monthly shifts: what the system counts as shifts for the people of a team in a month, and the manager's approval of it. When a manager
approves the month of their team, those shifts reach the allowance sheet of the administrator. If anything changes afterwards (a shift
modified, an HR file imported) the approval shows "changed after approval" until the manager approves again."""
import hashlib

from ..domain import allowance as al
from . import access, audit
from . import allowance_service as asv
from .schedule_service import ImportError_


def digest(days):
    """A short fingerprint of the counted days (employee, date, type): equal digests = the same sheet."""
    text = "|".join(sorted(f"{d['employee_id']}:{d['date']}:{d['kind']}" for d in days))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def team_employees(repo, user, team):
    return [e for e in access.visible_employees(repo, user) if team.strip().lower() in [t.lower() for t in e["teams"]]]


def _status(rec, days):
    if not rec:
        return "pending"
    return "approved" if rec["digest"] == digest(days) else "changed"


def month_view(repo, user, month, team):
    """The people of ``team`` with their counted days for ``month`` ('YYYY-MM') and the state of the manager's approval."""
    d1, d2 = asv.month_bounds(month)
    employees = team_employees(repo, user, team)
    days, skipped, rules, st = asv.compute(repo, d1, d2, employees)
    names = {e["id"]: e["name"] for e in employees}
    summary = al.summarise(days)
    rows = [{"employee_id": e["id"], "name": e["name"], **summary.get(e["id"], {k: {"count": 0} for k in (*al.KINDS, *al.OUT)} | {"total_count": 0})} for e in
            sorted(employees, key=lambda e: (e["name"], e["id"]))]
    rec = repo.shift_approvals().get(f"{month}|{team}")
    return {"month": month, "team": team, "from": d1, "to": d2, "rows": rows, "days": days, "skipped": skipped, "names": names,
            "kinds": al.enabled_kinds(rules), "rules": rules, "status": _status(rec, days), "approval": rec, "digest": digest(days),
            "totals": {k: sum(r[k]["count"] for r in rows) for k in al.OUT} | {"total_count": sum(r["total_count"] for r in rows)}}


def _may_approve(user, team):
    return user["role"] == "manager" and access.in_scope(user, team)             # only the team's own manager: not the administrator, not the GM


def approve(repo, user, month, team):
    """The manager of ``team`` (only) approves the shifts of the month as the system counts them now."""
    if not _may_approve(user, team):
        raise access.Forbidden("Only the manager of that team can approve its shifts.")
    mv = month_view(repo, user, month, team)
    if not mv["rows"]:
        raise ImportError_("That team has nobody to approve.")
    rec = {"month": month, "team": team, "by": user["user_id"], "by_name": user["name"], "at": repo._now() if hasattr(repo, "_now") else "",
           "digest": mv["digest"], "days": len(mv["days"]), "employees": len(mv["rows"])}
    repo.set_shift_approval(month, team, rec)
    audit(repo, user, "shifts_approved", "shift_approval", f"{month}|{team}", {"days": rec["days"], "employees": rec["employees"], "digest": rec["digest"]})
    return rec


def reopen(repo, user, month, team):
    if not _may_approve(user, team):
        raise access.Forbidden("Only the manager of that team can reopen its approval.")
    repo.delete_shift_approval(month, team)
    audit(repo, user, "shifts_reopened", "shift_approval", f"{month}|{team}", {})


def statuses(repo, months, teams):
    """The approval rows of the allowance page: [{month, team, status, by, at}] for every month x team (a team's days are recomputed only
    when it has an approval, to see whether anything changed since)."""
    approvals = repo.shift_approvals()
    everyone = repo.list_employees()
    out = []
    for ym in months:
        d1, d2 = asv.month_bounds(ym)
        for team in teams:
            rec = approvals.get(f"{ym}|{team}")
            status = "pending"
            if rec:
                members = [e for e in everyone if team.strip().lower() in [t.lower() for t in e["teams"]]]
                days = asv.compute(repo, d1, d2, members)[0]
                status = _status(rec, days)
            out.append({"month": ym, "team": team, "status": status, "by": (rec or {}).get("by_name"), "at": ((rec or {}).get("at") or "")[:16].replace("T", " ")})
    return out
