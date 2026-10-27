"""Teams counted as one in the SLA (``sla.merge_teams``, calllog/ops/teams.py): by default the security requests of the
MFA Team count under Open System - in the Live tickets, the timeline, the history points, the SLA Analysis and the
day-by-day log - and a manager of the MFA Team sees the whole of Open System there. E-mails, ITSM and the accounts keep
their own teams. Made-up people and data; the test clock is 2026-10-11 09:00."""
import pytest

from calllog.ops import performance, settings as hub_settings, teams
from tests.test_ops_api import beat, flex_mgr, make_account, ops, put_snapshot, sla_data, sla_request  # noqa: F401

MFA_SEC, OS_SEC, FLEX_SEC = "مستخدم ام اف اي", "مستخدم اوبن", "مستخدم فلكس"
APPS = [{"name": "Veridium", "team": "MFA Team"}, {"name": "Platinum Mail", "team": "Open System"},
        {"name": "Flexcube", "team": "Flex Cube"}]


def hub(**sla):
    out, errors = hub_settings.validate({"sla": sla} if sla else {})
    assert errors == [], errors
    return out


# ---------------------------------------------------------------- the rule
def test_the_mfa_teams_requests_count_under_open_system_by_default_and_only_in_the_sla():
    resolver = teams.Resolver(hub(), {}, APPS)
    assert hub()["sla"]["merge_teams"] == {"MFA Team": "Open System"}
    assert resolver.sla_team("Veridium") == resolver.application_team("Veridium") == "Open System"
    assert resolver.sla_team("Flexcube") == "Flex Cube" and resolver.sla_team("Platinum Mail") == "Open System"
    assert resolver.itsm_team("IMP MFA Team") == "MFA Team"                # ITSM keeps its own teams


def test_a_request_belongs_to_its_applications_team_never_to_the_persons():
    """The owner's rule: a request is classified by its system «النظام» - an MFA Team employee holding or executing a
    Flexcube request does not make it Open System, and an application no rule names has no team (the SLA Analysis
    lists it for the administrator), whoever holds it."""
    users = {"7101": {"user_id": "7101", "role": "user", "team": "MFA Team", "security_name": MFA_SEC, "active": True}}
    resolver = teams.Resolver(hub(), users, APPS)
    assert resolver.sla_team("Flexcube", MFA_SEC) == "Flex Cube"
    assert resolver.sla_team("Some new app", MFA_SEC) == ""
    assert teams.sla_record_team("Flexcube", resolver) == "Flex Cube"
    assert teams.sla_record_team("Some new app", resolver) == teams.NO_TEAM


def test_emptied_the_mfa_team_counts_on_its_own_again():
    resolver = teams.Resolver(hub(merge_teams={}), {}, APPS)
    assert resolver.sla_team("Veridium") == "MFA Team" and resolver.sla_scope(("MFA Team",)) == ("MFA Team",)


def test_a_manager_of_the_merged_team_sees_the_team_it_joined_and_the_other_way_round():
    resolver = teams.Resolver(hub(), {}, APPS)
    data = sla_data([sla_request("v-1", MFA_SEC, "breached", 150, **{"النظام": "Veridium"}),
                     sla_request("p-1", OS_SEC, "within", 10, **{"النظام": "Platinum Mail"}),
                     sla_request("f-1", FLEX_SEC, "within", 10)])
    keys = lambda scope: [r["key"] for r in teams.scope_sla(data, scope, resolver)["requests"]]   # noqa: E731
    assert keys(("MFA Team",)) == keys(("Open System",)) == ["v-1", "p-1"]
    assert keys(("Flex Cube",)) == ["f-1"]
    assert teams.scope_sla(data, ("MFA Team",), resolver)["totals"]["breached"] == 1


def test_a_team_is_never_merged_into_itself_nor_into_a_merged_team():
    _, errors = hub_settings.validate({"sla": {"merge_teams": {"Open System": "open system"}}})
    assert [e["message"] for e in errors] == ["cannot be merged into itself"]
    _, errors = hub_settings.validate({"sla": {"merge_teams": {"MFA Team": "Open System", "Open System": "Flex Cube"}}})
    assert [e["path"] for e in errors] == ["sla.merge_teams.MFA Team"]


def test_days_kept_before_the_merge_count_like_the_days_after_it(ops):  # noqa: F811
    ops_store = ops
    row = lambda team, came: {"team": team, "came": came, "executed": came, "exec_n": 0}   # noqa: E731
    log = {"at": "2026-10-05T23:00:00", "totals": {"came": 9}, "times": {},
           "by_team": [row("MFA Team", 2), row("Open System", 3), row("Flex Cube", 4)],
           "by_person": [{"person": MFA_SEC, "team": "MFA Team", "came": 2}], "people": []}
    ops_store.update_history("sla", "2026-10-05", lambda doc: doc.update(day_log=log))
    merges = teams.team_merges(hub())
    came = lambda scope: performance.read_day_logs(ops_store, "2026-10-05", "2026-10-05", scope, "", merges)[0]["came"]  # noqa: E731
    assert came(("Open System",)) == came(("MFA Team",)) == 5 and came(("Flex Cube",)) == 4 and came(None) == 9
    glance = performance.add_logged_status({"by_team": [], "by_person": []}, ops_store, "2026-10-05", "2026-10-05",
                                           ("MFA Team",), merges)
    assert [(t["team"], t["came"]) for t in glance["by_team"]] == [("Open System", 5)] and glance["came"] == 5
    assert [p["team"] for p in glance["by_person"]] == ["Open System"]


# ---------------------------------------------------------------- what each manager sees
@pytest.fixture
def three_teams(ops, make_account):  # noqa: F811
    """Live tickets, a timeline event, a history point and closed requests of the MFA Team (Veridium), Open System
    (Platinum Mail) and Flex Cube (Flexcube); the application list says which team each application is."""
    ops.update_auto_assign(lambda d: d.update(applications=APPS))
    put_snapshot(ops, "sla", sla_data([sla_request("v-1", MFA_SEC, "breached", 150, **{"النظام": "Veridium"}),
                                       sla_request("p-1", OS_SEC, "within", 10, **{"النظام": "Platinum Mail"}),
                                       sla_request("f-1", FLEX_SEC, "within", 10)]))
    beat(ops, "sla")
    groups = [{"system": "Veridium", "user": MFA_SEC, "n": 1}, {"system": "Platinum Mail", "user": OS_SEC, "n": 1},
              {"system": "Flexcube", "user": FLEX_SEC, "n": 1}]
    ops.add_events([{"ts": "2026-10-11T08:40:00", "module": "sla", "level": "danger", "kind": "sla_breached",
                     "text": "3 tickets passed the 2 h SLA", "count": 3, "items": [], "groups": groups}])
    ops.add_history_point("sla", "2026-10-11", {"ts": "2026-10-11T08:30:00", "open": 3, "within": 2, "reminder": 0,
                                                "breached": 1, "groups": [{**g, "open": 1, "within": 1, "reminder": 0,
                                                                           "breached": 0} for g in groups]})
    for rid, system, who in (("h-mfa", "Veridium", MFA_SEC), ("h-os", "Platinum Mail", OS_SEC),
                             ("h-flex", "Flexcube", FLEX_SEC)):
        rec = {"id": rid, "source": "history", "stage": "closed", "first_assigned_at": "2026-10-11T07:00:00",
               "assigned_at": "2026-10-11T07:00:00", "executed_at": "2026-10-11T07:30:00",
               "confirmed_at": "2026-10-11T08:00:00", "system": system, "executor": who, "holder": who,
               "final_outcome": "executed", "exec_outcome": "executed", "outcome": "completed"}
        ops.update_sla_closed_day("2026-10-11", lambda d, rec=rec: d.update({rec["id"]: rec}))
    ops.update_sla_history_state(lambda d: d.update(last_history="2026-10-11T08:55:00"))
    mfa = make_account("mona", "manager", "Mona MFA", email="mona@example.test", team="MFA Team")
    osm = make_account("omar", "manager", "Omar Open", email="omar@example.test", team="Open System")
    return mfa, osm


def test_an_mfa_manager_sees_the_whole_of_open_system_in_every_sla_view(three_teams, flex_mgr):  # noqa: F811
    mfa, osm = three_teams
    for viewer in (mfa, osm):
        live = viewer.get("/api/ops/sla").get_json()["data"]
        assert [r["key"] for r in live["requests"]] == ["v-1", "p-1"] and live["totals"]["open"] == 2
        events = viewer.get("/api/ops/events?day=2026-10-11").get_json()["events"]
        assert [(e["text"], e["count"]) for e in events] == [("2 tickets passed the 2 h SLA", 2)]
        point = viewer.get("/api/ops/sla/history?days=1").get_json()["days"][-1]["points"][-1]
        assert point["open"] == 2
        analysis = viewer.get("/api/ops/sla-analysis?from=2026-10-11&to=2026-10-11").get_json()
        assert analysis["options"]["teams"] == ["Open System"] and analysis["meta"]["requests"] == 2
        assert analysis["options"]["executors"] == sorted([MFA_SEC, OS_SEC])
    flex = flex_mgr.get("/api/ops/sla-analysis?from=2026-10-11&to=2026-10-11").get_json()
    assert flex["options"]["teams"] == ["Flex Cube"] and flex["meta"]["requests"] == 1
    assert [r["key"] for r in flex_mgr.get("/api/ops/sla").get_json()["data"]["requests"]] == ["f-1"]


def test_the_administrator_sees_one_open_system_and_can_undo_the_merge(three_teams, admin):  # noqa: F811
    teams_of = lambda: admin.get("/api/ops/sla-analysis?from=2026-10-11&to=2026-10-11").get_json()["options"]["teams"]  # noqa: E731
    assert teams_of() == ["Flex Cube", "Open System"]
    assert admin.get("/api/ops/settings").get_json()["settings"]["sla"]["merge_teams"] == {"MFA Team": "Open System"}
    assert admin.put("/api/ops/settings", {"sla": {"merge_teams": {}}}).status_code == 200
    assert teams_of() == ["Flex Cube", "MFA Team", "Open System"]
    mfa, _ = three_teams
    assert [r["key"] for r in mfa.get("/api/ops/sla").get_json()["data"]["requests"]] == ["v-1"]
    bad = admin.put("/api/ops/settings", {"sla": {"merge_teams": {"MFA Team": "MFA Team"}}})
    assert bad.status_code == 400


def test_the_day_by_day_log_and_the_analysis_count_the_mfa_requests_under_open_system(three_teams, ops, flex_mgr):  # noqa: F811
    mfa, osm = three_teams
    row = lambda team, came: {"team": team, "came": came, "executed": came, "exec_n": 0}   # noqa: E731
    ops.update_history("sla", "2026-10-05", lambda doc: doc.update(day_log={
        "at": "2026-10-05T23:00:00", "totals": {"came": 9}, "times": {}, "people": [], "by_person": [],
        "by_team": [row("MFA Team", 2), row("Open System", 3), row("Flex Cube", 4)]}))
    days = lambda viewer, extra="": viewer.get(f"/api/ops/sla-days?from=2026-10-05&to=2026-10-05{extra}").get_json()["days"]  # noqa: E731
    assert days(mfa)[0]["came"] == days(osm)[0]["came"] == 5 and days(flex_mgr)[0]["came"] == 4
    assert days(mfa, "&team=Open System")[0]["came"] == 5                  # the team it joined is the manager's too
    for viewer, n in ((mfa, 2), (osm, 2), (flex_mgr, 1)):
        tickets = viewer.get("/api/ops/analytics?from=2026-10-11&to=2026-10-11").get_json()["modules"]["tickets"]
        assert tickets["total"] == n and [t["team"] for t in tickets["by_team"]] == (["Open System"] if n == 2 else ["Flex Cube"])
    narrowed = mfa.get("/api/ops/analytics?from=2026-10-11&to=2026-10-11&team=MFA Team").get_json()["modules"]["tickets"]
    assert narrowed["total"] == 2


def test_the_mfa_teams_daily_summary_counts_the_tickets_of_open_system(three_teams, ops):  # noqa: F811
    from datetime import datetime
    from calllog.ops.engines import daily_summary
    now = datetime(2026, 10, 11, 9, 0)
    block = lambda team: daily_summary.tickets_block(ops, hub(), {}, "2026-10-11", now, team=team)   # noqa: E731
    mfa, os_, flex = block("MFA Team"), block("Open System"), block("Flex Cube")
    assert mfa["live"]["open"] == os_["live"]["open"] == 2 and flex["live"]["open"] == 1
    assert mfa["live"]["breached"] == 1 and [t["team"] for t in mfa["by_team"]] == ["Open System"]
    assert sorted(t["team"] for t in block(None)["by_team"]) == ["Flex Cube", "Open System"]   # no MFA Team row
