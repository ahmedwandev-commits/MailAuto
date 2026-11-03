"""The Best team of the day (owner, v5.18): the work is counted PER PERSON and the SLA counts - a team of 2 that answered
100 calls and 20 e-mails beats a team of 100 that answered 200 calls and 50 e-mails; with the same work per person, the team
that kept its SLA wins."""
import datetime as dt

import pytest

from calllog.ops import performance as perf
from calllog.ops.engines import best_performer as bp
from test_audit_ops import make_user, write_email_snapshot

DAY = "2026-10-11"
NOW = dt.datetime(2026, 10, 11, 15, 0)


def _users(sizes):
    out = {}
    for team, n in sizes.items():
        for i in range(n):
            uid = f"{team[:3]}{i}"
            out[uid] = {"user_id": uid, "name": uid, "role": "user", "active": True, "teams": [team]}
    return out


def _person(uid, team, calls=0, emails=0, tickets=0, role="user"):
    return {"user_id": uid, "name": uid, "role": role, "team": team, "calls": calls, "emails": emails, "tickets": tickets,
            "total": calls * 2 + emails * 3 + tickets}


@pytest.fixture
def sla(monkeypatch):
    box = {"mail": {}, "tick": {}}
    monkeypatch.setattr(perf, "_email_team_sla", lambda store, day, now: box["mail"])
    monkeypatch.setattr(perf, "_ticket_team_sla", lambda store, hub, users, day, numbers=None: box["tick"])
    return box


def test_the_work_is_counted_per_person(sla):
    users = _users({"Small Team": 2, "Big Team": 100})
    people = [_person("Sma0", "Small Team", calls=60, emails=10), _person("Sma1", "Small Team", calls=40, emails=10)]
    people += [_person(f"Big{i}", "Big Team", calls=2, emails=1 if i < 50 else 0) for i in range(100)]   # 200 calls, 50 e-mails
    rows = perf.team_day(None, None, {}, users, DAY, NOW, people=people)
    small, big = rows
    assert small["team"] == "Small Team" and small["rank"] == 1
    assert (small["manpower"], small["calls"], small["emails"], small["work"]) == (2, 100, 20, 260)
    assert (big["manpower"], big["calls"], big["emails"]) == (100, 200, 50)
    assert small["per_person"] == 130.0 and big["per_person"] == 5.5                              # (400 + 150) / 100
    assert small["sla_pct"] is None and small["score"] == small["per_person"]                     # nothing with an SLA


def test_only_the_people_who_worked_that_day_count(sla):
    """Owner (v5.18): somebody of the team on leave, off or on another task that day does not count as manpower."""
    users = _users({"Small Team": 10})
    people = [_person("Sma0", "Small Team", calls=50), _person("Sma1", "Small Team", tickets=20),
              _person("Sma2", "Small Team")]                                                     # nothing done: not counted
    (row,) = perf.team_day(None, None, {}, users, DAY, NOW, people=people)
    assert (row["manpower"], row["team_size"], row["work"], row["per_person"]) == (2, 10, 120, 60.0)


def test_the_sla_counts(sla):
    users = _users({"Fast Team": 2, "Slow Team": 2})
    people = [_person("Fas0", "Fast Team", calls=10, emails=10, tickets=20), _person("Slo0", "Slow Team", calls=10, emails=10, tickets=20)]
    sla["mail"] = {"Fast Team": {"ok": 10, "breached": 0}, "Slow Team": {"ok": 5, "breached": 5}}
    sla["tick"] = {"fast team": {"within": 18, "timed": 20, "median": 12}, "Slow Team": {"within": 10, "timed": 20, "median": 95}}
    fast, slow = perf.team_day(None, None, {}, users, DAY, NOW, people=people)
    assert fast["team"] == "Fast Team" and fast["per_person"] == slow["per_person"] == 70.0      # (20 + 30 + 20) / the 1 who worked
    assert (fast["email_sla_pct"], fast["ticket_sla_pct"], fast["sla_pct"]) == (100.0, 90.0, 93.3)    # 28 of 30
    assert (slow["email_sla_pct"], slow["ticket_sla_pct"], slow["sla_pct"]) == (50.0, 50.0, 50.0)
    assert fast["score"] == pytest.approx(65.3, abs=0.06) and slow["score"] == 35.0 and fast["exec_median_minutes"] == 12


def test_a_manager_who_replies_to_e_mails_counts_for_the_team(sla):
    """Owner (v5.18): a manager only replies to e-mails - their replies count, and a manager who replied counts as one of
    the team's people; their calls or requests never count."""
    users = _users({"Small Team": 2})
    users["boss"] = {"user_id": "boss", "name": "Boss", "role": "manager", "active": True, "teams": ["Small Team"]}
    users["idle"] = {"user_id": "idle", "name": "Idle", "role": "manager", "active": True, "teams": ["Small Team"]}
    people = [_person("Sma0", "Small Team", calls=10), _person("boss", "Small Team", emails=10, calls=4, tickets=9, role="manager"),
              _person("idle", "Small Team", calls=5, role="manager")]                       # a manager with no reply: not counted
    (row,) = perf.team_day(None, None, {}, users, DAY, NOW, people=people)
    assert (row["manpower"], row["team_size"], row["calls"], row["emails"], row["tickets"]) == (2, 4, 10, 10, 0)
    assert (row["work"], row["per_person"]) == (50, 25.0)                                       # (20 + 30) / 2
    assert [r["user_id"] for r in perf.employees_only(people)] == ["Sma0"]                     # Best employees: employees only


def test_a_team_without_work_or_without_people_is_not_listed(sla):
    users = _users({"Busy": 1, "Idle": 3})
    rows = perf.team_day(None, None, {}, users, DAY, NOW, people=[_person("Bus0", "Busy", calls=1), _person("x", "Nobody", calls=9)])
    assert [r["team"] for r in rows] == ["Busy"]


def test_the_email_sla_of_a_team_counts_the_mails_of_the_day():
    rows = [{"team": "A", "sla_status": "Met", "received_time": f"{DAY}T08:00:00", "first_reply_time": f"{DAY}T08:20:00"},
            {"team": "A", "sla_status": "Breached", "received_time": f"{DAY}T09:00:00"},
            {"team": "A", "sla_status": "Within SLA", "received_time": f"{DAY}T14:40:00"},
            {"team": "A", "sla_status": "Met", "received_time": "2026-10-10T08:00:00", "first_reply_time": "2026-10-10T08:10:00"},   # another day
            {"team": "A", "sla_status": "Breached", "received_time": "2026-10-10T16:00:00", "first_reply_time": f"{DAY}T08:00:00"},  # replied today
            {"team": "Unclassified", "sla_status": "Met", "received_time": f"{DAY}T08:00:00"},
            {"team": "B", "sla_status": "N/A", "received_time": f"{DAY}T08:00:00"}]
    assert perf.email_team_sla_of(rows, DAY) == {"A": {"ok": 2, "breached": 2}}


def test_the_mail_celebrates_the_team_of_the_day():
    star = _person("u1", "Small Team", calls=60, emails=10)
    best = {"team": "Small Team", "manpower": 2, "team_size": 3, "calls": 100, "emails": 20, "tickets": 0, "work": 260, "per_person": 130.0,
            "email_sla_pct": 95.0, "ticket_sla_pct": None, "sla_pct": 95.0, "score": 123.5, "rank": 1}
    html = bp.build_html([{**star, "rank": 1}], NOW, "Bravo", best)
    assert "TEAM OF THE DAY" in html and "Small Team" in html and "123.5" in html and "95%" in html and "2 of 3" in html
    assert "TEAM OF THE DAY" not in bp.build_html([{**star, "rank": 1}], NOW, "Bravo")


def test_the_page_lists_the_teams_and_a_manager_sees_every_team_but_only_their_people(app, admin):
    make_user(app, admin, "a1", teams=["Flex Cube"], email="a1@nbe.test")
    make_user(app, admin, "b1", teams=["Open System"], email="b1@nbe.test")
    make_user(app, admin, "b2", teams=["Open System"], email="b2@nbe.test")
    mgr = make_user(app, admin, "m1", role="manager", teams=["Flex Cube"], email="m1@nbe.test")
    rows = [{"message_id": "mm", "row_id": "mm", "team": "Flex Cube", "all_teams": ["Flex Cube"], "status": "Replied",
             "received_time": f"{DAY}T07:00:00", "first_reply_time": f"{DAY}T07:30:00", "first_reply_email": "m1@nbe.test",
             "sla_status": "Met", "response_minutes": 30, "sla_minutes": 60}]                   # the manager replied
    rows += [{"message_id": f"m{i}", "row_id": f"m{i}", "team": team, "all_teams": [team], "status": "Replied",
             "received_time": f"{DAY}T08:00:00", "first_reply_time": f"{DAY}T08:{10 + i:02d}:00", "first_reply_email": who,
             "sla_status": "Met", "response_minutes": 10, "sla_minutes": 60}
            for i, (team, who) in enumerate([("Flex Cube", "a1@nbe.test"), ("Flex Cube", "a1@nbe.test"), ("Open System", "b1@nbe.test")])]
    write_email_snapshot(app, rows)
    d = admin.get(f"/api/ops/performance?day={DAY}").get_json()
    by = {t["team"]: t for t in d["teams"]}
    assert (by["Flex Cube"]["manpower"], by["Flex Cube"]["team_size"], by["Flex Cube"]["emails"]) == (2, 2, 3)   # a1 + the manager
    assert by["Flex Cube"]["per_person"] == 4.5 and by["Flex Cube"]["email_sla_pct"] == 100.0           # 9 / 2
    assert (by["Open System"]["manpower"], by["Open System"]["team_size"]) == (1, 2)                # b2 did nothing that day
    assert by["Open System"]["per_person"] == 3.0
    assert d["teams"][0]["team"] == "Flex Cube" and d["teams"][0]["rank"] == 1
    assert [(x["user_id"], x["emails"]) for x in d["managers"]] == [("m1", 1)]                    # the Best manager
    m = mgr.get(f"/api/ops/performance?day={DAY}").get_json()
    assert {t["team"] for t in m["teams"]} == {"Flex Cube", "Open System"}                        # where my team stands
    assert {r["user_id"] for r in m["rows"]} == {"a1"}                                             # my people only (employees)
    assert {r["user_id"] for r in d["rows"]} == {"a1", "b1"} and [r["rank"] for r in d["rows"]] == [1, 2]   # no manager


# ------------------------------------------------------------------------------------- the Best manager (owner, v5.18)
class _Store:
    def __init__(self, quality):
        self.quality = quality

    def overrides(self, name):
        return self.quality if name == "quality" else {}


def _rating(rid, by, team, received=DAY, at=f"{DAY}T12:00:00", withdrawn=False):
    return {f"q{rid}": {"rating": "Good", "reviewed_by_id": by, "reviewed_at": at, "received": f"{received}T08:00:00",
                        "reply_team": team, "team": team, "rating_withdrawn": withdrawn}}


def test_the_best_manager_replies_and_rates_and_leaves_the_fewest_replies_unrated(monkeypatch):
    users = {"m1": {"user_id": "m1", "name": "Mona", "role": "manager", "active": True, "teams": ["Flex Cube"]},
             "m2": {"user_id": "m2", "name": "Omar", "role": "manager", "active": True, "teams": ["Open System"]},
             "m3": {"user_id": "m3", "name": "Idle", "role": "manager", "active": True, "teams": ["Open System"]}}
    quality = {}
    for i in range(8):
        quality.update(_rating(i, "m1", "Flex Cube"))                    # Mona rated 8 of Flex Cube's 10 replies
    for i in range(8, 12):
        quality.update(_rating(i, "m2", "Open System"))                  # Omar rated 4 of Open System's 20
    quality.update(_rating(99, "m2", "Open System", withdrawn=True))      # withdrawn after an appeal: not counted
    quality.update(_rating(98, "m2", "Open System", at="2026-10-10T12:00:00", received="2026-10-10"))   # another day
    monkeypatch.setattr(perf, "_team_reviewable", lambda store, day, now: {"Flex Cube": 10, "Open System": 20})
    people = [_person("m1", "Flex Cube", emails=5, calls=3, role="manager"), _person("m2", "Open System", emails=10, role="manager")]
    omar, mona = perf.manager_day(_Store(quality), users, DAY, NOW, people)
    # replying is a manager's choice: whoever replies keeps every point of it (owner, v5.18) - only the rating part
    # carries the team's unrated replies
    assert (omar["name"], omar["rank"], omar["emails"], omar["ratings"], omar["team_unrated"], omar["rated_pct"]) == ("Omar", 1, 10, 4, 16, 20.0)
    assert (omar["reply_points"], omar["rating_points"], omar["score"]) == (30, 0.8, 30.8)
    assert (mona["emails"], mona["ratings"], mona["team_reviewable"], mona["team_unrated"], mona["rated_pct"]) == (5, 8, 10, 2, 80.0)
    assert (mona["reply_points"], mona["rating_points"], mona["score"]) == (15, 6.4, 21.4)
    rater = {"m4": {"user_id": "m4", "name": "Rater", "role": "manager", "active": True, "teams": ["Flex Cube"]}}
    only = perf.manager_day(_Store(_rating(1, "m4", "Flex Cube")), rater, DAY, NOW, [])
    assert [(r["name"], r["emails"], r["ratings"]) for r in only] == [("Rater", 0, 1)]          # a manager who only rates is listed
    assert all(r["user_id"] != "m3" for r in perf.manager_day(_Store(quality), users, DAY, NOW, people))   # nothing done


def test_the_reviewable_replies_of_a_team():
    rows = [{"team": "A", "status": "Replied", "received_time": f"{DAY}T08:00:00"},
            {"team": "A", "status": "Replied", "received_time": f"{DAY}T09:00:00", "reply_by_manager": True},   # never rated
            {"team": "A", "status": "Pending Reply", "received_time": f"{DAY}T09:00:00"},
            {"team": "A", "status": "Replied", "received_time": "2026-10-10T09:00:00"},
            {"team": "Unclassified", "status": "Replied", "received_time": f"{DAY}T08:00:00"}]
    assert perf.team_reviewable_of(rows, DAY) == {"A": 1}


def test_the_mail_celebrates_the_manager_of_the_day():
    star = {**_person("u1", "Small Team", calls=60, emails=10), "rank": 1}
    best = {"user_id": "m1", "name": "Mona Manager", "team": "Flex Cube", "emails": 5, "ratings": 8, "points": 23,
            "team_reviewable": 10, "team_rated": 8, "team_unrated": 2, "rated_pct": 80.0, "score": 21.4, "rank": 1}
    html = bp.build_html([star], NOW, "Bravo", None, best)
    assert "MANAGER OF THE DAY" in html and "Mona Manager" in html and "80%" in html and "21.4" in html
