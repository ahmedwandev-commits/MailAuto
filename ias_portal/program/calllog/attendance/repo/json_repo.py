"""JSON-file implementation of the Repository contract - the storage used inside the IAS / OpsHub portal.

Layout inside ``<root>`` (the portal puts it in ``<data_dir>/attendance``)::

    meta.json                          id counters
    settings.json  shift_types.json  holidays.json  aliases.json  leave_types.json  entitlements.json  scoring_rules.json
    schedule_imports.json  imports/schedule-<id>.json (parsed + result)   schedule_versions.json  schedule_changes.json
    schedule_entries/<version_id>.json one file per schedule version (every revision of every entry)
    attendance_imports.json  imports/attendance-<id>.json   attendance_records/<YYYY-MM>.json (one file per month)
    corrections.json  punch_reports.json  exception_reviews.json  absence_reviews.json  leave_requests.json  comp_days.json
    uploads/<sha256>.bin               the original workbooks
    users.json / employees.json        ONLY with LocalDirectory (standalone); in the portal the accounts are the portal's own

Safety: every write goes through ``transaction()`` - one exclusive lock for the whole module, changes staged in memory and swapped
in file by file with ``os.replace`` (a reader never sees a half-written file); an exception inside the block discards everything.
When a unit touches several files the file that makes the change *visible* (the version list, the import status) is written last.
"""
import copy
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .base import ConflictError, Repository
from .jsonfiles import FileLock, JsonCache, write_bytes_atomic, write_json_atomic

DEFAULT_SHIFTS = [
    {"code": "M", "name": "Morning", "kind": "work", "start_time": "07:00", "end_time": "15:00", "sort_order": 1},
    {"code": "E", "name": "Evening", "kind": "work", "start_time": "14:00", "end_time": "22:00", "sort_order": 2},
    {"code": "N", "name": "Night", "kind": "work", "start_time": "22:00", "end_time": "07:00", "sort_order": 3},
    {"code": "D", "name": "Normal day", "kind": "work", "start_time": "08:00", "end_time": "16:00", "sort_order": 4},
    {"code": "SB", "name": "Stand-by (on call)", "kind": "standby", "start_time": None, "end_time": None, "sort_order": 8},
    {"code": "OFF", "name": "Rest day", "kind": "off", "start_time": None, "end_time": None, "sort_order": 9},
]
DEFAULT_LEAVE_TYPES = [{"code": "annual", "name": "Annual", "counts_against_balance": True},
                       {"code": "casual", "name": "Casual", "counts_against_balance": True},
                       {"code": "sick", "name": "Sick", "counts_against_balance": False},
                       {"code": "other", "name": "Other", "counts_against_balance": False}]


def _ym(date):
    return date[:7]


def _months(d1, d2):
    y, m = int(d1[:4]), int(d1[5:7])
    ey, em = int(d2[:4]), int(d2[5:7])
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class FileAudit:
    """Standalone audit trail: one JSONL file per day under ``<root>/audit``."""

    def __init__(self, root):
        self.dir = Path(root) / "audit"

    def write(self, rec):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f"audit-{rec['ts'][:10]}.jsonl", "ab") as f:
            f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))

    def read(self, action=None, entity=None, limit=500):
        out = []
        for p in sorted(self.dir.glob("audit-*.jsonl"), reverse=True) if self.dir.is_dir() else []:
            lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            for r in reversed(lines):
                if (not action or r["action"] == action) and (not entity or r.get("entity") == entity):
                    out.append(r)
                    if len(out) >= limit:
                        return out
        return out


class LocalDirectory:
    """Accounts and employees kept by the module itself (standalone / contract tests). The portal uses ``HubDirectory`` instead."""
    manages_employees = True
    manages_users = True

    def __init__(self, repo):
        self.repo = repo

    # users
    def get_user(self, user_id):
        u = self.repo._get("users.json", {}).get(user_id)
        return copy.deepcopy(u) if u else None

    def list_users(self):
        return [copy.deepcopy(u) for _, u in sorted(self.repo._get("users.json", {}).items())]

    def create_user(self, u):
        with self.repo.transaction():
            users = self.repo._mut("users.json", {})
            if u["user_id"] in users:
                raise ConflictError("User already exists.")
            users[u["user_id"]] = {"user_id": u["user_id"], "name": u["name"], "role": u["role"], "teams": list(u.get("teams", [])),
                                   "password_hash": u["password_hash"], "active": bool(u.get("active", True)),
                                   "must_change_password": bool(u.get("must_change_password", False)),
                                   "created_at": self.repo._now(), "last_login": None}

    def update_user(self, user_id, patch):
        with self.repo.transaction():
            users = self.repo._mut("users.json", {})
            if user_id in users:
                users[user_id].update(patch)

    # employees
    @staticmethod
    def _emp(r):
        r = copy.deepcopy(r)
        r["teams"] = [r["team"]] if r.get("team") else []
        return r

    def get_employee(self, employee_id):
        e = self.repo._get("employees.json", {}).get(str(employee_id))
        return self._emp(e) if e else None

    def list_employees(self, active_only=True):
        rows = [e for _, e in sorted(self.repo._get("employees.json", {}).items(), key=lambda kv: int(kv[0]))]
        return [self._emp(e) for e in rows if e["active"] or not active_only]

    def add_employees_if_missing(self, employees):
        n = 0
        with self.repo.transaction():
            emps = self.repo._mut("employees.json", {})
            for e in employees:
                if str(e["id"]) not in emps:
                    emps[str(e["id"])] = {"id": e["id"], "name": e["name"], "department": e.get("department", ""), "sector": e.get("sector", ""),
                                          "team": e.get("team", ""), "active": True, "created_at": self.repo._now()}
                    n += 1
        return n

    def update_employee(self, employee_id, patch):
        with self.repo.transaction():
            emps = self.repo._mut("employees.json", {})
            if str(employee_id) in emps:
                emps[str(employee_id)].update(patch)


class JsonRepository(Repository):
    def __init__(self, root, clock=datetime.now, directory=None, audit=None):
        self.root = Path(root)
        self.clock = clock
        self.directory = directory or LocalDirectory(self)
        self.audit_backend = audit or FileAudit(self.root)
        self._lock = FileLock(self.root / ".lock")
        self._cache = JsonCache()
        self._local = threading.local()

    @property
    def manages_employees(self):
        return getattr(self.directory, "manages_employees", False)

    # ------------------------------------------------------------------ plumbing
    def _now(self):
        return self.clock().isoformat(timespec="seconds")

    def _path(self, name):
        return self.root / name

    def _staged(self):
        return getattr(self._local, "staged", None)

    def _get(self, name, default):
        """Current content (this transaction's staged version first). Treat as READ-ONLY."""
        st = self._staged()
        if st is not None and name in st:
            return st[name]
        return self._cache.read(self._path(name), default)

    def _mut(self, name, default):
        """A mutable copy that is written when the outermost transaction commits."""
        st = self._staged()
        if st is None:
            raise RuntimeError("write outside a transaction")
        if name not in st:
            st[name] = copy.deepcopy(self._cache.read(self._path(name), default))
        return st[name]

    @contextmanager
    def transaction(self):
        if self._staged() is not None:                     # nested: join the outer one
            yield
            return
        with self._lock():
            self._local.staged = {}
            order = self._local.order = []
            try:
                yield
                staged = self._local.staged
            except BaseException:
                self._local.staged = None
                raise
            self._local.staged = None
            # files that make a change visible go last (version list, import status); the rest in the order they were staged
            names = sorted(staged, key=lambda n: n in ("schedule_versions.json", "schedule_imports.json", "attendance_imports.json", "meta.json"))
            for n in names:
                write_json_atomic(self._path(n), staged[n])
                self._cache.drop(self._path(n))

    def _next(self, table):
        meta = self._mut("meta.json", {"seq": {}})
        meta["seq"][table] = meta["seq"].get(table, 0) + 1
        return meta["seq"][table]

    def migrate(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if self._path("meta.json").exists():
            return []
        with self.transaction():
            self._mut("meta.json", {"seq": {}})
            self._mut("shift_types.json", []).extend(copy.deepcopy(DEFAULT_SHIFTS))
            self._mut("leave_types.json", []).extend(copy.deepcopy(DEFAULT_LEAVE_TYPES))
        return ["json-0001-initial"]

    def _ensure_defaults(self):
        pass

    # ------------------------------------------------------------------ users and employees (the directory)
    def get_user(self, user_id):
        return self.directory.get_user(user_id)

    def list_users(self):
        return self.directory.list_users()

    def create_user(self, user):
        return self.directory.create_user(user)

    def update_user(self, user_id, patch):
        return self.directory.update_user(user_id, patch)

    def get_employee(self, employee_id):
        return self.directory.get_employee(employee_id)

    def list_employees(self, active_only=True):
        return self.directory.list_employees(active_only)

    def add_employees_if_missing(self, employees):
        return self.directory.add_employees_if_missing(employees)

    def update_employee(self, employee_id, patch):
        return self.directory.update_employee(employee_id, patch)

    # ------------------------------------------------------------------ configuration
    def get_settings(self):
        return copy.deepcopy(self._get("settings.json", {}))

    def save_settings(self, values):
        with self.transaction():
            self._mut("settings.json", {}).update(copy.deepcopy(values))

    def list_shift_types(self):
        return sorted(copy.deepcopy(self._get("shift_types.json", [])), key=lambda s: (s["sort_order"], s["code"]))

    def save_shift_type(self, s):
        with self.transaction():
            rows = self._mut("shift_types.json", [])
            new = {"code": s["code"], "name": s["name"], "kind": s["kind"], "start_time": s.get("start_time"),
                   "end_time": s.get("end_time"), "sort_order": s.get("sort_order", 5)}
            for i, r in enumerate(rows):
                if r["code"] == s["code"]:
                    rows[i] = new
                    return
            rows.append(new)

    def list_holidays(self):
        return sorted(copy.deepcopy(self._get("holidays.json", [])), key=lambda h: h["date"])

    def add_holiday(self, date, name):
        with self.transaction():
            rows = self._mut("holidays.json", [])
            for r in rows:
                if r["date"] == date:
                    r["name"] = name
                    return
            rows.append({"date": date, "name": name})

    def remove_holiday(self, date):
        with self.transaction():
            rows = self._mut("holidays.json", [])
            rows[:] = [r for r in rows if r["date"] != date]

    def get_aliases(self):
        return {k: v["employee_id"] for k, v in self._get("aliases.json", {}).items()}

    def save_alias(self, alias_norm, employee_id, user_id):
        with self.transaction():
            self._mut("aliases.json", {})[alias_norm] = {"employee_id": employee_id, "created_by": user_id, "created_at": self._now()}

    def delete_alias(self, alias_norm):
        with self.transaction():
            self._mut("aliases.json", {}).pop(alias_norm, None)

    # ------------------------------------------------------------------ monthly shifts approved by a team's manager ("YYYY-MM|team")
    def shift_approvals(self):
        return copy.deepcopy(self._get("shift_approvals.json", {}))

    def set_shift_approval(self, month, team, rec):
        with self.transaction():
            self._mut("shift_approvals.json", {})[f"{month}|{team}"] = copy.deepcopy(rec)

    def delete_shift_approval(self, month, team):
        with self.transaction():
            self._mut("shift_approvals.json", {}).pop(f"{month}|{team}", None)

    # ------------------------------------------------------------------ imports (schedule and attendance share the shape)
    def _imp_full(self, kind, head):
        if not head:
            return None
        data = self._get(f"imports/{kind}-{head['id']}.json", {})
        return {**copy.deepcopy(head), "parsed": copy.deepcopy(data.get("parsed")), "result": copy.deepcopy(data.get("result"))}

    def _imp_create(self, kind, head, parsed):
        with self.transaction():
            rid = self._next(f"{kind}_imports")
            self._mut(f"{kind}_imports.json", []).append({"id": rid, **head, "uploaded_at": self._now()})
            self._mut(f"imports/{kind}-{rid}.json", {}).update({"parsed": parsed, "result": None})
        return rid

    def _imp_get(self, kind, import_id):
        for r in self._get(f"{kind}_imports.json", []):
            if r["id"] == import_id:
                return self._imp_full(kind, r)
        return None

    def _imp_update(self, kind, import_id, patch):
        with self.transaction():
            heads = self._mut(f"{kind}_imports.json", [])
            for r in heads:
                if r["id"] == import_id:
                    body = self._mut(f"imports/{kind}-{import_id}.json", {})
                    for k, v in patch.items():
                        if k in ("parsed", "result"):
                            body[k] = copy.deepcopy(v)
                        else:
                            r[k] = v

    def _imp_list(self, kind):
        out = []
        for r in sorted(self._get(f"{kind}_imports.json", []), key=lambda r: -r["id"]):
            body = self._get(f"imports/{kind}-{r['id']}.json", {})
            out.append({**copy.deepcopy(r), "parsed": None, "result": copy.deepcopy(body.get("result"))})
        return out

    def create_schedule_import(self, rec):
        return self._imp_create("schedule", {"year": rec["year"], "month": rec["month"], "filename": rec["filename"],
                                             "file_sha256": rec["file_sha256"], "content_sha256": rec.get("content_sha256"),
                                             "team": rec.get("team", ""), "uploaded_by": rec.get("uploaded_by"),
                                             "status": rec.get("status", "preview")}, copy.deepcopy(rec.get("parsed")))

    def get_schedule_import(self, import_id):
        return self._imp_get("schedule", import_id)

    def update_schedule_import(self, import_id, patch):
        self._imp_update("schedule", import_id, patch)

    def list_schedule_imports(self):
        return self._imp_list("schedule")

    def find_completed_schedule_import(self, file_sha256=None, content_sha256=None, year=None, month=None):
        for r in sorted(self._get("schedule_imports.json", []), key=lambda r: -r["id"]):
            if (r["status"] == "completed" and (not file_sha256 or r["file_sha256"] == file_sha256)
                    and (not content_sha256 or r.get("content_sha256") == content_sha256)
                    and (not year or r["year"] == year) and (not month or r["month"] == month)):
                return self._imp_full("schedule", r)
        return None

    def create_attendance_import(self, rec):
        return self._imp_create("attendance", {"filename": rec["filename"], "file_sha256": rec["file_sha256"],
                                               "uploaded_by": rec.get("uploaded_by"), "status": rec.get("status", "preview")},
                                copy.deepcopy(rec.get("parsed")))

    def get_attendance_import(self, import_id):
        return self._imp_get("attendance", import_id)

    def update_attendance_import(self, import_id, patch):
        self._imp_update("attendance", import_id, patch)

    def list_attendance_imports(self):
        return self._imp_list("attendance")

    def find_completed_attendance_import(self, file_sha256):
        for r in self._get("attendance_imports.json", []):
            if r["file_sha256"] == file_sha256 and r["status"] == "completed":
                return self._imp_full("attendance", r)
        return None

    # ------------------------------------------------------------------ schedule versions and entries
    def active_version(self, year, month):
        for v in self._get("schedule_versions.json", []):
            if v["year"] == year and v["month"] == month and v["status"] == "active":
                return copy.deepcopy(v)
        return None

    def list_versions(self, year, month):
        out = []
        for v in sorted(self._get("schedule_versions.json", []), key=lambda v: -v["version_no"]):
            if v["year"] == year and v["month"] == month:
                out.append({**copy.deepcopy(v), "entry_count": len(self._get(f"schedule_entries/{v['id']}.json", []))})
        return out

    def commit_schedule_version_atomic(self, year, month, import_id, entries, user_id, replace):
        with self.transaction():
            versions = self._mut("schedule_versions.json", [])
            old = next((v for v in versions if v["year"] == year and v["month"] == month and v["status"] == "active"), None)
            if old and not replace:
                raise ConflictError("An active schedule already exists for this month; replacing it needs confirmation.")
            nxt = max([v["version_no"] for v in versions if v["year"] == year and v["month"] == month], default=0) + 1
            now = self._now()
            if old:
                old["status"], old["superseded_at"] = "superseded", now
            vid = self._next("schedule_versions")
            versions.append({"id": vid, "year": year, "month": month, "version_no": nxt, "import_id": import_id, "status": "active",
                             "created_by": user_id, "created_at": now, "superseded_at": None})
            rows = self._mut(f"schedule_entries/{vid}.json", [])
            for e in entries:
                rows.append({"id": self._next("schedule_entries"), "version_id": vid, "employee_id": e["employee_id"], "work_date": e["work_date"],
                             "shift_code": e["shift_code"], "stream": e.get("stream", ""), "note": e.get("note", ""),
                             "source": e.get("source", "import"), "revision": 1, "is_current": 1, "flags": e.get("flags", ""),
                             "created_by": user_id, "created_at": now})
        return {"id": vid, "version_no": nxt, "year": year, "month": month}

    def schedule_entries(self, date_from, date_to, employee_ids=None):
        if employee_ids is not None and not employee_ids:
            return []
        wanted = set(employee_ids) if employee_ids is not None else None
        lo = (datetime.fromisoformat(date_from) - timedelta(days=31)).strftime("%Y-%m")
        hi = (datetime.fromisoformat(date_to) + timedelta(days=31)).strftime("%Y-%m")
        out = []
        for v in self._get("schedule_versions.json", []):
            if v["status"] != "active" or not (lo <= f"{v['year']:04d}-{v['month']:02d}" <= hi):
                continue
            for e in self._get(f"schedule_entries/{v['id']}.json", []):
                if e["is_current"] and date_from <= e["work_date"] <= date_to and (wanted is None or e["employee_id"] in wanted):
                    out.append(copy.deepcopy(e))
        return sorted(out, key=lambda e: (e["work_date"], e["employee_id"], e["id"]))

    def version_entries(self, version_id, include_history=False):
        rows = [copy.deepcopy(e) for e in self._get(f"schedule_entries/{version_id}.json", []) if include_history or e["is_current"]]
        return sorted(rows, key=lambda e: (e["work_date"], e["employee_id"], e["id"]))

    def replace_day_entries_atomic(self, year, month, employee_id, work_date, new_entries, reason, user_id):
        with self.transaction():
            v = next((v for v in self._get("schedule_versions.json", [])
                      if v["year"] == year and v["month"] == month and v["status"] == "active"), None)
            if not v:
                raise ConflictError("No active schedule for this month.")
            rows = self._mut(f"schedule_entries/{v['id']}.json", [])
            cur = [r for r in rows if r["employee_id"] == employee_id and r["work_date"] == work_date and r["is_current"]]
            old_val = ",".join(sorted(r["shift_code"] for r in cur)) or "(none)"
            rev = max([r["revision"] for r in cur], default=0) + 1
            now = self._now()
            for r in cur:
                r["is_current"] = 0
            for e in new_entries:
                rows.append({"id": self._next("schedule_entries"), "version_id": v["id"], "employee_id": employee_id, "work_date": work_date,
                             "shift_code": e["shift_code"], "stream": e.get("stream", ""), "note": e.get("note", ""), "source": "manual",
                             "revision": rev, "is_current": 1, "flags": "", "created_by": user_id, "created_at": now})
            new_val = ",".join(sorted(e["shift_code"] for e in new_entries)) or "(none)"
            self._mut("schedule_changes.json", []).append({"id": self._next("schedule_changes"), "version_id": v["id"], "employee_id": employee_id,
                                                           "work_date": work_date, "old_value": old_val, "new_value": new_val,
                                                           "reason": reason, "user_id": user_id, "changed_at": now})
        return {"old": old_val, "new": new_val}

    def list_schedule_changes(self, year=None, month=None, employee_id=None):
        vers = {v["id"]: v for v in self._get("schedule_versions.json", [])}
        out = []
        for c in self._get("schedule_changes.json", []):
            v = vers.get(c["version_id"])
            if v and (not year or v["year"] == year) and (not month or v["month"] == month) and (not employee_id or c["employee_id"] == employee_id):
                out.append(copy.deepcopy(c))
        return sorted(out, key=lambda c: -c["id"])

    # ------------------------------------------------------------------ attendance records
    def commit_attendance_rows_atomic(self, import_id, rows):
        for r in rows:                                                   # validate everything first: all or nothing
            if self.directory.get_employee(r["employee_id"]) is None:
                raise ConflictError(f"Unknown employee {r['employee_id']}.")
        ins = ex = 0
        with self.transaction():
            for r in rows:
                month = self._mut(f"attendance_records/{_ym(r['work_date'])}.json", {})
                key = f"{r['employee_id']}|{r['work_date']}"
                if key in month:
                    ex += 1
                    continue
                month[key] = {"employee_id": r["employee_id"], "work_date": r["work_date"], "day_type": r.get("day_type", ""),
                              "in_time": r.get("in_time"), "in_source": r.get("in_source", ""), "out_time": r.get("out_time"),
                              "out_source": r.get("out_source", ""), "leave_text": r.get("leave_text", ""),
                              "permit_text": r.get("permit_text", ""), "mission_text": r.get("mission_text", ""), "import_id": import_id}
                ins += 1
        return {"inserted": ins, "existing": ex}

    def attendance_records(self, date_from, date_to, employee_ids=None):
        if employee_ids is not None and not employee_ids:
            return []
        wanted = set(employee_ids) if employee_ids is not None else None
        out = []
        for ym in _months(date_from, date_to):
            for r in self._get(f"attendance_records/{ym}.json", {}).values():
                if date_from <= r["work_date"] <= date_to and (wanted is None or r["employee_id"] in wanted):
                    out.append(copy.deepcopy(r))
        return sorted(out, key=lambda r: (r["employee_id"], r["work_date"]))

    def dates_with_attendance(self, date_from, date_to):
        return {r["work_date"] for ym in _months(date_from, date_to) for r in self._get(f"attendance_records/{ym}.json", {}).values()
                if date_from <= r["work_date"] <= date_to}

    # ------------------------------------------------------------------ append-only logs
    def _log_add(self, name, rec):
        with self.transaction():
            row = {"id": self._next(name), **rec}
            self._mut(f"{name}.json", []).append(row)
        return row["id"]

    def _log_query(self, name, date_from=None, date_to=None, employee_ids=None, date_key="work_date"):
        if employee_ids is not None and not employee_ids:
            return []
        out = []
        for r in self._get(f"{name}.json", []):
            if ((not date_from or r[date_key] >= date_from) and (not date_to or r[date_key] <= date_to)
                    and (employee_ids is None or r["employee_id"] in employee_ids)):
                out.append(copy.deepcopy(r))
        return out

    def add_correction(self, rec):
        self._log_add("corrections", {"employee_id": rec["employee_id"], "work_date": rec["work_date"], "field": rec["field"],
                                      "old_value": rec.get("old_value"), "new_value": rec.get("new_value"), "reason": rec["reason"],
                                      "user_id": rec.get("user_id"), "at": self._now()})

    def corrections(self, date_from, date_to, employee_ids=None):
        return sorted(self._log_query("corrections", date_from, date_to, employee_ids), key=lambda r: r["id"])

    def add_punch_report(self, rec):
        return self._log_add("punch_reports", {"employee_id": rec["employee_id"], "work_date": rec["work_date"], "kind": rec["kind"],
                                               "reason": rec.get("reason", ""), "reported_by": rec.get("reported_by"),
                                               "reported_at": self._now(), "status": "pending", "decided_by": None, "decided_at": None})

    def decide_punch_report(self, report_id, status, user_id):
        with self.transaction():
            for r in self._mut("punch_reports.json", []):
                if r["id"] == report_id:
                    r.update(status=status, decided_by=user_id, decided_at=self._now())

    def punch_reports(self, date_from=None, date_to=None, employee_ids=None):
        return sorted(self._log_query("punch_reports", date_from, date_to, employee_ids), key=lambda r: -r["id"])

    def set_exception_review(self, employee_id, work_date, kind, decision, reason, user_id):
        self._log_add("exception_reviews", {"employee_id": employee_id, "work_date": work_date, "kind": kind, "decision": decision,
                                            "reason": reason, "user_id": user_id, "at": self._now()})

    def exception_reviews(self, date_from, date_to, employee_ids=None):
        rows = sorted(self._log_query("exception_reviews", date_from, date_to, employee_ids), key=lambda r: r["id"])
        return {(r["employee_id"], r["work_date"], r["kind"]): r["decision"] for r in rows}

    def set_absence_review(self, employee_id, work_date, decision, reason, user_id):
        self._log_add("absence_reviews", {"employee_id": employee_id, "work_date": work_date, "decision": decision,
                                          "reason": reason, "user_id": user_id, "at": self._now()})

    def absence_reviews(self, date_from, date_to, employee_ids=None):
        rows = sorted(self._log_query("absence_reviews", date_from, date_to, employee_ids), key=lambda r: r["id"])
        return {(r["employee_id"], r["work_date"]): r["decision"] for r in rows}

    # ------------------------------------------------------------------ leave and compensatory days
    def list_leave_types(self):
        return copy.deepcopy(self._get("leave_types.json", DEFAULT_LEAVE_TYPES))

    def save_leave_type(self, rec):
        with self.transaction():
            rows = self._mut("leave_types.json", copy.deepcopy(DEFAULT_LEAVE_TYPES))
            new = {"code": rec["code"], "name": rec["name"], "counts_against_balance": bool(rec.get("counts_against_balance", True))}
            for i, r in enumerate(rows):
                if r["code"] == rec["code"]:
                    rows[i] = new
                    return
            rows.append(new)

    def set_entitlement(self, employee_id, year, leave_type, entitlement, opening_balance):
        with self.transaction():
            rows = self._mut("entitlements.json", [])
            for r in rows:
                if (r["employee_id"], r["year"], r["leave_type"]) == (employee_id, year, leave_type):
                    r.update(entitlement=entitlement, opening_balance=opening_balance)
                    return
            rows.append({"employee_id": employee_id, "year": year, "leave_type": leave_type, "entitlement": entitlement,
                         "opening_balance": opening_balance})

    def entitlements(self, year, employee_ids=None):
        if employee_ids is not None and not employee_ids:
            return []
        return [copy.deepcopy(r) for r in self._get("entitlements.json", []) if r["year"] == year
                and (employee_ids is None or r["employee_id"] in employee_ids)]

    def create_leave_request(self, rec):
        with self.transaction():
            rows = self._mut("leave_requests.json", [])
            for r in rows:
                if (r["employee_id"] == rec["employee_id"] and r["status"] in ("pending", "approved")
                        and r["date_from"] <= rec["date_to"] and r["date_to"] >= rec["date_from"]):
                    raise ConflictError(f"Overlaps leave request #{r['id']} of the same employee.")
            rid = self._next("leave_requests")
            rows.append({"id": rid, "employee_id": rec["employee_id"], "leave_type": rec["leave_type"], "date_from": rec["date_from"],
                         "date_to": rec["date_to"], "days": rec["days"], "status": rec.get("status", "pending"), "reason": rec.get("reason", ""),
                         "created_by": rec.get("created_by"), "created_at": self._now(), "decided_by": None, "decided_at": None})
        return rid

    def decide_leave_request(self, request_id, status, user_id):
        with self.transaction():
            for r in self._mut("leave_requests.json", []):
                if r["id"] == request_id:
                    r.update(status=status, decided_by=user_id, decided_at=self._now())

    def leave_requests(self, employee_ids=None, date_from=None, date_to=None, status=None):
        if employee_ids is not None and not employee_ids:
            return []
        out = [copy.deepcopy(r) for r in self._get("leave_requests.json", [])
               if (employee_ids is None or r["employee_id"] in employee_ids) and (not date_from or r["date_to"] >= date_from)
               and (not date_to or r["date_from"] <= date_to) and (not status or r["status"] == status)]
        return sorted(out, key=lambda r: (r["date_from"], r["id"]), reverse=True)

    def add_comp_day(self, rec):
        return self._log_add("comp_days", {"employee_id": rec["employee_id"], "earned_date": rec["earned_date"], "days": rec.get("days", 1),
                                           "reason": rec["reason"], "planned_take_date": rec.get("planned_take_date"), "taken_date": None,
                                           "created_by": rec.get("created_by"), "created_at": self._now()})

    def update_comp_day(self, comp_id, patch):
        with self.transaction():
            for r in self._mut("comp_days.json", []):
                if r["id"] == comp_id:
                    r.update(patch)

    def comp_days(self, employee_ids=None):
        if employee_ids is not None and not employee_ids:
            return []
        out = [copy.deepcopy(r) for r in self._get("comp_days.json", []) if employee_ids is None or r["employee_id"] in employee_ids]
        return sorted(out, key=lambda r: (r["earned_date"], r["id"]), reverse=True)

    # ------------------------------------------------------------------ uploaded files
    def save_upload(self, sha256, filename, data):
        p = self._path(f"uploads/{sha256}.bin")
        if not p.exists():
            write_bytes_atomic(p, data)

    def load_upload(self, sha256):
        p = self._path(f"uploads/{sha256}.bin")
        return p.read_bytes() if p.exists() else None

    # ------------------------------------------------------------------ scoring rules and audit
    def scoring_rule_versions(self):
        return copy.deepcopy(self._get("scoring_rules.json", []))

    def add_scoring_rule_version(self, rules, effective_from, note, user_id):
        with self.transaction():
            rows = self._mut("scoring_rules.json", [])
            ver = self._next("scoring_rules")
            rows.append({"version": ver, "rules": copy.deepcopy(rules), "effective_from": effective_from, "note": note,
                         "created_by": user_id, "created_at": self._now()})
        return ver

    def audit(self, rec):
        self.audit_backend.write({"ts": rec.get("ts") or self._now(), "user_id": rec.get("user_id"), "username": rec.get("username"),
                                  "action": rec["action"], "entity": rec.get("entity"),
                                  "entity_id": str(rec.get("entity_id")) if rec.get("entity_id") is not None else None,
                                  "detail": rec.get("detail") or {}, "ip": rec.get("ip")})

    def list_audit(self, action=None, entity=None, limit=500):
        return self.audit_backend.read(action, entity, limit)
