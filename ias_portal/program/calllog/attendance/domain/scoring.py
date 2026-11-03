"""Transparent, fully configurable attendance & punctuality score.

No HR policy is built in: the shipped rule set has **no penalties and no categories** and says so (``configured: False``).
Management enters the values on *Administration -> Scoring rules*; every change is a new versioned rule set, so old periods are
still scored with the rules that applied to them.

Units: every penalty is a PERCENTAGE of the start score (start 100 -> 1 % = 1 point). Nothing below is a fixed formula - each
component has a *base* penalty and an optional *repeat* rule, and the repeat rule is where "repeated lateness over several days" is
defined by management:

  base      late / early : tiers by minutes (``tiers``: 1-15 = 1 %, 16-30 = 3 %, ...) and/or a flat % per occurrence (``flat_pct``);
                           lateness shorter than ``ignore_under_minutes`` is ignored
            absence      : ``penalty_per_day`` per VERIFIED unexcused absence
            missing_punch: ``penalty_per_day`` per day with a missing IN or OUT
            rejected_permit: ``penalty_per_day`` per permit (اذن حضور / اذن انصراف) that a manager REJECTED (never for a missing one)
  repeat    ``window``      : which earlier occurrences count: "period" (the whole evaluated period), "month" (the calendar month of the
                              occurrence) or "rolling" (the last ``rolling_days`` days)
            ``steps``       : by the ORDER of the occurrence inside the window: {"from": 3, "to": 5, "extra": 1}  (+1 % on top of the base) or
                              {"from": 6, "to": null, "factor": 2}  (the base multiplied by 2); both may be given
            ``consecutive_days`` / ``consecutive_extra`` : every occurrence that belongs to a run of at least N consecutive scheduled
                              days gets this extra % (days off and leave do not break a run)
  caps      ``max_deduction`` per component and ``max_total_deduction`` overall (blank = none); the score never goes below 0.
"""
from datetime import date, timedelta


def _repeat():
    return {"window": "period", "rolling_days": 30, "steps": [], "consecutive_days": None, "consecutive_extra": None}


def _component():
    return {"tiers": [], "flat_pct": None, "ignore_under_minutes": 0, "max_deduction": None, "repeat": _repeat()}


def default_rules():
    return {
        "start": 100,
        "late": _component(),
        "early": _component(),
        "absence": {"penalty_per_day": 0, "max_deduction": None, "repeat": _repeat()},
        "missing_punch": {"penalty_per_day": 0, "max_deduction": None, "repeat": _repeat()},
        "rejected_permit": {"penalty_per_day": 0, "max_deduction": None, "repeat": _repeat()},
        "max_total_deduction": None,
        "categories": [],
        "min_judged_days": 1,
    }


DEFAULT_RULES = default_rules()

LABELS = {"late": "Late arrivals", "early": "Early departures", "absence": "Verified unexcused absences",
          "missing_punch": "Missing punches", "rejected_permit": "Rejected permits (اذن حضور / اذن انصراف)"}
RULE_NAMES = {"late": "late arrival", "early": "early departure", "absence": "verified unexcused absence",
              "missing_punch": "missing punch", "rejected_permit": "rejected permit"}
ORDER = ("late", "early", "absence", "missing_punch", "rejected_permit")
OFF_STATUSES = {"rest_day", "holiday", "on_leave", "no_schedule", "standby", "rest_day_worked"}


def normalize(rules):
    """Fill every missing key (rule sets saved by earlier versions lack the newer ones) without changing what they mean."""
    base = default_rules()
    out = {**base, **(rules or {})}
    for k in ("late", "early", "absence", "missing_punch", "rejected_permit"):
        merged = {**base[k], **((rules or {}).get(k) or {})}
        merged["repeat"] = {**base[k]["repeat"], **(merged.get("repeat") or {})}
        out[k] = merged
    return out


def _pays(comp, with_tiers):
    return bool((with_tiers and (comp["tiers"] or comp.get("flat_pct"))) or comp.get("penalty_per_day"))


def is_configured(rules):
    r = normalize(rules)
    return any(_pays(r[k], k in ("late", "early")) for k in ORDER) and bool(r["categories"])


def _tier(tiers, minutes):
    for t in tiers:
        hi = t.get("to")
        if minutes >= t["from"] and (hi is None or minutes <= hi):
            return t["penalty"], f"{t['from']}-{hi if hi is not None else '∞'} min"
    return None, None


def _g(v):
    return f"{v:g}"


def _step_text(s):
    rng = f"{s['from']}" if s.get("to") == s["from"] else f"{s['from']}–{s['to'] if s.get('to') is not None else '∞'}"
    bits = []
    if s.get("factor") not in (None, 1):
        bits.append(f"×{_g(s['factor'])}")
    if s.get("extra"):
        bits.append(f"+{_g(s['extra'])} %")
    return f"#{rng}: " + (" ".join(bits) or "base only")


def describe_rules(rules):
    """The rule set in plain words (shown on the score page and next to the form)."""
    r = normalize(rules)
    out = []
    for k in ORDER:
        c = r[k]
        parts = []
        if k in ("late", "early"):
            if c["tiers"]:
                parts.append("by minutes: " + "; ".join(f"{t['from']}–{t['to'] if t['to'] is not None else '∞'} min = {_g(t['penalty'])} %" for t in c["tiers"]))
            if c.get("flat_pct"):
                parts.append(f"{_g(c['flat_pct'])} % per occurrence" + (" when no band matches" if c["tiers"] else ""))
            if c.get("ignore_under_minutes"):
                parts.append(f"under {c['ignore_under_minutes']} min is ignored")
        elif c.get("penalty_per_day"):
            parts.append(f"{_g(c['penalty_per_day'])} % per day")
        if not parts:
            out.append({"kind": k, "label": LABELS[k], "text": "no penalty", "repeat": "", "cap": ""})
            continue
        rp = c["repeat"]
        rep = []
        if rp["steps"]:
            win = {"period": "in the evaluated period", "month": "in the same calendar month", "rolling": f"in the last {rp['rolling_days']} days"}[rp["window"]]
            rep.append("repeats " + win + " — " + "; ".join(_step_text(s) for s in rp["steps"]))
        if rp.get("consecutive_days") and rp.get("consecutive_extra"):
            rep.append(f"+{_g(rp['consecutive_extra'])} % for each one in a run of {rp['consecutive_days']}+ consecutive scheduled days")
        out.append({"kind": k, "label": LABELS[k], "text": ", ".join(parts), "repeat": " · ".join(rep),
                    "cap": f"capped at {_g(c['max_deduction'])} %" if c.get("max_deduction") is not None else ""})
    if r["max_total_deduction"] is not None:
        out.append({"kind": "total", "label": "Overall", "text": f"total deduction capped at {_g(r['max_total_deduction'])} %", "repeat": "", "cap": ""})
    return out


def _events(kind, days, c):
    """Occurrences of one component: [{'date', 'base' (% of start), 'detail'}] sorted by date."""
    ev = []
    for d in days:
        if kind in ("late", "early"):
            m = d["late_minutes"] if kind == "late" else d["early_minutes"]
            if m <= 0 or m < (c.get("ignore_under_minutes") or 0):
                continue
            pct, band = _tier(c["tiers"], m)
            if pct is None:
                pct, band = (c.get("flat_pct") or 0), "flat rate"
            if pct:
                ev.append({"date": d["date"], "base": float(pct), "detail": f"{m} min ({band})"})
        elif kind == "absence":
            if d["status"] == "absent" and "absence_verified" in d["flags"] and c.get("penalty_per_day"):
                ev.append({"date": d["date"], "base": float(c["penalty_per_day"]), "detail": "reviewed as unexcused"})
        elif kind == "missing_punch":
            if d["status"] == "missing_punch" and c.get("penalty_per_day"):
                ev.append({"date": d["date"], "base": float(c["penalty_per_day"]), "detail": "IN or OUT not recorded"})
        elif kind == "rejected_permit":
            for key, label in (("late_permit", "اذن حضور"), ("early_permit", "اذن انصراف")):
                if d.get(key) == "rejected" and c.get("penalty_per_day"):
                    ev.append({"date": d["date"], "base": float(c["penalty_per_day"]), "detail": f"{label} rejected by management"})
    return sorted(ev, key=lambda e: e["date"])


def _in_window(rp, prev_date, this_date):
    if rp["window"] == "month":
        return prev_date[:7] == this_date[:7]
    if rp["window"] == "rolling":
        return (date.fromisoformat(this_date) - date.fromisoformat(prev_date)).days < int(rp.get("rolling_days") or 30)
    return True


def _runs(days, ev_dates, n):
    """Dates that belong to a run of >= n consecutive scheduled days that all have an occurrence (days off / leave do not break a run)."""
    out, run = set(), []
    for d in days:
        if d["status"] in OFF_STATUSES:          # days off and leave neither extend nor break a run
            continue
        if d["date"] in ev_dates:
            run.append(d["date"])
        else:
            if len(run) >= n:
                out.update(run)
            run = []
    if len(run) >= n:
        out.update(run)
    return out


def _apply_repeat(kind, events, days, c):
    rp = c.get("repeat") or _repeat()
    run_dates = set()
    if rp.get("consecutive_days") and rp.get("consecutive_extra"):
        run_dates = _runs(days, {e["date"] for e in events}, int(rp["consecutive_days"]))
    for i, e in enumerate(events):
        e["ordinal"] = 1 + sum(1 for p in events[:i] if _in_window(rp, p["date"], e["date"]))
        pct, notes = e["base"], []
        for s in rp.get("steps") or []:
            if e["ordinal"] >= s["from"] and (s.get("to") is None or e["ordinal"] <= s["to"]):
                if s.get("factor") not in (None, 1):
                    pct *= float(s["factor"])
                    notes.append(f"×{_g(s['factor'])}")
                if s.get("extra"):
                    pct += float(s["extra"])
                    notes.append(f"+{_g(s['extra'])} %")
                break
        if e["date"] in run_dates:
            pct += float(rp["consecutive_extra"])
            notes.append(f"+{_g(rp['consecutive_extra'])} % consecutive")
        e["pct"] = pct
        where = {"month": "this month", "rolling": f"in {rp.get('rolling_days')} days", "period": "in the period"}[rp["window"]]
        e["repeat_note"] = (f"occurrence #{e['ordinal']} {where}" + (": " + " ".join(notes) if notes else "")) if (rp.get("steps") or notes) else ""
    return events


def compute_score(days, rules, summary, period_complete=True):
    """days: evaluate_day() results; summary: summarize(days). Returns an explainable result (all deductions are in score points =
    % of the start score)."""
    r = normalize(rules)
    unit = r["start"] / 100.0
    deductions, caps = [], []
    for kind in ORDER:
        c = r[kind]
        events = _apply_repeat(kind, _events(kind, days, c), days, c)
        items = [{"date": e["date"], "rule": RULE_NAMES[kind], "detail": e["detail"] + (f" · {e['repeat_note']}" if e["repeat_note"] else ""),
                  "base": round(e["base"] * unit, 4), "points": round(e["pct"] * unit, 4), "ordinal": e["ordinal"]} for e in events]
        base_total = sum(i["base"] for i in items)
        total = sum(i["points"] for i in items)
        cap = c.get("max_deduction")
        if cap is not None and total > cap * unit:
            caps.append(f"{LABELS[kind]}: {total * 1:g} capped at {cap * unit:g}")
            total = cap * unit
        deductions.append({"kind": kind, "label": LABELS[kind], "items": items, "count": len(items), "base_total": round(base_total, 4),
                           "repeat_total": round(max(total, 0) - min(base_total, total), 4), "total": round(total, 4),
                           "cap": None if cap is None else cap * unit, "active": _pays(c, kind in ("late", "early"))})
    total_ded = sum(x["total"] for x in deductions)
    cap = r.get("max_total_deduction")
    if cap is not None and total_ded > cap * unit:
        caps.append(f"Overall: {total_ded:g} capped at {cap * unit:g}")
        total_ded = cap * unit
    score = max(0, r["start"] - total_ded)
    score = round(score, 2)
    configured = is_configured(r)
    category = tone = None
    if configured:
        cats = sorted(r["categories"], key=lambda c: -c["min"])
        for i, c in enumerate(cats):
            if score >= c["min"]:
                category = c["name"]
                tone = "t-ok" if i == 0 else ("t-danger" if i == len(cats) - 1 and len(cats) > 1 else "t-warn")
                break
    insufficient = summary["judged_days"] < r.get("min_judged_days", 1)
    if insufficient:                      # a score built on too little data is shown, but never classified
        category = tone = None
    notes = []
    if not configured:
        notes.append("Scoring rules are not configured by management: no penalties or categories are applied.")
    if insufficient:
        notes.append(f"Insufficient data: {summary['judged_days']} evaluated working day(s), minimum {r.get('min_judged_days', 1)}.")
    if not period_complete:
        notes.append("The period is not complete (it includes days without attendance data or future days).")
    mp = summary["missing_in"] + summary["missing_out"] + summary["missing_both"]
    if mp and not r["missing_punch"].get("penalty_per_day"):
        notes.append(f"{mp} missing punch(es) were not penalised (they need verification).")
    unverified = sum(1 for d in days if d["status"] == "absent" and "absence_verified" not in d["flags"])
    if unverified:
        notes.append(f"{unverified} absence candidate(s) are not penalised until reviewed.")
    pending_permits = sum(1 for d in days for k in ("late_permit", "early_permit") if d.get(k) == "required")
    if pending_permits:
        notes.append(f"{pending_permits} attendance permit(s) are still unreviewed: a missing permit never lowers the score by itself.")
    return {"score": score, "start": r["start"], "category": category, "category_tone": tone, "configured": configured,
            "insufficient_data": insufficient, "incomplete_period": not period_complete,
            "deductions": deductions, "total_deduction": round(total_ded, 4), "caps_applied": caps, "notes": notes,
            "rules_text": describe_rules(r), "judged_days": summary["judged_days"]}
