"""Scoring-rule administration: every change is a NEW version; old versions and what they produced stay reproducible."""
from datetime import date

from ..domain import scoring
from . import audit
from .schedule_service import ImportError_


def _int(v, field):
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise ImportError_(f"{field}: enter a whole number.") from exc


def parse_tiers(text, label):
    """'1-15=1; 16-=3'  ->  [{'from':1,'to':15,'penalty':1}, {'from':16,'to':None,'penalty':3}]"""
    tiers = []
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            rng, pen = part.split("=")
            lo, _, hi = rng.partition("-")
            tiers.append({"from": int(lo), "to": int(hi) if hi.strip() else None, "penalty": float(pen)})
        except ValueError as exc:
            raise ImportError_(f"{label}: '{part}' is not in the form 1-15=2 (minutes range = points).") from exc
    return tiers


def parse_categories(text):
    cats = []
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if part:
            try:
                name, mn = part.rsplit("=", 1)
                cats.append({"name": name.strip(), "min": float(mn)})
            except ValueError as exc:
                raise ImportError_(f"Category '{part}' must look like Excellent=90.") from exc
    return cats


def parse_steps(text, label):
    """Repeat steps by the ORDER of the occurrence: '3-5=+1; 6-=+2' (extra %) or '2-3=x1.5' (multiplier) or '4-=x2+1' (both).
    ->  [{'from': 3, 'to': 5, 'extra': 1.0, 'factor': None}, ...]"""
    steps = []
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            rng, val = part.split("=")
            lo, dash, hi = rng.strip().partition("-")
            lo, hi = int(lo), (int(hi) if hi.strip() else None) if dash else int(lo)
            val = val.strip().lower().replace(" ", "")
            factor = extra = None
            if val.startswith("x"):
                body = val[1:]
                for i in range(1, len(body)):
                    if body[i] in "+-":
                        factor, extra = float(body[:i]), float(body[i:])
                        break
                else:
                    factor = float(body)
            else:
                extra = float(val.lstrip("+"))
            if lo < 1 or (hi is not None and hi < lo) or (factor is not None and factor < 0) or (extra is not None and extra < 0):
                raise ValueError
            steps.append({"from": lo, "to": hi, "extra": extra, "factor": factor})
        except ValueError as exc:
            raise ImportError_(f"{label}: '{part}' is not in the form 3-5=+1 (occurrence range = extra %) or 2-=x1.5 (multiplier).") from exc
    return steps


def _pct(v, label, hi=100):
    s = str(v if v is not None else "").strip().rstrip("%").strip()
    if not s:
        return None
    try:
        x = float(s)
    except ValueError as exc:
        raise ImportError_(f"{label}: enter a number.") from exc
    if x < 0 or x > hi:
        raise ImportError_(f"{label}: enter a percentage between 0 and {hi}.")
    return x


def _repeat(form, key, label):
    window = form.get(f"{key}_window") or "period"
    if window not in ("period", "month", "rolling"):
        raise ImportError_(f"{label}: unknown repeat window.")
    days = form.get(f"{key}_rolling_days") or 30
    try:
        days = int(days)
    except ValueError as exc:
        raise ImportError_(f"{label}: the rolling window must be a whole number of days.") from exc
    cd = form.get(f"{key}_consec_days")
    cd = int(cd) if str(cd or "").strip().isdigit() and int(cd) > 1 else None
    return {"window": window, "rolling_days": max(days, 1), "steps": parse_steps(form.get(f"{key}_steps"), f"{label} repeat steps"),
            "consecutive_days": cd, "consecutive_extra": _pct(form.get(f"{key}_consec_extra"), f"{label} consecutive extra") if cd else None}


def parse_rules(form):
    """The rule set of the Scoring rules form (every message is the user's to fix); nothing is saved."""
    def cap(v, label):
        return _pct(v, label, 1000)

    rules = scoring.default_rules()
    rules["start"] = _int(form.get("start") or 100, "Start score")
    if rules["start"] <= 0:
        raise ImportError_("Start score must be positive.")
    for key, label in (("late", "Late arrivals"), ("early", "Early departures")):
        rules[key] = {"tiers": parse_tiers(form.get(f"{key}_tiers"), f"{label} bands"), "flat_pct": _pct(form.get(f"{key}_flat"), f"{label} flat %"),
                      "ignore_under_minutes": _int(form.get(f"{key}_ignore") or 0, f"{label}: ignore under (minutes)"),
                      "max_deduction": cap(form.get(f"{key}_cap"), f"{label} cap"), "repeat": _repeat(form, key, label)}
    for key, label in (("absence", "Absences"), ("missing_punch", "Missing punches"), ("rejected_permit", "Rejected permits")):
        rules[key] = {"penalty_per_day": _pct(form.get(f"{key}_penalty"), f"{label} penalty") or 0,
                      "max_deduction": cap(form.get(f"{key}_cap"), f"{label} cap"), "repeat": _repeat(form, key, label)}
    rules["max_total_deduction"] = cap(form.get("max_total"), "Overall cap")
    rules["categories"] = parse_categories(form.get("categories"))
    rules["min_judged_days"] = _int(form.get("min_judged_days") or 1, "Minimum days")
    return rules


def save_new_version(repo, user, form, effective_from, note):
    if user["role"] != "admin":
        raise PermissionError
    try:
        date.fromisoformat(effective_from)
    except ValueError as exc:
        raise ImportError_("Enter a valid effective date.") from exc
    rules = parse_rules(form)
    v = repo.add_scoring_rule_version(rules, effective_from, (note or "").strip(), user["user_id"])
    audit(repo, user, "scoring_rules_changed", "scoring_rules", v, {"effective_from": effective_from, "note": note, "rules": rules})
    return v


def to_form(rules):
    r = scoring.normalize(rules)

    def g(v):
        return "" if v in (None, "") else f"{v:g}"

    def tiers(ts):
        return "; ".join(f"{t['from']}-{t['to'] if t['to'] is not None else ''}={t['penalty']:g}" for t in ts)

    def steps(ss):
        out = []
        for s in ss:
            rng = f"{s['from']}-{s['to'] if s.get('to') is not None else ''}"
            val = (f"x{s['factor']:g}" if s.get("factor") not in (None, 1) else "") + (f"+{s['extra']:g}" if s.get("extra") else "")
            out.append(f"{rng}={val or '+0'}")
        return "; ".join(out)

    f = {"start": g(r["start"]), "max_total": g(r["max_total_deduction"]), "min_judged_days": r.get("min_judged_days", 1),
         "categories": "; ".join(f"{c['name']}={c['min']:g}" for c in r["categories"])}
    for k in scoring.ORDER:
        c = r[k]
        if k in ("late", "early"):
            f.update({f"{k}_tiers": tiers(c["tiers"]), f"{k}_flat": g(c["flat_pct"]), f"{k}_ignore": g(c["ignore_under_minutes"])})
        else:
            f[f"{k}_penalty"] = g(c["penalty_per_day"])
        f[f"{k}_cap"] = g(c["max_deduction"])
        rp = c["repeat"]
        f.update({f"{k}_window": rp["window"], f"{k}_rolling_days": rp["rolling_days"], f"{k}_steps": steps(rp["steps"]),
                  f"{k}_consec_days": g(rp["consecutive_days"]), f"{k}_consec_extra": g(rp["consecutive_extra"])})
    return f


# ---------------------------------------------------------------- ready-made starting points (the administrator edits them freely)
PRESETS = {
    "lenient": {"label": "Lenient", "about": "small deductions, big tolerance", "values": {
        "start": "100", "min_judged_days": "5", "max_total": "30",
        "late_tiers": "1-15=0.5; 16-30=1; 31-60=2; 61-=3", "late_flat": "", "late_ignore": "10", "late_cap": "15", "late_window": "month",
        "late_rolling_days": "30", "late_steps": "4-=+0.5", "late_consec_days": "", "late_consec_extra": "",
        "early_tiers": "1-30=0.5; 31-=1.5", "early_flat": "", "early_ignore": "10", "early_cap": "5", "early_window": "month",
        "early_rolling_days": "30", "early_steps": "", "early_consec_days": "", "early_consec_extra": "",
        "absence_penalty": "3", "absence_cap": "15", "absence_window": "month", "absence_rolling_days": "30", "absence_steps": "", "absence_consec_days": "", "absence_consec_extra": "",
        "missing_punch_penalty": "0", "missing_punch_cap": "", "missing_punch_window": "period", "missing_punch_rolling_days": "30", "missing_punch_steps": "",
        "missing_punch_consec_days": "", "missing_punch_consec_extra": "",
        "rejected_permit_penalty": "1", "rejected_permit_cap": "5", "rejected_permit_window": "period", "rejected_permit_rolling_days": "30", "rejected_permit_steps": "",
        "rejected_permit_consec_days": "", "rejected_permit_consec_extra": "",
        "categories": "Excellent=85; Good=70; Needs Improvement=50; Critical=0"}},
    "standard": {"label": "Standard", "about": "balanced - the proposed policy", "values": {
        "start": "100", "min_judged_days": "10", "max_total": "40",
        "late_tiers": "1-15=1; 16-30=2; 31-60=3; 61-=5", "late_flat": "", "late_ignore": "5", "late_cap": "25", "late_window": "month",
        "late_rolling_days": "30", "late_steps": "3-4=+0.5; 5-=+1", "late_consec_days": "3", "late_consec_extra": "1",
        "early_tiers": "1-15=1; 16-30=2; 31-=3", "early_flat": "", "early_ignore": "5", "early_cap": "10", "early_window": "month",
        "early_rolling_days": "30", "early_steps": "3-=+0.5", "early_consec_days": "", "early_consec_extra": "",
        "absence_penalty": "5", "absence_cap": "30", "absence_window": "month", "absence_rolling_days": "30", "absence_steps": "2-=x1.5",
        "absence_consec_days": "3", "absence_consec_extra": "2",
        "missing_punch_penalty": "1", "missing_punch_cap": "5", "missing_punch_window": "period", "missing_punch_rolling_days": "30", "missing_punch_steps": "",
        "missing_punch_consec_days": "", "missing_punch_consec_extra": "",
        "rejected_permit_penalty": "2", "rejected_permit_cap": "10", "rejected_permit_window": "period", "rejected_permit_rolling_days": "30", "rejected_permit_steps": "",
        "rejected_permit_consec_days": "", "rejected_permit_consec_extra": "",
        "categories": "Excellent=90; Very Good=80; Good=70; Needs Improvement=50; Critical=0"}},
    "strict": {"label": "Strict", "about": "bigger deductions, repeats cost a lot", "values": {
        "start": "100", "min_judged_days": "10", "max_total": "60",
        "late_tiers": "1-10=1; 11-30=3; 31-60=5; 61-=8", "late_flat": "", "late_ignore": "3", "late_cap": "40", "late_window": "month",
        "late_rolling_days": "30", "late_steps": "2-3=+1; 4-=+2", "late_consec_days": "3", "late_consec_extra": "2",
        "early_tiers": "1-15=2; 16-30=4; 31-=6", "early_flat": "", "early_ignore": "3", "early_cap": "20", "early_window": "month",
        "early_rolling_days": "30", "early_steps": "2-=+1", "early_consec_days": "", "early_consec_extra": "",
        "absence_penalty": "10", "absence_cap": "50", "absence_window": "month", "absence_rolling_days": "30", "absence_steps": "2-=x2",
        "absence_consec_days": "2", "absence_consec_extra": "5",
        "missing_punch_penalty": "2", "missing_punch_cap": "10", "missing_punch_window": "period", "missing_punch_rolling_days": "30", "missing_punch_steps": "",
        "missing_punch_consec_days": "", "missing_punch_consec_extra": "",
        "rejected_permit_penalty": "4", "rejected_permit_cap": "20", "rejected_permit_window": "period", "rejected_permit_rolling_days": "30", "rejected_permit_steps": "",
        "rejected_permit_consec_days": "", "rejected_permit_consec_extra": "",
        "categories": "Excellent=95; Very Good=85; Good=75; Needs Improvement=60; Critical=0"}},
}


# ---------------------------------------------------------------- the live preview: the real scoring code on an invented month
def _blank_day(iso):
    return {"employee_id": 0, "date": iso, "planned_codes": ["M"], "planned_primary": "M", "status": "present", "flags": [], "expected_start": "07:00",
            "expected_end": "15:00", "actual_in": "07:00:00", "actual_out": "15:00:00", "late_minutes": 0, "early_minutes": 0, "excused_late_minutes": 0,
            "scheduled_hours": 8.0, "actual_hours": 8.0, "needs_replacement": False, "leave_type": None, "note": "", "default_shift": False,
            "late_permit": None, "early_permit": None, "late_raw_minutes": 0, "early_raw_minutes": 0, "expected_present": True}


def _numbers(text, label, cap=60):
    out = []
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(float(part)))
            except ValueError as exc:
                raise ImportError_(f"{label}: use numbers separated by commas (for example 10, 25, 40).") from exc
    return out[:cap]


def preview(rules, sample):
    """The score of an invented employee-month under ``rules``: ``sample`` = {late: '10, 20', early: '5', absences: n, missing: n, rejected: n,
    days: n, consecutive: bool}. Uses the same functions as the real score, so what the screen shows is what will happen."""
    from datetime import timedelta
    from ..domain import daily
    n = max(5, min(int(sample.get("days") or 22), 31))
    first = date(2026, 1, 4)                                    # a Sunday: the invented days are consecutive working days
    days = [_blank_day((first + timedelta(days=i)).isoformat()) for i in range(n)]
    step = 1 if sample.get("consecutive") else 3
    lates, earlys = _numbers(sample.get("late"), "Late arrivals"), _numbers(sample.get("early"), "Early departures")
    for i, m in enumerate(lates):
        d = days[(i * step) % n]
        d["status"], d["late_minutes"] = "late", m
    for i, m in enumerate(earlys):
        days[(i * step + 1) % n]["early_minutes"] = m
    free = [d for d in reversed(days) if d["status"] == "present" and not d["early_minutes"]]
    k = 0
    for count, make in ((int(sample.get("absences") or 0), "absent"), (int(sample.get("missing") or 0), "missing"), (int(sample.get("rejected") or 0), "rejected")):
        for _ in range(min(count, 30)):
            if k >= len(free):
                break
            d = free[k] if not sample.get("consecutive") else free[k]
            k += 1
            if make == "absent":
                d.update(status="absent", flags=["missing_both", "absence_verified"], actual_in=None, actual_out=None, actual_hours=None)
            elif make == "missing":
                d.update(status="missing_punch", flags=["missing_out"], actual_out=None, actual_hours=None)
            else:
                d["late_permit"] = "rejected"
    days.sort(key=lambda d: d["date"])
    summary = daily.summarize(days)
    res = scoring.compute_score(days, rules, summary, True)
    return {"score": res["score"], "category": res.get("category"), "tone": res.get("category_tone"), "start": scoring.normalize(rules)["start"],
            "deductions": [{"label": d["label"], "count": d["count"], "base": d["base_total"], "repeat": d["repeat_total"], "total": d["total"],
                            "cap": d["cap"], "active": d["active"]} for d in res["deductions"]],
            "caps": res.get("caps_applied", []), "notes": [x for x in res.get("notes", []) if "Insufficient" not in x and "period is not complete" not in x],
            "days": n}
