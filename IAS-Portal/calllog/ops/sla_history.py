# -*- coding: utf-8 -*-
"""Exact SLA lifecycle of a security request, read LIVE from the portal's status history.

The «الاستفسار عن الطلبات» page (Security_Assign_Maker/RequestDetails_Inquiry.aspx) lists the requests of a period,
and each request's «التفاصيل» holds «الحالة التاريخية للطلب»: one line per step - who did it (user number, name, user
type), when (date and time) and which status. The bank's workflow is:

    تسجيل                              Maker Branch        the branch registers the request
    تم التعزيز من قبل مشرف الفرع       Checker Branch      the branch supervisor confirms it
    اسناد لمستخدم السرية               the assigner        assigned to a security user        <- SLA clock starts
    معلق                               security user       optional hold
    تم التنفيذ / لم يتم التنفيذ        Maker Security      the security user (مستخدم السرية) acts on it
    تم التعزيز على التنفيذ / عدم ...    Checker security    the reviewer (مراجع السرية) confirms it

``events_from_table`` turns that table into events in TIME order (the page does not list them in order),
``lifecycle`` turns the events into exact stamps and people. The grid helpers key the inquiry rows (the grid shows no
request number, so a row is keyed on its own fixed columns) and decide which rows need their details opened (new, or
status changed). ``fold_history`` writes the result into the SLA recording (``sla_records``), and ``analyze`` builds the
SLA Analysis from it. Everything is live and kept as history: nothing is uploaded. Pure functions over plain dicts; the
status labels come from the SLA settings (``sla_records.default_config()['statuses']``)."""
import hashlib
import re
from datetime import datetime

from .engines.common import norm_arabic

_FMT = "%Y-%m-%dT%H:%M:%S"
SOURCE = "history"                  # records whose stamps come from the portal's status history (exact)

# step key -> the SLA-settings status list it is read from (in the order a request goes through them)
STEP_KEYS = ("registered", "branch_confirmed", "region_approved", "assigned", "held", "returned", "executed",
             "not_executed", "confirmed_executed", "confirmed_not", "rejected", "transferred")
# a settings list that is read as another step: a re-assignment is an assignment (to another security user)
STEP_ALIASES = {"reassigned": "assigned"}
_RANK = {k: i for i, k in enumerate(STEP_KEYS)}                  # tie-break for two steps stamped the same second
_RANK["confirmed_not"] = _RANK["confirmed_executed"]
_RANK["not_executed"] = _RANK["executed"]
_RANK["transferred"] = _RANK["region_approved"]

# the stages of a request's life the analysis times (minutes), in journey order; "sla" is THE SLA (first assignment ->
# the reviewer's confirmation, target ``sla_minutes``); the others have their own targets (``stage_targets``)
STAGES = (("branch", "Branch approval", "registered_at", "branch_confirmed_at"),
          ("region", "Region / sector approval", "branch_confirmed_at", "region_approved_at"),
          ("pickup", "Waiting to be assigned", "pool_at", "first_assigned_at"),
          ("work", "Security user (assignment → execution)", "assigned_at", "executed_at"),
          ("review", "Reviewer (execution → confirmation)", "executed_at", "confirmed_at"),
          ("sla", "SLA (first assignment → confirmation)", "first_assigned_at", "confirmed_at"),
          ("lifetime", "Whole request (registration → confirmation)", "registered_at", "confirmed_at"))
JOURNEY = ("branch", "region", "pickup", "work", "review")        # the hand-offs that add up to a request's lifetime

HISTORY_COLUMNS = {"user_no": "رقم المستخدم", "user_name": "اسم المستخدم", "user_type": "نوع المستخدم",
                   "at": "التاريخ", "status": "الحالة", "notes": "ملاحظات"}
GRID_COLUMNS = {"date": "تاريخ الطلب", "form": "نوع النموذج", "branch": "الفرع / الادارة", "employee": "اسم الموظف",
                "employee_no": "رقم الموظف", "user_no": "رقم المستخدم", "system": "النظام", "group": "المجموعة",
                "request_type": "نوع الطلب", "assigner": "موظف الاسناد", "executor": "مستخدم السرية",
                "checker": "مراجع السرية", "notes": "ملاحظات", "status": "الحالة"}
# the grid columns that never change over a request's life: what identifies a row before its details are opened
GRID_KEY_FIELDS = ("date", "employee_no", "user_no", "system", "request_type", "form", "group", "branch", "employee")

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_DMY = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")
_YMD = re.compile(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")
_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|ص|م)?", re.I)


def _clean(text):
    return " ".join(str(text or "").replace("﻿", "").split())


def _norm(text):
    return norm_arabic(_clean(text)).lower()


def parse_time(text):
    """The portal's '21/10/2026 08:54:23' (day first; Arabic digits; AM/PM or ص/م; or yyyy-mm-dd) -> datetime/None."""
    s = _clean(text).translate(_DIGITS)
    m = _YMD.search(s)
    if m:
        year, month, day = map(int, m.groups())
    else:
        m = _DMY.search(s)
        if not m:
            return None
        day, month, year = map(int, m.groups())
    rest = s[:m.start()] + " " + s[m.end():]
    hh = mm = ss = 0
    t = _TIME.search(rest)
    if t:
        hh, mm, ss = int(t.group(1)), int(t.group(2)), int(t.group(3) or 0)
        ampm = (t.group(4) or "").upper()
        if ampm in ("PM", "م") and hh < 12:
            hh += 12
        elif ampm in ("AM", "ص") and hh == 12:
            hh = 0
    try:
        return datetime(year, month, day, hh, mm, ss)
    except ValueError:
        return None


def find_col(headers, wanted):
    """Index of the header equal to ``wanted`` (Arabic-spelling-insensitive), else the first containing it; None."""
    w = _norm(wanted)
    names = [_norm(h) for h in headers or []]
    for i, n in enumerate(names):
        if n == w:
            return i
    for i, n in enumerate(names):
        if w and w in n:
            return i
    return None


_LABELS_MEMO = {}


def _labels(cfg):
    """[(normalised label, step or None)] of the settings, longest label first (memoised per settings)."""
    statuses = (cfg or {}).get("statuses") or {}
    memo_key = repr(sorted((k, tuple(v or ())) for k, v in statuses.items()))
    hit = _LABELS_MEMO.get(memo_key)
    if hit is None:
        pairs = []
        for key, labels in statuses.items():
            step = STEP_ALIASES.get(key, key)
            if step not in STEP_KEYS and key != "in_progress":
                continue
            for label in labels or []:
                n = _norm(label)
                if n:
                    pairs.append((n, None if key == "in_progress" else step))
        pairs.sort(key=lambda p: -len(p[0]))
        if len(_LABELS_MEMO) > 32:
            _LABELS_MEMO.clear()
        hit = _LABELS_MEMO[memo_key] = pairs
    return hit


def step_of(status, cfg):
    """The lifecycle step a status label means (``STEP_KEYS``), or None for a label that is not a step. An exact label
    wins; else the LONGEST label the status contains («تم التعزيز من قبل مشرف الفرع و بانتظار رد المنطقة» is a branch
    confirmation, «إعادة اسناد لمسخدم السرية» a re-assignment, «لم يتم التنفيذ» is not «تم التنفيذ»). A current-status
    word («تحت التنفيذ») is never a step."""
    s = _norm(status)
    if not s:
        return None
    pairs = _labels(cfg)
    for label, step in pairs:
        if s == label:
            return step
    for label, step in pairs:
        if label in s:
            return step
    return None


def events_from_table(headers, rows, cfg):
    """The history table -> events in time order: {at (ISO), step, status, user, user_no, user_type, notes}. A line
    without a readable time, or whose status is not a lifecycle step, is kept out (never fatal)."""
    col = {k: find_col(headers, v) for k, v in HISTORY_COLUMNS.items()}
    out = []
    for n, r in enumerate(rows or []):
        cell = lambda k: _clean(r[col[k]]) if col[k] is not None and col[k] < len(r) else ""   # noqa: E731
        dt = parse_time(cell("at"))
        step = step_of(cell("status"), cfg)
        if dt is None or step is None:
            continue
        out.append({"at": dt.strftime(_FMT), "step": step, "status": cell("status"), "user": cell("user_name"),
                    "user_no": cell("user_no"), "user_type": cell("user_type"), "notes": cell("notes"), "_n": n})
    out.sort(key=lambda e: (e["at"], _RANK[e["step"]], e["_n"]))
    for e in out:
        e.pop("_n", None)
    return out


def _minutes(a, b):
    try:
        ta, tb = datetime.strptime(_clean(a)[:19], _FMT), datetime.strptime(_clean(b)[:19], _FMT)
    except (ValueError, TypeError):
        return None
    m = (tb - ta).total_seconds() / 60.0
    return m if m >= 0 else None


TRAIL_NOTE_STEPS = ("returned", "rejected", "not_executed", "confirmed_not", "held", "transferred")


def trail_of(events):
    """The request's timeline as kept with its record: [[at, step, status, who, their number, note]] - the note only
    where it explains something (why it was sent back, rejected, not executed, held), at most 160 characters."""
    return [[e["at"], e["step"], e.get("status", ""), e.get("user", ""), e.get("user_no", ""),
             _clean(e.get("notes"))[:160] if e["step"] in TRAIL_NOTE_STEPS else ""] for e in events or []]


def lifecycle(events, cfg=None):
    """Exact stamps of one request from its time-ordered events. Each clock measures the FINAL turn of each person:
    the confirmation is the last one; the execution the last one before it; the assignment the last one before the
    execution (a reassignment restarts the security user's clock; ``first_assigned_at`` keeps the team's full clock).
    ``held_minutes`` adds up every «معلق» until the next step. ``region_approved_at`` = an approval above the branch
    (region / financial sector / Platinum) and ``pool_at`` = when the request reached the security team's pool (that
    approval, else the branch supervisor's, else the registration). ``returned`` = times it was sent back to the branch
    («مطلوب تعديل»), ``rejected_at`` = a rejection with no confirmation after it. ``stage``: closed (confirmed) /
    rejected / awaiting (executed, waiting for the reviewer) / open (with a security user) / pre (still at the branch)
    / none. ``outcome``: completed / not_done / rejected / in_progress."""
    ev = list(events or [])
    of = lambda *keys: [e for e in ev if e["step"] in keys]          # noqa: E731
    before = lambda items, limit: [e for e in items if not limit or e["at"] <= limit["at"]]  # noqa: E731
    confirms = of("confirmed_executed", "confirmed_not")
    confirm = confirms[-1] if confirms else None
    execs = before(of("executed", "not_executed"), confirm)
    execu = execs[-1] if execs else None
    assigns = of("assigned")
    assign = (before(assigns, execu) or [None])[-1] if assigns else None
    first_assign = assigns[0] if assigns else None
    registers = of("registered")
    branch = before(of("branch_confirmed"), first_assign)
    region = before(of("region_approved"), first_assign) or of("region_approved")
    rejects = [e for e in of("rejected") if not confirm or e["at"] > confirm["at"]]
    reject = rejects[-1] if rejects else None
    held = 0.0
    on_hold = False
    for i, e in enumerate(ev):
        if e["step"] != "held":
            continue
        nxt = next((x for x in ev[i + 1:] if x["step"] != "held"), None)
        if nxt is None:
            on_hold = True
            continue
        held += _minutes(e["at"], nxt["at"]) or 0.0
    if confirm:
        stage = "closed"
    elif reject:
        stage = "rejected"
    elif execu:
        stage = "awaiting"
    elif assign:
        stage = "open"
    elif registers or branch or region:
        stage = "pre"
    else:
        stage = "none"
    if confirm:
        outcome = "not_done" if confirm["step"] == "confirmed_not" else "completed"
    else:
        outcome = "rejected" if reject else "in_progress"
    pick = lambda e, k: e[k] if e else ""                            # noqa: E731
    # a re-assignment hands the request to another security user; the assignment after a request came back corrected
    # from the branch is not one
    reassigned, last = 0, None
    for e in ev:
        if e["step"] == "assigned" and last is not None and e is not first_assign and last != "returned":
            reassigned += 1
        if e["step"] != "held":
            last = e["step"]
    registered_at = registers[0]["at"] if registers else ""
    branch_at = branch[-1]["at"] if branch else ""
    region_at = region[0]["at"] if region else ""
    return {
        "registered_at": registered_at,
        "branch_confirmed_at": branch_at,
        "region_approved_at": region_at, "region_by": pick(region[0] if region else None, "user"),
        "pool_at": region_at or branch_at or registered_at,
        "first_assigned_at": pick(first_assign, "at"),
        "assigned_at": pick(assign, "at"), "assigner": pick(assign, "user"), "assigner_no": pick(assign, "user_no"),
        "executed_at": pick(execu, "at"), "executor": pick(execu, "user"), "executor_no": pick(execu, "user_no"),
        "exec_outcome": ("not_executed" if execu["step"] == "not_executed" else "executed") if execu else "",
        "confirmed_at": pick(confirm, "at"), "checker": pick(confirm, "user"), "checker_no": pick(confirm, "user_no"),
        "final_outcome": ("not_executed" if confirm["step"] == "confirmed_not" else "executed") if confirm else "",
        "rejected_at": pick(reject, "at"), "rejected_by": pick(reject, "user"),
        "returned": len(of("returned")), "transferred": len(of("transferred")),
        "held_minutes": round(held, 1), "on_hold": on_hold,
        "reassigned": reassigned,
        "stage": stage, "outcome": outcome, "steps": len(ev),
        "last_status": ev[-1]["status"] if ev else "",
        "trail": trail_of(ev),
    }


def closing_day(r):
    """The day a finished request is filed under: its confirmation, else its rejection ("" while it is not finished)."""
    if r.get("stage") == "rejected" or (not r.get("confirmed_at") and r.get("rejected_at")):
        return _clean(r.get("rejected_at"))[:10]
    return _clean(r.get("confirmed_at"))[:10]


def is_finished(r):
    return stage_of(r) in ("closed", "rejected") and bool(closing_day(r))


def stage_minutes(r):
    """{stage key: minutes or None} of one record (``STAGES``)."""
    pool = r.get("pool_at") or r.get("region_approved_at") or r.get("branch_confirmed_at") or r.get("registered_at")
    first = r.get("first_assigned_at") or r.get("assigned_at")
    stamps = {**r, "pool_at": pool, "first_assigned_at": first}
    return {key: _minutes(stamps.get(a), stamps.get(b)) for key, _, a, b in STAGES}


def stage_targets(cfg):
    """{stage key: target minutes}: the stage targets of the settings, and the SLA target for "sla"."""
    from .sla_records import DEFAULT_STAGE_TARGETS
    out = {**DEFAULT_STAGE_TARGETS, **((cfg or {}).get("stage_targets") or {})}
    out["sla"] = int((cfg or {}).get("sla_minutes") or 180)
    return {k: int(v) for k, v in out.items()}


def rag(minutes, target):
    """"ok" within the target, "warn" up to one and a half times it, "bad" beyond; "" when unknown."""
    if minutes is None or not target:
        return ""
    return "ok" if minutes <= target else "warn" if minutes <= 1.5 * target else "bad"


# --------------------------------------------------------------------------------------------- the inquiry grid
def grid_rows(headers, rows):
    """The inquiry grid -> [{date, form, ..., status, key}] (``key`` from the columns that never change). «تاريخ الطلب»
    has no time, so the same employee, system and request type can be listed twice in a day - two requests, each with
    its own «رقم الطلب». The second and later rows with the same fields get an occurrence number in their key (in grid
    order), so no request is ever folded into another; the first keeps the key it always had."""
    col = {k: find_col(headers, v) for k, v in GRID_COLUMNS.items()}
    # «رقم الموظف» must not be taken by «رقم الموظف TP»: find_col prefers the exact header, so it is not
    out, seen = [], {}
    for r in rows or []:
        item = {k: (_clean(r[i]) if i is not None and i < len(r) else "") for k, i in col.items()}
        if not any(item[k] for k in ("date", "employee_no", "system", "status")):
            continue                                   # a footer / "no data" line
        base = grid_key(item)
        seen[base] = seen.get(base, 0) + 1
        item["key"] = base if seen[base] == 1 else grid_key(item, seen[base])
        out.append(item)
    return out


def grid_key(item, occurrence=1):
    raw = "|".join(_norm(item.get(k)) for k in GRID_KEY_FIELDS)
    if occurrence > 1:
        raw += f"|#{occurrence}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


MAX_TRIES = 2                       # a row whose details failed this many times is left until its status moves


def needs_details(item, index):
    """Open this row's details? Yes when the row is new, its status changed since the last read (the history only
    grows when the status moves), or it moved to another security user (a re-assignment keeps the status but starts a
    new assignment). ``index`` = {grid key: {"id", "status", "holder", "day", "fail"}} kept with the recording. A
    row that could not be read ``MAX_TRIES`` times in the same status waits until its status moves (so one broken row
    never costs every scan a full wait)."""
    known = (index or {}).get(item.get("key"))
    fail = (known or {}).get("fail") or {}
    if fail and int(fail.get("n") or 0) >= MAX_TRIES and _norm(fail.get("status")) == _norm(item.get("status")):
        return False
    if not known or not known.get("id"):
        return True
    if _norm(known.get("status")) != _norm(item.get("status")):
        return True
    # a re-assignment keeps the status but hands the request to another security user: its assignment time is new.
    # An entry read before holders were kept is read once more while the request is with a security user.
    if "holder" in known:
        return _norm(known.get("holder")) != _norm(item.get("executor"))
    return grid_stage(item.get("status"), None) in ("open", "awaiting")


GRID_STAGES = ("closed", "awaiting", "open", "pre", "rejected")


def grid_stage(status, cfg=None):
    """Where a request stands from the grid's CURRENT status alone: closed (confirmed by the reviewer), rejected,
    awaiting (executed / not executed, waiting for the reviewer), open (with a security user: assigned, on hold, in
    progress) or pre (still with the branch, or sent back to it to be corrected). An unknown label counts as open: the
    request is somewhere in the security team."""
    if cfg is None:
        from .sla_records import default_config
        cfg = default_config()
    step = step_of(status, cfg)
    if step in ("confirmed_executed", "confirmed_not"):
        return "closed"
    if step == "rejected":
        return "rejected"
    if step in ("executed", "not_executed"):
        return "awaiting"
    if step in ("registered", "branch_confirmed", "region_approved", "returned"):
        return "pre"
    return "open"


def detail_priority(status, cfg):
    """Which rows' details are read first when a scan cannot read them all: a request still with a security user (its
    assignment line is the clock the Live tab and My tickets count from) before one awaiting the reviewer, before a
    CLOSED one (its history keeps every stamp until it is read), before a rejected one (only its outcome is new).
    None = never opened (still with the branch: no SLA step yet)."""
    return {"open": 0, "awaiting": 1, "closed": 2, "rejected": 3}.get(grid_stage(status, cfg))


def grid_state(grid, previous, now, cfg):
    """The latest inquiry grid as {key: {status, stage, system, holder, checker, employee, request_date, since}}:
    what is open / awaiting the reviewer RIGHT NOW (complete even before a row's details are read). ``since`` = when
    the row was first seen in its current status (kept across scans)."""
    out = {}
    for g in grid or []:
        prev = (previous or {}).get(g["key"]) or {}
        same = _norm(prev.get("status")) == _norm(g.get("status"))
        day = parse_time(g.get("date"))
        out[g["key"]] = {"status": _clean(g.get("status")), "stage": grid_stage(g.get("status"), cfg),
                         "system": _clean(g.get("system")), "holder": _clean(g.get("executor")),
                         "checker": _clean(g.get("checker")), "employee": _clean(g.get("employee")),
                         "request_type": _clean(g.get("request_type")), "user_no": _clean(g.get("user_no")),
                         "request_date": day.strftime(_FMT) if day else "",
                         "since": prev.get("since") if same and prev.get("since") else now}
    return out


# ------------------------------------------------------------------------------------------- into the recording
_NAME_OF_STEP = (("assigner", "assigned_at"), ("executor", "executed_at"), ("checker", "confirmed_at"))


def fold_history(records, index, items, cfg, now, system_team=None):
    """Fold one live inquiry read into the recording (``records`` keyed by the request number, kept as history).
    ``items`` = [{"grid": a grid row, "id": the request number read from its details ("" when not opened), "history":
    its events (None when not opened)}]. A row whose details were opened gets the exact stamps (and the employee
    numbers) of its lifecycle and its index entry takes the new status. A row that was NOT opened keeps its old index
    status - so a row that moved and could not be read this scan is read at the next one - and only refreshes the grid
    fields of a record already in ``records``. ``index`` = {grid key: {id, status, day}} (``day`` = the confirmation
    day of a closed request: where its record is kept). Returns {"added", "updated", "seen"}."""
    now = _clean(now) or datetime.now().strftime(_FMT)
    tmap = {str(k).strip().lower(): v for k, v in (system_team or {}).items()}
    stats = {"added": 0, "updated": 0, "seen": 0}
    for it in items or []:
        g = it.get("grid") or {}
        key = g.get("key") or grid_key(g)
        events = it.get("history")
        known = (index or {}).get(key) or {}
        if it.get("failed"):                           # remember the failure (tried again only a few times)
            fail = known.get("fail") or {}
            same = _norm(fail.get("status")) == _norm(g.get("status"))
            index[key] = known = {"id": known.get("id", ""), "status": known.get("status", ""),
                                  "day": known.get("day", ""), "fail": {"status": _clean(g.get("status")),
                                                                       "n": (int(fail.get("n") or 0) if same else 0) + 1,
                                                                       "at": now[:10]}}
        rid = _clean(it.get("id")) or _clean(known.get("id"))
        if not rid:
            continue                                   # never opened and not known: nothing to key it on
        rec = records.get(rid)
        if rec is None:
            if events is None:
                continue                               # known, not opened, not loaded (closed, in its day file)
            rec = records[rid] = {"id": rid, "first_seen": now}
            stats["added"] += 1
        system = _clean(g.get("system")) or rec.get("system") or ""
        day = parse_time(g.get("date"))
        rec.update(source=SOURCE, last_seen=now, grid_key=key, system=system,
                   team=tmap.get(system.lower(), rec.get("team") or ""),
                   grid_status=_clean(g.get("status")) or rec.get("grid_status", ""),
                   holder=_clean(g.get("executor")) or rec.get("holder", ""),        # the security user it is with
                   requester=_clean(g.get("employee")) or rec.get("requester", ""),
                   requester_no=_clean(g.get("employee_no")) or rec.get("requester_no", ""),
                   user_no=_clean(g.get("user_no")) or rec.get("user_no", ""),     # «رقم المستخدم»: links the live ticket
                   branch=_clean(g.get("branch")) or rec.get("branch", ""),
                   request_type=_clean(g.get("request_type")) or rec.get("request_type", ""),
                   request_date=day.strftime("%Y-%m-%d") if day else rec.get("request_date", ""))
        if events is not None:
            lc = lifecycle(events, cfg)
            for name, stamp in _NAME_OF_STEP:          # the history names who did each step; the grid fills a gap
                if lc[stamp] and not lc[name]:
                    lc[name] = _clean(g.get(name))
            rec.update(lc)
            stats["updated"] += 1
            index[key] = {"id": rid, "status": _clean(g.get("status")), "holder": _clean(g.get("executor")),
                          "day": closing_day(rec) if is_finished(rec) else ""}
        else:
            stats["seen"] += 1
            index[key] = {"id": rid, "status": known.get("status", ""), "day": known.get("day", ""),
                          **({"holder": known["holder"]} if "holder" in known else {}),
                          **({"fail": known["fail"]} if known.get("fail") else {})}
    return stats


# ------------------------------------------------------------------------------------------------ right now
def right_now(grid, open_records, index, cfg, now, keep=None):
    """What is open / awaiting the reviewer RIGHT NOW, from the latest inquiry grid (complete: every listed request,
    read or not). The time a request has been waiting comes from its exact history when its details were read for its
    CURRENT stage; otherwise it is None ("not read yet") - never a guess. ``keep(row)`` narrows the rows (team, person,
    exclusions)."""
    sla = int((cfg or {}).get("sla_minutes") or 180)
    open_now, awaiting_now, pre = [], [], 0
    for key, g in (grid or {}).items():
        if keep and not keep(g):
            continue
        stage = g.get("stage")
        entry = (index or {}).get(key) or {}
        rid = entry.get("id") or ""
        rec = (open_records or {}).get(rid) or {}
        # the record was read at this very stage, and with the security user who holds it now (a re-assignment keeps
        # the status: its clock is not read yet - never shown as the previous holder's time)
        same = (bool(rec) and stage_of(rec) == stage
                and ("holder" not in entry or _norm(entry.get("holder")) == _norm(g.get("holder"))))
        if stage == "pre":
            pre += 1
        elif stage == "open":
            m = _minutes(rec.get("assigned_at"), now) if same else None
            open_now.append({"id": rid, "holder": g.get("holder") or "", "system": g.get("system") or "",
                             "requester": g.get("employee") or "", "assigned_at": rec.get("assigned_at", "") if same else "",
                             "minutes": round(m, 1) if m is not None else None, "on_hold": bool(same and rec.get("on_hold")),
                             "breached": m is not None and m > sla, "status": g.get("status") or ""})
        elif stage == "awaiting":
            m = _minutes(rec.get("executed_at"), now) if same else None
            awaiting_now.append({"id": rid, "executor": (rec.get("executor") if same else "") or g.get("holder") or "",
                                 "system": g.get("system") or "", "executed_at": rec.get("executed_at", "") if same else "",
                                 "minutes": round(m, 1) if m is not None else None,
                                 "outcome": rec.get("exec_outcome", "") if same else ""})
    holders = {}
    for o in open_now:
        h = holders.setdefault(o["holder"] or "—", {"holder": o["holder"] or "—", "n": 0, "breached": 0, "on_hold": 0,
                                                     "oldest_minutes": None, "unread": 0})
        h["n"] += 1
        h["breached"] += o["breached"]
        h["on_hold"] += o["on_hold"]
        h["unread"] += o["minutes"] is None
        if o["minutes"] is not None:
            h["oldest_minutes"] = max(h["oldest_minutes"] or 0, o["minutes"])
    ages = [o["minutes"] for o in open_now if o["minutes"] is not None]
    open_now.sort(key=lambda x: -(x["minutes"] if x["minutes"] is not None else -1))
    awaiting_now.sort(key=lambda x: -(x["minutes"] if x["minutes"] is not None else -1))
    return {"now": {"open": len(open_now), "awaiting": len(awaiting_now), "pre": pre,
                    "breached": sum(1 for o in open_now if o["breached"]),
                    "on_hold": sum(1 for o in open_now if o["on_hold"]),
                    "unread": sum(1 for o in open_now if o["minutes"] is None),
                    "oldest_minutes": round(max(ages), 1) if ages else None,
                    "by_holder": sorted(holders.values(), key=lambda x: (-x["breached"], -(x["oldest_minutes"] or 0), -x["n"]))},
            "open_now": open_now[:300], "awaiting_now": awaiting_now[:300]}


# ------------------------------------------------------------------- live tickets: the age from the last assignment
LIVE_FIELDS = {"user_no": "رقم المستخدم", "system": "النظام", "request_type": "نوع الطلب"}


def _live_field(req, key):
    fields = (req or {}).get("fields") or {}
    want = _norm(LIVE_FIELDS[key])
    for name, value in fields.items():
        if _norm(name) == want:
            return _norm(value)
    return ""


def match_live_requests(requests, open_records):
    """{live request key: its open history record} - the Re-Assign page only gives «تاريخ الارسال للسرية» (when the
    request was sent to the security team), not when the security user holding it got it. A live request is matched
    to an OPEN record (with a security user, not yet executed) on «رقم المستخدم» + «النظام» + «نوع الطلب»; of several,
    the one held by the same security user wins, then the latest assignment. Each record is used once."""
    pool = {}
    for rid, r in (open_records or {}).items():
        if stage_of(r) != "open" or not r.get("assigned_at"):
            continue
        key = (_norm(r.get("user_no")), _norm(r.get("system")), _norm(r.get("request_type")))
        if key[0]:
            pool.setdefault(key, []).append(r)
    out, used = {}, set()
    # requests whose holder matches a record go first, so a shared key never steals another holder's record
    def holder_matches(req):
        key = tuple(_live_field(req, k) for k in ("user_no", "system", "request_type"))
        return any(_norm(r.get("holder")) == _norm(req.get("security_user")) for r in pool.get(key, []))
    for req in sorted(requests or [], key=lambda q: not holder_matches(q)):
        key = tuple(_live_field(req, k) for k in ("user_no", "system", "request_type"))
        cands = [r for r in pool.get(key, []) if r.get("id") not in used]
        if not key[0] or not cands:
            continue
        cands.sort(key=lambda r: (_norm(r.get("holder")) != _norm(req.get("security_user")), -_stamp_sort(r.get("assigned_at"))))
        best = cands[0]
        used.add(best.get("id"))
        out[req.get("key")] = best
    return out


def _stamp_sort(stamp):
    t = parse_time(stamp)
    return t.timestamp() if t else 0.0


def apply_assignment_ages(requests, open_records, now, warn, breach):
    """Age every live request from its LAST assignment to the security user holding it (``match_live_requests``), not
    from the day it was sent to the team: sets ``assigned_at``, ``age_from`` ("assignment" / "sent"), ``age_minutes`` and
    ``status`` (within / reminder / breached / no_date). Without a match the sent date stays the clock. Returns how many
    were matched."""
    from . import schemas
    matched = match_live_requests(requests, open_records)
    for r in requests or []:
        rec = matched.get(r.get("key"))
        start = parse_time(rec.get("assigned_at")) if rec else None
        if start is not None:
            r["assigned_at"], r["age_from"] = start.strftime(_FMT), "assignment"
        else:
            r["assigned_at"], r["age_from"] = None, "sent"
            start = parse_time(r.get("sent_at")) if r.get("sent_at") else None
        age = None if start is None else max(0, round((now - start).total_seconds() / 60))
        r["age_minutes"] = age
        r["status"] = (schemas.SLA_NO_DATE if age is None else schemas.SLA_BREACHED if age >= breach
                       else schemas.SLA_REMINDER if age >= warn else schemas.SLA_WITHIN)
    return len(matched)


# --------------------------------------------------------------------- compared with the portal's inquiry results
def portal_comparison(grid, index, date_from="", date_to="", cfg=None):
    """What the portal's «الاستفسار عن الطلبات» lists for the period's request dates (the latest grid, Third Party and
    other teams already left out by the caller) next to what the SLA history holds: every inquiry row; with the
    security team (assigned, awaiting the reviewer or confirmed - ``closed`` of them confirmed); still with the branch;
    rejected; status history read; not read yet (a row with an SLA step whose history is not recorded). ``rows`` =
    with_security + with_branch + rejected."""
    dfrom, dto = _day(date_from), _day(date_to)
    out = {"rows": 0, "with_security": 0, "closed": 0, "with_branch": 0, "rejected": 0, "read": 0, "not_read": 0}
    for key, g in (grid or {}).items():
        day = _day(g.get("request_date"))
        if (dfrom or dto) and not day:
            continue
        if (dfrom and day < dfrom) or (dto and day > dto):
            continue
        stage = g.get("stage") or grid_stage(g.get("status"), cfg)
        out["rows"] += 1
        if stage == "pre":
            out["with_branch"] += 1
            continue
        if stage == "rejected":
            out["rejected"] += 1
        else:
            out["with_security"] += 1
            out["closed"] += stage == "closed"
        if ((index or {}).get(key) or {}).get("id"):
            out["read"] += 1
        else:
            out["not_read"] += 1
    return out


# --------------------------------------------------------------------------------------------- filter bar
def _day(stamp):
    return _clean(stamp)[:10]


FILTER_TEXT = ("system", "branch", "request_type")        # exact (spelling-insensitive) matches on the record
OUTCOMES = ("completed", "not_done", "rejected", "in_progress")


def assignment_day(r):
    """The day a request belongs to in the SLA Analysis (the owner's rule, like the bank's own SLA sheet): the day it
    was FIRST assigned to a security user, else its (last) assignment, else its request date / registration."""
    return _day(r.get("first_assigned_at") or r.get("assigned_at") or r.get("request_date")
                or r.get("registered_at") or r.get("first_seen"))


def filter_records(records, filt):
    """The records of one view of the SLA Analysis. The date range is the ASSIGNMENT day: every request - closed,
    rejected, awaiting the reviewer or still open - belongs to the day it was first assigned (``assignment_day``), so
    the period's numbers are the requests that reached the security team in it, however long they took. «Employee»
    matches the security user who executed it or holds it now (مستخدم السرية), «Checker» the reviewer (مراجع السرية),
    «Team» any of its teams; «Application», «Branch», «Request type» and «Outcome» their own field; «Sent back» only
    the requests returned to the branch at least once. Blank values match everything."""
    filt = filt or {}
    dfrom, dto = _day(filt.get("date_from")), _day(filt.get("date_to"))
    execu, check, team = (_norm(filt.get(k)) for k in ("executor", "checker", "team"))
    texts = {k: _norm(filt.get(k)) for k in FILTER_TEXT if _norm(filt.get(k))}
    outcome = _clean(filt.get("outcome"))
    returned = str(filt.get("returned") or "").lower() in ("1", "true", "yes")
    out = {}
    for rid, r in (records or {}).items():
        day = assignment_day(r)
        if (dfrom or dto) and not day:
            continue
        if (dfrom and day < dfrom) or (dto and day > dto):
            continue
        if execu and execu not in (_norm(r.get("executor")), _norm(r.get("holder"))):
            continue
        if check and _norm(r.get("checker")) != check:
            continue
        if team and team not in {_norm(t) for t in (r.get("teams") or [r.get("team")])}:
            continue
        if any(_norm(r.get(k)) != v for k, v in texts.items()):
            continue
        if outcome and outcome_of(r) != outcome:
            continue
        if returned and not r.get("returned"):
            continue
        out[rid] = r
    return out


def filter_options(records):
    """What the filter bar offers (from the records the viewer may see): employees (executors and current holders),
    checkers, teams, applications, branches, request types, and the first / last day with data."""
    execs, checks, teams_, days = set(), set(), set(), []
    texts = {k: set() for k in FILTER_TEXT}
    for r in (records or {}).values():
        for name in (r.get("executor"), r.get("holder")):
            if _clean(name):
                execs.add(_clean(name))
        if _clean(r.get("checker")):
            checks.add(_clean(r.get("checker")))
        for t in r.get("teams") or [r.get("team")]:
            if _clean(t):
                teams_.add(_clean(t))
        for k in FILTER_TEXT:
            if _clean(r.get(k)):
                texts[k].add(_clean(r.get(k)))
        day = _day(r.get("confirmed_at") or r.get("rejected_at") or r.get("assigned_at") or r.get("registered_at"))
        if day:
            days.append(day)
    return {"executors": sorted(execs), "checkers": sorted(checks), "teams": sorted(teams_),
            "systems": sorted(texts["system"]), "branches": sorted(texts["branch"]),
            "request_types": sorted(texts["request_type"]), "outcomes": list(OUTCOMES),
            "date_from": min(days) if days else None, "date_to": max(days) if days else None}


# ------------------------------------------------------------------------------------------------- analysis
def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def percentile(values, p):
    """The ``p``-th percentile (0-100) with linear interpolation between the closest ranks; None for no values."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


class _Agg:
    __slots__ = ("n", "executed", "not_executed", "sla_ok", "sla_n", "_mins")

    def __init__(self):
        self.n = self.executed = self.not_executed = self.sla_ok = self.sla_n = 0
        self._mins = []

    def add(self, outcome, minutes, sla_minutes):
        self.n += 1
        if outcome == "not_executed":
            self.not_executed += 1
        else:
            self.executed += 1
        if minutes is not None:
            self.sla_n += 1
            self._mins.append(minutes)
            if minutes <= sla_minutes:
                self.sla_ok += 1

    def out(self, **extra):
        med = round(sorted(self._mins)[len(self._mins) // 2], 1) if self._mins else None
        avg = round(sum(self._mins) / len(self._mins), 1) if self._mins else None
        return dict(extra, n=self.n, executed=self.executed, not_executed=self.not_executed,
                    executed_pct=_pct(self.executed, self.n), sla_pct=_pct(self.sla_ok, self.sla_n),
                    median_minutes=med, average_minutes=avg, p90_minutes=percentile(self._mins, 90),
                    breached=self.sla_n - self.sla_ok, measured=self.sla_n)


def _stage_row(key, label, values, target):
    vals = [v for v in values if v is not None]
    over = sum(1 for v in vals if v > target)
    return {"key": key, "label": label, "n": len(vals), "target": target,
            "average_minutes": round(sum(vals) / len(vals), 1) if vals else None,
            "p50_minutes": percentile(vals, 50), "p90_minutes": percentile(vals, 90),
            "max_minutes": round(max(vals), 1) if vals else None,
            "breached": over, "within_pct": _pct(len(vals) - over, len(vals)),
            "rag": rag(sum(vals) / len(vals), target) if vals else ""}


REQUEST_LIST_LIMIT = 1500           # the period's requests sent to the page (the CSV export has every one)
ATTENTION_LIMIT = 150


def stage_of(r):
    if r.get("stage"):
        return r["stage"]
    if r.get("confirmed_at"):
        return "closed"
    if r.get("rejected_at"):
        return "rejected"
    if r.get("executed_at"):
        return "awaiting"
    return "open" if r.get("assigned_at") else "pre"


def outcome_of(r):
    """completed / not_done / rejected / in_progress (records read before the outcome was kept get it from stamps)."""
    if r.get("outcome"):
        return r["outcome"]
    if r.get("confirmed_at"):
        return "not_done" if r.get("final_outcome") == "not_executed" else "completed"
    return "rejected" if r.get("rejected_at") else "in_progress"


def _hour_of(stamp):
    t = _clean(stamp)
    return int(t[11:13]) % 24 if len(t) >= 13 and t[11:13].isdigit() else None


def hourly(recs, period=None):
    """Hour by hour, for every team together and for each team: how many requests REACHED the security team in that
    hour (their first assignment), how many were FINISHED in that hour (confirmed by the reviewer, whenever they
    arrived), and how many of those that arrived were finished within the same clock hour - with the percentages
    (finished ÷ arrived: over 100 % means the hour cleared older work too). ``period`` = (first day, last day): only
    arrivals / finishes inside it count (blank = no bound)."""
    dfrom, dto = (_day(p) for p in (period or ("", "")))
    inside = lambda s: bool(s) and (not dfrom or _day(s) >= dfrom) and (not dto or _day(s) <= dto)   # noqa: E731
    blank = lambda: [{"received": 0, "done": 0, "same_hour": 0} for _ in range(24)]                  # noqa: E731
    total, by_team = blank(), {}
    for r in recs:
        first = r.get("first_assigned_at") or r.get("assigned_at")
        confirmed = r.get("confirmed_at")
        team = by_team.setdefault(r.get("team") or "—", blank())
        h = _hour_of(first)
        if h is not None and inside(first):
            for row in (total[h], team[h]):
                row["received"] += 1
                if confirmed and _clean(confirmed)[:13] == _clean(first)[:13]:
                    row["same_hour"] += 1
        h = _hour_of(confirmed)
        if h is not None and inside(confirmed):
            for row in (total[h], team[h]):
                row["done"] += 1

    def rates(rows):
        return [{"hour": i, **x, "done_pct": _pct(x["done"], x["received"]) if x["received"] else None,
                 "same_hour_pct": _pct(x["same_hour"], x["received"]) if x["received"] else None}
                for i, x in enumerate(rows)]
    teams = []
    for name, rows in by_team.items():
        received, done = sum(x["received"] for x in rows), sum(x["done"] for x in rows)
        if received or done:
            teams.append({"team": name, "received": received, "done": done,
                          "same_hour": sum(x["same_hour"] for x in rows), "hours": rates(rows)})
    teams.sort(key=lambda t: (-t["received"], -t["done"], t["team"]))
    return {"hours": rates(total), "teams": teams}


def analyze(records, cfg, now=None, list_limit=REQUEST_LIST_LIMIT, period=None):
    """The SLA Analysis from the live, exact history: where the requests stand (pre / open / awaiting / closed), the
    SECURITY USER's clock (assignment -> execution, by مستخدم السرية), the REVIEWER's clock (execution -> confirmation,
    by مراجع السرية), the full clock (first assignment -> confirmation, judged by the SLA target), time on hold, the
    wait before the security team got it, who assigned, and by team / system / day. A ``now`` (ISO) ages what is still
    open. Only history-sourced records are used: no guess, no upload."""
    sla = int((cfg or {}).get("sla_minutes") or 180)
    targets = stage_targets(cfg)
    now = _clean(now)
    recs = [r for r in (records or {}).values() if r.get("source") == SOURCE]
    buckets = {"pre": 0, "open": 0, "awaiting": 0, "closed": 0, "rejected": 0}
    overall, emp, mgr, wait = _Agg(), _Agg(), _Agg(), _Agg()
    by_exec, by_check, by_team, by_system, by_day = ({} for _ in range(5))
    by_branch, by_type = {}, {}
    by_assigner = {}
    hold_n, hold_min = 0, 0.0
    open_now, awaiting_now = [], []
    first_day = last_day = None
    stage_values = {key: [] for key, *_ in STAGES}
    outcomes = {k: 0 for k in OUTCOMES}
    funnel = {"registered": 0, "branch_confirmed": 0, "assigned": 0, "executed": 0, "confirmed": 0}
    returned_n = reassigned_n = region_n = 0
    attention, listed = [], []
    for r in recs:
        stage = stage_of(r)
        if stage in buckets:
            buckets[stage] += 1
        assigned, executed, confirmed = r.get("assigned_at"), r.get("executed_at"), r.get("confirmed_at")
        first = r.get("first_assigned_at") or assigned
        mins = stage_minutes(r)
        for key, value in mins.items():
            if value is not None and (key != "sla" or confirmed):
                stage_values[key].append(value)
        result = outcome_of(r)
        outcomes[result] = outcomes.get(result, 0) + 1
        for key, field in (("registered", "registered_at"), ("branch_confirmed", "branch_confirmed_at"),
                           ("assigned", "first_assigned_at"), ("executed", "executed_at"), ("confirmed", "confirmed_at")):
            funnel[key] += bool(r.get(field) or (key == "assigned" and assigned))
        returned_n += bool(r.get("returned"))
        reassigned_n += bool(r.get("reassigned"))
        region_n += bool(r.get("region_approved_at"))
        if r.get("assigner"):
            by_assigner[r["assigner"]] = by_assigner.get(r["assigner"], 0) + 1
        if (r.get("held_minutes") or 0) > 0:
            hold_n += 1
            hold_min += float(r["held_minutes"])
        start = r.get("branch_confirmed_at") or r.get("registered_at")
        if start and first:
            wait.add("executed", _minutes(start, first), sla)
        if executed:
            m = _minutes(assigned, executed)
            emp.add(r.get("exec_outcome"), m, sla)
            by_exec.setdefault(r.get("executor") or "—", _Agg()).add(r.get("exec_outcome"), m, sla)
        if confirmed:
            outcome = r.get("final_outcome")
            mgr_m = _minutes(executed, confirmed) if executed else None
            mgr.add(outcome, mgr_m, sla)
            by_check.setdefault(r.get("checker") or "—", _Agg()).add(outcome, mgr_m, sla)
            full = _minutes(first, confirmed)
            overall.add(outcome, full, sla)
            by_team.setdefault(r.get("team") or "—", _Agg()).add(outcome, full, sla)
            by_system.setdefault(r.get("system") or "—", _Agg()).add(outcome, full, sla)
            by_branch.setdefault(r.get("branch") or "—", _Agg()).add(outcome, full, sla)
            by_type.setdefault(r.get("request_type") or "—", _Agg()).add(outcome, full, sla)
            day = assignment_day(r)                     # the day it reached the security team
            by_day.setdefault(day, _Agg()).add(outcome, full, sla)
            first_day = day if first_day is None or day < first_day else first_day
            last_day = day if last_day is None or day > last_day else last_day
        # the request's line in the period's list, and what needs a look (worst first)
        live = None
        if now and stage == "open":
            live = _minutes(assigned, now)
        elif now and stage == "awaiting":
            live = _minutes(executed, now)
        worst = max(((k, v / targets[k]) for k, v in mins.items() if v is not None and k in JOURNEY and targets.get(k)),
                    key=lambda kv: kv[1], default=(None, 0))
        line = {"id": r.get("id"), "stage": stage, "outcome": result, "request_date": r.get("request_date", ""),
                "branch": r.get("branch", ""), "requester": r.get("requester", ""), "requester_no": r.get("requester_no", ""),
                "system": r.get("system", ""), "team": r.get("team", ""), "request_type": r.get("request_type", ""),
                "assigner": r.get("assigner", ""), "executor": r.get("executor") or r.get("holder") or "",
                "checker": r.get("checker", ""), "returned": int(r.get("returned") or 0),
                "reassigned": int(r.get("reassigned") or 0), "held_minutes": r.get("held_minutes") or 0,
                "region": bool(r.get("region_approved_at")), "minutes": {k: (round(v, 1) if v is not None else None)
                                                                       for k, v in mins.items()},
                "waiting_minutes": round(live, 1) if live is not None else None,
                "finished_at": closing_day(r) if stage in ("closed", "rejected") else "",
                "worst_stage": worst[0], "sla_breached": bool((mins["sla"] or 0) > sla)}
        listed.append(line)
        reasons = []
        if line["sla_breached"]:
            reasons.append(("SLA breached", 3))
        if live is not None and stage == "open" and live > sla:
            reasons.append(("Open past the SLA", 3))
        if result == "rejected":
            reasons.append(("Rejected", 2))
        if line["returned"]:
            reasons.append(("Sent back to the branch", 2))
        if result == "not_done":
            reasons.append(("Not executed", 1))
        if r.get("on_hold"):
            reasons.append(("On hold", 1))
        if reasons:
            attention.append({**line, "reasons": [x for x, _ in reasons], "severity": max(s for _, s in reasons)})
        if now and stage == "open":
            m = _minutes(assigned, now)
            open_now.append({"id": r.get("id"), "holder": r.get("holder") or "", "system": r.get("system") or "",
                             "team": r.get("team") or "", "requester": r.get("requester") or "",
                             "assigned_at": assigned, "minutes": round(m, 1) if m is not None else None,
                             "on_hold": bool(r.get("on_hold")), "breached": m is not None and m > sla})
        if now and stage == "awaiting":
            m = _minutes(executed, now)
            awaiting_now.append({"id": r.get("id"), "executor": r.get("executor") or "", "system": r.get("system") or "",
                                 "team": r.get("team") or "", "executed_at": executed,
                                 "minutes": round(m, 1) if m is not None else None})

    numbers = {}                                         # a person's name -> their employee number (status history)
    for r in recs:
        for name, no in ((r.get("executor"), r.get("executor_no")), (r.get("checker"), r.get("checker_no")),
                         (r.get("assigner"), r.get("assigner_no"))):
            if name and no:
                numbers.setdefault(name, no)

    def top(groups, key, n=25):
        out = [g.out(**{key: name, "employee_no": numbers.get(name, "")}) for name, g in groups.items()]
        out.sort(key=lambda x: -x["n"])
        return out[:n] if n else out

    open_now.sort(key=lambda x: -(x["minutes"] or 0))
    awaiting_now.sort(key=lambda x: -(x["minutes"] or 0))
    holders = {}
    for o in open_now:                                   # who holds the open requests right now
        h = holders.setdefault(o["holder"] or "—", {"holder": o["holder"] or "—", "n": 0, "breached": 0,
                                                     "on_hold": 0, "oldest_minutes": None})
        h["n"] += 1
        h["breached"] += o["breached"]
        h["on_hold"] += o["on_hold"]
        if o["minutes"] is not None:
            h["oldest_minutes"] = max(h["oldest_minutes"] or 0, o["minutes"])
    hours = [{"hour": h, "assigned": 0, "executed": 0, "confirmed": 0} for h in range(24)]
    for r in recs:                                       # when in the day the work happens
        for field, key in (("first_assigned_at", "assigned"), ("executed_at", "executed"), ("confirmed_at", "confirmed")):
            t = _clean(r.get(field))
            if len(t) >= 13 and t[11:13].isdigit():
                hours[int(t[11:13]) % 24][key] += 1
    opened_ages = [o["minutes"] for o in open_now if o["minutes"] is not None]
    stage_rows = [_stage_row(key, label, stage_values[key], targets.get(key) or sla) for key, label, *_ in STAGES]
    journey = [s for s in stage_rows if s["key"] in JOURNEY and s["average_minutes"]]
    total_avg = sum(s["average_minutes"] for s in journey)
    waterfall = [{"key": s["key"], "label": s["label"], "average_minutes": s["average_minutes"],
                  "share_pct": _pct(s["average_minutes"], total_avg)} for s in journey]
    bottleneck = max(waterfall, key=lambda s: s["average_minutes"], default=None)
    attention.sort(key=lambda a: (-a["severity"], -((a["minutes"].get("sla") or a["waiting_minutes"] or 0))))
    listed.sort(key=lambda x: (x["finished_at"] or "9999", x["request_date"] or ""), reverse=True)
    n = len(recs)
    return {
        "meta": {"requests": n, "sla_minutes": sla, "date_from": first_day, "date_to": last_day,
                 "source": SOURCE, "targets": targets},
        "buckets": buckets,
        "stages": stage_rows,                 # every stage of a request's life against its own target
        "waterfall": waterfall,               # where the time goes, hand-off by hand-off
        "bottleneck": bottleneck,
        "outcomes": {**outcomes, "returned": returned_n, "returned_pct": _pct(returned_n, n),
                     "rejected_pct": _pct(outcomes.get("rejected", 0), n), "reassigned": reassigned_n,
                     "exception": region_n},
        "funnel": [{"key": k, "n": v, "pct": _pct(v, funnel["registered"] or n)} for k, v in funnel.items()],
        "attention": attention[:ATTENTION_LIMIT], "attention_total": len(attention),
        "requests_list": listed[:list_limit] if list_limit else listed, "requests_total": len(listed),
        "by_branch": top(by_branch, "branch"),
        "by_request_type": top(by_type, "request_type"),
        "hourly": hourly(recs, period),       # hour by hour: arrived vs finished, every team and each team
        "overall": overall.out(),
        "employee_clock": emp.out(),          # assignment -> execution (مستخدم السرية)
        "manager_clock": mgr.out(),           # execution -> confirmation (مراجع السرية)
        "branch_wait": wait.out(),            # branch confirmation -> first assignment (before the security team)
        "hold": {"requests": hold_n, "minutes": round(hold_min, 1)},
        "by_employee": top(by_exec, "executor"),
        "by_checker": top(by_check, "checker"),
        "by_assigner": sorted(({"assigner": k, "n": v, "employee_no": numbers.get(k, "")} for k, v in by_assigner.items()),
                              key=lambda x: -x["n"]),
        "by_team": top(by_team, "team", 0),
        "by_system": top(by_system, "system"),
        "by_day": [by_day[d].out(date=d) for d in sorted(by_day)],
        "by_hour": hours,
        "open_now": open_now[:200],
        "awaiting_now": awaiting_now[:200],
        "now": {"open": len(open_now), "awaiting": len(awaiting_now), "pre": buckets["pre"],
                "breached": sum(1 for o in open_now if o["breached"]),
                "on_hold": sum(1 for o in open_now if o["on_hold"]),
                "oldest_minutes": round(max(opened_ages), 1) if opened_ages else None,
                "by_holder": sorted(holders.values(), key=lambda x: (-x["breached"], -(x["oldest_minutes"] or 0)))},
    }
