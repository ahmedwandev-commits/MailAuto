"""Historical HR attendance import (preview -> confirm), corrections, punch reports, absence reviews."""
import io

import openpyxl

from ..importers import attendance_xlsx
from . import access, audit, sha256_bytes
from .schedule_service import ImportError_

MAX_UPLOAD_BYTES = 60 * 1024 * 1024


def start_import(repo, user, data: bytes, filename, mapping_override=None):
    if not filename.lower().endswith(".xlsx"):
        raise ImportError_("Upload an .xlsx file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportError_("The file is too large.")
    sha = sha256_bytes(data)
    dup = repo.find_completed_attendance_import(sha)
    if dup:
        raise ImportError_(f"This exact file was already imported (import #{dup['id']}). Nothing was changed.")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parsed = attendance_xlsx.parse_workbook(wb, mapping_override)
        wb.close()
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"The file could not be read as an Excel workbook ({exc.__class__.__name__}).") from exc
    # an unrecognised layout is not a dead end: the preview offers the column-mapping screen (parsed["needs_mapping"])
    repo.save_upload(sha, filename, data)
    import_id = repo.create_attendance_import({"filename": filename, "file_sha256": sha, "uploaded_by": user["user_id"],
                                               "status": "preview", "parsed": parsed})
    audit(repo, user, "attendance_import_uploaded", "attendance_import", import_id,
          {"filename": filename, "rows": len(parsed["rows"])})
    return import_id


def remap_columns(repo, user, import_id, override, header_row=None):
    """Re-read the stored workbook with a corrected column mapping ({field: column index})."""
    imp = repo.get_attendance_import(import_id)
    if not imp or imp["status"] != "preview":
        raise ImportError_("This import is no longer open.")
    data = repo.load_upload(imp["file_sha256"])
    if data is None:
        raise ImportError_("The uploaded file is no longer stored; upload it again.")
    clean = {k: int(v) for k, v in override.items() if str(v).strip().isdigit()}
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    hdr = int(header_row) if str(header_row or "").isdigit() and int(header_row) > 0 else None
    parsed = attendance_xlsx.parse_workbook(wb, clean, hdr)
    wb.close()
    parsed["manual_mapping"] = clean
    parsed["manual_header_row"] = hdr
    repo.update_attendance_import(import_id, {"parsed": parsed})
    audit(repo, user, "attendance_import_remapped", "attendance_import", import_id, {"mapping": clean, "header_row": hdr})
    return parsed


def build_preview(repo, import_id):
    imp = repo.get_attendance_import(import_id)
    if not imp:
        return None
    p = imp["parsed"]
    if p.get("error"):
        return {"import": imp, "parsed": p, "error": p["error"], "fields": list(attendance_xlsx.FIELDS),
                "required": attendance_xlsx.REQUIRED, "unknown_employees": {}, "accepted": 0, "existing": 0, "differs": 0,
                "rows_unknown": 0, "sample": [], "punch_stats": None}
    known = {e["id"] for e in repo.list_employees(active_only=False)}
    links = hr_links(repo)
    p = {**p, "rows": [relink(r, links) for r in p["rows"]]}           # HR numbers linked to a portal account count as known
    unknown = {}
    for r in p["rows"]:
        if r["employee_id"] not in known:
            unknown.setdefault(r["employee_id"], p["employees"].get(str(r["employee_id"]), p["employees"].get(r["employee_id"], {})))
    accepted = existing = differs = 0
    if p["rows"]:
        d1, d2 = p["period"]
        stored = {(r["employee_id"], r["work_date"]): r for r in repo.attendance_records(d1, d2)}
        for r in p["rows"]:
            if r["employee_id"] not in known:
                continue
            s = stored.get((r["employee_id"], r["work_date"]))
            if s is None:
                accepted += 1
            else:
                existing += 1
                if (s["in_time"], s["out_time"]) != (r["in_time"], r["out_time"]):
                    differs += 1
    rows_unknown = sum(1 for r in p["rows"] if r["employee_id"] not in known)
    # punch quality on working days (weekly rest days and holidays legitimately have no punches)
    from . import settings as settings_svc
    from ..domain.textnorm import norm_keep
    work_labels = {norm_keep(x) for x in settings_svc.load(repo)["hr_workday_labels"]}
    ps = {"working_rows": 0, "missing_in": 0, "missing_out": 0, "missing_both": 0, "complete": 0, "on_leave_or_mission": 0}
    for r in p["rows"]:
        if r["day_type"] and norm_keep(r["day_type"]) not in work_labels:
            continue
        ps["working_rows"] += 1
        if r["leave_text"] or r["mission_text"]:
            ps["on_leave_or_mission"] += 1
        elif r["in_time"] and r["out_time"]:
            ps["complete"] += 1
        elif r["in_time"]:
            ps["missing_out"] += 1
        elif r["out_time"]:
            ps["missing_in"] += 1
        else:
            ps["missing_both"] += 1
    fields = list(attendance_xlsx.FIELDS)
    return {"import": imp, "parsed": p, "raw": imp["parsed"], "unlinked": unlinked_numbers(repo, imp["parsed"]),
            "unknown_employees": unknown, "accepted": accepted, "existing": existing,
            "differs": differs, "rows_unknown": rows_unknown, "sample": p["rows"][:25], "punch_stats": ps,
            "fields": fields, "required": attendance_xlsx.REQUIRED}


def confirm_import(repo, user, import_id, create_unknown):
    pv = build_preview(repo, import_id)
    if not pv or pv["import"]["status"] != "preview":
        raise ImportError_("This import was already processed or does not exist.")
    if pv.get("error"):
        raise ImportError_("Map the columns first: " + pv["error"])
    p = pv["parsed"]
    if pv["unknown_employees"] and create_unknown and user["role"] != "admin":
        raise access.Forbidden("Only an administrator can create employees from an attendance file.")
    with repo.transaction():
        created = 0
        if create_unknown and pv["unknown_employees"]:
            created = repo.add_employees_if_missing([
                {"id": int(i), "name": v.get("name") or f"Employee {i}", "department": v.get("department", ""),
                 "sector": v.get("sector", "")} for i, v in pv["unknown_employees"].items()])
        known = {e["id"] for e in repo.list_employees(active_only=False)}
        links = hr_links(repo)
        rows = [r for r in (relink(r, links) for r in p["rows"]) if r["employee_id"] in known]
        skipped_unknown = len(p["rows"]) - len(rows)
        if rows:
            d1, d2 = p["period"]
            before = {(s["employee_id"], s["work_date"]) for s in repo.attendance_records(d1, d2)}
        else:
            before = set()
        new_rows = [r for r in rows if (r["employee_id"], r["work_date"]) not in before]
        res = repo.commit_attendance_rows_atomic(import_id, rows)
        result = {"inserted": res["inserted"], "existing_kept": res["existing"], "differs_from_stored": pv["differs"],
                  "rejected": len(p["rejected"]), "rejected_unknown_employee": skipped_unknown,
                  "duplicates_in_file": p["duplicates_in_file"], "employees_created": created,
                  "period": p["period"], "rejected_rows": p["rejected"][:200]}
        result["unlinked"] = unlinked_numbers(repo, p)
        repo.update_attendance_import(import_id, {"status": "completed", "result": result})
        audit(repo, user, "attendance_import_completed", "attendance_import", import_id,
              {k: v for k, v in result.items() if k != "rejected_rows"})
    from . import notify                                   # forgotten check-in / check-out: the employee and their manager are told
    result["missing_punch_people"] = notify.missing_punches(repo, user, forgotten_punches(repo, new_rows))
    return result


# ---------------------------------------------------------------- HR numbers <-> portal accounts
def hr_links(repo):
    """{HR number: portal employee id} - for a person whose number in the HR file is not their portal User ID."""
    return {int(k): int(v["employee_id"]) for k, v in repo._get("hr_links.json", {}).items()}


def relink(row, links):
    emp = links.get(row["employee_id"])
    return {**row, "employee_id": emp, "hr_number": row["employee_id"]} if emp else row


def link_hr_number(repo, user, hr_number, employee_id):
    """Link (or with ``employee_id`` None unlink) an HR number to a portal employee. Administrator only. Then every stored
    import that had that number gets the person's days (days already stored are never overwritten)."""
    if user["role"] != "admin":
        raise access.Forbidden("Only an administrator links HR numbers to portal accounts.")
    hr_number = int(hr_number)
    if employee_id in (None, "", 0, "0"):
        with repo.transaction():
            repo._mut("hr_links.json", {}).pop(str(hr_number), None)
        audit(repo, user, "hr_number_unlinked", "employee", hr_number)
        return 0
    emp = repo.get_employee(int(employee_id))
    if not emp:
        raise ImportError_("Choose a portal employee.")
    with repo.transaction():
        repo._mut("hr_links.json", {})[str(hr_number)] = {"employee_id": emp["id"], "by": user["user_id"], "at": repo._now()}
    added = 0
    for imp in repo.list_attendance_imports() if hasattr(repo, "list_attendance_imports") else []:
        if imp.get("status") != "completed":
            continue
        full = repo.get_attendance_import(imp["id"]) or {}
        rows = [{**r, "employee_id": emp["id"], "hr_number": hr_number} for r in (full.get("parsed") or {}).get("rows", [])
                if r["employee_id"] == hr_number]
        if rows:
            added += repo.commit_attendance_rows_atomic(imp["id"], rows)["inserted"]
    audit(repo, user, "hr_number_linked", "employee", emp["id"], {"hr_number": hr_number, "days_added": added})
    return added


def unlinked_numbers(repo, parsed):
    """HR numbers of a file that are neither a portal account nor linked: with the name in the file and the best portal match
    («خالد فكري» -> «خالد محمد فكري»)."""
    from ..domain import matching
    from ..domain.textnorm import norm
    emps = repo.list_employees(active_only=False)
    known = {e["id"] for e in emps}
    links = hr_links(repo)
    index = matching.build_index(emps)
    out = {}
    for r in parsed.get("rows", []):
        n = r["employee_id"]
        if n in known or n in links or n in out:
            continue
        info = parsed.get("employees", {}).get(str(n)) or parsed.get("employees", {}).get(n) or {}
        name = info.get("name") or ""
        m = matching.match_name(norm(name), index) if name else {"status": "unmatched", "employee_id": None, "candidates": []}
        out[n] = {"hr_number": n, "name": name, "status": m["status"], "employee_id": m.get("employee_id"),
                  "candidates": m.get("candidates") or []}
    return list(out.values())


def auto_link(repo, user, parsed):
    """Link every unknown HR number whose name matches exactly one portal employee by its words (and that employee is not in
    the file under their own number). Returns how many were linked."""
    in_file = {r["employee_id"] for r in parsed.get("rows", [])}
    linked = 0
    for u in unlinked_numbers(repo, parsed):
        if u["status"] == "auto" and u["employee_id"] and u["employee_id"] not in in_file:
            with repo.transaction():
                repo._mut("hr_links.json", {})[str(u["hr_number"])] = {"employee_id": u["employee_id"], "by": user["user_id"],
                                                                     "at": repo._now(), "auto": True, "name": u["name"]}
            audit(repo, user, "hr_number_linked", "employee", u["employee_id"], {"hr_number": u["hr_number"], "auto": True,
                                                                               "name_in_file": u["name"]})
            linked += 1
    return linked


def forgotten_punches(repo, rows):
    """{employee_id: [(date, 'in'|'out')]} - working days of ``rows`` with a check-out but no check-in, or the other way round
    (a day with neither is an absence, not a forgotten punch; leave / mission days are skipped)."""
    from . import settings as settings_svc
    from ..domain.textnorm import norm_keep
    work_labels = {norm_keep(x) for x in settings_svc.load(repo)["hr_workday_labels"]}
    out = {}
    for r in rows:
        if r.get("day_type") and norm_keep(r["day_type"]) not in work_labels:
            continue
        if r.get("leave_text") or r.get("mission_text"):
            continue
        if bool(r.get("in_time")) == bool(r.get("out_time")):
            continue
        out.setdefault(r["employee_id"], []).append((r["work_date"], "out" if r.get("in_time") else "in"))
    return out


def import_directly(repo, user, import_id):
    """Administrator import without a confirmation step. Rows of employee numbers that have no portal account are skipped and counted;
    days already stored are never overwritten. Returns None when the columns could not be recognised (the caller shows the
    column-mapping page)."""
    pv = build_preview(repo, import_id)
    if not pv or pv.get("error"):
        return None
    if user["role"] == "admin":
        auto_link(repo, user, pv["parsed"])                # «خالد فكري» in the file is «خالد محمد فكري» on the portal
        pv = build_preview(repo, import_id)
    res = confirm_import(repo, user, import_id, False)
    audit(repo, user, "attendance_import_direct", "attendance_import", import_id, {"inserted": res["inserted"], "skipped_unknown": res["rejected_unknown_employee"]})
    return {**res, "unknown_numbers": sorted(int(i) for i in pv["unknown_employees"])}


def cancel_import(repo, user, import_id):
    imp = repo.get_attendance_import(import_id)
    if imp and imp["status"] == "preview":
        repo.update_attendance_import(import_id, {"status": "cancelled"})
        audit(repo, user, "attendance_import_cancelled", "attendance_import", import_id)


# ---------------------------------------------------------------- corrections, reports, reviews
def correct_punch(repo, user, employee_id, work_date, field, new_value, reason, allow_clear=False):
    """Set one check-in / check-out time of a day (audited, with its reason). ``allow_clear``: an empty value removes it -
    with both removed the person has no attendance that day (an administrator's / manager's decision)."""
    access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    if field not in ("in_time", "out_time"):
        raise ImportError_("Unknown field.")
    reason = (reason or "").strip()
    if not reason:
        raise ImportError_("A reason is required for every correction.")
    from ..importers.attendance_xlsx import parse_time
    if allow_clear and not str(new_value or "").strip():
        value = None
    else:
        t, ok = parse_time(new_value)
        if not ok or t is None:
            raise ImportError_("Enter the time as HH:MM or HH:MM:SS.")
        value = t.isoformat()
    rec = next((r for r in repo.attendance_records(work_date, work_date, [employee_id])), None)
    old = rec[field] if rec else None
    repo.add_correction({"employee_id": employee_id, "work_date": work_date, "field": field, "old_value": old,
                         "new_value": value, "reason": reason, "user_id": user["user_id"]})
    audit(repo, user, "attendance_corrected", "employee", employee_id,
          {"date": work_date, "field": field, "previous": old, "new": value, "reason": reason})


def report_forgotten_punch(repo, user, employee_id, work_date, kind, reason):
    access.require_employee(repo, user, employee_id)
    if kind not in ("in", "out", "both"):
        raise ImportError_("Choose in, out or both.")
    rid = repo.add_punch_report({"employee_id": employee_id, "work_date": work_date, "kind": kind,
                                 "reason": (reason or "").strip(), "reported_by": user["user_id"]})
    audit(repo, user, "punch_reported", "employee", employee_id, {"date": work_date, "kind": kind})
    return rid


def decide_punch_report(repo, user, report_id, status):
    if not access.can_write(user) or status not in ("verified", "rejected"):
        raise access.Forbidden("Not allowed.")
    rep = next((r for r in repo.punch_reports() if r["id"] == report_id), None)
    if not rep:
        raise ImportError_("Report not found.")
    access.require_employee(repo, user, rep["employee_id"])
    repo.decide_punch_report(report_id, status, user["user_id"])
    audit(repo, user, "punch_report_" + status, "punch_report", report_id)


def review_absence(repo, user, employee_id, work_date, decision, reason):
    access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    if decision not in ("excused", "unexcused"):
        raise ImportError_("Decision must be excused or unexcused.")
    if not (reason or "").strip():
        raise ImportError_("A reason is required.")
    repo.set_absence_review(employee_id, work_date, decision, reason.strip(), user["user_id"])
    audit(repo, user, "absence_reviewed", "employee", employee_id,
          {"date": work_date, "decision": decision, "reason": reason.strip()})


def review_permit(repo, user, employee_id, work_date, kind, decision, reason):
    """Review of an attendance permit exception: kind late (اذن حضور) or early (اذن انصراف); decision provided | excused | rejected.
    Reviewing never changes attendance, never makes anyone absent and never applies a penalty by itself - until it is reviewed
    the exception just stays visible on the dashboards and in the reports."""
    emp = access.require_employee(repo, user, employee_id)
    if not access.can_write(user):
        raise access.Forbidden("Read-only role.")
    if kind not in ("late", "early") or decision not in ("provided", "excused", "rejected"):
        raise ImportError_("Choose the permit type and a decision.")
    if not (reason or "").strip():
        raise ImportError_("A reason is required.")
    previous = repo.exception_reviews(work_date, work_date, [employee_id]).get((employee_id, work_date, kind))
    repo.set_exception_review(employee_id, work_date, kind, decision, reason.strip(), user["user_id"])
    audit(repo, user, "permit_reviewed", "employee", employee_id,
          {"employee": emp["name"], "date": work_date, "permit": "اذن حضور" if kind == "late" else "اذن انصراف",
           "previous": previous, "new": decision, "reason": reason.strip()})
