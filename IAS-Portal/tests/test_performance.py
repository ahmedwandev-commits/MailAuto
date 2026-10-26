"""Best performers (calllog/ops/performance.py): one day of each employee's calls answered, e-mails replied and security
requests handled, best first - the daily summary's section at each checkpoint, Insights › Analysis' card, a manager's
own team only. Made-up people and data only."""
from calllog.ops import performance
from calllog.ops.engines import daily_summary as ds
from calllog.storage import Store
from test_daily_summary import DAY, NOW, Mailer, ctx_for, world


def _sla_day(ops):
    """Person 7001 executed one Flexcube request today (the status history shows their number); Person 7002, as the
    assigner, rejected a CardSys one («رفض»); one request is still with the branch (never anyone's work)."""
    ops.update_sla_closed_day(DAY, lambda d: d.update({
        "R1": {"id": "R1", "source": "history", "stage": "closed", "outcome": "completed", "final_outcome": "executed",
               "system": "Flexcube", "first_assigned_at": f"{DAY}T09:00:00", "assigned_at": f"{DAY}T09:00:00",
               "executed_at": f"{DAY}T09:40:00", "exec_outcome": "executed", "confirmed_at": f"{DAY}T10:00:00",
               "executor": "Person 7001", "executor_no": "7001"},
        "R2": {"id": "R2", "source": "history", "stage": "rejected", "outcome": "rejected", "system": "CardSys",
               "rejected_at": f"{DAY}T11:00:00", "rejected_by": "Person 7002", "pool_at": f"{DAY}T10:30:00"}}))
    ops.update_sla_history_state(lambda d: d.update(open={}, index={}, last_history=f"{DAY}T13:00:00", grid={
        "g9": {"status": "تسجيل", "stage": "pre", "system": "Flexcube", "holder": "", "request_date": f"{DAY}T00:00:00"}}))


def _hub_and_users(ops, data):
    ctx = ctx_for(ops, data, {"to": ["boss@example.test"]}, Mailer())
    return ctx, ctx.hub, Store(str(data)).users()


def test_each_employee_counts_calls_emails_and_tickets_best_first(tmp_path):
    ops, data = world(tmp_path)
    _sla_day(ops)
    _, hub, users = _hub_and_users(ops, data)
    rows = performance.people_day(ops, Store(str(data)), hub, users, DAY, NOW)
    got = [(r["rank"], r["user_id"], r["calls"], r["emails"], r["tickets"], r["total"]) for r in rows]
    assert got == [(1, "7001", 2, 1, 1, 4), (2, "7002", 1, 0, 1, 2)]
    assert rows[0]["executed"] == 1 and rows[1]["rejected"] == 1 and rows[0]["team"] == "Flex Cube"


def test_a_manager_sees_only_their_own_teams_people(tmp_path):
    ops, data = world(tmp_path)
    _sla_day(ops)
    _, hub, users = _hub_and_users(ops, data)
    rows = performance.people_day(ops, Store(str(data)), hub, users, DAY, NOW, ("Card Management",))
    assert [r["user_id"] for r in rows] == ["7002"] and rows[0]["rank"] == 1


def test_an_earlier_days_emails_come_from_the_replies_kept_that_day(tmp_path):
    ops, data = world(tmp_path)
    _, hub, users = _hub_and_users(ops, data)
    ops.update_history("email", "2026-10-10", lambda d: d.update(replies={"f1@example.test": 5}))
    rows = performance.people_day(ops, Store(str(data)), hub, users, "2026-10-10", NOW)
    assert [(r["user_id"], r["emails"]) for r in rows] == [("7001", 5)]
    none = performance.people_day(ops, Store(str(data)), hub, users, "2026-10-09", NOW)
    assert none == []                                           # nothing that day: nobody listed


def test_the_daily_summary_names_the_best_performers_at_its_checkpoint(tmp_path):
    ops, data = world(tmp_path)
    _sla_day(ops)
    ctx, _, _ = _hub_and_users(ops, data)
    collected = ds.collect(ctx, NOW)
    assert [r["user_id"] for r in collected["people"]["rows"]] == ["7001", "7002"]
    html = ds.build_html(collected, ctx.settings, "13:00", NOW)
    assert "Best performers - 13:00 checkpoint" in html and "Person 7001" in html
    flex = ds.collect(ctx, NOW, "Flex Cube")
    assert [r["user_id"] for r in flex["people"]["rows"]] == ["7001"]
