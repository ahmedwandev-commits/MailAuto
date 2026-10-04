import pytest

from cem_bot.config import load_config
from cem_bot.mapping import load_mapping
from cem_bot.models import Action, Ticket, TicketType
from cem_bot.rules import Rules, extract_branch_id, extract_job_code
from cem_bot.textnorm import best_match, compact, norm


@pytest.fixture(scope="module")
def rules():
    cfg = load_config(need_credentials=False)
    mapping = load_mapping(cfg.path(cfg.settings["run"]["mapping_file"]), cfg.rules["categories"])
    return Rules(cfg.rules, mapping)


def T(ttype, user="300101", job="100056380-مصرفي / العمليات بالفروع", branch="حلوان - 15100"):
    return Ticket(user_id=user, user_name="مستخدم تجريبي", ticket_type_raw=ttype,
                  branch_raw=branch, job_title_raw=job, request_date="01/10/2026")


# ---------------------------------------------------------------- text
def test_norm_spelling_variants():
    assert norm("رقم الوظيفى") == norm("رقم الوظيفي")
    assert norm("إعادة تشغيل") == norm("اعادة تشغيل")
    assert norm("ليس لدية كود") == norm("ليس لديه كود")
    assert norm("١٥١٠٠") == "15100"
    assert compact("الفرع / الادارة") == compact("الفرع/الإدارة")


def test_best_match_prefers_exact():
    opts = ["إختر", "لم يتم التنفيذ", "تم التنفيذ"]
    assert best_match(opts, "تم التنفيذ") == "تم التنفيذ"
    assert best_match(opts, "لم يتم التنفيذ") == "لم يتم التنفيذ"
    assert best_match(["Heads", "Branch Superviser", "Branch Manager (Component)"],
                      "branch manager (component)") == "Branch Manager (Component)"


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("raw,expected", [
    ("حلوان - 15100", "151"), ("15100 - حلوان", "151"), ("15100", "151"),
    ("62753 - 62753-الادارة العامة", "627"), ("المعادي-25400", "254"), ("", None), ("بدون رقم", None),
])
def test_branch_id(raw, expected):
    assert extract_branch_id(raw) == expected


def test_job_code():
    assert extract_job_code("100056389-مصرفي / العمليات بالفروع") == "100056389"
    assert extract_job_code("مصرفي") is None


@pytest.mark.parametrize("raw,expected", [
    ("تعديل صلاحية", TicketType.MODIFY),
    ("تعديل صلاحية أكواد سريه بصفه استثنائيه", TicketType.MODIFY),
    ("تعديل صلاحيه اكواد سرية بصفة استثنائية", TicketType.MODIFY),
    ("إعادة تشغيل", TicketType.REACTIVATE), ("اعادة تشغيل ", TicketType.REACTIVATE),
    ("الغاء", TicketType.CANCEL), ("إلغاء", TicketType.CANCEL),
    ("انشاء", TicketType.CREATE), ("إنشاء أكواد سريه بصفه استثنائيه", TicketType.CREATE),
    ("نقل", None),
])
def test_classify(rules, raw, expected):
    assert rules.classify(raw) == expected


# ---------------------------------------------------------------- mapping
def test_mapping_loaded(rules):
    s = rules.mapping.summary()
    assert s == {"Maker": 39, "Checker": 40, "Manager": 7}
    assert rules.mapping.category_for("100056380") == "Maker"
    assert rules.mapping.category_for("100056068") == "Checker"
    assert rules.mapping.category_for("100074090") == "Manager"
    assert rules.mapping.category_for("999") is None
    # 100056192 has "grant monitoring" twice and no "grant supervising" in the sheet
    assert any("100056192" in w for w in rules.mapping.warnings)


# ---------------------------------------------------------------- plans
def test_modify_maker_found(rules):
    p = rules.build_plan(T("تعديل صلاحية"), user_exists=True)
    assert p.action == Action.MODIFY_USER and p.category == "Maker"
    assert p.role is None and p.clear_roles and p.clear_branches
    assert p.grants == ["serving"] and p.branch_id == "151"
    assert p.set_active is True and p.set_locked is False
    assert p.decision == "تم التنفيذ" and p.confirm_matrix


def test_modify_checker_found(rules):
    p = rules.build_plan(T("تعديل صلاحية", job="100056068-رئيس قسم"), True)
    assert p.category == "Checker" and p.role == "Branch Superviser"
    assert p.grants == ["serving", "monitoring", "alerts_receiving"]


def test_modify_manager_found(rules):
    p = rules.build_plan(T("تعديل صلاحية", job="100074090-مدير فرع"), True)
    assert p.category == "Manager" and p.role == "Branch Manager (Component)"
    assert p.grants == ["supervising", "monitoring", "alerts_receiving"]
    assert p.clear_branches is False


def test_modify_not_found_rejects(rules):
    p = rules.build_plan(T("تعديل صلاحية"), False)
    assert p.action == Action.REJECT_NO_CODE
    assert (p.decision, p.reason, p.confirm_matrix) == ("لم يتم التنفيذ", "ليس لدية كود", True)


def test_unmapped_job_skipped(rules):
    p = rules.build_plan(T("تعديل صلاحية", job="100099999-وظيفة غير موجودة"), True)
    assert p.action == Action.SKIP and "100099999" in p.skip_reason


def test_reactivate_and_cancel(rules):
    p = rules.build_plan(T("إعادة تشغيل", job="anything"), True)
    assert p.action == Action.REACTIVATE_USER and (p.set_active, p.set_locked) == (True, False)
    assert p.confirm_matrix is False
    p = rules.build_plan(T("الغاء", job=""), True)
    assert p.action == Action.CANCEL_USER and (p.set_active, p.set_locked) == (False, False)
    assert rules.build_plan(T("الغاء"), False).action == Action.REJECT_NO_CODE


def test_create(rules):
    p = rules.build_plan(T("انشاء"), False)
    assert p.action == Action.CREATE_USER and p.grants == ["serving"] and p.branch_id == "151"
    p = rules.build_plan(T("انشاء"), True)
    assert p.action == Action.REJECT_ALREADY_EXISTS
    assert p.reason == "Other" and p.notes.startswith("لديه كود بالفعل")


def test_override(rules):
    rules.cfg["overrides"] = {"CREATE": {"Checker": {"grants": ["serving", "supervising", "alerts_receiving"]}}}
    try:
        p = rules.build_plan(T("انشاء", job="100056068"), False)
        assert p.grants == ["serving", "supervising", "alerts_receiving"]
        p = rules.build_plan(T("تعديل صلاحية", job="100056068"), True)
        assert p.grants == ["serving", "monitoring", "alerts_receiving"]
    finally:
        rules.cfg["overrides"] = {}


def test_new_user_name(rules):
    t = T("انشاء", user="300110")
    t.user_name = "  مستخدم   تجريبي "
    assert rules.new_user_name(t) == "300110 - مستخدم تجريبي"
