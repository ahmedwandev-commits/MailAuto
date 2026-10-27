"""Third Party (Settings › Ticketing › Left out of the SLA): never in the SLA - no breach, no reminder, no age, no
time - but how many of its requests there are is shown, by the statuses agreed: the SLA tab, the Live tab and the daily
summary. A manager sees them only for their own team. Made-up data; the test clock is 2026-10-11 09:00."""
from datetime import datetime

import pytest

from calllog.ops import performance, settings as hub_settings, sla_history, sla_records, teams
from calllog.ops.engines import daily_summary
from tests.test_ops_api import flex_mgr, make_account, ops, put_snapshot, sla_data, sla_request  # noqa: F401
from tests.test_sla_history_api import grid_row, history  # noqa: F401

CFG = sla_records.default_config()
TP_APPS = [{"name": "TP App", "team": "Third Party"}, {"name": "Flexcube", "team": "Flex Cube"}]
LIVE_LEFT = {"open": 2, "by_team": {"Third Party": 2}, "teams": ["Third Party"]}


def hub(**sla):
    out, errors = hub_settings.validate({"sla": sla} if sla else {})
    assert errors == [], errors
    return out


def tp(status, day="2026-10-09"):
    return {**grid_row(status, day, system="TP App"), "team": "Third Party"}


def test_counts_only_keeps_how_many_and_nothing_of_the_sla():
    grid = {"a": tp("تم التعزيز على التنفيذ"), "b": tp("رفض"), "c": tp("اسناد لمستخدم السرية"), "d": tp("تسجيل"),
            "e": tp("تم التنفيذ"), "f": tp("تم التعزيز على التنفيذ", "2026-10-08")}
    out = sla_history.counts_only(sla_history.status_numbers(grid, {}, CFG, "2026-10-09", "2026-10-09"),
                                  {"Third Party": 2})
    assert (out["came"], out["executed"], out["not_executed"], out["rejected"], out["open"]) == (4, 2, 0, 1, 1)
    assert (out["waiting"], out["confirmed"], out["now"], out["pre"]) == (1, 1, 2, 1)
    assert set(out) == set(sla_history.LEFT_OUT_KEYS) | {"pre", "by_team"}           # no time, hour or person
    assert [(r["team"], r["came"], r["now"]) for r in out["by_team"]] == [("Third Party", 4, 2)]


# ---------------------------------------------------------------- the Live tab
def test_the_live_tab_keeps_only_how_many_third_party_tickets_and_a_manager_only_their_own():
    resolver = teams.Resolver(hub(), {}, TP_APPS)
    data = {**sla_data([sla_request("f-1", "مستخدم فلكس", "within", 10)]), "left_out": LIVE_LEFT}
    assert "left_out" not in teams.scope_sla(data, ("Flex Cube",), resolver)
    mine = teams.scope_sla(data, ("Third Party",), resolver)
    assert mine["left_out"] == LIVE_LEFT and mine["requests"] == []
    assert teams.scope_sla(data, None, resolver)["left_out"] == LIVE_LEFT


# ---------------------------------------------------------------- the SLA tab
@pytest.fixture
def third_party(ops, history):  # noqa: F811
    """The history of test_sla_history_api (its «TP App» request) with TP App a Third Party application, and two Third
    Party tickets on the Re-Assign page."""
    ops.update_auto_assign(lambda d: d.update(applications=TP_APPS))
    put_snapshot(ops, "sla", {"requests": [], "totals": {}, "by_user": [], "thresholds": {}, "left_out": LIVE_LEFT})
    return ops


def test_the_sla_tab_counts_third_party_by_status_apart_and_never_in_the_sla(admin, third_party):
    body = admin.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()
    left = body["left_out_numbers"]
    assert (left["came"], left["executed"], left["confirmed"], left["open"], left["now"]) == (1, 1, 1, 0, 2)
    assert left["teams"] == ["Third Party"] and [r["team"] for r in left["by_team"]] == ["Third Party"]
    assert not {"times", "hours", "by_person", "by_status"} & set(left)
    assert body["glance"]["came"] == 4                                              # the security team's own four
    assert "Third Party" not in [t["team"] for t in body["glance"]["by_team"]]
    other = admin.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09&team=Flex Cube").get_json()
    assert other["left_out_numbers"]["came"] == 0 and other["left_out_numbers"]["now"] == 0


def test_a_manager_sees_third_party_counts_only_when_it_is_their_team(third_party, flex_mgr, make_account):  # noqa: F811
    assert "left_out_numbers" not in flex_mgr.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()
    tp_mgr = make_account("tpm", "manager", "Tamer Third", email="tpm@example.test", team="Third Party")
    left = tp_mgr.get("/api/ops/sla-analysis?from=2026-10-09&to=2026-10-09").get_json()["left_out_numbers"]
    assert (left["came"], left["now"], left["teams"]) == (1, 2, ["Third Party"])


def test_days_before_the_inquiry_window_add_the_third_party_counts_kept_those_days(admin, third_party):
    third_party.update_sla_history_state(lambda d: d.update(grid_from="2026-10-09"))
    row = {"team": "Third Party", **{k: 0 for k in sla_history.LEFT_OUT_KEYS}, "came": 5, "rejected": 2, "open": 3}
    third_party.update_history("sla", "2026-10-05", lambda doc: doc.update(day_log={
        "at": "2026-10-05T23:00:00", "totals": {"came": 0}, "times": {}, "people": [], "by_person": [], "by_team": [],
        "left_out": {"by_team": [row]}}))
    left = admin.get("/api/ops/sla-analysis?from=2026-10-05&to=2026-10-09").get_json()["left_out_numbers"]
    assert (left["came"], left["rejected"], left["open"], left["executed"]) == (6, 2, 3, 1)


# ---------------------------------------------------------------- the day log and the daily summary
def test_the_day_log_keeps_the_third_party_counts(third_party):
    from calllog.storage import Store
    now = datetime(2026, 10, 11, 9, 0)
    third_party.update_sla_history_state(lambda d: d.update(grid_from="2026-10-09"))
    performance.save_day_log(third_party, Store(third_party.root.parent), hub(), {}, now)
    nine = (third_party.read_history("sla", "2026-10-09") or {})["day_log"]["left_out"]
    today = (third_party.read_history("sla", "2026-10-11") or {})["day_log"]["left_out"]
    assert (nine["came"], nine["confirmed"], nine["now"]) == (1, 1, 0) and today["now"] == 2


def test_the_daily_summary_has_a_third_party_card_with_counts_only(third_party):
    now = datetime(2026, 10, 11, 9, 0)
    third_party.update_sla_history_state(lambda d: d.update(grid={"t1": grid_row("اسناد لمستخدم السرية", "2026-10-11", system="TP App"),
                                                                  "t2": grid_row("رفض", "2026-10-11", system="TP App"),
                                                                  "f1": grid_row("تم التنفيذ", "2026-10-11")}))
    block = daily_summary.tickets_block(third_party, hub(), {}, "2026-10-11", now)
    left = block["left_out"]
    assert (left["came"], left["open"], left["rejected"], left["now"], left["teams"]) == (2, 1, 1, 2, ["Third Party"])
    assert block["came"] == 1                                                       # Third Party not in the SLA's own
    html = daily_summary.build_html({"tickets": block}, {}, "13:00", now)
    assert "Third Party (counted only, not in the SLA)" in html and "On the Re-Assign page now" in html
    assert daily_summary.tickets_block(third_party, hub(), {}, "2026-10-11", now, team="Flex Cube").get("left_out") is None
