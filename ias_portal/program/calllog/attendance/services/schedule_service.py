"""Monthly shift-schedule import, versioning, calendar data and manual changes."""
import calendar
import io
from collections import defaultdict
from datetime import date, timedelta

import openpyxl

from ..domain import matching, schedule_rules
from ..importers import schedule_xlsx
from ..repo import ConflictError
from . import access, audit, canonical_hash, settings as settings_svc, sha256_bytes

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ImportError_(Exception):
    """A problem the user can fix; the message is shown as is."""


def shifts_by_code(repo):
    return {s["code"]: s for s in repo.list_shift_types()}


def month_dates(year, month):
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d).isoformat() for d in range(1, n + 1)]


# ---------------------------------------------------------------- step 1: upload + preview
def start_import(repo, user, data: bytes, filename, year, month, team):
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        raise ImportError_("Choose a valid month and year.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportError_("The file is too large.")
    if not filename.lower().endswith(".xlsx"):
        raise ImportError_("Upload an .xlsx file.")
    team = (team or "").strip()
    if user["role"] == "manager":
        if not team or not access.in_scope(user, team):
            raise ImportError_("Choose one of your own teams.")
    file_sha = sha256_bytes(data)
    dup = repo.find_completed_schedule_import(file_sha256=file_sha)
    if dup:
        raise ImportError_(f"This exact file was already imported (import #{dup['id']} for {dup['year']}-{dup['month']:02d}). "
                           "Nothing was changed.")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        parsed = schedule_xlsx.parse_workbook(wb)
    except schedule_xlsx.TemplateError as exc:
        raise ImportError_(f"This does not look like a monthly shift schedule: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - corrupt / non-Excel content
        raise ImportError_(f"The file could not be read as an Excel workbook ({exc.__class__.__name__}).") from exc
    content_hash = canonical_hash(sorted(
        (e["date"], e["shift_code"], e["name_norm"], e["stream"], e["note"]) for e in parsed["entries"]))
    same = repo.find_completed_schedule_import(content_sha256=content_hash, year=year, month=month)
    if same:
        raise ImportError_(f"A file with identical schedule content was already imported for {year}-{month:02d} "
                           f"(import #{same['id']}). Nothing was changed.")
    import_id = repo.create_schedule_import({
        "year": year, "month": month, "filename": filename, "file_sha256": file_sha, "content_sha256": content_hash,
        "team": team, "uploaded_by": user["user_id"], "status": "preview", "parsed": parsed})
    audit(repo, user, "schedule_import_uploaded", "schedule_import", import_id,
          {"filename": filename, "year": year, "month": month, "entries": len(parsed["entries"])})
    return import_id


def build_preview(repo, import_id):
    """Everything the preview screen shows. Re-computed on every view so it always reflects current employees/aliases."""
    imp = repo.get_schedule_import(import_id)
    if not imp:
        return None
    parsed, year, month = imp["parsed"], imp["year"], imp["month"]
    st = settings_svc.load(repo)
    shifts = shifts_by_code(repo)
    employees = repo.list_employees()
    matches = matching.match_all(parsed["entries"], employees, repo.get_aliases())
    # names that need a human first, then the confident ones
    matches = dict(sorted(matches.items(), key=lambda kv: (kv[1]["status"] in ("alias", "auto"), -kv[1]["count"])))
    in_month = [e for e in parsed["entries"] if e["date"][:7] == f"{year}-{month:02d}"]
    out_month = len(parsed["entries"]) - len(in_month)
    blocking, warnings = [], []
    if parsed["title_year"] and (parsed["title_year"], parsed["title_month"]) != (year, month):
        blocking.append(f"The sheet title says {parsed['title_year']}-{parsed['title_month']:02d} but you selected "
                        f"{year}-{month:02d}. Re-upload with the right month or fix the file.")
    if out_month:
        warnings.append(f"{out_month} entr(ies) fall outside {year}-{month:02d} and will be ignored.")
    for i in parsed["issues"]:
        (blocking if i["level"] == "error" else warnings).append(
            (f"Row {i['row']}: " if i.get("row") else "") + i["message"])
    # shift hours in the sheet vs configured shift types
    for g in parsed["groups"]:
        sh = shifts.get(g["code"])
        if sh and g["start"] and (sh["start_time"], sh["end_time"]) != (g["start"], g["end"]):
            warnings.append(f"Shift {g['code']} is {g['start']}-{g['end']} in the file but {sh['start_time']}-{sh['end_time']} "
                            "in the configured shift types; the configured times are used for analytics.")
    # tentative conflicts (matched names only)
    tentative = []
    for e in in_month:
        m = matches[e["name_norm"]]
        emp = m["employee_id"]
        if emp and e["kind"] == "work":
            tentative.append({"employee_id": emp, "work_date": e["date"], "shift_code": e["shift_code"]})
    conflicts = schedule_rules.detect_conflicts(tentative, shifts, st["min_rest_hours"])
    names = {e["id"]: e["name"] for e in employees}
    for c in conflicts:
        c["employee_name"] = names.get(c["employee_id"], c["employee_id"])
    # evidence for/against "not listed = rest day": do people punch on the days they would be inferred as resting?
    rest_check = None
    matched_ids = sorted({m["employee_id"] for m in matches.values() if m["employee_id"]})
    d_all = month_dates(year, month)
    if matched_ids and repo.dates_with_attendance(d_all[0], d_all[-1]):
        busy = {(m["employee_id"], e["date"]) for e in in_month for m in [matches[e["name_norm"]]] if m["employee_id"]}
        recs = {(r["employee_id"], r["work_date"]): r for r in repo.attendance_records(d_all[0], d_all[-1], matched_ids)}
        loaded = repo.dates_with_attendance(d_all[0], d_all[-1])
        cand = [(i, d) for i in matched_ids for d in d_all if (i, d) not in busy and d in loaded]
        worked = sum(1 for k in cand if recs.get(k) and (recs[k]["in_time"] or recs[k]["out_time"]))
        if cand:
            rest_check = {"candidate_days": len(cand), "with_punches": worked, "pct": round(100 * worked / len(cand), 1)}
    active = repo.active_version(year, month)
    counts = defaultdict(int)
    for e in in_month:
        counts[e["shift_code"]] += 1
    unresolved = [n for n, m in matches.items() if m["status"] not in ("alias", "auto")]
    return {"import": imp, "parsed": parsed, "matches": matches, "unresolved": unresolved, "conflicts": conflicts,
            "blocking": blocking, "warnings": warnings, "active_version": active, "counts": dict(counts),
            "dates_corrected": [d for d in parsed["days"] if d["corrected"]], "entries_in_month": len(in_month),
            "employees": employees, "no_employees": not employees, "rest_check": rest_check}


def import_directly(repo, user, import_id):
    """Administrator import without a confirmation step: the file is imported at once with the safe automatic choices below, and
    everything that was decided automatically is reported (and stored in the import's result). Returns None when the file has a
    problem that needs a human (wrong month, unreadable rows, no employees yet): the caller then shows the review page.

    * dates the sheet gets wrong (a typo in the day sequence) are corrected as the preview proposes;
    * names that match an employee exactly, by alias or as a confident single suggestion are used; every other name is skipped and listed;
    * an active schedule of the same month is replaced - never deleted: the old version stays in the history as superseded;
    * rest days are not inferred."""
    pv = build_preview(repo, import_id)
    if not pv or pv["blocking"] or pv["no_employees"]:
        return None
    resolutions, accepted, skipped = {}, [], []
    for n, m in pv["matches"].items():
        if m["status"] in ("alias", "auto"):
            continue
        if m["status"] == "suggested" and len(m["candidates"]) >= 1:
            resolutions[n] = str(m["candidates"][0]["id"])
            accepted.append({"name": m["display"], "employee": m["candidates"][0]["name"], "id": m["candidates"][0]["id"]})
        else:
            resolutions[n] = "skip"
            skipped.append(m["display"])
    replaced = pv["active_version"]["version_no"] if pv["active_version"] else None
    res = confirm_import(repo, user, import_id, resolutions, True, bool(replaced), True, False)
    auto = {"date_corrections": [d["date"] for d in pv["dates_corrected"]], "suggestions_used": accepted, "names_skipped": skipped,
            "replaced_version": replaced}
    imp = repo.get_schedule_import(import_id)
    repo.update_schedule_import(import_id, {"result": {**(imp.get("result") or {}), "direct": True, "auto": auto}})
    audit(repo, user, "schedule_import_direct", "schedule_import", import_id, {**auto, "version_no": res["version_no"]})
    return {**res, "auto": auto, "year": imp["year"], "month": imp["month"]}


# ---------------------------------------------------------------- name mapping (schedule names -> portal accounts)
def latest_schedule_imports(repo):
    """The newest completed import of every month - the files whose names are shown on the mapping page."""
    seen, out = set(), []
    for h in repo.list_schedule_imports():          # newest first
        if h["status"] == "completed" and (h["year"], h["month"]) not in seen:
            seen.add((h["year"], h["month"]))
            out.append(repo.get_schedule_import(h["id"]))
    return out


def name_mapping(repo):
    """Every distinct name of the latest schedule of each month, with how it is matched to a portal account right now."""
    employees = repo.list_employees()
    aliases = repo.get_aliases()
    names = {}
    for imp in latest_schedule_imports(repo):
        entries = (imp.get("parsed") or {}).get("entries", [])
        for n, m in matching.match_all(entries, employees, aliases).items():
            a = names.setdefault(n, {**m, "count": 0, "months": []})
            a["count"] += m["count"]
            a["months"].append(f"{imp['year']}-{imp['month']:02d}")
    rows = sorted(names.items(), key=lambda kv: (kv[1]["status"] in ("alias", "auto"), kv[1]["display"]))
    return {"rows": rows, "employees": employees, "months": [(i["year"], i["month"]) for i in latest_schedule_imports(repo)],
            "to_map": sum(1 for _, m in rows if m["status"] not in ("alias", "auto")),
            "include_managers": bool(repo.get_settings().get("roster_include_managers", True)),
            "manager_ids": {e["id"] for e in employees if (repo.directory.get_user(str(e["id"])) or {}).get("hub_role") == "manager"}}


def save_name_mapping(repo, user, choices):
    """choices: {name_norm: employee id | 'clear'}. A chosen account is remembered for every later import; 'clear' forgets it."""
    ids = {str(e["id"]) for e in repo.list_employees()}
    saved = cleared = 0
    for n, v in choices.items():
        if v == "clear":
            if n in repo.get_aliases():
                repo.delete_alias(n)
                cleared += 1
        elif v in ids:
            repo.save_alias(n, int(v), user["user_id"])
            saved += 1
    if saved or cleared:
        audit(repo, user, "schedule_names_mapped", "alias", "", {"saved": saved, "cleared": cleared})
    return saved, cleared


def _missing(repo, imp, employees, aliases):
    """{(employee, date): [entries of the file]} for the people who match an employee now and have nothing at all planned that day."""
    year, month = imp["year"], imp["month"]
    prefix = f"{year}-{month:02d}"
    entries = [e for e in (imp.get("parsed") or {}).get("entries", []) if not e["invalid"] and e["date"][:7] == prefix and e["kind"] == "work"]
    match = matching.match_all(entries, employees, aliases)
    d = month_dates(year, month)
    planned = {(r["employee_id"], r["work_date"]) for r in repo.schedule_entries(d[0], d[-1])}
    out = {}
    for e in entries:
        emp = match[e["name_norm"]]["employee_id"]
        if emp and (emp, e["date"]) not in planned:
            out.setdefault((emp, e["date"]), []).append(e)
    return out


def add_missing_names(repo, user, year, month):
    """Plan, from the file already imported, the people who were skipped at the time and match an employee now. Nothing that is planned
    already (a manager's change included) is touched. -> number of person-days added."""
    imp = next((i for i in latest_schedule_imports(repo) if (i["year"], i["month"]) == (year, month)), None)
    if not imp or not repo.active_version(year, month):
        raise ImportError_("There is no imported schedule for that month.")
    todo = _missing(repo, imp, repo.list_employees(), repo.get_aliases())
    added = 0
    with repo.transaction():
        for (emp, d), es in sorted(todo.items()):
            codes = list(dict.fromkeys(e["shift_code"] for e in es))
            repo.replace_day_entries_atomic(year, month, emp, d, [{"shift_code": c, "stream": next(e["stream"] for e in es if e["shift_code"] == c),
                                                                    "note": next(e["note"] for e in es if e["shift_code"] == c)} for c in codes],
                                            "name matched after the file was imported", user["user_id"])
            added += 1
    audit(repo, user, "schedule_names_added", "schedule", f"{year}-{month:02d}", {"person_days": added})
    return added


def stale_months(repo):
    """The imported months whose schedule is missing people that match an employee NOW (a name was skipped when the file was imported, and
    the employee was added / renamed / mapped since). -> [{year, month, people, missing, names}]; the fix is the «Rebuild» of Name mapping."""
    employees = repo.list_employees()
    aliases = repo.get_aliases()
    out = []
    for imp in latest_schedule_imports(repo):
        parsed = imp.get("parsed") or {}
        year, month = imp["year"], imp["month"]
        if not parsed or not repo.active_version(year, month):
            continue
        todo = _missing(repo, imp, employees, aliases)
        prefix = f"{year}-{month:02d}"
        entries = [e for e in parsed.get("entries", []) if not e["invalid"] and e["date"][:7] == prefix and e["kind"] == "work"]
        nobody = sorted({m["display"] for m in matching.match_all(entries, employees, aliases).values() if m["status"] not in ("alias", "auto")})
        if todo or nobody:
            out.append({"year": year, "month": month, "missing": len(todo), "people": len({k[0] for k in todo}),
                        "names": sorted({e["name_raw"] for es in todo.values() for e in es})[:12], "nobody": nobody[:12]})
    return out


def reapply_month(repo, user, year, month):
    """Rebuild the schedule of a month from the file already imported, with today's name mapping. The old version stays in the history."""
    src = next((i for i in latest_schedule_imports(repo) if (i["year"], i["month"]) == (year, month)), None)
    if not src:
        raise ImportError_("There is no imported schedule for that month.")
    import_id = repo.create_schedule_import({
        "year": year, "month": month, "filename": src["filename"], "file_sha256": f"{src['file_sha256']}:remap:{auth_stamp(repo)}",
        "content_sha256": src.get("content_sha256"), "team": src.get("team", ""), "uploaded_by": user["user_id"],
        "status": "preview", "parsed": src["parsed"]})
    res = import_directly(repo, user, import_id)
    if res is None:
        raise ImportError_("The schedule could not be rebuilt: open the original import and review it.")
    return res


def auth_stamp(repo):
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


# ---------------------------------------------------------------- step 2: confirm
def confirm_import(repo, user, import_id, resolutions, accept_date_corrections, replace_confirmed, skip_unresolved,
                   infer_rest=False):
    pv = build_preview(repo, import_id)
    if not pv:
        raise ImportError_("Import not found.")
    imp = pv["import"]
    if imp["status"] != "preview":
        raise ImportError_("This import was already processed.")
    if imp["uploaded_by"] != user["user_id"] and user["role"] != "admin":
        raise access.Forbidden("Only the uploader or an administrator can confirm this import.")
    if pv["blocking"]:
        raise ImportError_("Fix these problems first: " + " | ".join(pv["blocking"]))
    if pv["dates_corrected"] and not accept_date_corrections:
        raise ImportError_("Tick the confirmation for the proposed date correction(s) or fix the file.")
    year, month, team = imp["year"], imp["month"], imp["team"]
    emp_by_id = {e["id"]: e for e in pv["employees"]}

    # resolve every unique name -> employee id | None (skip)
    final, still = {}, []
    for n, m in pv["matches"].items():
        choice = resolutions.get(n)
        if m["status"] in ("alias", "auto") and not choice:
            final[n] = m["employee_id"]
        elif choice == "skip":
            final[n] = None
        elif choice and str(choice).isdigit() and int(choice) in emp_by_id:
            final[n] = int(choice)
        elif skip_unresolved:
            final[n] = None
        else:
            still.append(m["display"])
    if still:
        raise ImportError_("These names still need an employee (or tick 'skip unresolved'): " + ", ".join(still))

    # scope: a manager may only schedule people of their teams. Where the module owns the employee list (standalone) people without a
    # team join the import's team; where the employee list belongs to the portal (shared accounts) teams are never changed from here.
    to_assign = set()
    owns_employees = getattr(repo, "manages_employees", True)
    for emp_id in {v for v in final.values() if v}:
        e = emp_by_id[emp_id]
        if user["role"] == "manager" and ((access.emp_teams(e) and not access.can_see(user, e)) or (not access.emp_teams(e) and not owns_employees)):
            raise access.Forbidden(f"{e['name']} is not in one of your teams.")
        if owns_employees and not e["team"] and team:
            to_assign.add(emp_id)

    prefix = f"{year}-{month:02d}"
    seen, entries, dropped = set(), [], []
    for e in pv["parsed"]["entries"]:
        emp = final[e["name_norm"]]
        if e["invalid"] or e["date"][:7] != prefix:
            dropped.append({"row": e["row"], "name": e["name_raw"], "reason": "invalid or outside month"})
            continue
        if emp is None:
            dropped.append({"row": e["row"], "name": e["name_raw"], "reason": "skipped (no employee)"})
            continue
        key = (emp, e["date"], e["shift_code"])
        if key in seen:
            dropped.append({"row": e["row"], "name": e["name_raw"], "reason": "duplicate in file"})
            continue
        seen.add(key)
        entries.append({"employee_id": emp, "work_date": e["date"], "shift_code": e["shift_code"],
                        "stream": e["stream"], "note": e["note"], "source": "import"})
    if not entries:
        raise ImportError_("Nothing to import after validation.")
    shifts = shifts_by_code(repo)
    st = settings_svc.load(repo)
    conflicts = schedule_rules.detect_conflicts(entries, shifts, st["min_rest_hours"])
    flag_by = defaultdict(set)
    for c in conflicts:
        flag_by[(c["employee_id"], c["work_date"], c["shift_code"])].add(c["type"])
    for e in entries:
        e["flags"] = ",".join(sorted(flag_by.get((e["employee_id"], e["work_date"], e["shift_code"]), ())))
    roster = {e["employee_id"] for e in entries}
    if infer_rest:      # OFF rows are an inference ("not listed = rest day"); the template itself never says it
        entries += schedule_rules.infer_rest_days(roster, month_dates(year, month), entries)

    if pv["active_version"] and not replace_confirmed:
        raise ImportError_(f"A schedule for {prefix} already exists (version {pv['active_version']['version_no']}). "
                           "Tick 'replace' to import this file as a new version; the old version is kept.")
    try:
        with repo.transaction():
            version = repo.commit_schedule_version_atomic(year, month, import_id, entries, user["user_id"],
                                                          replace=bool(pv["active_version"]))
            for emp_id in to_assign:
                repo.update_employee(emp_id, {"team": team})
            saved = 0
            for n, emp_id in final.items():
                if emp_id and pv["matches"][n]["status"] not in ("alias", "auto"):
                    repo.save_alias(n, emp_id, user["user_id"])
                    saved += 1
            result = {"version_no": version["version_no"], "entries": len(entries) - sum(1 for e in entries if e["source"] == "inferred"),
                      "inferred_rest_days": sum(1 for e in entries if e["source"] == "inferred"), "rest_days_inferred": bool(infer_rest),
                      "employees": len(roster), "dropped": dropped, "conflicts": len(conflicts),
                      "replaced_version": pv["active_version"]["version_no"] if pv["active_version"] else None,
                      "aliases_saved": saved, "employees_assigned_to_team": len(to_assign)}
            repo.update_schedule_import(import_id, {"status": "completed", "result": result})
            audit(repo, user, "schedule_import_completed", "schedule_import", import_id,
                  {**{k: v for k, v in result.items() if k != "dropped"}, "dropped": len(dropped), "year": year, "month": month})
    except ConflictError as exc:
        raise ImportError_(str(exc)) from exc
    from . import notify                                   # the employees of the schedule and their managers are told
    notify.schedule_published(repo, user, year, month, roster, replaced=bool(pv["active_version"]))
    return result


def cancel_import(repo, user, import_id):
    imp = repo.get_schedule_import(import_id)
    if imp and imp["status"] == "preview":
        repo.update_schedule_import(import_id, {"status": "cancelled"})
        audit(repo, user, "schedule_import_cancelled", "schedule_import", import_id)


# ---------------------------------------------------------------- calendar data
def month_view(repo, user, year, month, team=None):
    dates = month_dates(year, month)
    emps = access.visible_employees(repo, user)
    if team:
        emps = [e for e in emps if access.has_team(e, team)]
    ids = [e["id"] for e in emps]
    entries = repo.schedule_entries(dates[0], dates[-1], ids)
    shifts = shifts_by_code(repo)
    by_cell = defaultdict(list)
    for e in entries:
        by_cell[(e["employee_id"], e["work_date"])].append(e)
    roster_ids = sorted({e["employee_id"] for e in entries})
    names = {e["id"]: e for e in emps}
    roster = [names[i] for i in roster_ids if i in names]
    by_day = {d: {"M": [], "E": [], "N": [], "SB": []} for d in dates}
    for e in entries:
        if e["shift_code"] in by_day[e["work_date"]]:
            by_day[e["work_date"]][e["shift_code"]].append(e)
    st = settings_svc.load(repo)
    cov = schedule_rules.coverage(entries, dates, shifts)
    return {"dates": dates, "roster": roster, "cells": by_cell, "by_day": by_day, "names": names, "shifts": shifts,
            "coverage": cov, "version": repo.active_version(year, month), "versions": repo.list_versions(year, month),
            "teams": sorted({t for e in access.visible_employees(repo, user) for t in e["teams"]})}


def edit_day(repo, user, employee_id, work_date, codes, reason):
    emp = access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    reason = (reason or "").strip()
    if not reason:
        raise ImportError_("A reason is required for every schedule change.")
    shifts = shifts_by_code(repo)
    codes = [c for c in codes if c in shifts]
    if not codes:
        raise ImportError_("Choose at least one shift or Rest day.")
    if "OFF" in codes and len(codes) > 1:
        raise ImportError_("A rest day cannot be combined with a shift.")
    d = date.fromisoformat(work_date)
    # conflicts against the neighbouring days
    around = repo.schedule_entries((d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat(), [employee_id])
    keep = [e for e in around if e["work_date"] != work_date]
    probe = keep + [{"employee_id": employee_id, "work_date": work_date, "shift_code": c} for c in codes]
    st = settings_svc.load(repo)
    problems = schedule_rules.detect_conflicts(probe, shifts, st["min_rest_hours"])
    problems = [p for p in problems if p["work_date"] == work_date or p["work_date"] == (d + timedelta(days=1)).isoformat()]
    res = repo.replace_day_entries_atomic(d.year, d.month, employee_id, work_date,
                                          [{"shift_code": c} for c in codes], reason, user["user_id"])
    audit(repo, user, "schedule_changed", "employee", employee_id,
          {"date": work_date, "previous": res["old"], "new": res["new"], "reason": reason, "warnings": [p["message"] for p in problems]})
    from . import notify                                   # the employee is told their shift changed
    notify.shift_changed(repo, user, employee_id, work_date, res["old"], res["new"], reason)
    return {"emp": emp, "old": res["old"], "new": res["new"], "warnings": [p["message"] for p in problems]}
