"""Decision engine: turns a portal ticket (+ whether the user exists in CEM)
into a Plan.  Pure Python - no browser - so it is fully unit-tested.

Logic.docx summary
------------------
MODIFY  (تعديل صلاحية)       user missing -> reject "ليس لدية كود"
                              user found   -> active ON / locked OFF, reset roles,
                                              set branch + grants by category, close done
REACTIVATE (إعادة تشغيل)     missing -> reject "ليس لدية كود"; found -> active ON, locked OFF, done
CANCEL  (الغاء)               missing -> reject "ليس لدية كود"; found -> active OFF, locked OFF, done
CREATE  (انشاء)               found   -> reject "Other" + note "لديه كود بالفعل ..."
                              missing -> create user, role, branch + grants, close done
MODIFY / CREATE are only automated for job codes listed in Mapping.xlsx.
"""
from __future__ import annotations

import re

from .mapping import MappingTable
from .models import Action, Plan, Ticket, TicketType
from .textnorm import compact, norm


class Rules:
    def __init__(self, rules_cfg: dict, mapping: MappingTable):
        self.cfg = rules_cfg
        self.mapping = mapping
        # longest alias first so "تعديل صلاحية أكواد ..." wins over "تعديل صلاحية"
        self._aliases: list[tuple[str, TicketType]] = sorted(
            ((compact(a), TicketType(t)) for t, al in rules_cfg["ticket_types"].items() for a in al),
            key=lambda x: -len(x[0]))

    # ------------------------------------------------------------ parsing
    def classify(self, ticket_type_raw: str) -> TicketType | None:
        c = compact(ticket_type_raw)
        if not c:
            return None
        for alias, ttype in self._aliases:
            if c == alias:
                return ttype
        for alias, ttype in self._aliases:   # tolerate trailing extras
            if c.startswith(alias):
                return ttype
        return None

    def branch_id(self, branch_raw: str) -> str | None:
        return extract_branch_id(branch_raw, int(self.cfg.get("branch_id_digits", 3)))

    @staticmethod
    def job_code(job_title_raw: str) -> str | None:
        return extract_job_code(job_title_raw)

    # ------------------------------------------------------------ planning
    def category_settings(self, category: str, ttype: TicketType) -> dict:
        base = dict(self.cfg["categories"][category])
        override = (self.cfg.get("overrides") or {}).get(ttype.value, {}).get(category, {})
        base.update(override or {})
        return base

    def precheck(self, t: Ticket) -> Plan | None:
        """Return a SKIP plan if the ticket cannot be automated, else None.
        Runs before CEM is touched."""
        if t.ticket_type is None:
            t.ticket_type = self.classify(t.ticket_type_raw)
        if t.ticket_type is None:
            return Plan(Action.SKIP, t, skip_reason=f"unknown ticket type '{t.ticket_type_raw}'")
        if not re.fullmatch(r"\d{3,}", norm(t.user_id) or ""):
            return Plan(Action.SKIP, t, skip_reason=f"invalid user id '{t.user_id}'")
        if t.ticket_type in (TicketType.MODIFY, TicketType.CREATE):
            code = self.job_code(t.job_title_raw)
            if not code:
                return Plan(Action.SKIP, t, skip_reason=f"no job code in '{t.job_title_raw}'")
            if not self.mapping.category_for(code):
                return Plan(Action.SKIP, t, job_code=code,
                            skip_reason=f"job code {code} is not in Mapping.xlsx - handle manually")
            if not self.branch_id(t.branch_raw):
                return Plan(Action.SKIP, t, job_code=code,
                            skip_reason=f"no branch number in '{t.branch_raw}'")
        return None

    def build_plan(self, t: Ticket, user_exists: bool) -> Plan:
        skip = self.precheck(t)
        if skip:
            return skip
        tt = t.ticket_type
        dec, rsn, notes = self.cfg["decision_text"], self.cfg["reason_text"], self.cfg["notes_text"]
        confirm = tt.value in (self.cfg.get("confirm_matrix_for") or [])

        def reject_no_code() -> Plan:
            return Plan(Action.REJECT_NO_CODE, t, decision=dec["not_done"], reason=rsn["no_code"],
                        confirm_matrix=confirm)

        if tt in (TicketType.REACTIVATE, TicketType.CANCEL):
            if not user_exists:
                return reject_no_code()
            flags = self.cfg["user_flags"][tt.value]
            action = Action.REACTIVATE_USER if tt == TicketType.REACTIVATE else Action.CANCEL_USER
            return Plan(action, t, set_active=flags["active"], set_locked=flags["locked"],
                        decision=dec["done"], confirm_matrix=confirm)

        code = self.job_code(t.job_title_raw)
        category = self.mapping.category_for(code)
        cs = self.category_settings(category, tt)
        common = dict(category=category, job_code=code, branch_id=self.branch_id(t.branch_raw),
                      role=cs.get("role"), grants=list(cs.get("grants") or []),
                      decision=dec["done"], confirm_matrix=confirm)

        if tt == TicketType.MODIFY:
            if not user_exists:
                p = reject_no_code()
                p.category, p.job_code = category, code
                return p
            flags = self.cfg["user_flags"]["MODIFY"]
            return Plan(Action.MODIFY_USER, t, clear_roles=True,
                        clear_branches=bool(cs.get("clear_existing_branches", True)),
                        set_active=flags["active"], set_locked=flags["locked"], **common)

        # CREATE
        if user_exists:
            return Plan(Action.REJECT_ALREADY_EXISTS, t, category=category, job_code=code,
                        decision=dec["not_done"], reason=rsn["other"],
                        notes=notes["already_has_code"], confirm_matrix=confirm)
        return Plan(Action.CREATE_USER, t, clear_roles=False, clear_branches=False, **common)

    def new_user_name(self, t: Ticket) -> str:
        fmt = self.cfg["new_user"]["name_format"]
        return fmt.format(user_id=norm(t.user_id), user_name=" ".join(t.user_name.split()))


# ---------------------------------------------------------------- helpers
def extract_branch_id(branch_raw: str, digits: int = 3) -> str | None:
    """"حلوان - 15100" / "15100 - حلوان" / "15100" -> "151".

    The procedure: take the number next to the "-" symbol, then its first
    *digits* characters from the left."""
    text = norm(branch_raw)
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"\s*-\s*", text)]
    number = next((p for p in parts if re.fullmatch(r"\d+", p) and len(p) >= digits), None)
    if number is None:
        m = re.search(r"\d{%d,}" % digits, text)
        number = m.group(0) if m else None
    return number[:digits] if number else None


def extract_job_code(job_title_raw: str) -> str | None:
    """"100056389-مصرفي / العمليات بالفروع" -> "100056389"."""
    m = re.search(r"\d{6,}", norm(job_title_raw))
    return m.group(0) if m else None
