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
