"""Best performers (calllog/ops/performance.py): one day of each employee's calls answered, e-mails replied and security
requests handled, best first - Insights › Analysis' card and the Best Performance e-mail (never the daily summary), a
manager's own team only. Made-up people and data only."""
from calllog.ops import performance
from calllog.ops.engines import daily_summary as ds
from calllog.storage import Store
from test_daily_summary import DAY, NOW, Mailer, ctx_for, world


def _sla_day(ops):
    """Person 7001 executed one Flexcube request today (the status history shows their number); Person 7002, as the
    assigner, rejected a CardSys one («رفض»); one request is still with the branch (never anyone's work)."""
    ops.update_sla_closed_day(DAY, lambda d: d.update({
        "R1": {"id": "R1", "source": "history", "stage": "closed", "outcome": "completed", "final_outcome": "executed",
               "system": "Flexcube", "request_date": DAY, "first_assigned_at": f"{DAY}T09:00:00", "assigned_at": f"{DAY}T09:00:00",
               "executed_at": f"{DAY}T09:40:00", "exec_outcome": "executed", "confirmed_at": f"{DAY}T10:00:00",
               "executor": "Person 7001", "executor_no": "7001"},
        "R2": {"id": "R2", "source": "history", "stage": "rejected", "outcome": "rejected", "system": "CardSys", "request_date": DAY,
               "rejected_at": f"{DAY}T11:00:00", "rejected_by": "Person 7002", "pool_at": f"{DAY}T10:30:00"}}))
    ops.update_sla_history_state(lambda d: d.update(open={}, last_history=f"{DAY}T13:00:00", index={
        "g1": {"id": "R1", "status": "تم التعزيز على التنفيذ", "day": DAY}, "g2": {"id": "R2", "status": "رفض", "day": DAY}},
        grid={
        "g1": {"status": "تم التعزيز على التنفيذ", "system": "Flexcube", "holder": "Person 7001", "request_date": f"{DAY}T00:00:00"},
        "g2": {"status": "رفض", "system": "CardSys", "holder": "", "assigner": "Person 7002", "request_date": f"{DAY}T00:00:00"},
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
    assert got == [(1, "7001", 2, 1, 1, 8), (2, "7002", 1, 0, 1, 3)]                 # 2x2 + 1x3 + 1 ; 1x2 + 1
    assert rows[0]["executed"] == 1 and rows[1]["rejected"] == 1 and rows[0]["team"] == "Flex Cube"


def test_the_score_counts_a_call_as_two_requests_and_an_e_mail_as_three(tmp_path):
    """The owner's rule: one call answered = 2 security requests, one e-mail replied = 3: three calls (6) beat five
    requests (5), two e-mails (6) beat five requests too, and a request is worth one."""
    ops, data = world(tmp_path)
    assert performance.WEIGHTS == {"calls": 2, "emails": 3, "tickets": 1}
    calls = Store(str(data))
    reason = calls.reasons()[0]["key"]
    calls.add_record({"id": "r9", "date": DAY, "user_id": "7002", "time": f"{DAY}T10:00:00", "reason": reason})
    calls.add_record({"id": "r8", "date": DAY, "user_id": "7002", "time": f"{DAY}T10:05:00", "reason": reason})
    _, hub, users = _hub_and_users(ops, data)
    rows = performance.people_day(ops, Store(str(data)), hub, users, DAY, NOW)
    assert [(r["user_id"], r["calls"], r["emails"], r["tickets"], r["total"]) for r in rows] == [
        ("7001", 2, 1, 0, 7), ("7002", 3, 0, 0, 6)]                                      # 2x2 + 1x3 = 7 ; 3x2 = 6


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


def test_the_daily_summary_never_lists_the_best_performers(tmp_path):
    """The best performers are celebrated once a day by the Best Performance e-mail only: the summary - counts and team
    names only - names nobody, every team's or a team's own."""
    ops, data = world(tmp_path)
    _sla_day(ops)
    ctx, _, _ = _hub_and_users(ops, data)
    for team in (None, "Flex Cube"):
        collected = ds.collect(ctx, NOW, team)
        html = ds.build_html(collected, ctx.settings, "13:00", NOW)
        assert "people" not in collected and "Best performers" not in html and "Person 7001" not in html


def test_every_scan_keeps_the_days_numbers_and_each_scope_reads_its_own(tmp_path):
    """The day's numbers are kept (refreshed at every scan): every team's totals, a manager's own teams summed, one
    employee's own row - and the best performer of each."""
    ops, data = world(tmp_path)
    _sla_day(ops)
    _, hub, users = _hub_and_users(ops, data)
    log = performance.save_day_log(ops, Store(str(data)), hub, users, NOW)
    assert log["totals"]["executed"] == 1 and log["totals"]["rejected"] == 1
    assert (ops.read_history("sla", DAY) or {}).get("day_log", {}).get("at") == "2026-10-11T13:05:00"
    every = performance.read_day_logs(ops, "", "", None)
    assert [(d["day"], d["executed"], d["rejected"]) for d in every] == [(DAY, 1, 1)]
    assert every[0]["best"] == {"name": "Person 7001", "total": 8}
    card = performance.read_day_logs(ops, "", "", ("Card Management",))
    assert (card[0]["executed"], card[0]["rejected"]) == (0, 1) and card[0]["best"]["name"] == "Person 7002"
    one = performance.read_day_logs(ops, "", "", None, person="Person 7001")
    assert (one[0]["executed"], one[0]["exec_n"]) == (1, 1) and one[0]["exec_average_minutes"] == 40.0


def test_a_request_is_its_applications_team_whoever_executed_it_and_third_party_apps_stay_out(tmp_path):
    """The owner's rule: a request is classified by its system «النظام», never by the person - a Flexcube request
    executed by a Third Party person counts for Flex Cube; a Third Party application's request is left out of the
    SLA whoever executed it."""
    ops, data = world(tmp_path)
    from calllog.storage import Store as Calls
    calls = Calls(str(data))
    calls.create_user({"user_id": "7009", "name": "Person 7009", "role": "user", "active": True, "team": "Third Party",
                       "email": "tp@example.test"})
    ops.update_auto_assign(lambda d: d.update(applications=(d.get("applications") or []) + [
        {"name": "TP Bank App", "team": "Third Party"}]))

    def closed(rid, system, executor_no, executor):
        return {"id": rid, "source": "history", "stage": "closed", "outcome": "completed", "final_outcome": "executed",
                "system": system, "first_assigned_at": f"{DAY}T09:00:00", "assigned_at": f"{DAY}T09:00:00",
                "executed_at": f"{DAY}T09:30:00", "exec_outcome": "executed", "confirmed_at": f"{DAY}T09:40:00",
                "executor": executor, "executor_no": executor_no}
    ops.update_sla_closed_day(DAY, lambda d: d.update({"R9": closed("R9", "Flexcube", "7009", "Person 7009"),
                                                       "R10": closed("R10", "TP Bank App", "7001", "Person 7001")}))
    ops.update_sla_history_state(lambda d: d.update(open={}, index={}, grid={}, last_history=f"{DAY}T13:00:00"))
    _, hub, users = _hub_and_users(ops, data)
    sc = performance.sla_scope(ops, hub, users, DAY, DAY, None)
    assert sc["records"]["R9"]["team"] == "Flex Cube" and "R10" not in sc["records"]
    assert "R9" in performance.sla_scope(ops, hub, users, DAY, DAY, ("Flex Cube",))["records"]
