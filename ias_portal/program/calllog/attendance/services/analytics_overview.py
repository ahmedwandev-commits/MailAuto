"""Analytics overview: the picture of a period (indicators and charts). The tables behind every number are in Reports."""
import calendar
from collections import Counter, defaultdict
from datetime import date

from ..domain import daily
from . import analytics_service as an

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _month_ends(d2, count):
    y, m = int(d2[:4]), int(d2[5:7])
    out = []
    for _ in range(count):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def build(repo, user, d1, d2, now, team=None):
    evaluated = {}                                    # the period's days, evaluated once for every chart below
    rows, rules, version = an.team_rows(repo, user, d1, d2, now, team, days_out=evaluated)
    ids = [r["employee"]["id"] for r in rows]
    S = lambda f: sum(r["summary"][f] for r in rows)              # noqa: E731
    scheduled, present = S("scheduled_days"), S("present_days")
    judged = S("judged_days")
    k = {"employees": len(rows), "attendance_pct": round(100 * present / judged, 1) if judged else None, "scheduled_days": scheduled, "present_days": present,
         "late_count": S("late_count"), "late_minutes": S("late_minutes"), "absent": S("absent_days"), "verified_unexcused": S("verified_unexcused_days"),
         "missing": S("missing_in") + S("missing_out") + S("missing_both"), "leave": S("leave_days"), "rest": S("rest_days"),
         "permits": S("late_permit_required") + S("early_permit_required"),
         "avg_score": round(sum(r["score"]["score"] for r in rows) / len(rows), 1) if rows else None}
    # the lateness by weekday needs the days themselves
    by_wd = defaultdict(lambda: [0, 0])
    if ids:
        for days in evaluated.values():
            for d in days:
                if d["late_minutes"] > 0:
                    w = date.fromisoformat(d["date"]).weekday()
                    by_wd[w][0] += 1
                    by_wd[w][1] += d["late_minutes"]
    weekdays = [{"label": WEEKDAYS[i], "count": by_wd[i][0], "minutes": by_wd[i][1]} for i in range(7)]
    teams = an.compare_groups(rows, "team")
    cats = Counter((r["score"]["category"] or ("Not enough days" if r["score"]["insufficient_data"] else "No category")) for r in rows)
    order = [c["name"] for c in sorted(rules.get("categories", []), key=lambda c: -c["min"])]
    categories = [(n, cats.pop(n, 0)) for n in order] + sorted(cats.items())
    trend = []
    for y, m in _month_ends(d2, 6):
        m1 = f"{y}-{m:02d}-01"
        m2 = f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"          # 29 February in a leap year too
        # the month of the period itself is already evaluated (rows): not a second time
        mr = rows if (m1, m2) == (d1, d2) else an.team_rows(repo, user, m1, m2, now, team)[0]
        sch = sum(r["summary"]["judged_days"] for r in mr)
        pres = sum(r["summary"]["present_days"] for r in mr)
        trend.append({"month": f"{y}-{m:02d}", "pct": round(100 * pres / sch, 1) if sch else None, "late": sum(r["summary"]["late_count"] for r in mr)})
    top_late = sorted(((r["employee"]["id"], r["employee"]["name"], r["summary"]["late_count"], r["summary"]["late_minutes"]) for r in rows if r["summary"]["late_count"]),
                      key=lambda x: (-x[2], -x[3]))[:5]
    # one plain sentence per chart: what to take from it
    insight = {}
    worst = max(weekdays, key=lambda w: w["count"])
    insight["weekdays"] = (f"Most late arrivals fall on {worst['label']}: {worst['count']} times, {worst['minutes']} minutes in all." if worst["count"] else "Nobody was late in this period.")
    if k["scheduled_days"]:
        parts = [f"{k['present_days']} attended ({round(100 * k['present_days'] / k['scheduled_days'])}%)"]
        if k["absent"]:
            parts.append(f"{k['absent']} absent and not reviewed yet")
        insight["days"] = f"Out of {k['scheduled_days']} planned days: " + ", ".join(parts) + "."
    rated = [t for t in teams if t.get("attendance_pct") is not None]
    if rated:
        low = min(rated, key=lambda t: t["attendance_pct"])
        insight["teams"] = f"Lowest attendance: {low['group']} ({low['attendance_pct']:g}%)."
    known = [(n, c) for n, c in categories if c]
    if known:
        n, c = max(known, key=lambda x: x[1])
        insight["categories"] = f"Most people are “{n}” ({c} of {len(rows)})."
    done = [m for m in trend if m["pct"] is not None]
    if len(done) >= 2:
        d = done[-1]["pct"] - done[-2]["pct"]
        insight["trend"] = f"{done[-1]['month']}: {done[-1]['pct']:g}% attendance, " + ("up" if d > 0 else "down" if d < 0 else "the same as") + (f" {abs(d):g} points on {done[-2]['month']}." if d else f" {done[-2]['month']}.")
    return {"k": k, "insight": insight, "weekdays": weekdays, "teams": teams, "categories": categories, "trend": trend, "top_late": top_late, "configured": rows and rows[0]["score"]["configured"]}
