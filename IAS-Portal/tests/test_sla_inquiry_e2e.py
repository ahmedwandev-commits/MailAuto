"""The SLA history read (sla_engine.read_history) end to end: a real Chromium driven over CDP (the engine's own
CdpBrowser) against tests/fake_inquiry_portal.py - the search by date only, every result page of the grid, every line
of each request's status history, and nothing recorded from a history that cannot be read whole. Made-up data only.
Skips itself when Chromium cannot start."""
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from calllog.ops import settings, sla_history, sla_records
from calllog.ops.engines import cdp
from calllog.ops.engines import sla_engine as sla
from tests.fake_inquiry_portal import FakeInquiryPortal, request

DAY = date(2026, 10, 21)
CFG = sla_records.default_config()


def at(hh, mm, ss=0):
    return datetime(2026, 10, 21, hh, mm, ss)


def six_line_history(user_no="700601"):
    """The real box's example shape: six lines over two pages of five, sorted by name on the page."""
    return [("700611", "أ موظف فرع", "Maker Branch", at(8, 40), "تسجيل", ""),
            ("700612", "ب مشرف فرع", "Checker Branch", at(8, 52, 51), "تم التعزيز من قبل مشرف الفرع", ""),
            (user_no, "ج مستخدم سرية", "Assign_Maker_Security", at(8, 54, 23), "اسناد لمستخدم السرية", "-----"),
            (user_no, "ج مستخدم سرية", "Assign_Maker_Security", at(9, 9, 56), "معلق", ""),
            (user_no, "ج مستخدم سرية", "Assign_Maker_Security", at(10, 28, 31), "تم التنفيذ", ""),
            ("700613", "د مراجع سرية", "Checker security", at(10, 45, 29), "تم التعزيز على التنفيذ", "")]


def open_history(user_no="700601"):
    return [("700621", "ه مشرف فرع", "Checker Branch", at(9, 36), "تم التعزيز من قبل مشرف الفرع", ""),
            ("700622", "و مراجع سرية", "Checker security", at(10, 23, 48), "اسناد لمستخدم السرية", ""),
            ("700623", "ز موظف فرع", "Maker Branch", at(8, 55, 2), "تسجيل", "")]


@pytest.fixture(scope="module")
def chromium(tmp_path_factory):
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "")
    found = [str(p) for p in sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True)] if str(root) != "." else []
    errors = []
    for exe in [os.environ.get("CHROME_PATH") or ""] + found + [cdp.find_browser("") or ""]:
        if not exe:
            continue
        try:
            cdp.CdpBrowser({"browser_executable": exe, "profile_dir": str(tmp_path_factory.mktemp("probe"))},
                           headless=True).close()
            return exe
        except Exception as exc:                         # missing binary, no sandbox permission, ...
            errors.append(str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc))
    pytest.skip("Chromium cannot start here: " + (" | ".join(errors) or "none found"))


@pytest.fixture
def browser(chromium, tmp_path):
    b = cdp.CdpBrowser({"browser_executable": chromium, "profile_dir": str(tmp_path / "profile")}, headless=True)
    yield b
    b.close()


def read(browser, portal, tmp_path, index=None, **over):
    raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {"sla": over})
    cfg, errors = settings.validate(raw)
    assert errors == [], errors
    cfg = cfg["sla"]
    cfg["timeout_seconds"] = 8
    logs = []
    plan = {"date_from": DAY, "date_to": DAY, "index": index or {}, "cfg": CFG, "excluded": None, "seconds": 120}
    out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, logs.append)
    out["logs"] = logs
    return out


def recorded(out):
    """{request number: (lifecycle, grid item)} of what the read would record."""
    return {it["id"]: (sla_history.lifecycle(it["history"], CFG), it["grid"])
            for it in out["items"] if it.get("history") is not None}


# ------------------------------------------------------------------------------------------- the status history
@pytest.mark.parametrize("mode", ["api", "show", "pages"])
def test_every_line_of_the_status_history_is_read(browser, tmp_path, mode):
    """Five lines a page, sorted by name: the assignment and the execution can sit on the second page. With the
    DataTables API, with «Show» only, or with the pager only - every line is read, so the clocks are exact."""
    reqs = [request(8100001, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history())]
    with FakeInquiryPortal(reqs, history=mode) as portal:
        out = read(browser, portal, tmp_path)
    assert portal.details_opened == ["8100001"]
    (lc, _), = recorded(out).values()
    assert lc["steps"] == 6 and lc["stage"] == "closed"
    assert lc["assigned_at"] == "2026-10-21T08:54:23" and lc["executed_at"] == "2026-10-21T10:28:31"
    assert lc["confirmed_at"] == "2026-10-21T10:45:29" and lc["registered_at"] == "2026-10-21T08:40:00"
    assert out["opened"] == 1 and out["failed"] == 0 and out["timing"]["partial"] == 0


def test_a_status_history_that_cannot_be_read_whole_is_not_recorded(browser, tmp_path):
    """The pager never moves: only five of the six lines can be read. A partial history could give a wrong clock
    (the assignment may be the missing line), so the request is not recorded and is read again next scan."""
    reqs = [request(8100002, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history())]
    with FakeInquiryPortal(reqs, history="stuck") as portal:
        out = read(browser, portal, tmp_path)
    assert portal.details_opened == ["8100002"]
    assert recorded(out) == {} and out["opened"] == 0 and out["timing"]["partial"] == 1
    assert [it.get("failed") for it in out["items"]] == [True]
    assert any("only 5 of its 6 history lines" in line for line in out["logs"])


# ------------------------------------------------------------------------------------------- the inquiry grid
def test_the_search_types_the_date_only(browser, tmp_path):
    """«تاريخ الطلب من / الى» take a date without a time («21-10-2026»): the fake refuses anything else."""
    reqs = [request(8100003, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", history=open_history())]
    with FakeInquiryPortal(reqs) as portal:
        out = read(browser, portal, tmp_path)
    assert portal.searches == [("21-10-2026", "21-10-2026")] and portal.refused == []
    assert out["rows"] == 1 and list(recorded(out)) == ["8100003"]


def _many(n, dup_at=None):
    """``n`` open requests of one day, each its own employee; two identical rows at ``dup_at`` (two requests the grid
    cannot tell apart: same employee, application and type the same day)."""
    out = []
    for i in range(n):
        name = f"موظف رقم {i}"
        if dup_at is not None and i == dup_at + 1:
            name = f"موظف رقم {dup_at}"
        out.append(request(8200000 + i, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee=name,
                           history=open_history()))
    return out


def test_every_result_page_of_the_portal_is_read_and_each_request_opened_on_its_page(browser, tmp_path):
    """The portal sends at most a page of rows («All» = 1000); the rest are behind «التالي» under the grid. Every page
    is read, and each row's «التفاصيل» (numbered on ITS page) is opened while that page is shown - so request 8200004
    is never recorded as 8200001."""
    reqs = _many(7, dup_at=2)
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 7 and out["timing"]["grid_pages"] == 3 and out["timing"]["grid_short"] is False
    got = recorded(out)
    assert sorted(got) == [r["number"] for r in reqs]                    # every request, the two identical ones too
    for number, (_, grid) in got.items():
        mine = next(r for r in reqs if r["number"] == number)
        assert grid["employee"] == mine["grid"]["employee"]
    assert len({it["grid"]["key"] for it in out["items"]}) == 7
    assert sorted(set(portal.pages_shown)) == [0, 1, 2]
    assert len(portal.searches) == 2               # the last page first, then one new search and «التالي» forward
    assert any("7 row(s) over 3 result pages, the portal lists 7" in line for line in out["logs"])


def test_the_banks_show_next_button_is_pressed_until_the_portals_total_is_read(browser, tmp_path):
    """The bank's page shows at most 1000 rows; the rest are behind the button «عرض التالي» (next to «عرض السابق»),
    and «اجمالي عدد الطلبات : - N» under the grid says how many there are. Every page is read and each request
    opened on its own page; once the total is read, «عرض التالي» is not pressed again."""
    reqs = _many(7, dup_at=2)
    with FakeInquiryPortal(reqs, server_page=3, pager="buttons") as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 7 and out["portal_total"] == 7 and out["timing"]["grid_pages"] == 3
    assert sorted(recorded(out)) == [r["number"] for r in reqs]
    assert sorted(set(portal.pages_shown)) == [0, 1, 2]
    text = "\n".join(out["logs"])
    assert "7 row(s) over 3 result pages, the portal lists 7 in all («اجمالي عدد الطلبات»)" in text
    assert "WARNING" not in text


def test_a_next_page_that_cannot_be_opened_is_reported_with_the_portals_total(browser, tmp_path):
    reqs = _many(7)
    with FakeInquiryPortal(reqs, server_page=3, pager="total_only") as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 3 and out["portal_total"] == 7
    assert any("WARNING only 3 of the 7 requests could be read" in line for line in out["logs"])


@pytest.mark.parametrize("grid", ["paged_api", "dom"])
def test_a_grid_that_holds_only_its_current_page_is_walked_to_the_end(browser, tmp_path, grid):
    """«Show» offers no «All» here and the DataTable holds only the rows of its current page: its own pager is walked
    until the page's «of N records» are all read, the two identical rows included."""
    reqs = _many(12, dup_at=5)
    with FakeInquiryPortal(reqs, grid=grid, grid_lengths=(5,), dt_len=5) as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 12 and out["timing"]["grid_total"] == 12 and out["timing"]["grid_short"] is False
    assert sorted(recorded(out)) == [r["number"] for r in reqs]


def test_show_all_is_used_when_the_grid_holds_one_page(browser, tmp_path):
    reqs = _many(9)
    with FakeInquiryPortal(reqs, grid="paged_api", dt_len=5) as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 9 and sorted(recorded(out)) == [r["number"] for r in reqs]


# ------------------------------------------------------------------- «نموذج (7)»: a request with a period
def _early_assigned(user_no="700601"):
    """Assigned two days before its period starts, executed and confirmed on the period's first day."""
    return [("700631", "ح موظف فرع", "Maker Branch", datetime(2026, 10, 19, 9, 0), "تسجيل", ""),
            ("700632", "ط مشرف فرع", "Checker Branch", datetime(2026, 10, 19, 9, 20), "تم التعزيز من قبل مشرف الفرع", ""),
            (user_no, "ج مستخدم سرية", "Assign_Maker_Security", datetime(2026, 10, 19, 9, 45), "اسناد لمستخدم السرية", ""),
            (user_no, "ج مستخدم سرية", "Assign_Maker_Security", at(8, 40), "تم التنفيذ", ""),
            ("700633", "د مراجع سرية", "Checker security", at(9, 5), "تم التعزيز على التنفيذ", "")]


@pytest.mark.parametrize("period, expected_to", [(("21/10/2026", "25/10/2026"), "2026-10-25T00:00:00"),
                                                 ("21/10/2026 - 25/10/2026", "2026-10-25T00:00:00")])
def test_the_period_of_a_form_7_request_is_read_and_its_clocks_start_there(browser, tmp_path, period, expected_to):
    """A «نموذج (7)» request registered and assigned on the 19th for a period from the 21st is executed on the 21st:
    its «الفترة من» (two fields, or one «الفتره من / الى») is read from the details and its clocks start at the
    period's first working hour - it belongs to the 21st and took 40 minutes, not two days."""
    reqs = [request(8300001, date(2026, 10, 19), "تم التعزيز على التنفيذ", executor="ج مستخدم سرية",
                    history=_early_assigned(), period=period, form="نموذج (7)")]
    with FakeInquiryPortal(reqs) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        plan = {"date_from": date(2026, 10, 19), "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None,
                "seconds": 120, "day_start": "08:00"}
        out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
    item, = [it for it in out["items"] if it.get("history") is not None]
    assert item["period_from"] == "2026-10-21T08:00:00" and item["period_to"] == expected_to
    records, index = {}, {}
    sla_history.fold_history(records, index, out["items"], CFG, "2026-10-21T12:00:00")
    rec = records["8300001"]
    assert rec["first_assigned_at"] == rec["assigned_at"] == "2026-10-21T08:00:00"
    assert rec["first_assigned_real_at"] == "2026-10-19T09:45:00" and rec["period_from"] == "2026-10-21T08:00:00"
    assert sla_history.assignment_day(rec) == "2026-10-21"
    assert round(sla_history.stage_minutes(rec)["work"]) == 40 and round(sla_history.stage_minutes(rec)["sla"]) == 65
    assert index[item["grid"]["key"]]["pv"] == sla_history.PERIOD_VERSION


def test_a_request_without_a_period_keeps_its_assignment(browser, tmp_path):
    reqs = [request(8300002, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history())]
    with FakeInquiryPortal(reqs) as portal:
        out = read(browser, portal, tmp_path)
    item, = [it for it in out["items"] if it.get("history") is not None]
    assert "period_from" not in item
    records = {}
    sla_history.fold_history(records, {}, out["items"], CFG, "2026-10-21T12:00:00")
    assert records["8300002"]["first_assigned_at"] == "2026-10-21T08:54:23"
    assert records["8300002"]["first_assigned_real_at"] == "" and records["8300002"]["period_from"] == ""


# ------------------------------------------------------------------- no result page waits scan after scan
def test_a_scan_that_cannot_read_every_row_starts_the_next_one_where_it_stopped(browser, tmp_path):
    """12 requests over 4 result pages (3 a page), 10 details a scan: the first scan reads pages 1-3 and one row of page
    4, and says the next scan starts on page 4; a scan starting there reads page 4 first - so the last pages are not
    left behind scan after scan while the first ones keep changing."""
    reqs = _many(12)
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        first = read(browser, portal, tmp_path, history_max_details=10)
    assert first["opened"] == 10 and first["left"] == 2 and first["resume_page"] == 3
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url, "history_max_details": 10}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                "resume_page": first["resume_page"]}
        second = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug2"), plan, lambda m: None)
        opened = list(portal.details_opened)
    last_page = {r["number"] for r in reqs[9:]}
    assert set(opened[:3]) == last_page                  # page 4 first, then pages 1, 2 ...
    assert second["opened"] == 10 and second["resume_page"] == 2


def test_a_live_ticket_whose_assignment_is_not_read_is_opened_before_the_finished_ones(browser, tmp_path):
    """The Live tab's clock needs the ticket's last «اسناد لمستخدم السرية»: an open request that is a Live ticket whose
    assignment is not read yet (``plan['live_keys']``) is opened first - before the finished ones, which otherwise come
    first - so a detail budget smaller than the day never leaves it behind."""
    reqs = [request(8300000 + i, DAY, "تم التعزيز على التنفيذ", executor="مستخدم اول", checker="مراجع سرية",
                    employee=f"موظف رقم {i}", history=six_line_history()) for i in range(11)]
    reqs.append(request(8300099, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee="موظف حي",
                        user_no="700299", history=open_history()))
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url, "history_max_details": 10}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        live = {sla_history.live_key({"fields": {"رقم المستخدم": "700299", "النظام": "Flexcube", "نوع الطلب": "اضافة صلاحية"}})}
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                "live_keys": live}
        out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
        opened = list(portal.details_opened)
    assert opened[0] == "8300099" and out["opened"] == 10 and "8300099" in recorded(out)


# ------------------------------------------------------- the recent days + one older day, within one time budget
def test_each_scan_reads_the_recent_days_and_one_older_day_within_its_time_budget(browser, tmp_path):
    """The search never reaches a month back at once: the recent window, then the one older day with a request still
    in progress, each its own search; both are «covered» when every row of them was read."""
    from datetime import timedelta
    older = DAY - timedelta(days=9)
    reqs = [request(8400001, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history()),
            request(8400002, older, "اسناد لمستخدم السرية", executor="مستخدم اول", history=open_history())]
    with FakeInquiryPortal(reqs) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        plan = {"date_from": DAY, "date_to": DAY, "slices": [(DAY, DAY), (older, older)], "older_day": older.isoformat(),
                "index": {}, "cfg": CFG, "excluded": None, "seconds": 120}
        out = sla.read_history_slices(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
    assert portal.searches == [("21-10-2026", "21-10-2026"), ("12-10-2026", "12-10-2026")]
    assert out["covered"] == ["2026-10-12", "2026-10-21"] and out["older_day"] == "2026-10-12"
    assert sorted(it["id"] for it in out["items"] if it.get("id")) == ["8400001", "8400002"]


def test_the_result_pages_are_not_walked_past_the_time_budget(browser, tmp_path):
    """A walk that would outlive the scan stops at the budget: the run is never killed, and the days of a slice cut
    short are not «covered» (their other rows keep what the earlier scans read)."""
    reqs = _many(9)
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        logs = []
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None,
                "deadline": __import__("time").monotonic() + sla.PAGE_RESERVE_S}    # spent once page 1 is read
        out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, logs.append)
    assert out["complete"] is False and out["rows"] == 3          # page 1 only; its rows are read in the reserve
    assert any("the time budget ran out after" in line for line in logs)
