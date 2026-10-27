"""Insights › Analysis › Tickets (api._analytics_ticket_days): counted like the SLA tab and the portal's own
«الاستفسار عن الطلبات» - from the numbers the collector keeps for each «تاريخ الطلب» (``performance.save_day_log``) -
so the card holds the numbers even while few status histories are read; the hours of the day come from the histories
read. A manager sees their own team(s). Made-up data; the test clock is 2026-10-11 09:00."""
from tests.test_ops_api import flex_mgr, make_account, ops  # noqa: F401


def kept(ops, day, rows):  # noqa: F811
    """The day log the collector keeps for ``day``: rows = [(team, came, confirmed)]."""
    log = {"at": f"{day}T23:00:00", "times": {}, "people": [], "by_person": [],
           "totals": {"came": sum(r[1] for r in rows), "confirmed": sum(r[2] for r in rows)},
           "by_team": [{"team": t, "came": c, "confirmed": f, "exec_n": 0} for t, c, f in rows]}
    ops.update_history("sla", day, lambda doc: doc.update(day_log=log))


def test_the_tickets_card_holds_the_days_kept_numbers_with_the_hours_of_the_histories_read(ops, admin, flex_mgr):  # noqa: F811
    kept(ops, "2026-10-09", [("Flex Cube", 40, 30), ("Card Management", 12, 10)])
    kept(ops, "2026-10-10", [("Flex Cube", 25, 5), ("MFA Team", 4, 4)])          # a day kept before the MFA merge
    rec = {"id": "h1", "source": "history", "stage": "closed", "system": "Flexcube", "executor": "مستخدم اول",
           "first_assigned_at": "2026-10-09T10:15:00", "assigned_at": "2026-10-09T10:15:00",
           "executed_at": "2026-10-09T10:40:00", "confirmed_at": "2026-10-09T11:05:00", "final_outcome": "executed",
           "exec_outcome": "executed", "outcome": "completed"}
    ops.update_sla_closed_day("2026-10-09", lambda d: d.update({"h1": rec}))   # the only history read: 1 of 52
    body = admin.get("/api/ops/analytics?from=2026-10-09&to=2026-10-10").get_json()["modules"]["tickets"]
    assert body["total"] == 81 and body["done"]["total"] == 49                  # 40 + 12 + 25 + 4; 30 + 10 + 5 + 4
    assert [d["n"] for d in body["daily"]] == [52, 29]
    assert {t["team"]: t["n"] for t in body["by_team"]} == {"Flex Cube": 65, "Card Management": 12, "Open System": 4}
    assert body["peak_hour"]["hour"] == 10 and body["done"]["peak_hour"]["hour"] == 11   # from the history read
    mine = flex_mgr.get("/api/ops/analytics?from=2026-10-09&to=2026-10-10").get_json()["modules"]["tickets"]
    assert mine["total"] == 65 and mine["done"]["total"] == 35


def test_a_day_without_a_kept_log_counts_the_histories_read_as_before(ops, admin):  # noqa: F811
    rec = {"id": "h2", "source": "history", "stage": "closed", "system": "Flexcube", "executor": "مستخدم اول",
           "first_assigned_at": "2026-10-08T09:00:00", "assigned_at": "2026-10-08T09:00:00",
           "executed_at": "2026-10-08T09:30:00", "confirmed_at": "2026-10-08T10:00:00", "final_outcome": "executed",
           "exec_outcome": "executed", "outcome": "completed"}
    ops.update_sla_closed_day("2026-10-08", lambda d: d.update({"h2": rec}))
    body = admin.get("/api/ops/analytics?from=2026-10-08&to=2026-10-08").get_json()["modules"]["tickets"]
    assert body["total"] == 1 and body["done"]["total"] == 1
