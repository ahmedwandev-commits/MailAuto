"""Regression tests for bugs found in the collector engines audit (calllog/ops/engines)."""
from calllog.ops.engines import sla_analysis
from calllog.ops.engines import sla_engine as S


# ------------------------------------------------------------------------------------------- sla_analysis.compute
def _row(rid, start, end, status="تم التعزيز على التنفيذ", app="FCUBS", emp="1"):
    return {"ReqID2": rid, "DateF": start, "DateT1": end, "SStatusID": status, "ApplicationName": app, "EmpNo": emp}


def test_sla_analysis_target_is_not_a_module_global():
    """compute(sla_hours=...) used to overwrite the module's SLA_HOURS (shared by every roll-up running at the same
    time, and by every _Bucket made afterwards)."""
    rows = [_row("1", "10/21/2026 9:00:00 AM", "10/21/2026 1:00:00 PM")]          # 4 hours
    out = sla_analysis.compute(rows, sla_hours=5)
    assert out["overall"]["sla_pct"] == 100.0 and out["meta"]["sla_hours"] == 5.0
    assert sla_analysis.SLA_HOURS == 3.0                                         # untouched
    bucket = sla_analysis._Bucket()                                              # the default target is still 3 h
    bucket.add(True, 4.0, keep_hours=False)
    assert bucket.within_sla == 0


def test_sla_analysis_median_of_an_even_count():
    """The median of an even number of durations is the mean of the two middle ones (it took the upper one)."""
    rows = [_row("1", "10/21/2026 9:00:00 AM", "10/21/2026 10:00:00 AM"),        # 1 h
            _row("2", "10/21/2026 9:00:00 AM", "10/21/2026 11:00:00 AM")]        # 2 h
    assert sla_analysis.compute(rows)["overall"]["median_hours"] == 1.5
    rows.append(_row("3", "10/21/2026 9:00:00 AM", "10/21/2026 3:00:00 PM"))     # 6 h: odd count, the middle one
    assert sla_analysis.compute(rows)["overall"]["median_hours"] == 2.0


# ------------------------------------------------------------------------------- sla_engine.read_requests (paging)
HEADERS = ["اسم الموظف", "النظام", "تاريخ الارسال للسرية", "مستخدم السرية"]
CFG = {"sent_date_header": "تاريخ الارسال للسرية", "owner_header": "مستخدم السرية", "search_button_text": "بحث",
       "timeout_seconds": 1}


class PagedBrowser:
    """A Re-Assign table without the DataTables API: plain "next" paging over ``pages`` (each a list of rows), or a
    table that grows (``grow``: each "next" shows the rows so far plus the next page)."""

    def __init__(self, pages, total, grow=False):
        self.pages, self.total, self.grow, self.i = pages, total, grow, 0

    def eval(self, expr, timeout_ms=None):
        if expr == S.READ_TABLES:
            rows = [r for p in self.pages[:self.i + 1] for r in p] if self.grow else self.pages[self.i]
            return [{"source": "dom", "headers": HEADERS, "rows": [list(r) for r in rows], "pbs": []}]
        if expr == S.JS_GRID_STATE:
            return {"empty": False, "info": f"Showing 1 to 2 of {self.total} entries"}
        if expr == S.js_next_page():
            return {"x": 1, "y": 1} if self.i < len(self.pages) - 1 else None
        return None

    def click_at(self, x, y):
        self.i += 1


class NoDebug:
    def snap(self, browser, name):
        pass


def _fast(monkeypatch):
    monkeypatch.setattr(S, "PAGE_MS", 5)
    monkeypatch.setattr(S, "SETTLE_MS", 5)
    monkeypatch.setattr(S, "_sleep_ms", lambda ms: None)


def test_read_requests_keeps_identical_rows_on_two_pages(monkeypatch):
    """Two requests that look the same, one on page 1 and one on page 2: the second was dropped as "already seen", and
    the count check then refused the whole scan (read 3, the portal counts 4)."""
    _fast(monkeypatch)
    same = ["Ali", "FCUBS", "21/10/2026 09:00", "sec1"]
    pages = [[same, ["Bob", "Kondor", "21/10/2026 09:05", "sec2"]],
             [same, ["Cy", "FCUBS", "21/10/2026 09:10", "sec1"]]]
    headers, rows = S.read_requests(PagedBrowser(pages, total=4), CFG, NoDebug())
    assert headers == HEADERS
    assert len(rows) == 4 and rows.count(same) == 2


def test_read_requests_growing_table_counts_each_row_once(monkeypatch):
    _fast(monkeypatch)
    pages = [[["Ali", "FCUBS", "21/10/2026 09:00", "sec1"], ["Bob", "Kondor", "21/10/2026 09:05", "sec2"]],
             [["Cy", "FCUBS", "21/10/2026 09:10", "sec1"]]]
    _, rows = S.read_requests(PagedBrowser(pages, total=3, grow=True), CFG, NoDebug())
    assert [r[0] for r in rows] == ["Ali", "Bob", "Cy"]
