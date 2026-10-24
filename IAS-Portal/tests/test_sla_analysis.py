# -*- coding: utf-8 -*-
"""Closed security-request analysis (calllog/ops/engines/sla_analysis.py): the SLA = DateF -> DateT1 full clock
(3h target), executed vs not-executed from the final status, rolled up per day / team / system / employee."""
from calllog.ops.engines import sla_analysis as sa

EXEC = "تم التعزيز على التنفيذ"
NOEXEC = "تم التعزيز على عدم التنفيذ"


def _rows():
    return [
        {"ReqID2": "1", "SStatusID": EXEC, "DateF": "01/04/2026 10:00:00 AM",   # Sun: executed, 2h  (within 3h)
         "DateT1": "01/04/2026 12:00:00 PM", "EmpNo": "100", "ApplicationName": "Flexcube"},
        {"ReqID2": "2", "SStatusID": EXEC, "DateF": "01/04/2026 09:00:00 AM",   # Sun: executed, 5h  (breach)
         "DateT1": "01/04/2026 02:00:00 PM", "EmpNo": "100", "ApplicationName": "Flexcube"},
        {"ReqID2": "3", "SStatusID": NOEXEC, "DateF": "01/05/2026 10:00:00 AM",  # Mon: not executed, 1h
         "DateT1": "01/05/2026 11:00:00 AM", "EmpNo": "200", "ApplicationName": "CRM"},
        {"ReqID2": "1", "SStatusID": EXEC, "DateF": "01/04/2026 10:00:00 AM",   # duplicate ReqID2 -> dropped
         "DateT1": "01/04/2026 12:00:00 PM", "EmpNo": "100", "ApplicationName": "Flexcube"},
    ]


def test_compute_core_numbers():
    m = sa.compute(_rows(), system_team={"flexcube": "Flex Cube", "crm": "Open System"})
    o = m["overall"]
    assert (o["closed"], o["executed"], o["not_executed"]) == (3, 2, 1)       # the duplicate was dropped
    assert o["executed_pct"] == 66.67
    assert o["sla_pct"] == 66.67                                              # 2 of 3 within 3h (2h, 1h); the 5h breaches
    assert o["median_hours"] == 2.0
    assert m["meta"]["requests"] == 3 and m["meta"]["days"] == 2 and m["meta"]["sla_hours"] == 3.0


def test_rolls_up_by_team_day_and_outcome():
    m = sa.compute(_rows(), system_team={"flexcube": "Flex Cube", "crm": "Open System"})
    teams = {r["team"]: r for r in m["by_team"]}
    assert teams["Flex Cube"]["closed"] == 2 and teams["Open System"]["closed"] == 1
    assert teams["Flex Cube"]["executed"] == 2 and teams["Open System"]["executed"] == 0
    days = {r["date"]: r for r in m["by_day"]}
    assert days["2026-01-04"]["closed"] == 2 and days["2026-01-04"]["executed"] == 2
    assert days["2026-01-05"]["closed"] == 1 and days["2026-01-05"]["executed"] == 0
    outcomes = {r["status"]: r["outcome"] for r in m["by_status"]}
    assert outcomes[EXEC] == "executed" and outcomes[NOEXEC] == "not_executed"


def test_unknown_application_falls_in_open_system():
    m = sa.compute([{"ReqID2": "9", "SStatusID": EXEC, "DateF": "01/04/2026 10:00:00 AM",
                     "DateT1": "01/04/2026 10:30:00 AM", "EmpNo": "1", "ApplicationName": "Mystery"}], system_team={})
    assert m["by_team"][0]["team"] == "Open System" and m["by_team"][0]["sla_pct"] == 100.0


def test_bom_and_blank_tolerated():
    m = sa.compute([{"﻿ReqID2": "x", "ReqID2": "x", "SStatusID": EXEC, "DateF": "", "DateT1": "",
                     "EmpNo": "", "ApplicationName": ""}], system_team={})
    assert m["overall"]["closed"] == 1 and m["overall"]["sla_pct"] == 0.0     # no dates -> not SLA-eligible
    assert m["by_employee"][0]["emp"] == "—"
