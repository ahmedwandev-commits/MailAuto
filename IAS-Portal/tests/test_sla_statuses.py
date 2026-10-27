"""The portal's status wordings (calllog/ops/sla_history.step_of / grid_stage): the owner's rule - «تم التعزيز …»
and «لم يتم …» always count, in whatever words the portal writes them - and the wordings no SLA status names listed on
the SLA tab for the administrator. Made-up data; the test clock is 2026-10-11 09:00."""
import pytest

from calllog.ops import sla_history as h
from calllog.ops.sla_records import default_config
from tests.test_ops_api import ops  # noqa: F401
from tests.test_sla_history_api import grid_row

CFG = default_config()


@pytest.mark.parametrize("status, step", [
    ("تم التعزيز على التنفيذ", "confirmed_executed"), ("تم التعزيز", "confirmed_executed"),
    ("تعزيز التنفيذ", "confirmed_executed"), ("تم التعزيز على عدم التنفيذ", "confirmed_not"),
    ("تم التعزيز بعدم التنفيذ", "confirmed_not"), ("تم التعزيز على لم يتم التنفيذ", "confirmed_not"),
    ("لم يتم التنفيذ", "not_executed"), ("لم يتم", "not_executed"), ("لم يتم تنفيذ", "not_executed"),
    ("تم عدم التنفيذ", "not_executed"), ("تم التنفيذ", "executed"), ("تم تنفيذ", "executed"),
    ("بانتظار التعزيز", "executed"),
    # never these: the branch, its region or sector, and the words of a request still in progress
    ("تم التعزيز من قبل مشرف الفرع", "branch_confirmed"), ("تم التعزيز من قبل المنطقة", "region_approved"),
    ("تم التعزيز ال Platinum", "region_approved"), ("تم التعزيز من قبل القطاع المالى", "region_approved"),
    ("تحت التنفيذ", None), ("قيد الإجراء", None), ("تسجيل", "registered"), ("رفض", "rejected"),
])
def test_the_confirmation_and_execution_families_count_in_any_wording(status, step):
    assert h.step_of(status, CFG) == step


def test_a_confirmed_request_with_no_security_user_on_its_row_is_counted_not_left_at_the_branch():
    grid = {f"g{n}": {**grid_row(s, "2026-10-11"), "holder": ""} for n, s in enumerate(
        ["تم التعزيز بعدم التنفيذ", "تم التعزيز على لم يتم التنفيذ", "تم التعزيز", "لم يتم تنفيذ", "تسجيل"])}
    nums = h.status_numbers(grid, {}, CFG, "2026-10-11", "2026-10-11")
    assert (nums["came"], nums["confirmed"], nums["confirmed_not"], nums["confirmed_executed"]) == (4, 3, 2, 1)
    assert (nums["not_executed"], nums["pre"]) == (3, 1)          # 2 confirmed not executed + 1 awaiting; the registration


def test_the_sla_tab_lists_the_statuses_no_setting_names(ops, admin):  # noqa: F811
    grid = {"g1": grid_row("حالة جديدة من البوابة", "2026-10-11", holder=""),
            "g2": grid_row("حالة جديدة من البوابة", "2026-10-11", holder=""),
            "g3": grid_row("تم التعزيز بعدم التنفيذ", "2026-10-11")}
    ops.update_sla_history_state(lambda d: d.update(grid=grid, index={}, open={}, last_history="2026-10-11T08:55:00"))
    body = admin.get("/api/ops/sla-analysis?from=2026-10-11&to=2026-10-11").get_json()
    assert body["unknown_statuses"] == [{"status": "حالة جديدة من البوابة", "requests": 2}]
    assert (body["glance"]["came"], body["glance"]["confirmed_not"], body["glance"]["pre"]) == (1, 1, 2)
