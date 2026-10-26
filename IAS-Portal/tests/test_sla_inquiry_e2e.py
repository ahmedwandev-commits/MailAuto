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
