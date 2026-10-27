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
    # the background read sees the server's own pager (the history comes in pages): the row is opened on the page
    assert portal.fetched == ["8100002"] and portal.details_opened == ["8100002", "8100002"]
    assert any("comes in pages" in line for line in out["logs"])
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


def test_the_log_says_the_dates_searched_and_warns_when_the_portal_lists_other_dates(browser, tmp_path):
    """A window far bigger than its days (19706 rows for «3 days») must be explainable from the log: it says the
    dates typed and the request dates the portal listed, and warns when the portal listed days outside the search
    (its «من» was not taken)."""
    from datetime import timedelta
    reqs = [request(8100004, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", history=open_history()),
            request(8100005, DAY - timedelta(days=5), "اسناد لمستخدم السرية", executor="مستخدم اول",
                    employee="موظف قديم", history=open_history())]
    with FakeInquiryPortal(reqs[:1]) as portal:
        out = read(browser, portal, tmp_path)
    text = "\n".join(out["logs"])
    assert "inquiry 21-10-2026 – 21-10-2026: 1 row(s)" in text and "request dates 21/10/2026 – 21/10/2026" in text
    assert "WARNING" not in text
    with FakeInquiryPortal(reqs, ignore_from=True) as portal:
        out = read(browser, portal, tmp_path)
    text = "\n".join(out["logs"])
    assert "request dates 16/10/2026 – 21/10/2026" in text
    assert "WARNING the portal listed requests dated 16/10/2026 – 21/10/2026 for a search of 21-10-2026 – 21-10-2026" in text


def _many(n, dup_at=None, status="اسناد لمستخدم السرية"):
    """``n`` open requests of one day, each its own employee; two identical rows at ``dup_at`` (two requests the grid
    cannot tell apart: same employee, application and type the same day)."""
    out = []
    for i in range(n):
        name = f"موظف رقم {i}"
        if dup_at is not None and i == dup_at + 1:
            name = f"موظف رقم {dup_at}"
        out.append(request(8200000 + i, DAY, status, executor="مستخدم اول", employee=name,
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
    assert len(portal.searches) == 1               # each page's rows opened while it is shown: never searched again
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


def test_a_busy_portal_gets_every_request_opened_once_on_one_search(browser, tmp_path):
    """The bank's portal is busy: requests come in while the result pages are walked, so the first page is never the
    same twice and the rows move down a page. Each page's rows are opened while it is shown (one search, never a
    page to find again), the row pushed onto the next page is seen twice and left out - the portal's total grew in
    between - and no request is opened twice or recorded under another row."""
    reqs = _many(7)
    newcomer = request(8299999, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee="موظف جديد",
                       history=open_history())
    with FakeInquiryPortal(reqs, server_page=3, pager="buttons", arrive_on_next=[newcomer]) as portal:
        out = read(browser, portal, tmp_path)
        opened = list(portal.details_opened)
    assert len(portal.searches) == 1 and sorted(opened) == sorted(set(opened)) == [r["number"] for r in reqs]
    assert out["rows"] == 7 and out["portal_total"] == 7 and out["complete"] is True and out["timing"]["repeats"] == 1
    got = recorded(out)
    assert sorted(got) == [r["number"] for r in reqs]
    for number, (_, grid) in got.items():                # each history under its own row
        assert grid["employee"] == next(r for r in reqs if r["number"] == number)["grid"]["employee"]
    assert len({it["grid"]["key"] for it in out["items"]}) == 7
    text = "\n".join(out["logs"])
    assert "1 row(s) seen twice left out" in text and "could not be shown again" not in text and "WARNING" not in text


def test_the_next_result_page_opens_with_a_requests_details_shown_under_the_grid(browser, tmp_path):
    """The bank's page fills «تفاصيل الطلب» into the page under the grid, with the status history's own pager
    «التالي» (Page$Next) in it. «عرض التالي» is the page's own button: a pager inside the details is never taken for
    it (the owner's v5.11 log: «only 2000 of the 3406 requests could be read» and a minute lost waiting), and the
    inquiry control's own name «UC_RequestDeatils_Inquiry1» - on the grid and on «عرض التالي» too - never hides it
    (v5.12: «only 1000 of the 3406»)."""
    reqs = _many(7)
    with FakeInquiryPortal(reqs, server_page=3, pager="buttons", details="in_page") as portal:
        out = read(browser, portal, tmp_path)
        searches = len(portal.searches)
    assert out["rows"] == 7 and out["complete"] is True and searches == 1
    assert sorted(recorded(out)) == [r["number"] for r in reqs] and "WARNING" not in "\n".join(out["logs"])


@pytest.mark.parametrize("details", ["modal", "in_page"])
def test_a_next_page_that_will_not_open_after_the_details_is_reached_by_a_new_search(browser, tmp_path, details):
    """When «عرض التالي» does nothing right after a request's details were opened, the scan does not wait out its time:
    it searches again (the details closed) and goes forward to the next page, so every request is still counted - on
    the bank's page too, where the grid and «عرض التالي» are inside the inquiry control «UC_RequestDeatils_Inquiry1»
    (v5.12 skipped every control of that name: «only 1000 of the 3406 requests could be read»)."""
    import time as _time
    reqs = _many(7)
    with FakeInquiryPortal(reqs, server_page=3, pager="buttons", next_after_details=False, details=details,
                           background=False) as portal:
        started = _time.monotonic()
        out = read(browser, portal, tmp_path)
        took = _time.monotonic() - started
        searches = len(portal.searches)
    assert out["rows"] == 7 and out["complete"] is True and sorted(recorded(out)) == [r["number"] for r in reqs]
    assert searches == 3 and took < 30                      # pages 2 and 3 each reached by a new search
    assert sum("background" in line for line in out["logs"]) == 1     # said once: the background is refused here
    assert sum("shown again from the first" in line for line in out["logs"]) == 2


def test_the_details_are_read_four_at_a_time_in_the_background_and_nothing_needed_is_left(browser, tmp_path):
    """The owner: «I want nothing left for the next run». Each «التفاصيل» is the portal's own post-back, sent in the
    background (fetch, the page never redrawn) four at a time - here a portal taking half a second a request reads 12
    in about two seconds, not six. «left» counts only what the numbers need (finished, waiting for confirmation, a Live
    ticket's clock); a request still with a security user is read when time allows."""
    done_ = [request(8600000 + i, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", employee=f"موظف {i}",
                     history=six_line_history()) for i in range(12)]
    still = [request(8600100 + i, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee=f"موظف ب {i}",
                     history=open_history()) for i in range(3)]
    with FakeInquiryPortal(done_ + still, details_delay=0.5) as portal:
        out = read(browser, portal, tmp_path)
        fetched, opened = list(portal.fetched), list(portal.details_opened)
    assert sorted(fetched) == sorted(opened) == sorted(r["number"] for r in done_ + still)    # never on the page
    assert out["opened"] == 15 and out["left"] == 0 and out["background"] is True
    assert sorted(recorded(out)) == sorted(r["number"] for r in done_ + still)
    assert out["timing"]["drill_avg_seconds"] < 0.4                 # 0.5 s a request on the portal, four at a time
    assert any("15 status histor(ies) read, nothing left" in line for line in out["logs"])


def test_only_what_the_numbers_need_counts_as_left(browser, tmp_path):
    """With no time for the details at all, the finished requests are «left»; the ones still with a security user are
    not (said apart: read when time allows)."""
    reqs = [request(8700000, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history()),
            request(8700001, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee="موظف ب",
                    history=open_history())]
    with FakeInquiryPortal(reqs) as portal:
        out = read(browser, portal, tmp_path, history_details_per_scan=10)
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        logs = []
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None,
                "deadline": __import__("time").monotonic() - 1}                        # the time is spent already
        late = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug2"), plan, logs.append)
    assert out["left"] == 0 and out["opened"] == 2
    assert late["opened"] == 0 and late["left"] == 1 and late["optional_left"] == 1
    assert any("1 left for the next scan (1 still with a security user: read when time allows)" in line for line in logs)


def test_a_portal_that_refuses_the_background_gets_every_request_opened_on_the_page(browser, tmp_path):
    reqs = _many(5, status="تم التعزيز على التنفيذ")
    with FakeInquiryPortal(reqs, background=False) as portal:
        out = read(browser, portal, tmp_path)
    assert sorted(recorded(out)) == [r["number"] for r in reqs] and out["background"] is False
    assert sum("background" in line for line in out["logs"]) == 1


def test_two_requests_that_look_the_same_across_two_pages_stay_two(browser, tmp_path):
    """Two real requests the grid cannot tell apart, one the last row of a page and one the first of the next: the
    portal's total did not grow, so they are two requests - both opened, none left out."""
    reqs = _many(7, dup_at=2)
    with FakeInquiryPortal(reqs, server_page=3, pager="buttons") as portal:
        out = read(browser, portal, tmp_path)
    assert out["rows"] == 7 and out["timing"]["repeats"] == 0 and sorted(recorded(out)) == [r["number"] for r in reqs]


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
def test_a_scan_that_cannot_read_every_row_starts_the_next_one_after_the_page_where_it_stopped(browser, tmp_path):
    """24 requests over 4 result pages (6 a page), 10 details a scan: the first scan reads page 1 and four rows of page
    2, and says the next scan starts on page 3; that one reads page 3 and four rows of page 4 (and wraps to page 1
    next); a scan that starts on page 4 reads it first, then page 1 by a new search - so with more to read than a scan
    can, no page keeps the others waiting scan after scan (the first pages keep changing on a busy portal)."""
    reqs = _many(24, status="تم التعزيز على التنفيذ")       # finished: each one's times are needed
    numbers = [r["number"] for r in reqs]

    def scan(resume, budget):
        with FakeInquiryPortal(reqs, server_page=6) as portal:
            raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url,
                                               "history_details_per_scan": budget}}, {})
            cfg, errors = settings.validate(raw)
            assert errors == []
            cfg = cfg["sla"]
            cfg["timeout_seconds"] = 8
            plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                    "resume_page": resume}
            out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / f"debug{resume}"), plan, lambda m: None)
            return out, list(portal.details_opened), len(portal.searches)
    first, opened, searches = scan(0, 10)
    # read 4 at a time in the background: the order within a page is the portal's, page after page is fixed
    assert sorted(opened) == numbers[:10] and first["left"] == 14 and first["resume_page"] == 2 and searches == 1
    second, opened, searches = scan(first["resume_page"], 10)
    assert sorted(opened) == numbers[12:22] and second["resume_page"] == 0 and searches == 1
    third, opened, searches = scan(3, 10)
    assert sorted(opened[:6]) == numbers[18:24] and sorted(opened[6:]) == numbers[:4]
    assert third["resume_page"] == 1 and searches == 2


def test_the_rows_held_back_so_the_walk_could_finish_are_read_by_a_second_search(browser, tmp_path, monkeypatch):
    """Time is kept for the result pages still to come, so every request is counted; what that leaves unread on the
    early pages is read after the walk, by a second search, while the scan has time - the budget is never left
    unused. Here every page but the last keeps all its time for the pages after it."""
    monkeypatch.setattr(sla, "WALK_SLACK_S", 10_000)
    reqs = _many(15)
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        out = read(browser, portal, tmp_path)
        opened = list(portal.details_opened)
        searches = len(portal.searches)
    numbers = [r["number"] for r in reqs]
    assert sorted(opened[:3]) == numbers[12:] and searches == 2           # the last page, then pages 1-4 again
    assert [sorted(opened[3 + 3 * p:6 + 3 * p]) for p in range(4)] == [numbers[3 * p:3 * p + 3] for p in range(4)]
    assert out["opened"] == 15 and out["left"] == 0 and out["complete"] is True and out["resume_page"] == 0


def test_a_live_ticket_whose_assignment_is_not_read_is_never_left_behind_by_the_rows_before_it(browser, tmp_path):
    """The Live tab's clock needs the ticket's last «اسناد لمستخدم السرية»: an open request that is a Live ticket whose
    assignment is not read yet (``plan['live_keys']``) is opened even when the detail budget is smaller than the
    finished requests listed before it - each page keeps a detail for the Live tickets the pages to come may list, and
    the next scan starts on the page whose rows waited."""
    reqs = [request(8300000 + i, DAY, "تم التعزيز على التنفيذ", executor="مستخدم اول", checker="مراجع سرية",
                    employee=f"موظف رقم {i}", history=six_line_history()) for i in range(14)]
    reqs.append(request(8300099, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee="موظف حي",
                        user_no="700299", history=open_history()))
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url, "history_details_per_scan": 10}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        live = {sla_history.live_key({"fields": {"رقم المستخدم": "700299", "النظام": "Flexcube", "نوع الطلب": "اضافة صلاحية"}})}
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                "live_keys": live}
        out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
        opened = list(portal.details_opened)
    assert out["opened"] == 10 and "8300099" in recorded(out) and opened[-1] == "8300099"
    assert sorted(opened[:9]) == [r["number"] for r in reqs[:9]] and out["resume_page"] == 3 and out["left"] == 5


@pytest.mark.parametrize("span, last, resume", [(("2026-10-18", "2026-10-21"), "8300012", 3),
                                                (("2026-10-01", "2026-10-04"), "8300009", 4)])
def test_only_the_live_tickets_this_search_can_list_keep_a_detail(browser, tmp_path, span, last, resume):
    """A Live ticket requested on another day (its «تاريخ الطلب» is at most a few days before it was sent) is not on
    this search's pages: no detail is kept for it, so the pages before the last are not held back for nothing."""
    reqs = [request(8300000 + i, DAY, "تم التعزيز على التنفيذ", executor="مستخدم اول", checker="مراجع سرية",
                    employee=f"موظف رقم {i}", history=six_line_history()) for i in range(15)]
    key = ("700999", "Flexcube", "اضافة صلاحية")
    with FakeInquiryPortal(reqs, server_page=3) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url, "history_details_per_scan": 10}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                "live_keys": {key}, "live_spans": {key: span}}
        out = sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
        opened = list(portal.details_opened)
    assert out["opened"] == 10 and sorted(opened[:9]) == [r["number"] for r in reqs[:9]] and opened[-1] == last
    assert out["resume_page"] == resume       # held back on page 4: it starts there; else page 4 had its turn


def test_the_live_ticket_on_the_page_shown_comes_first(browser, tmp_path):
    reqs = [request(8300000 + i, DAY, "تم التعزيز على التنفيذ", executor="مستخدم اول", checker="مراجع سرية",
                    employee=f"موظف رقم {i}", history=six_line_history()) for i in range(2)]
    reqs.append(request(8300099, DAY, "اسناد لمستخدم السرية", executor="مستخدم اول", employee="موظف حي",
                        user_no="700299", history=open_history()))
    with FakeInquiryPortal(reqs) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        live = {sla_history.live_key({"fields": {"رقم المستخدم": "700299", "النظام": "Flexcube", "نوع الطلب": "اضافة صلاحية"}})}
        plan = {"date_from": DAY, "date_to": DAY, "index": {}, "cfg": CFG, "excluded": None, "seconds": 120,
                "live_keys": live}
        sla.read_history(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
        assert portal.details_opened[0] == "8300099"


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


def test_today_a_recent_day_and_an_older_day_are_each_searched_and_a_day_not_read_keeps_its_turn(browser, tmp_path):
    """Each scan searches today, ONE other day of the recent window and ONE older day: never the whole window at once
    (20 result pages took two minutes). A day the scan had no time for keeps its turn for the next scan."""
    from datetime import timedelta
    recent, older = DAY - timedelta(days=1), DAY - timedelta(days=9)
    reqs = [request(8400011, DAY, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", history=six_line_history()),
            request(8400012, recent, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية", employee="موظف امس",
                    history=six_line_history()),
            request(8400013, older, "اسناد لمستخدم السرية", executor="مستخدم اول", history=open_history())]
    plan = {"date_from": DAY - timedelta(days=2), "date_to": DAY, "slices": [(DAY, DAY), (recent, recent), (older, older)],
            "recent_day": recent.isoformat(), "older_day": older.isoformat(), "index": {}, "cfg": CFG, "excluded": None,
            "seconds": 120}
    with FakeInquiryPortal(reqs) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        out = sla.read_history_slices(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, lambda m: None)
        assert portal.searches == [("21-10-2026", "21-10-2026"), ("20-10-2026", "20-10-2026"), ("12-10-2026", "12-10-2026")]
        assert out["covered"] == ["2026-10-12", "2026-10-20", "2026-10-21"] and out["date_from"] == "2026-10-19"
        assert (out["recent_day"], out["older_day"]) == ("2026-10-20", "2026-10-12")
        assert sorted(it["id"] for it in out["items"] if it.get("id")) == ["8400011", "8400012", "8400013"]
        logs = []
        late = sla.read_history_slices(browser, cfg, sla.Debug(False, tmp_path / "debug2"),
                                       {**plan, "seconds": sla.PAGE_RESERVE_S}, logs.append)   # no time at all
    assert late["covered"] == [] and late["items"] == [] and (late["recent_day"], late["older_day"]) == ("", "")
    assert [line for line in logs if "no time left" in line] == [
        f"SLA history: no time left for {d}; it is read at a next scan" for d in ("21/10", "20/10", "12/10")]


def test_a_day_with_more_requests_than_time_still_leaves_the_other_days_their_search(browser, tmp_path, monkeypatch):
    """Today alone could fill the scan (the owner's log: «no time left for 21/10 / 18/10»): while one day's requests
    are opened, time is kept for the days still to search - and for the moment a search may still start - so every
    day of the plan is counted, and the last one gets what is left."""
    from datetime import timedelta
    monkeypatch.setattr(sla, "PAGE_RESERVE_S", 2)
    monkeypatch.setattr(sla, "SLICE_RESERVE_S", 2)
    recent, older = DAY - timedelta(days=1), DAY - timedelta(days=9)
    reqs = [request(8500000 + 1000 * n + i, d, "تم التعزيز على التنفيذ", executor="ج مستخدم سرية",
                    employee=f"موظف {n} {i}", history=six_line_history())
            for n, d in enumerate((DAY, recent, older)) for i in range(30)]
    plan = {"date_from": DAY - timedelta(days=2), "date_to": DAY, "slices": [(DAY, DAY), (recent, recent), (older, older)],
            "recent_day": recent.isoformat(), "older_day": older.isoformat(), "index": {}, "cfg": CFG, "excluded": None,
            "seconds": 14}
    with FakeInquiryPortal(reqs, details_delay=0.25) as portal:
        raw = settings.deep_merge({"sla": {"username": "700101", "base_url": portal.url}}, {})
        cfg = settings.validate(raw)[0]["sla"]
        cfg["timeout_seconds"] = 8
        logs = []
        out = sla.read_history_slices(browser, cfg, sla.Debug(False, tmp_path / "debug"), plan, logs.append)
        searched = list(portal.searches)
        opened = list(portal.details_opened)
    assert [s[0] for s in searched] == ["21-10-2026", "20-10-2026", "12-10-2026"]
    assert not any("no time left" in line for line in logs)
    assert out["covered"] == ["2026-10-12", "2026-10-20", "2026-10-21"]
    by_day = [sum(1 for x in opened if x[3] == str(n)) for n in range(3)]           # 850n...: the day's digit
    assert all(by_day) and out["opened"] == len(opened) and out["left"] == 90 - len(opened)


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
