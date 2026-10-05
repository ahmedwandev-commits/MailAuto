"""In-memory state shared by the mock portal and the mock CEM.

All names, ids and data are invented test data.
"""
from __future__ import annotations

import copy
import itertools
import threading
from datetime import datetime

LOCK = threading.RLock()

CEM_SYSTEM = "CEM الاستعلام الآلي الجديد"

ROLES = [
    {"name": "Heads", "modified": "25/12/2023 07:38:56"},
    {"name": "Branch Superviser", "modified": "06/05/2023 13:58:09"},
    {"name": "Branch Manager (Component)", "modified": "23/10/2022 11:40:17"},
    {"name": "Branches Support", "modified": "23/10/2022 11:34:42"},
    {"name": "Main Area Manager", "modified": "23/10/2022 11:26:32"},
    {"name": "Sub Zone Area Manager", "modified": "23/10/2022 11:19:12"},
]

BRANCHES = [
    {"identity": "151", "name": "Helwan"},
    {"identity": "1510", "name": "Helwan Mobile Unit"},   # decoy: contains "151"
    {"identity": "254", "name": "Maadi"},
    {"identity": "167", "name": "Heliopolis"},
    {"identity": "424", "name": "Tanta"},
    {"identity": "521", "name": "Assiut"},
    {"identity": "236", "name": "Nasr City"},
    {"identity": "252", "name": "Zamalek"},
]

GRANTS = ["supervising", "monitoring", "serving", "alerts_receiving", "serving_random"]

EMPLOYEES = ["233786", "233787", "233788"]   # security staff for re-assignment


def _ticket(n, ttype, user_id, name, branch, job, assigned_to=None, system=CEM_SYSTEM):
    return {
        "id": n, "request_date": f"0{1 + n % 9}/10/2026 1{n % 10}:{10 + n:02d}:00",
        "sent_date": f"0{1 + n % 9}/10/2026 1{n % 10}:{20 + n:02d}:00",
        "branch": branch, "form_type": "نموذج (6)", "ticket_type": ttype,
        "user_id": user_id, "user_name": name, "tp_id": "", "system": system,
        "dept": "العمليات / وحدة العمليات", "eiam": "", "job_title": job,
        "period": "-", "assigned_to": assigned_to, "status": "open",
        "decision": None, "reason": None, "notes": None, "matrix_checked": None, "closed_by": None,
    }


def _seed():
    J_MAKER = "100056380-مصرفي / العمليات بالفروع"
    J_CHECK = "100056068-رئيس قسم / خدمة العملاء"
    J_MGR = "100074090-مدير فرع / ادارة الفرع"
    tickets = [
        # --- MODIFY (تعديل صلاحية)
        _ticket(1, "تعديل صلاحية", "300101", "كريم اختبار سامي", "حلوان - 15100", J_MAKER),
        _ticket(2, "تعديل صلاحية أكواد سريه بصفه استثنائيه", "300102", "منى تجربة فؤاد", "المعادي - 25400", J_CHECK),
        _ticket(3, "تعديل صلاحية", "300103", "حسام نموذج علي", "مصر الجديدة - 16700", J_MGR),
        _ticket(4, "تعديل صلاحية", "300104", "ياسر عينة حسن", "حلوان - 15100", J_MAKER),   # not in CEM
        _ticket(5, "تعديل صلاحية", "300105", "نادية مثال رضا", "حلوان - 15100",
                "100099999-وظيفة غير موجودة بالمصفوفة"),                                  # unmapped job
        # --- REACTIVATE (إعادة تشغيل)
        _ticket(6, "إعادة تشغيل", "300106", "عمر تجريبي ماهر", "طنطا - 42400", J_MAKER),
        _ticket(7, "اعادة تشغيل", "300107", "سلمى اختبار نبيل", "طنطا - 42400", J_MAKER),  # not in CEM
        # --- CANCEL (الغاء)
        _ticket(8, "الغاء", "300108", "طارق عينة وليد", "اسيوط - 52100", J_CHECK),
        _ticket(9, "إلغاء", "300109", "ريم مثال شريف", "اسيوط - 52100", J_CHECK),          # not in CEM
        # --- CREATE (انشاء)
        # --- deliberate failure: CEM cannot verify login names starting with 9
        #     -> shows error handling (screenshot, ticket left open, run continues)
        _ticket(16, "انشاء", "900116", "حالة خطأ تجريبية", "حلوان - 15100", J_MAKER),
        _ticket(10, "انشاء", "300110", "هاني جديد صبري", "حلوان - 15100", J_MAKER),
        _ticket(11, "انشاء أكواد سريه بصفه استثنائيه", "300111", "دينا جديدة عادل", "طنطا - 42400", J_CHECK),
        _ticket(12, "انشاء", "300112", "مروان جديد فتحي", "اسيوط - 52100", "100208129-مدير فرع / ادارة"),
        _ticket(13, "انشاء", "300113", "شادي موجود رامي", "حلوان - 15100", J_MAKER),       # already in CEM
        # --- not to be touched
        _ticket(14, "نقل", "300114", "سارة غير معروف", "حلوان - 15100", J_MAKER),           # unknown type
        _ticket(15, "تعديل صلاحية", "300115", "علا نظام اخر", "حلوان - 15100", J_MAKER,
                system="OBDX نظام آخر"),                                                    # other system
    ]
    # half already assigned to the operator, half waiting for re-assignment
    for t in tickets:
        if t["id"] % 2 == 0:
            t["assigned_to"] = "233786"
    next(t for t in tickets if t["id"] == 15)["assigned_to"] = None   # other-system ticket stays unassigned

    def user(pid, login, name, active=True, locked=False, roles=(), branches=None):
        return {"pid": pid, "login": login, "name_en": f"{login} - {name}", "name_ar": f"{login} - {name}",
                "auth_type": "Windows", "department": "Main", "active": active, "locked": locked,
                "roles": list(roles), "branches": branches or [],
                "created_by": "200000 - Mock Admin", "modified_by": "", }

    users = [
        user(185, "233786", "Mock Operator", roles=["Heads"]),
        user(201, "300101", "كريم اختبار سامي", active=False, locked=True, roles=["Heads"],
             branches=[{"link": 9001, "identity": "252", "grants": ["serving", "monitoring"]},
                       {"link": 9002, "identity": "236", "grants": ["supervising"]}]),
        user(202, "300102", "منى تجربة فؤاد", roles=[],
             branches=[{"link": 9003, "identity": "151", "grants": ["serving"]}]),
        user(203, "300103", "حسام نموذج علي", roles=["Branch Superviser"],
             branches=[{"link": 9004, "identity": "151", "grants": ["serving", "monitoring", "alerts_receiving"]}]),
        user(206, "300106", "عمر تجريبي ماهر", active=False, locked=True),
        user(208, "300108", "طارق عينة وليد", active=True, locked=True),
        user(213, "300113", "شادي موجود رامي"),
        user(214, "1300104", "Decoy User Contains 300104"),   # substring decoy for 300104
        user(215, "3001070", "Decoy User Contains 300107"),
    ]
    return {"tickets": tickets, "users": {u["pid"]: u for u in users}, "events": []}


INQUIRY_HEADERS = [
    "تاريخ الطلب", "نوع الطلب", "الفرع / الادارة", "اسم الموظف", "رقم المستخدم",
    "رقم المستخدم TP", "رقم الوظيفى", "تفاصيل الطلب", "النظام", "البرنامج",
    "موظف الاسناد", "مستخدم السرية", "المسمى الوظيفى", "الحالة", "",
]
INQUIRY_PAGE_SIZE = 5   # small on purpose so the mock exercises multi-page paging


def inquiry_rows(total: int = 12) -> list[list[str]]:
    """Deterministic fake inquiry data (dates dd/mm/yyyy, inside 07..09/2026)."""
    types = ["تعديل صلاحية", "انشاء", "الغاء", "إعادة تشغيل"]
    systems = ["CEM", "TMS", "Essentis Issuer"]
    rows = []
    for i in range(total):
        day = 1 + (i % 28)
        month = 7 + (i % 3)
        rows.append([
            f"{day:02d}/{month:02d}/2026",
            types[i % len(types)],
            BRANCHES[i % len(BRANCHES)]["identity"],
            f"موظف اختبار رقم {i + 1}",
            str(500000 + i),
            str(6000000 + i),
            str(700000 + i),
            f"{1000000 + i}-تفاصيل الطلب التجريبي رقم {i + 1}",
            systems[i % len(systems)],
            "INAB" if i % 2 else "",
            "مستخدم الاسناد",
            "مستخدم السرية",
            "مصرفي / العمليات بالفروع",
            "تم الاسناد الى السرية",
            "",
        ])
    return rows


STATE: dict = {}
_ids = itertools.count(5000)


def reset():
    with LOCK:
        STATE.clear()
        STATE.update(copy.deepcopy(_seed()))


def log(source: str, msg: str):
    with LOCK:
        STATE["events"].append({"t": datetime.now().strftime("%H:%M:%S"), "src": source, "msg": msg})


def next_id() -> int:
    return next(_ids)


def branch(identity):
    return next((b for b in BRANCHES if b["identity"] == str(identity)), None)


def snapshot() -> dict:
    with LOCK:
        snap = copy.deepcopy(STATE)
    snap["users"] = {str(k): v for k, v in snap["users"].items()}
    for u in snap["users"].values():
        for b in u["branches"]:
            b["name"] = (branch(b["identity"]) or {}).get("name")
    return snap


reset()
