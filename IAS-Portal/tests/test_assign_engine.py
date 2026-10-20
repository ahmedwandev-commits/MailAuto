# -*- coding: utf-8 -*-
"""Ticketing auto-assign engine orchestration (calllog/ops/engines/assign_engine.py), with the browser replaced by two
injected seams so the whole plan -> group -> assign flow is exercised without a real portal."""
import datetime as dt

from calllog.ops.engines import assign_engine as ae
from calllog.ops.engines.common import JobContext

NOW = dt.datetime(2026, 10, 19, 12, 0, 0)
MEMBERS = {"a@x": {"name": "Alice", "portal_id": "111"}, "b@x": {"name": "Bob", "portal_id": "222"}}


def _ctx(teams, dry_run=False, log=None):
    return JobContext(job="assign", settings={}, general={}, store=None, now=lambda: NOW,
                      log=(log or (lambda m: None)), dry_run=dry_run,
                      options={"teams": teams, "username": "u", "password": "p"})


def _team(rules):
    return {"Flex Cube": {"block": {"enabled": True, "rules": rules}, "members": MEMBERS}}


def test_run_plans_groups_and_assigns():
    rules = [{"employees": ["a@x"], "applications": ["Flexcube"], "from": "", "to": ""},
             {"employees": ["b@x"], "applications": [], "from": "", "to": ""}]
    rows = [{"id": "r1", "system": "Flexcube"}, {"id": "r2", "system": "DWP"}, {"id": "r3", "system": "Flexcube"}]
    seen_groups = []

    def scraper(ctx, browser, form_type):
        return rows

    def assigner(ctx, browser, group):
        seen_groups.append((group["portal_id"], sorted(group["ids"])))
        return len(group["ids"])

    res = ae.run(_ctx(_team(rules)), scraper=scraper, assigner=assigner)
    assert res.ok and res.snapshot_data["assigned"] == 3
    assert res.snapshot_data["by_employee"] == {"Alice": 2, "Bob": 1}
    assert ("111", ["r1", "r3"]) in seen_groups and ("222", ["r2"]) in seen_groups


def test_run_dry_run_is_carried():
    rules = [{"employees": ["a@x"], "applications": [], "from": "", "to": ""}]
    res = ae.run(_ctx(_team(rules), dry_run=True),
                 scraper=lambda c, b, f: [{"id": "r1", "system": "x"}],
                 assigner=lambda c, b, g: len(g["ids"]))
    assert res.ok and res.snapshot_data["dry_run"] is True and res.snapshot_data["assigned"] == 1


def test_run_no_teams_fails():
    res = ae.run(_ctx({}), scraper=lambda c, b, f: [], assigner=lambda c, b, g: 0)
    assert not res.ok and "team" in (res.error or "").lower()


def test_run_skips_requests_with_no_matching_rule_or_portalless_employee():
    # the only rule names Bob's team-mate Carol, who has no portal id -> nothing assignable
    members = {"c@x": {"name": "Carol", "portal_id": ""}}
    teams = {"Flex Cube": {"block": {"enabled": True, "rules": [{"employees": ["c@x"], "applications": [], "from": "", "to": ""}]},
                           "members": members}}
    res = ae.run(_ctx(teams), scraper=lambda c, b, f: [{"id": "r1", "system": "x"}],
                 assigner=lambda c, b, g: len(g["ids"]))
    assert res.ok and res.snapshot_data["assigned"] == 0


def test_row_sig_is_stable_and_order_independent_per_row():
    assert ae.row_sig(["A", "B", "C"]) == ae.row_sig(["A", "B", "C"])
    assert ae.row_sig(["A", "B"]) != ae.row_sig(["A", "C"])


def test_js_signature_matches_python_row_sig():
    """The tick-by-signature JS must hash a row to the SAME value Python's row_sig does, or a planned row is never
    ticked. Run the engine's own _JS_NORM in Node against a row full of Arabic-Indic digits, tatweel, diacritics and
    alef/taa/alef-maqsura variants, and compare. Skipped where Node is unavailable (like the browser tests)."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    cells = ["طلـبأآ ٠١٢ رقــمًة ىX", "abc", "٩٩٩"]
    js = (ae._JS_NORM + "const crypto=require('crypto');"
          + "const cells=" + json.dumps(cells) + ";"
          + "const raw=cells.map(norm).join('|');"
          + "process.stdout.write(crypto.createHash('sha256').update(raw,'utf8').digest('hex').slice(0,16));")
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ae.row_sig(cells)


# ---------------------------------------------------------------------- the date filter ported from the stand-alone bot
def test_start_date_reads_the_from_part_only():
    assert ae.start_date("01/10/2026 - 31/12/2026") == dt.date(2026, 10, 1)
    assert ae.start_date("٠١/١٠/٢٠٢٦ - ٣١/١٢/٢٠٢٦") == dt.date(2026, 10, 1)          # Arabic-Indic digits
    assert ae.start_date("-") is None and ae.start_date("") is None


def test_classify_never_assigns_a_future_dated_request():
    rows = [{"id": "a", "period": "-"}, {"id": "b", "period": "01/10/2026 - 31/12/2026"},
            {"id": "c", "period": "01/01/2099 - 31/12/2099"}, {"id": "d", "period": "99/99/2026 - x"}]
    eligible, stats = ae.classify(rows, dt.date(2026, 10, 19))
    assert [r["id"] for r in eligible] == ["a", "b"]
    assert stats == {"total": 4, "eligible": 2, "future": 1, "unreadable": 1, "no_start": 1, "no_period": 0}
    assert len(ae.classify(rows, dt.date(2026, 10, 19), "all")[0]) == 4                 # "all" mode takes everything


def test_resolve_cutoff():
    today = dt.date(2026, 10, 19)
    assert ae.resolve_cutoff("today", today) == today and ae.resolve_cutoff("", today) == today
    assert ae.resolve_cutoff("25/10/2026", today) == dt.date(2026, 10, 25)
    assert ae.resolve_cutoff("2026-11-01", today) == dt.date(2026, 11, 1)
    assert ae.resolve_cutoff("nonsense", today) == today                                  # never wider than today


def test_why_nothing_planned_names_the_reason():
    rows = [{"id": "r", "system": "GL"}]
    off = {"T": {"block": {"enabled": True, "rules": [{"employees": ["a@x"], "applications": [], "from": "08:00",
                                                       "to": "09:00"}]}, "members": MEMBERS}}
    assert "no rule is active" in ae.why_nothing_planned(_ctx(off), rows)
    nopid = {"T": {"block": {"enabled": True, "rules": [{"employees": ["c@x"], "applications": []}]},
                   "members": {"c@x": {"name": "C", "portal_id": ""}}}}
    assert "no 'Number in the security portal'" in ae.why_nothing_planned(_ctx(nopid), rows)
    apps = {"T": {"block": {"enabled": True, "rules": [{"employees": ["a@x"], "applications": ["Flexcube"]}]},
                  "members": MEMBERS}}
    assert "applications: GL" in ae.why_nothing_planned(_ctx(apps), rows)


# ---------------------------------------------------------------------- end to end: real Chromium over CDP + fake portal
import glob  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

from calllog.ops.engines import cdp  # noqa: E402
from tests.fake_assign_portal import SUPERVISOR_PACKAGE, FakeAssignPortal  # noqa: E402

BROWSER = {}


@pytest.fixture(scope="module")
def chromium(tmp_path_factory):
    """A Chromium the engine's own CdpBrowser can start here, else skip (like the other browser tests)."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    found = [os.environ.get("CHROME_PATH") or ""] + sorted(glob.glob(os.path.join(root, "chromium-*/chrome-linux/chrome")),
                                                       reverse=True)
    errors = []
    for exe in [e for e in found if e] + [cdp.find_browser("") or ""]:
        if not exe:
            continue
        try:
            cdp.CdpBrowser({"browser_executable": exe, "profile_dir": str(tmp_path_factory.mktemp("probe"))},
                           headless=True).close()
        except Exception as exc:                                  # noqa: BLE001 - missing binary, no sandbox, ...
            errors.append(str(exc).splitlines()[0] if str(exc) else repr(exc))
            continue
        BROWSER["exe"] = exe
        return exe
    pytest.skip("Chromium cannot start here: " + (" | ".join(errors) or "none found"))


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(ae, "SETTLE_MS", 250)
    monkeypatch.setattr(ae, "POSTBACK_MS", 250)


TEAM_AB = {"Flex Cube": {"block": {"enabled": True, "rules": [{"employees": ["a@x", "b@x"], "applications": [],
                                                                  "from": "", "to": ""}]}, "members": MEMBERS}}


def e2e_run(portal, chromium, tmp_path, teams=TEAM_AB, dry_run=False, password=None, **settings):
    logs = []
    cfg = {"base_url": portal.url, "browser_executable": chromium, "profile_dir": str(tmp_path / "profile"),
           "timeout_seconds": 15, **settings}
    ctx = JobContext(job="assign", settings=cfg, general={}, store=None, now=lambda: NOW, log=logs.append,
                     dry_run=dry_run, options={"teams": teams, "username": portal.username,
                                               "password": portal.password if password is None else password})
    return ae.run(ctx), logs


@pytest.mark.usefixtures("fast")
def test_e2e_assigns_every_eligible_request_balanced_and_never_a_future_one(chromium, tmp_path):
    with FakeAssignPortal() as portal:
        res, logs = e2e_run(portal, chromium, tmp_path)
    text = "\n".join(logs)
    assert res.ok, text
    # form 1: R101/R102/R104/R105 eligible (R103 is future-dated), split 111/222; form 2: R201 -> 111
    assert portal.assigned == {"R101": "111", "R102": "222", "R104": "111", "R105": "222", "R201": "111"}, text
    assert "R103" not in portal.assigned and res.snapshot_data["assigned"] == 5
    assert f"entered the «{SUPERVISOR_PACKAGE}» package" in text and "skipped 1 future-dated" in text
    assert "confirmed by the portal: تم التنفيذ بنجاح - 2 request(s) -> Alice (111)" in text and portal.password not in text


@pytest.mark.usefixtures("fast")
def test_e2e_execute_is_confirmed_by_the_portals_success_label_although_new_requests_arrive(chromium, tmp_path):
    """The real portal says «تم التنفيذ بنجاح» after Execute; its queue keeps growing, so the grid alone cannot prove it."""
    with FakeAssignPortal(arrivals=True) as portal:
        res, logs = e2e_run(portal, chromium, tmp_path)
    text = "\n".join(logs)
    assert res.ok and len(portal.assigned) == 5, text
    assert res.snapshot_data["assigned"] == 5, text                    # every Execute counted
    assert "confirmed by the portal: تم التنفيذ بنجاح" in text and "may NOT have committed" not in text


@pytest.mark.usefixtures("fast")
def test_e2e_a_session_ended_mid_run_is_reported_with_what_was_already_assigned(chromium, tmp_path):
    """The real portal bounces sessions to the login page mid-cycle: the run must say so (not 'verified' / 'not
    offered') and still report the requests the first Execute already assigned."""
    with FakeAssignPortal(expire_after_executes=1) as portal:
        res, logs = e2e_run(portal, chromium, tmp_path)
    text = "\n".join(logs)
    first = len(portal.executes[0][2])
    assert not res.ok and "session ended" in res.error, text
    assert res.snapshot_data and res.snapshot_data["assigned"] == first, text
    assert "verified" not in text.split("Execute pressed", 2)[-1], text      # nothing after the bounce claims success


@pytest.mark.usefixtures("fast")
def test_e2e_same_tab_package_and_dry_run_assign_nothing(chromium, tmp_path):
    with FakeAssignPortal(open_mode="same") as portal:
        res, logs = e2e_run(portal, chromium, tmp_path, dry_run=True)
    assert res.ok and portal.assigned == {} and portal.executes == [], "\n".join(logs)
    assert res.snapshot_data["assigned"] == 5 and any("DRY-RUN" in ln for ln in logs)


@pytest.mark.usefixtures("fast")
def test_e2e_stops_and_lists_packages_when_the_supervisor_package_is_missing(chromium, tmp_path):
    with FakeAssignPortal(supervisor=False) as portal:
        res, logs = e2e_run(portal, chromium, tmp_path)
    assert not res.ok and portal.assigned == {}
    assert "does not hold" in res.error and "مدخل البيانات" in res.error


@pytest.mark.usefixtures("fast")
def test_e2e_an_employee_not_offered_for_a_form_is_skipped_and_explained(chromium, tmp_path):
    only_bob = {"Flex Cube": {"block": {"enabled": True, "rules": [{"employees": ["b@x"], "applications": []}]},
                              "members": MEMBERS}}
    with FakeAssignPortal() as portal:
        res, logs = e2e_run(portal, chromium, tmp_path, teams=only_bob)
    text = "\n".join(logs)
    assert res.ok and portal.assigned == {"R101": "222", "R102": "222", "R104": "222", "R105": "222"}, text
    assert "Bob (222) is not in «الموظفين»" in text and "offered employees: 111 - 111 - Alice Maker" in text


@pytest.mark.usefixtures("fast")
def test_e2e_application_rule_only_takes_its_applications(chromium, tmp_path):
    flex = {"Flex Cube": {"block": {"enabled": True, "rules": [{"employees": ["a@x"], "applications": ["Flexcube"]}]},
                          "members": MEMBERS}}
    with FakeAssignPortal() as portal:
        res, logs = e2e_run(portal, chromium, tmp_path, teams=flex)
    text = "\n".join(logs)
    assert res.ok and portal.assigned == {"R101": "111", "R102": "111", "R105": "111"}, text
    assert "no active rule covers these applications: T24" in text      # 'Flex Cube' and the FCIS line are Flexcube


@pytest.mark.usefixtures("fast")
def test_e2e_wrong_password_is_an_auth_failure(chromium, tmp_path):
    with FakeAssignPortal() as portal:
        res, _logs = e2e_run(portal, chromium, tmp_path, password="wrong-one")
    assert not res.ok and res.error_kind == "auth" and portal.logins == [False]


def test_classify_fails_closed_when_the_grid_has_no_period_column():
    rows = [{"id": "a", "period": "", "period_missing": True}, {"id": "b", "period": "", "period_missing": True}]
    eligible, stats = ae.classify(rows, dt.date(2026, 10, 19))
    assert eligible == [] and stats["no_period"] == 2                  # never assign blind in "not future-dated" mode
    assert len(ae.classify(rows, dt.date(2026, 10, 19), "all")[0]) == 2   # "every request" mode still takes them
