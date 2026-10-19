"""The auto-assign core logic (pure functions): e-mail distribution, time windows, app filtering, validation."""
import datetime as dt

from calllog.ops import auto_assign as aa

NOW = dt.datetime(2026, 10, 17, 15, 0, 0)        # 15:00
MEMBERS = {"a@x": "Alice", "b@x": "Bob", "c@x": "Carol"}


def row(mid, system="", status="Pending Reply", received="2026-10-17T09:00:00", assigned=""):
    return {"message_id": mid, "detected_systems": system, "status": status,
            "received_time": received, "assigned_to_email": assigned}


def test_disabled_does_nothing():
    block = {"enabled": False, "rules": [{"employees": ["a@x"], "applications": [], "from": "", "to": ""}]}
    assert aa.plan_email(block, [row("1")], NOW, MEMBERS) == []


def test_no_active_rule_does_nothing():
    block = {"enabled": True, "rules": [{"employees": ["a@x"], "applications": [], "from": "08:00", "to": "09:00"}]}
    assert aa.plan_email(block, [row("1")], NOW, MEMBERS) == []     # window 08-09, now is 15:00


def test_distributes_balanced_oldest_first():
    block = {"enabled": True, "rules": [{"employees": ["a@x", "b@x"], "applications": [], "from": "", "to": ""}]}
    rows = [row("3", received="2026-10-17T11:00:00"), row("1", received="2026-10-17T09:00:00"),
            row("2", received="2026-10-17T10:00:00")]
    out = aa.plan_email(block, rows, NOW, MEMBERS)
    assert [o["message_id"] for o in out] == ["1", "2", "3"]                               # oldest first
    assert [o["email"] for o in out] == ["a@x", "b@x", "a@x"] and out[0]["name"] == "Alice"  # balanced round-robin


def test_respects_existing_load():
    block = {"enabled": True, "rules": [{"employees": ["a@x", "b@x"], "applications": [], "from": "", "to": ""}]}
    assert aa.plan_email(block, [row("1")], NOW, MEMBERS, load={"a@x": 5})[0]["email"] == "b@x"


def test_skips_assigned_and_replied():
    block = {"enabled": True, "rules": [{"employees": ["a@x"], "applications": [], "from": "", "to": ""}]}
    rows = [row("1", assigned="someone@x"), row("2", status="Replied"), row("3")]
    assert [o["message_id"] for o in aa.plan_email(block, rows, NOW, MEMBERS)] == ["3"]


def test_application_filter_specific_wins_then_catch_all():
    block = {"enabled": True, "rules": [
        {"employees": ["a@x"], "applications": ["Flexcube"], "from": "", "to": ""},
        {"employees": ["b@x"], "applications": [], "from": "", "to": ""}]}
    rows = [row("1", system="Flexcube; GL"), row("2", system="DWP")]
    assert {o["message_id"]: o["email"] for o in aa.plan_email(block, rows, NOW, MEMBERS)} == {"1": "a@x", "2": "b@x"}


def test_time_window_active_and_inactive():
    r = {"employees": ["a@x"], "applications": [], "from": "14:00", "to": "16:00"}
    assert aa.rule_active(r, NOW) is True
    assert aa.rule_active(r, dt.datetime(2026, 10, 17, 17, 0)) is False
    assert aa.plan_email({"enabled": True, "rules": [r]}, [row("1")], dt.datetime(2026, 10, 17, 17, 0), MEMBERS) == []


def test_time_window_wraps_midnight():
    r = {"from": "22:00", "to": "02:00"}
    assert aa.rule_active(r, dt.datetime(2026, 10, 17, 23, 0)) is True
    assert aa.rule_active(r, dt.datetime(2026, 10, 17, 1, 0)) is True
    assert aa.rule_active(r, dt.datetime(2026, 10, 17, 12, 0)) is False


def test_empty_window_is_always_active():
    assert aa.rule_active({"from": "", "to": ""}, NOW) is True


def test_unknown_employee_dropped_from_pool():
    block = {"enabled": True, "rules": [{"employees": ["a@x", "ghost@x"], "applications": [], "from": "", "to": ""}]}
    assert all(o["email"] == "a@x" for o in aa.plan_email(block, [row("1"), row("2")], NOW, MEMBERS))


# ---------------------------------------------------------------------- ticketing distribution (the portal RPA, pure)
TMEMBERS = {"a@x": {"name": "Alice", "portal_id": "111"}, "b@x": {"name": "Bob", "portal_id": "222"},
            "c@x": {"name": "Carol", "portal_id": ""}}        # Carol has no portal id -> never pickable for ticketing


def treq(rid, system=""):
    return {"id": rid, "system": system}


def test_ticketing_disabled_or_no_rule():
    block = {"enabled": False, "rules": [{"employees": ["a@x"], "applications": [], "from": "", "to": ""}]}
    assert aa.plan_ticketing(block, [treq("1")], NOW, TMEMBERS) == []
    off = {"enabled": True, "rules": [{"employees": ["a@x"], "applications": [], "from": "08:00", "to": "09:00"}]}
    assert aa.plan_ticketing(off, [treq("1")], NOW, TMEMBERS) == []     # window 08-09, now 15:00


def test_ticketing_balances_across_portal_ids():
    block = {"enabled": True, "rules": [{"employees": ["a@x", "b@x"], "applications": [], "from": "", "to": ""}]}
    out = aa.plan_ticketing(block, [treq("1"), treq("2"), treq("3")], NOW, TMEMBERS)
    assert [o["portal_id"] for o in out] == ["111", "222", "111"]       # round-robin by least-loaded
    assert out[0]["name"] == "Alice"


def test_ticketing_respects_load_and_skips_portalless():
    block = {"enabled": True, "rules": [{"employees": ["a@x", "b@x"], "applications": [], "from": "", "to": ""}]}
    assert aa.plan_ticketing(block, [treq("1")], NOW, TMEMBERS, load={"111": 5})[0]["portal_id"] == "222"
    only_carol = {"enabled": True, "rules": [{"employees": ["c@x"], "applications": [], "from": "", "to": ""}]}
    assert aa.plan_ticketing(only_carol, [treq("1")], NOW, TMEMBERS) == []   # Carol has no portal id -> skipped


def test_ticketing_application_filter_and_grouping():
    block = {"enabled": True, "rules": [
        {"employees": ["a@x"], "applications": ["Flexcube"], "from": "", "to": ""},
        {"employees": ["b@x"], "applications": [], "from": "", "to": ""}]}
    rows = [treq("1", "Flexcube"), treq("2", "DWP"), treq("3", "Flexcube; GL")]
    plan = aa.plan_ticketing(block, rows, NOW, TMEMBERS)
    assert {o["id"]: o["portal_id"] for o in plan} == {"1": "111", "3": "111", "2": "222"}
    groups = aa.group_by_employee(plan)
    assert groups[0]["portal_id"] == "111" and sorted(groups[0]["ids"]) == ["1", "3"]   # busiest first
    assert groups[1]["portal_id"] == "222" and groups[1]["ids"] == ["2"]


def test_team_applications_scoped():
    cfg = {"applications": [{"name": "Flexcube", "team": "Flex Cube"}, {"name": "DWP", "team": "Card Management"},
                            {"name": "GL", "team": "Flex Cube"}]}
    assert aa.team_applications(cfg, "Flex Cube") == ["Flexcube", "GL"]
    assert aa.team_applications(cfg, "Card Management") == ["DWP"]
    assert aa.team_applications(cfg, "MFA Team") == []


def test_clean_rule_drops_unknowns_and_bad_time():
    r = aa.clean_rule({"employees": ["A@X", "ghost@x"], "applications": ["Flexcube", "Nope"], "from": "9:00", "to": "17:00"},
                      ["Flexcube"], {"a@x": "Alice"})
    assert r == {"employees": ["a@x"], "applications": ["Flexcube"], "from": "", "to": "17:00"}


def test_clean_rule_none_without_employees():
    assert aa.clean_rule({"employees": ["ghost@x"]}, [], {"a@x": "A"}) is None


def test_clean_applications():
    apps = aa.clean_applications([{"name": "Flexcube", "team": "Flex Cube"}, {"name": "X", "team": "No Team"},
                                  {"name": "Flexcube", "team": "Flex Cube"}, {"name": "", "team": "Flex Cube"}],
                                 ["Flex Cube", "Card Management"])
    assert apps == [{"name": "Flexcube", "team": "Flex Cube"}]


def test_clean_block():
    b = aa.clean_block({"enabled": True, "rules": [{"employees": ["a@x"]}, {"employees": ["ghost@x"]}]},
                       [], {"a@x": "Alice"})
    assert b["enabled"] is True and len(b["rules"]) == 1 and b["rules"][0]["employees"] == ["a@x"]
