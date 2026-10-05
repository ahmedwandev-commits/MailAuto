"""Bulk export of "الاستفسار عن الطلبات" (RequestDetails_Inquiry.aspx) to CSV.

The inquiry page shows the requests that match a date range in an ASP.NET
GridView.  The grid is *server-paged*: it renders at most 1000 rows at a time
and the "التالى" / "السابق" (next / previous) buttons post back to load the
next / previous 1000.  A label ("اجمالى عدد الطلبات") shows the full match
count (e.g. 303,796), so "Show all" in the client only ever reveals the 1000
rows of the current page.

To get *all* of them this walks every server page from the first to the last,
reading the grid straight from the DOM each time (so DataTables' own client
paging never hides a row), and streams the rows to a UTF-8 (BOM) CSV as it
goes - the file is therefore complete and openable even if a long crawl is
interrupted part way through.
"""
from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import Config
from .portal import Portal
from .report import setup_logging
from .textnorm import norm
from .web import UIError, Web

log = logging.getLogger("cem_bot")

# Read the whole GridView from the DOM: the last <thead> row for the column
# names and every <tbody> row for the data (one cell is a <span> holding the
# request details - innerText flattens that automatically).
_JS_READ_GRID = r"""(grid) => {
  const clean = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  let headers = [];
  if (grid.tHead && grid.tHead.rows.length)
    headers = Array.from(grid.tHead.rows[grid.tHead.rows.length - 1].cells).map(clean);
  const bodyRows = grid.tBodies.length ? Array.from(grid.tBodies[0].rows)
                                       : Array.from(grid.rows).slice(headers.length ? 1 : 0);
  const rows = [];
  for (const tr of bodyRows) {
    const cells = Array.from(tr.cells);
    if (cells.length <= 1) continue;              // "No data available" placeholder
    rows.push(cells.map(clean));
  }
  return {headers, rows};
}"""


class Extractor:
    """Drives one inquiry export against an already-open portal page."""

    def __init__(self, web: Web, cfg: Config):
        self.web = web
        self.cfg = cfg
        self.ui = cfg.ui["portal"]["inquiry"]
        self.s = cfg.settings["inquiry"]

    @property
    def page(self):
        return self.web.page

    # --------------------------------------------------------------- helpers
    def _grid(self, required: bool = True):
        loc = self.web.by_selectors(self.ui["grid_selectors"], timeout_ms=self.web.timeout_ms if required else 1500)
        if loc is None and required:
            raise UIError("Request grid (GridView_RequestDetails) not found on the inquiry page")
        return loc

    def _read_grid(self) -> tuple[list[str], list[list[str]]]:
        grid = self._grid()
        data = grid.evaluate(_JS_READ_GRID)
        return data["headers"], data["rows"]

    def _total_count(self) -> int | None:
        loc = self.web.by_selectors(self.ui["total_count_selectors"], timeout_ms=1500)
        if loc is None:
            return None
        digits = "".join(ch for ch in norm(loc.inner_text()) if ch.isdigit())
        return int(digits) if digits else None

    @staticmethod
    def _sig(rows: list[list[str]]) -> str | None:
        """A short fingerprint of a page, used to tell whether 'next' advanced."""
        if not rows:
            return None
        return f"{len(rows)}|{'|'.join(rows[0])}|{'|'.join(rows[-1])}"

    def _next_button(self):
        loc = self.web.by_selectors(self.ui["next_selectors"], timeout_ms=800)
        if loc is None:
            loc = self.web.find_text(self.ui.get("next_button_text", "التالى"), "click", "prefix",
                                     timeout_ms=800, required=False)
        return loc

    def _wait_page_changed(self, prev_sig: str | None, timeout_ms: int) -> bool:
        """After clicking 'next', wait until the grid's fingerprint differs."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                _, rows = self._read_grid()
                if self._sig(rows) != prev_sig:
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(250)
        return False

    # --------------------------------------------------------------- search
    def open_and_search(self, date_from: str, date_to: str):
        log.info("Inquiry: opening %s", self.cfg.portal_url("portal_inquiry"))
        self.web.goto(self.cfg.portal_url("portal_inquiry"))
        frm = self.web.by_selectors(self.ui["from_date_selectors"]) \
            or self.web.field_by_label("تاريخ الطلب من", "input", required=False)
        to = self.web.by_selectors(self.ui["to_date_selectors"]) \
            or self.web.field_by_label("تاريخ الطلب الى", "input", required=False)
        if frm is None or to is None:
            raise UIError("Could not find the 'from'/'to' date boxes on the inquiry page")
        self.web.fill(frm, date_from)
        self.web.fill(to, date_to)
        log.info("Inquiry: searching %s .. %s", date_from, date_to)
        self.web.click_text(self.ui.get("search_button_text", "بحث"), selectors=self.ui["search_selectors"])
        self.web.settle()

    # --------------------------------------------------------------- export
    def export_csv(self, out_path: Path, date_from: str, date_to: str) -> dict:
        self.open_and_search(date_from, date_to)
        total = self._total_count()
        nav_timeout = self.cfg.settings["browser"].get("navigation_timeout_ms", 45000)
        max_pages = int(self.s.get("max_pages") or 0)
        pause_ms = int(self.s.get("page_pause_ms") or 0)
        log.info("Inquiry: total matching requests reported = %s", total if total is not None else "unknown")

        written = 0
        page_no = 0
        prev_sig = None
        headers_out: list[str] = []
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            while True:
                headers, rows = self._read_grid()
                sig = self._sig(rows)
                if page_no > 0 and sig is not None and sig == prev_sig:
                    log.warning("Inquiry: page did not advance - stopping at page %d", page_no)
                    break
                if page_no == 0:
                    headers_out = headers or [f"col_{i + 1}" for i in range(len(rows[0]) if rows else 0)]
                    writer.writerow(headers_out)
                for r in rows:
                    writer.writerow((r + [""] * len(headers_out))[:len(headers_out)] if headers_out else r)
                fh.flush()
                written += len(rows)
                prev_sig = sig
                page_no += 1
                log.info("Inquiry: page %d -> +%d rows (total written %d%s)", page_no, len(rows), written,
                         f"/{total}" if total else "")

                if not rows:
                    break
                if total is not None and written >= total:
                    log.info("Inquiry: reached the reported total (%d)", total)
                    break
                if max_pages and page_no >= max_pages:
                    log.info("Inquiry: reached max_pages=%d - stopping", max_pages)
                    break
                nxt = self._next_button()
                if nxt is None:
                    log.info("Inquiry: no 'next' button - last page reached")
                    break
                try:
                    if nxt.is_disabled():
                        log.info("Inquiry: 'next' is disabled - last page reached")
                        break
                except Exception:
                    pass
                nxt.scroll_into_view_if_needed()
                nxt.click()
                self.web.settle()
                if not self._wait_page_changed(prev_sig, nav_timeout):
                    log.warning("Inquiry: grid did not change after 'next' within %d ms - stopping", nav_timeout)
                    break
                if pause_ms:
                    self.page.wait_for_timeout(pause_ms)

        result = {"rows": written, "pages": page_no, "total_reported": total, "path": str(out_path)}
        if total is not None and written < total:
            log.warning("Inquiry: wrote %d of %d reported rows (%d short)", written, total, total - written)
        log.info("Inquiry: done - %d rows across %d page(s) -> %s", written, page_no, out_path)
        return result


def extract_requests(cfg: Config, date_from: str | None = None, date_to: str | None = None,
                     out: str | Path | None = None, headless: bool | None = None,
                     slow_mo: int | None = None, verbose: bool = False) -> dict:
    """Log in, open the provisioning portal and export the inquiry grid to CSV."""
    s = cfg.settings["inquiry"]
    date_from = date_from or s.get("default_from", "")
    date_to = date_to or s.get("default_to", "")
    if not date_from or not date_to:
        raise UIError("A --from and --to date are required (or set inquiry.default_from/default_to)")

    out_dir = cfg.path(s.get("output_dir", "exports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir, verbose)
    if out is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe = lambda d: d.replace("/", "-").replace(" ", "")  # noqa: E731
        out = out_dir / f"requests_{safe(date_from)}_to_{safe(date_to)}_{stamp}.csv"
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = cfg.path(str(out_path))

    b = cfg.settings["browser"]
    headless = b.get("headless", False) if headless is None else headless
    slow_mo = b.get("slow_mo_ms", 0) if slow_mo is None else slow_mo
    timeout = b.get("default_timeout_ms", 20000)

    log.info("Inquiry export | profile=%s | %s .. %s -> %s", cfg.profile, date_from, date_to, out_path)
    with sync_playwright() as pw:
        launch = dict(headless=headless, slow_mo=slow_mo)
        if b.get("channel"):
            launch["channel"] = b["channel"]
        browser = pw.chromium.launch(**launch)
        context = browser.new_context(ignore_https_errors=b.get("ignore_https_errors", True),
                                      viewport=b.get("viewport") or {"width": 1600, "height": 900})
        context.set_default_timeout(timeout)
        context.set_default_navigation_timeout(b.get("navigation_timeout_ms", 45000))
        try:
            page = context.new_page()
            portal = Portal(Web(page, timeout), cfg, rules=None, context=context)
            portal.login()
            portal.open_provisioning()
            return Extractor(portal.web, cfg).export_csv(out_path, date_from, date_to)
        finally:
            context.close()
            browser.close()
