"""The storage contract.

Every read and write the module does goes through ``Repository``. Services, domain logic, views and the API never
import ``sqlite3`` and never see SQL. To store the data elsewhere (for example in OpsHub's JSON files) write another
class with the same methods and pass it to ``create_app(repo=...)``; ``tests/test_repository_contract.py`` runs the same
behavioural tests against any implementation (see docs/STORAGE-ADAPTATION.md).

Conventions - all data crosses this boundary as plain ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``None``:

* dates are ``YYYY-MM-DD`` strings, times ``HH:MM`` or ``HH:MM:SS``, timestamps ISO 8601 (naive local time);
* employee ids are ``int`` (the HR employee number); team names are ``str``; users are identified by ``user_id`` (str);
* methods that return a record return ``None`` when it does not exist; lists are never ``None``;
* ``*_atomic`` methods must be all-or-nothing (SQLite: one transaction; JSON: write a temp file and swap it in);
* nothing here enforces permissions - team scoping is applied by the services, which pass explicit ``employee_ids``.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager


class ConflictError(Exception):
    """A write that must not overwrite existing data (duplicate import, overlapping leave, active version exists)."""


class Repository(ABC):
    # ---------------------------------------------------------------- lifecycle
    @abstractmethod
    def migrate(self) -> list:
        """Create / upgrade the storage. Returns the names of the migrations applied now."""

    @contextmanager
    def transaction(self):
        """Group several calls into one all-or-nothing unit (a no-op is acceptable if the backend is single-writer)."""
        yield

    # ---------------------------------------------------------------- users, teams
    @abstractmethod
    def get_user(self, user_id): ...
    @abstractmethod
    def list_users(self): ...
    @abstractmethod
    def create_user(self, user: dict): """user_id, name, role, teams(list), password_hash, active, must_change_password"""
    @abstractmethod
    def update_user(self, user_id, patch: dict): ...

    # ---------------------------------------------------------------- employees
    @abstractmethod
    def get_employee(self, employee_id): ...
    @abstractmethod
    def list_employees(self, active_only=True): """-> [{id, name, department, sector, team, active}]"""
    @abstractmethod
    def add_employees_if_missing(self, employees: list) -> int:
        """Insert employees whose id does not exist yet; NEVER update an existing one. Returns how many were created."""
    @abstractmethod
    def update_employee(self, employee_id, patch: dict): ...

    # ---------------------------------------------------------------- configuration
    @abstractmethod
    def get_settings(self) -> dict: ...
    @abstractmethod
    def save_settings(self, values: dict): ...
    @abstractmethod
    def list_shift_types(self): """-> [{code, name, kind(work|off|standby), start_time, end_time, sort_order}]"""
    @abstractmethod
    def save_shift_type(self, shift: dict): ...
    @abstractmethod
    def list_holidays(self): """-> [{date, name}]"""
    @abstractmethod
    def add_holiday(self, date, name): ...
    @abstractmethod
    def remove_holiday(self, date): ...
    @abstractmethod
    def get_aliases(self) -> dict: """normalised schedule name -> employee_id"""
    @abstractmethod
    def save_alias(self, alias_norm, employee_id, user_id): ...
    def delete_alias(self, alias_norm): ...
    def shift_approvals(self) -> dict: """'YYYY-MM|team' -> {month, team, by, by_name, at, digest, days, employees}"""
    def set_shift_approval(self, month, team, rec): ...
    def delete_shift_approval(self, month, team): ...

    # ---------------------------------------------------------------- schedule imports and versions
    @abstractmethod
    def create_schedule_import(self, rec: dict) -> int: """year, month, filename, file_sha256, content_sha256, team, uploaded_by, status, parsed(dict)"""
    @abstractmethod
    def get_schedule_import(self, import_id): ...
    @abstractmethod
    def update_schedule_import(self, import_id, patch: dict): ...
    @abstractmethod
    def list_schedule_imports(self): ...
    @abstractmethod
    def find_completed_schedule_import(self, file_sha256=None, content_sha256=None, year=None, month=None): ...
    @abstractmethod
    def active_version(self, year, month): """-> {id, year, month, version_no, import_id, status, created_by, created_at} or None"""
    @abstractmethod
    def list_versions(self, year, month): ...
    @abstractmethod
    def commit_schedule_version_atomic(self, year, month, import_id, entries: list, user_id, replace: bool) -> dict:
        """Create version N+1 with ``entries`` (see schedule_entries) and make it the active one. If an active version exists
        and ``replace`` is False raise ConflictError; if True the old one becomes 'superseded' (rows are kept)."""
    @abstractmethod
    def schedule_entries(self, date_from, date_to, employee_ids=None) -> list:
        """CURRENT entries (latest revision) of the ACTIVE version(s) covering the dates.
        Entry: {id, version_id, employee_id, work_date, shift_code, stream, note, source, revision, flags}"""
    @abstractmethod
    def version_entries(self, version_id, include_history=False) -> list: ...
    @abstractmethod
    def replace_day_entries_atomic(self, year, month, employee_id, work_date, new_entries: list, reason, user_id): """Keep the old rows (not current), add new revision rows, write a schedule_changes row."""
    @abstractmethod
    def list_schedule_changes(self, year=None, month=None, employee_id=None): ...

    # ---------------------------------------------------------------- attendance
    @abstractmethod
    def create_attendance_import(self, rec: dict) -> int: ...
    @abstractmethod
    def get_attendance_import(self, import_id): ...
    @abstractmethod
    def update_attendance_import(self, import_id, patch: dict): ...
    @abstractmethod
    def list_attendance_imports(self): ...
    @abstractmethod
    def find_completed_attendance_import(self, file_sha256): ...
    @abstractmethod
    def commit_attendance_rows_atomic(self, import_id, rows: list) -> dict:
        """Insert rows whose (employee_id, work_date) does not exist; never overwrite. -> {'inserted': n, 'existing': n}"""
    @abstractmethod
    def attendance_records(self, date_from, date_to, employee_ids=None) -> list: ...
    @abstractmethod
    def dates_with_attendance(self, date_from, date_to) -> set: ...
    @abstractmethod
    def add_correction(self, rec: dict): """employee_id, work_date, field(in_time|out_time), new_value, reason, user_id (old value is stored)"""
    @abstractmethod
    def corrections(self, date_from, date_to, employee_ids=None): ...
    @abstractmethod
    def add_punch_report(self, rec: dict) -> int: """employee-reported forgotten punch: employee_id, work_date, kind(in|out|both), reason, reported_by"""
    @abstractmethod
    def decide_punch_report(self, report_id, status, user_id): """verified|rejected"""
    @abstractmethod
    def punch_reports(self, date_from=None, date_to=None, employee_ids=None): ...
    @abstractmethod
    def set_exception_review(self, employee_id, work_date, kind, decision, reason, user_id):
        """Review of an attendance permit: kind late (اذن حضور) | early (اذن انصراف), decision provided | excused | rejected. Append-only, latest wins."""
    @abstractmethod
    def exception_reviews(self, date_from, date_to, employee_ids=None) -> dict: """(employee_id, work_date, kind) -> decision"""
    @abstractmethod
    def set_absence_review(self, employee_id, work_date, decision, reason, user_id): """excused|unexcused - append-only, latest wins"""
    @abstractmethod
    def absence_reviews(self, date_from, date_to, employee_ids=None) -> dict: """(employee_id, work_date) -> decision"""

    # ---------------------------------------------------------------- leave and compensatory days
    @abstractmethod
    def list_leave_types(self): """-> [{code, name, counts_against_balance}]"""
    @abstractmethod
    def save_leave_type(self, rec: dict): ...
    @abstractmethod
    def set_entitlement(self, employee_id, year, leave_type, entitlement, opening_balance): ...
    @abstractmethod
    def entitlements(self, year, employee_ids=None): """-> [{employee_id, year, leave_type, entitlement, opening_balance}]"""
    @abstractmethod
    def create_leave_request(self, rec: dict) -> int:
        """employee_id, leave_type, date_from, date_to, days, status, reason, created_by. Raise ConflictError when the dates overlap
        another pending/approved request of the same employee (prevents double deduction)."""
    @abstractmethod
    def decide_leave_request(self, request_id, status, user_id): """approved|rejected|cancelled"""
    @abstractmethod
    def leave_requests(self, employee_ids=None, date_from=None, date_to=None, status=None): ...
    @abstractmethod
    def add_comp_day(self, rec: dict) -> int: """employee_id, earned_date, days, reason, planned_take_date, created_by"""
    @abstractmethod
    def update_comp_day(self, comp_id, patch: dict): """taken_date, planned_take_date"""
    @abstractmethod
    def comp_days(self, employee_ids=None): ...

    # ---------------------------------------------------------------- uploaded files
    @abstractmethod
    def save_upload(self, sha256, filename, data: bytes): """keep the original workbook (idempotent on the hash)"""
    @abstractmethod
    def load_upload(self, sha256) -> bytes: """-> the bytes, or None"""

    # ---------------------------------------------------------------- scoring rules, audit
    @abstractmethod
    def scoring_rule_versions(self): """-> [{version, rules(dict), effective_from, note, created_by, created_at}] oldest first"""
    @abstractmethod
    def add_scoring_rule_version(self, rules: dict, effective_from, note, user_id) -> int: ...
    @abstractmethod
    def audit(self, rec: dict): """ts, user_id, username, action, entity, entity_id, detail(dict), ip"""
    @abstractmethod
    def list_audit(self, action=None, entity=None, limit=500): ...
