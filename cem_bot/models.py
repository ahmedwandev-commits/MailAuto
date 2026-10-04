"""Plain data objects shared by the rules engine, the browser layer and reports."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TicketType(str, Enum):
    MODIFY = "MODIFY"          # تعديل صلاحية
    REACTIVATE = "REACTIVATE"  # إعادة تشغيل
    CANCEL = "CANCEL"          # الغاء
    CREATE = "CREATE"          # انشاء


class Action(str, Enum):
    MODIFY_USER = "MODIFY_USER"                  # update existing CEM user + close as done
    REACTIVATE_USER = "REACTIVATE_USER"          # active ON, locked OFF + close as done
    CANCEL_USER = "CANCEL_USER"                  # active OFF, locked OFF + close as done
    CREATE_USER = "CREATE_USER"                  # create CEM user + close as done
    REJECT_NO_CODE = "REJECT_NO_CODE"            # user not in CEM -> لم يتم التنفيذ / ليس لدية كود
    REJECT_ALREADY_EXISTS = "REJECT_ALREADY_EXISTS"  # create but user exists -> Other + note
    SKIP = "SKIP"                                # not handled automatically, left open


class Outcome(str, Enum):
    DONE = "DONE"            # CEM updated, ticket closed "تم التنفيذ"
    REJECTED = "REJECTED"    # ticket closed "لم يتم التنفيذ"
    SKIPPED = "SKIPPED"      # left open for a human
    PLANNED = "PLANNED"      # dry-run only: what would happen
    ERROR = "ERROR"          # something failed, ticket left open


@dataclass
class Ticket:
    """One row of the Request_Making results table."""
    user_id: str
    user_name: str
    ticket_type_raw: str
    branch_raw: str
    job_title_raw: str
    request_date: str = ""
    system: str = ""
    ticket_type: TicketType | None = None
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return "|".join([self.user_id, self.ticket_type_raw.strip(), self.request_date.strip()])


@dataclass
class Plan:
    action: Action
    ticket: Ticket
    category: str | None = None          # Maker / Checker / Manager
    job_code: str | None = None
    branch_id: str | None = None
    role: str | None = None              # role to add (None = none)
    grants: list[str] = field(default_factory=list)
    clear_roles: bool = False
    clear_branches: bool = False
    set_active: bool | None = None
    set_locked: bool | None = None
    decision: str | None = None          # text for "القرار"
    reason: str | None = None            # text for "الأسباب"
    notes: str | None = None             # text for "ملاحظات"
    confirm_matrix: bool = False
    skip_reason: str | None = None

    def describe(self) -> str:
        a = self.action
        if a == Action.SKIP:
            return f"SKIP - {self.skip_reason}"
        if a == Action.REJECT_NO_CODE:
            return f"Reject: {self.decision} / {self.reason} (user not found in CEM)"
        if a == Action.REJECT_ALREADY_EXISTS:
            return f"Reject: {self.decision} / {self.reason} / '{self.notes}' (user already in CEM)"
        parts = []
        if a == Action.CREATE_USER:
            parts.append("create CEM user")
        if self.set_active is not None:
            parts.append(f"active={'ON' if self.set_active else 'OFF'}")
        if self.set_locked is not None:
            parts.append(f"locked={'ON' if self.set_locked else 'OFF'}")
        if self.clear_roles or self.role:
            parts.append(f"roles=[{self.role or ''}]")
        if self.branch_id:
            parts.append(("replace branches with " if self.clear_branches else "add branch ")
                         + f"{self.branch_id} grants={'+'.join(self.grants)}")
        parts.append(f"close: {self.decision}")
        return "; ".join(parts)


@dataclass
class TicketResult:
    ticket: Ticket
    plan: Plan | None
    outcome: Outcome
    message: str = ""
    steps: list[str] = field(default_factory=list)
    user_existed: bool | None = None
    started: str = ""
    seconds: float = 0.0
    screenshot: str = ""
