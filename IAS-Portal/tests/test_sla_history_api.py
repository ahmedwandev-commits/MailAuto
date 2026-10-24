"""/api/ops/sla-analysis on the live status history (calllog/ops/sla_history.py): the period is the ASSIGNMENT day
(a request assigned in the period counts there however late it closed), and the block that compares the analysis with
the portal's own «الاستفسار عن الطلبات» results (Third Party never shown). Made-up data; the test clock is
2026-10-11 09:00."""
import pytest

from calllog.ops import sla_history
from tests.test_ops_api import flex_mgr, make_account, ops  # noqa: F401  (fixtures)

H = sla_history.SOURCE


def rec(rid, assigned, confirmed="", system="Flexcube", **extra):
    stage = "closed" if confirmed else "open"
    return {"id": rid, "source": H, "stage": stage, "first_assigned_at": assigned, "assigned_at": assigned,
            "executed_at": confirmed, "confirmed_at": confirmed, "system": system, "executor": "مستخدم اول",
            "holder": "مستخدم اول", "final_outcome": "executed" if confirmed else "",
            "outcome": "completed" if confirmed else "in_progress", "exec_outcome": "executed" if confirmed else "",
            **extra}


def file_closed(ops, *records):  # noqa: F811
    for r in records:
        day = r["confirmed_at"][:10]
        ops.update_sla_closed_day(day, lambda d, r=r: d.update({r["id"]: r}))


def grid_row(status, day, system="Flexcube", holder="مستخدم اول"):
    stage = sla_history.grid_stage(status)
    return {"status": status, "stage": stage, "system": system, "holder": holder, "checker": "", "employee": "موظف",
            "request_type": "اضافة صلاحية", "user_no": "700301", "request_date": f"{day}T00:00:00",
            "since": "2026-10-11T08:00:00"}


@pytest.fixture
def history(ops):  # noqa: F811
    """Three requests assigned on 9 October - one closed the same day, one closed on the 10th, one still open - one
    assigned on the 8th and closed on the 9th, and a Third Party one."""
    file_closed(ops, rec("same_day", "2026-10-09T10:00:00", "2026-10-09T11:00:00"),
                rec("next_day", "2026-10-09T15:00:00", "2026-10-10T09:00:00"),
                rec("earlier", "2026-10-08T15:00:00", "2026-10-09T09:00:00"),
                rec("tp", "2026-10-09T10:00:00", "2026-10-09T12:00:00", system="TP App"))
    grid = {"g1": grid_row("تم التعزيز على التنفيذ", "2026-10-09"), "g2": grid_row("تم التعزيز على التنفيذ", "2026-10-09"),
            "g3": grid_row("اسناد لمستخدم السرية", "2026-10-09"), "g4": grid_row("تسجيل", "2026-10-09"),
            "g5": grid_row("رفض", "2026-10-09"), "g6": grid_row("تم التعزيز على التنفيذ", "2026-10-08"),
            "g7": grid_row("تم التعزيز على التنفيذ", "2026-10-09", system="TP App")}
    index = {"g1": {"id": "same_day"}, "g2": {"id": "next_day"}, "g3": {"id": "open_one"}, "g6": {"id": "earlier"},
             "g7": {"id": "tp"}}
    ops.update_sla_history_state(lambda d: d.update(
        open={"open_one": rec("open_one", "2026-10-09T16:00:00")}, grid=grid, index=index,
        last_history="2026-10-11T08:55:00"))
    return ops


@pytest.fixture
def no_third_party(admin):  # noqa: F811
    assert admin.put("/api/ops/settings", {"sla": {"exclude_applications": ["TP App"]}}).status_code == 200
    return admin


def test_a_request_counts_on_the_day_it_was_assigned_however_late_it_closed(no_third_party, history):
    """9 October: the request closed the same day, the one closed on the 10th (its closing-day file is read too) and
    the one still open - not the one assigned the day before, and never Third Party."""
    body = no_third_party.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()
    assert body["available"] is True
    assert body["meta"]["requests"] == 3
    assert body["buckets"]["closed"] == 2 and body["buckets"]["open"] == 1
    assert [d["date"] for d in body["by_day"]] == ["2026-10-09"]
    ten = no_third_party.get("/api/ops/sla-analysis?from=2026-10-10&to=2026-10-10").get_json()
    assert ten["meta"]["requests"] == 0                        # nothing was ASSIGNED on the 10th


def test_the_analysis_says_what_the_portal_lists_for_the_period(no_third_party, history):
    """«Compared with the portal»: the latest inquiry rows of the period's request dates (Third Party left out) - with
    the security team (and of them closed), still with the branch, rejected, status history read, not read yet."""
    body = no_third_party.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()
    assert body["portal"] == {"rows": 5, "with_security": 3, "closed": 2, "with_branch": 1, "rejected": 1,
                              "read": 3, "not_read": 1, "last_read": "2026-10-11T08:55:00"}


def test_a_manager_compares_only_their_own_team(flex_mgr, history, admin):  # noqa: F811
    """The Flex Cube manager's block counts the Flexcube rows only; the row of another application is left out (the
    administrator, with nothing excluded in the settings, sees it)."""
    mine = flex_mgr.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()["portal"]
    assert {k: v for k, v in mine.items() if k != "last_read"} == {
        "rows": 5, "with_security": 3, "closed": 2, "with_branch": 1, "rejected": 1, "read": 3, "not_read": 1}
    assert admin.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()["portal"]["rows"] == 6
