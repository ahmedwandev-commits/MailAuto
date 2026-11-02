"""My schedule: an employee's own shifts (the next two weeks and a month with the real check-in / check-out). It shows only the signed-in
person's rows - the employee number is the account's User ID, never a request parameter."""
import calendar
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import auth
from ..domain import daily
from ..services import allowance_confirm as ac, analytics_service as an
from ..services.schedule_service import ImportError_, shifts_by_code

bp = Blueprint("att_mine", __name__)


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@bp.get("/my")
@auth.login_required
def index():
    if not g.att_user.get("is_employee"):
        raise ImportError_("Your account is not on the attendance roster, so there is no schedule to show.")
    repo = auth.repo()
    emp_id = int(g.att_user["user_id"])                       # always the signed-in person
    emp = repo.get_employee(emp_id)
    now, today = auth.now(), auth.today()
    t = date.fromisoformat(today)
    year, month = _int(request.args.get("year"), t.year), _int(request.args.get("month"), t.month)
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        year, month = t.year, t.month
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    shifts = shifts_by_code(repo)
    ahead_to = (t + timedelta(days=13)).isoformat()
    ev = an.evaluate_range(repo, [emp_id], min(first.isoformat(), today), max(last.isoformat(), ahead_to), now)[emp_id]
    by = {d["date"]: d for d in ev}
    upcoming = [by[(t + timedelta(days=i)).isoformat()] for i in range(14)]
    month_days = [by[(first + timedelta(days=i)).isoformat()] for i in range((last - first).days + 1)]
    summary = daily.summarize([d for d in month_days if d["date"] <= today], an.settings_svc.load(repo)["repeat_late_threshold"])
    prev_m, next_m = (first - timedelta(days=1)), (last + timedelta(days=1))
    return render_template("attendance/mine.html", emp=emp, upcoming=upcoming, month_days=month_days, summary=summary, shifts=shifts,
                           year=year, month=month, today=today, first=first, prev_m=prev_m, next_m=next_m,
                           allowance=_allowance_cards(repo, emp))


def _allowance_cards(repo, emp):
    """The months sent for confirmation that are still open (the General Manager has not closed them): my days and my answer."""
    out = []
    for m, rnd in sorted(ac.rounds(repo).items(), reverse=True):
        if (rnd.get("gm") or {}).get("status") == "confirmed":
            continue
        nums, rates, currency, not_counted = ac.figures(repo, m, [emp])
        rec = (rnd.get("employees") or {}).get(str(emp["id"])) or {}
        team_done = any(((rnd.get("teams") or {}).get(t) or {}).get("status") == "confirmed" for t in emp["teams"])
        out.append({"month": m, "label": ac.label(m), "figures": nums.get(emp["id"]), "rates": rates, "currency": currency,
                    "status": rec.get("status", "pending"), "note": rec.get("note", ""), "at": rec.get("at"), "on_behalf": rec.get("on_behalf"),
                    "by_name": rec.get("by_name"), "team_done": team_done, "not_counted": not_counted.get(emp["id"], []),
                    "kinds": [(k, ac.al.ENGLISH[k]) for k in ac.al.KINDS], "days": ac.days_of(repo, m, emp)})
    return out


@bp.post("/my/allowance")
@auth.login_required
def allowance_answer():
    if not g.att_user.get("is_employee"):
        raise ImportError_("Your account is not on the attendance roster.")
    month = request.form.get("month", "")
    answer = ac.employee_answer(auth.repo(), g.att_user, month, request.form.get("answer"), request.form.get("note"))
    flash("Thank you - your shift allowance is confirmed." if answer == "confirm" else
          "Your objection was sent to your team manager.", "ok")
    return redirect(url_for("att_mine.index"))
