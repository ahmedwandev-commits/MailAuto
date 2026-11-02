"""The daily summary's own sending days (v5.17): Settings › Daily summary › "Send on these days"; none ticked = the general days."""
from datetime import datetime
from pathlib import Path

from calllog.ops import runner, settings as hub_settings

FRIDAY, SUNDAY = datetime(2026, 11, 6, 10, 0), datetime(2026, 11, 8, 10, 0)


def cfg(summary_days, general=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
    return {"general": {"work_days": list(general), "active_hours": "", "holidays": []},
            "summary": {"enabled": True, "send_times": ["10:00"], "schedule": {"work_days": summary_days, "active_hours": None}}}


def test_the_summary_goes_only_on_its_own_days():
    s = cfg(["Sun", "Mon", "Tue", "Wed", "Thu"])
    assert runner.day_allowed(s, "summary", SUNDAY) and not runner.day_allowed(s, "summary", FRIDAY)
    assert runner.day_allowed(s, "email", FRIDAY)                                   # the other modules keep the general days


def test_no_day_ticked_follows_the_general_days():
    assert runner.day_allowed(cfg(None), "summary", FRIDAY)
    assert not runner.day_allowed(cfg(None, general=("Sun", "Mon", "Tue", "Wed", "Thu")), "summary", FRIDAY)


def test_the_setting_is_saved_and_shown_under_the_send_times(app, admin):
    r = admin.put("/api/ops/settings", {"summary": {"schedule": {"work_days": ["Sun", "Mon", "Tue", "Wed", "Thu"], "active_hours": None}}})
    assert r.status_code == 200, r.get_json()
    saved = hub_settings.load(app.extensions["ops"])["summary"]["schedule"]
    assert saved["work_days"] == ["Sun", "Mon", "Tue", "Wed", "Thu"]
    js = (Path(__file__).resolve().parents[1] / "program/calllog/static/system.js").read_text(encoding="utf-8")
    block = js.split('{ id: "summary", title: "Daily summary"')[1].split("Best Performance e-mail")[0]
    assert 'path: "summary.schedule.work_days", type: "days", emptyAs: null, label: "Send on these days"' in block
    assert block.index("summary.send_times") < block.index("summary.schedule.work_days")
    assert 'SCHEDULE("summary.schedule"' not in js                                  # the same setting is not shown twice
