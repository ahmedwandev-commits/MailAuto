"""Browser helpers on top of Playwright.

The real pages are ASP.NET / SEDCO screens whose element ids we do not
know, so the bot locates things the way a person does: by the text on the
screen.  Every helper first tries explicit selectors from config/ui.yaml
(if any) and then falls back to text search.  All text comparisons use the
same Arabic normalisation as the rules engine.
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PWTimeout

from .textnorm import JS_COMPACT_SUFFIX, JS_NORM, best_match, compact, match_score

log = logging.getLogger("cem_bot")


class UIError(Exception):
    """An expected element / state was not found on the page."""


_JS_HELPERS = f"""
const NORM = {JS_NORM};
const C = (s) => NORM(s){JS_COMPACT_SUFFIX};
const isVis = (el) => {{
  if (!el || !el.getBoundingClientRect) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const cs = getComputedStyle(el);
  return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05;
}};
const ownText = (el) => (el.tagName === 'INPUT') ? (el.value || '') : (el.innerText || el.textContent || '');
"""

_JS_FIND_TEXT = "(args) => {" + _JS_HELPERS + r"""
  const {target, kind, mode, scopeCss, excludeCss, mark} = args;
  const t = C(target);
  if (!t) return 0;
  const minScore = mode === 'exact' ? 3 : (mode === 'prefix' ? 2 : 1);
  let roots = [document.body];
  if (scopeCss) roots = Array.from(document.querySelectorAll(scopeCss)).filter(isVis);
  const clickSel = 'a,button,input[type=button],input[type=submit],input[type=reset],[role=button],[role=link],' +
                   '[role=menuitem],[role=tab],[onclick],li,span,div,td,th,label,b,strong,i,p,font';
  const labelSel = 'label,span,td,th,div,b,strong,p,legend,h1,h2,h3,h4,h5,h6,dt,font';
  const sel = kind === 'label' ? labelSel : clickSel;
  let cands = [];
  const seen = new Set();
  for (const root of roots) {
    for (const el of root.querySelectorAll(sel)) {
      if (seen.has(el)) continue; seen.add(el);
      if (excludeCss && el.closest(excludeCss)) continue;
      const c = C(ownText(el));
      if (!c || c.length > t.length * 3 + 30) continue;
      let score = 0;
      if (c === t) score = 3; else if (c.startsWith(t)) score = 2; else if (c.includes(t)) score = 1;
      if (score < minScore) continue;
      if (!isVis(el)) continue;
      const clickable = el.matches('a,button,input,[role=button],[role=link],[onclick]') ||
                        !!el.closest('a,button,[role=button],[role=link],[onclick]');
      const isLabel = el.tagName === 'LABEL';
      cands.push({el, score, len: c.length, clickable, isLabel});
    }
  }
  // keep the deepest element of each nested chain
  cands = cands.filter(a => !cands.some(b => b !== a && a.el.contains(b.el) && b.score >= a.score));
  cands.sort((a, b) => (b.score - a.score) ||
      (kind === 'label' ? (b.isLabel - a.isLabel) : (b.clickable - a.clickable)) || (a.len - b.len));
  cands.forEach((c, i) => c.el.setAttribute('data-botmark', mark + '-' + i));
  return cands.length;
}"""

_JS_TABLES = "(root) => {" + _JS_HELPERS + r"""
  const scope = root || document;
  const all = Array.from(document.querySelectorAll('table'));
  const tables = root ? Array.from(scope.querySelectorAll('table')) : all;
  return tables.map((t) => {
    let headers = [];
    if (t.tHead && t.tHead.rows.length) {
      headers = Array.from(t.tHead.rows[t.tHead.rows.length - 1].cells).map(c => (c.innerText || '').trim());
    }
    const trs = Array.from(t.querySelectorAll(':scope > tbody > tr, :scope > tr'));
    const rows = [];
    trs.forEach((tr, i) => {
      const cells = Array.from(tr.cells);
      if (!cells.length) return;
      if (cells.every(c => c.tagName === 'TH')) {
        if (!headers.length) headers = cells.map(c => (c.innerText || '').trim());
        return;
      }
      if (!isVis(tr)) return;
      rows.push({dom: i, cells: cells.map(c => (c.innerText || '').trim()), checkbox: !!tr.querySelector('input[type=checkbox]')});
    });
    return {index: all.indexOf(t), visible: isVis(t), headers, rows};
  });
}"""

_JS_SELECT_INFO = r"""(sel) => {
  const sib = sel.nextElementSibling;
  const s2 = !!(sib && (' ' + sib.className + ' ').indexOf(' select2') >= 0) || sel.classList.contains('select2-hidden-accessible');
  const r = sel.getBoundingClientRect();
  const cs = getComputedStyle(sel);
  return {tag: sel.tagName, select2: s2, value: sel.value,
          selectedText: sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].text : '',
          visible: r.width > 3 && r.height > 3 && cs.visibility !== 'hidden' && cs.display !== 'none',
          options: sel.tagName === 'SELECT' ? Array.from(sel.options).map(o => ({v: o.value, t: o.text})) : []};
}"""

_JS_BOX = r"""(el) => {
  let e = el;
  if (e.tagName === 'SELECT' && e.nextElementSibling && (' ' + e.nextElementSibling.className).indexOf(' select2') >= 0)
    e = e.nextElementSibling;
  let r = e.getBoundingClientRect();
  if (r.width < 3 || r.height < 3) {
    const p = e.closest('label') || e.parentElement;
    if (p) r = p.getBoundingClientRect();
  }
  return {x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height};
}"""

_FIELD_XPATH = {
    "select": "xpath=following::select[1]",
    "input": "xpath=following::input[not(@type='hidden') and not(@type='checkbox') and not(@type='radio') "
             "and not(@type='submit') and not(@type='button')][1]",
    "textarea": "xpath=following::textarea[1]",
    "checkbox": "xpath=following::input[@type='checkbox'][1]",
}

_marks = itertools.count(1)


@dataclass
class Table:
    index: int                 # index among document.querySelectorAll('table')
    headers: list[str]
    rows: list[list[str]]
    dom_rows: list[int]
    row_has_checkbox: list[bool]

    def col(self, name: str, required: bool = True) -> int | None:
        idx = best_match(range(len(self.headers)), name, key=lambda i: self.headers[i])
        if idx is None and required:
            raise UIError(f"Column '{name}' not found. Columns: {self.headers}")
        return idx

    def records(self, columns: dict[str, str]) -> list[dict]:
        idx = {k: self.col(v) for k, v in columns.items()}
        out = []
        for r in self.rows:
            if len(r) <= 1:          # "No data available" row
                continue
            out.append({k: (r[i] if i is not None and i < len(r) else "") for k, i in idx.items()})
        return out


class Web:
    def __init__(self, page: Page, timeout_ms: int = 20000, exclude_css: str | None = None):
        self.page = page
        self.timeout_ms = timeout_ms
        self.exclude_css = exclude_css

    # ----------------------------------------------------------- waiting
    def settle(self, idle_ms: int = 4000):
        """Wait for the page (incl. ASP.NET post-backs) to finish loading."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except PWTimeout:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=idle_ms)
        except PWTimeout:
            pass

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")
        self.settle()

    # ----------------------------------------------------------- finding
    def by_selectors(self, selectors, timeout_ms: int | None = None, root=None) -> Locator | None:
        """First visible element among explicit selectors (tried in order)."""
        selectors = [s for s in (selectors or []) if s]
        if not selectors:
            return None
        base = root or self.page
        deadline = time.time() + (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000
        while True:
            for s in selectors:
                try:
                    loc = base.locator(s)
                    n = loc.count()
                    for i in range(min(n, 10)):
                        if loc.nth(i).is_visible():
                            return loc.nth(i)
                except Exception:   # invalid selector etc.
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def find_text_all(self, text: str, kind: str = "click", mode: str = "exact",
                      scope_css: str | None = None, exclude_css: str | None = None) -> list[Locator]:
        mark = f"m{next(_marks)}"
        n = self.page.evaluate(_JS_FIND_TEXT, {"target": text, "kind": kind, "mode": mode,
                                               "scopeCss": scope_css, "excludeCss": exclude_css, "mark": mark})
        return [self.page.locator(f'[data-botmark="{mark}-{i}"]') for i in range(n)]

    def find_text(self, text: str, kind: str = "click", mode: str = "exact", scope_css: str | None = None,
                  exclude_css: str | None = None, timeout_ms: int | None = None, required: bool = True) -> Locator | None:
        deadline = time.time() + (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000
        while True:
            found = self.find_text_all(text, kind, mode, scope_css, exclude_css)
            if found:
                return found[0]
            if time.time() >= deadline:
                if required:
                    raise UIError(f"Could not find '{text}' on {self.page.url}")
                return None
            time.sleep(0.3)

    def click_text(self, text: str, selectors=None, exclude_sidebar: bool = False, mode: str = "exact",
                   timeout_ms: int | None = None, scope_css: str | None = None):
        loc = self.by_selectors(selectors, timeout_ms=1500) if selectors else None
        if loc is None:
            loc = self.find_text(text, "click", mode, scope_css=scope_css,
                                 exclude_css=self.exclude_css if exclude_sidebar else None, timeout_ms=timeout_ms)
        loc.scroll_into_view_if_needed()
        loc.click()

    def exists_text(self, text: str, mode: str = "exact", exclude_sidebar: bool = False, scope_css=None) -> bool:
        return bool(self.find_text_all(text, "click", mode, scope_css,
                                       self.exclude_css if exclude_sidebar else None))

    # ----------------------------------------------------------- fields
    def _near(self, label: Locator, field: Locator) -> bool:
        try:
            a = label.evaluate(_JS_BOX)
            b = field.evaluate(_JS_BOX)
        except Exception:
            return False
        if b["w"] <= 0 and b["h"] <= 0:
            return False
        same_row = abs((a["y"] + a["h"] / 2) - (b["y"] + b["h"] / 2)) < 25
        just_below = (a["y"] - 10) <= b["y"] <= (a["y"] + a["h"] + 70)
        return same_row or just_below

    def field_by_label(self, label: str, field: str = "select", selectors=None,
                       timeout_ms: int | None = None, required: bool = True) -> Locator | None:
        """The <select>/<input>/<textarea>/checkbox that belongs to a visible label."""
        if selectors:
            loc = self.by_selectors(selectors, timeout_ms=1500)
            if loc is not None:
                return loc
            # explicit selectors may target hidden selects (select2)
            for s in selectors:
                try:
                    if self.page.locator(s).count():
                        return self.page.locator(s).first
                except Exception:
                    pass
        deadline = time.time() + (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000
        while True:
            for lab in self.find_text_all(label, "label", "prefix"):
                f = lab.locator(_FIELD_XPATH[field])
                if f.count() and self._near(lab, f.first):
                    return f.first
            if time.time() >= deadline:
                if required:
                    raise UIError(f"No {field} found next to label '{label}' on {self.page.url}")
                return None
            time.sleep(0.3)

    def fill(self, loc: Locator, value: str):
        loc.scroll_into_view_if_needed()
        loc.click()
        loc.fill("")
        loc.fill(value)

    def choose(self, select: Locator, target: str, type_text: str | None = None):
        """Select an option in a native <select> or a select2 dropdown."""
        info = select.evaluate(_JS_SELECT_INFO)
        if info["tag"] != "SELECT":
            raise UIError(f"choose(): element is <{info['tag']}> not <select>")
        if info["select2"]:
            self._choose_select2(select, target, type_text)
        else:
            opt = best_match(info["options"], target, key=lambda o: o["t"])
            if opt is None:
                raise UIError(f"Option '{target}' not in dropdown. Options: {[o['t'] for o in info['options']]}")
            if info["visible"]:
                select.select_option(value=opt["v"])
            else:
                select.evaluate("""(s, v) => { s.value = v; s.dispatchEvent(new Event('input', {bubbles: true}));
                    s.dispatchEvent(new Event('change', {bubbles: true}));
                    if (window.jQuery) window.jQuery(s).trigger('change'); }""", opt["v"])
        self.settle(2500)
        after = select.evaluate(_JS_SELECT_INFO)
        if match_score(after["selectedText"], target) == 0 and match_score(after["value"], target) == 0:
            raise UIError(f"Dropdown did not take value '{target}' (now '{after['selectedText']}')")
        log.debug("  chose '%s' -> '%s'", target, after["selectedText"])

    def choose_by_label(self, label: str, target: str, field: str = "select", selectors=None,
                        type_text: str | None = None, tries: int = 3):
        """Find the <select> next to *label* and choose *target*, re-finding and
        settling between attempts.

        Real ASP.NET decision panels post back when the row link ("إختر") is
        clicked and again when a value is picked, which can leave a just-found
        control stale or raise a navigation timeout mid-evaluate. Re-finding the
        control from the label on each try (instead of reusing one locator)
        rides those post-backs out. On the mock the first attempt succeeds, so
        behaviour there is unchanged."""
        last = None
        for attempt in range(max(1, tries)):
            try:
                sel = self.field_by_label(label, field, selectors=selectors)
                self.choose(sel, target, type_text=type_text)
                return
            except Exception as e:   # UIError / navigation / timeout -> settle and retry
                last = e
                log.debug("  choose '%s'='%s' attempt %d failed: %s", label, target, attempt + 1, e)
                self.settle()
        raise UIError(f"Could not set '{label}' to '{target}' after {tries} tries: {last}")

    def _choose_select2(self, select: Locator, target: str, type_text: str | None):
        page = self.page
        container = select.locator(
            "xpath=following-sibling::*[contains(concat(' ', normalize-space(@class), ' '), ' select2 ')][1]")
        handle = container.locator(".select2-selection").first if container.count() else None
        if handle is None or not handle.count():
            raise UIError("select2 container not found next to the <select>")
        handle.scroll_into_view_if_needed()
        handle.click()
        search = page.locator(".select2-container--open .select2-search__field")
        try:
            search.first.wait_for(state="visible", timeout=2500)
            search.first.fill("")
            search.first.press_sequentially(type_text or target, delay=40)
        except PWTimeout:
            pass   # dropdown without search box
        options = page.locator(".select2-container--open .select2-results__option")
        deadline = time.time() + 8
        best_i = None
        while time.time() < deadline:
            texts = options.all_inner_texts()
            texts = [t for t in texts if t.strip()]
            best_i = best_match(range(len(texts)), target, key=lambda i: texts[i])
            if best_i is not None:
                break
            time.sleep(0.3)
        if best_i is None:
            page.keyboard.press("Escape")
            raise UIError(f"select2: no option matching '{target}'")
        options.nth(best_i).click()

    def set_checkbox(self, box: Locator, checked: bool):
        """Tick/untick a checkbox, also when it is hidden behind a styled switch."""
        if box.is_checked() == checked:
            return
        try:
            if box.is_visible():
                box.set_checked(checked)
        except Exception:
            pass
        if box.is_checked() != checked:
            # click the visible switch/label that wraps the hidden input
            target = box.locator("xpath=following-sibling::*[1]")
            if not (target.count() and target.first.is_visible()):
                target = box.locator("xpath=ancestor::label[1]")
            if target.count() and target.first.is_visible():
                target.first.click()
        if box.is_checked() != checked:
            box.evaluate("(el) => el.click()")
        if box.is_checked() != checked:
            raise UIError("Could not change checkbox state")

    # ----------------------------------------------------------- tables
    def tables(self, root_css: str | None = None) -> list[Table]:
        root = None
        if root_css:
            root = self.page.locator(root_css).first.element_handle()
        data = self.page.evaluate(_JS_TABLES, root)
        out = []
        for t in data:
            if not t["visible"]:
                continue
            out.append(Table(t["index"], t["headers"], [r["cells"] for r in t["rows"]],
                             [r["dom"] for r in t["rows"]], [r["checkbox"] for r in t["rows"]]))
        return out

    def find_table(self, header_keywords: list[str], selectors=None, timeout_ms: int | None = None,
                   required: bool = True) -> Table | None:
        deadline = time.time() + (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000
        want = [compact(k) for k in header_keywords]
        while True:
            if selectors:
                loc = self.by_selectors(selectors, timeout_ms=500)
                if loc is not None:
                    idx = loc.evaluate("(t) => Array.from(document.querySelectorAll('table')).indexOf(t)")
                    hit = next((t for t in self.tables() if t.index == idx), None)
                    if hit:
                        return hit
            all_tables = self.tables()
            for n, t in enumerate(all_tables):
                heads = [compact(h) for h in t.headers]
                if all(any(w and w in h for h in heads) for w in want):
                    if not t.rows:   # DataTables "scrollX": header and body are two tables
                        for body in all_tables[n + 1:n + 3]:
                            if body.rows and (not body.headers or len(body.headers) == len(t.headers)) and \
                                    max(len(r) for r in body.rows) == len(t.headers):
                                return Table(body.index, t.headers, body.rows, body.dom_rows, body.row_has_checkbox)
                    return t
            if time.time() >= deadline:
                if required:
                    raise UIError(f"No table with columns {header_keywords} on {self.page.url}")
                return None
            time.sleep(0.3)

    def table_locator(self, table: Table) -> Locator:
        return self.page.locator("table").nth(table.index)

    def row_locator(self, table: Table, row_pos: int) -> Locator:
        dom = table.dom_rows[row_pos]
        return self.table_locator(table).locator(f"xpath=(./tbody/tr | ./tr)[{dom + 1}]")

    def click_in(self, root: Locator, text: str, mode: str = "exact"):
        """Click the element inside *root* whose text matches."""
        mark = f"m{next(_marks)}"
        n = root.evaluate("(r, args) => {" + _JS_HELPERS + r"""
            const t = C(args.target); let best = null, bestScore = 0;
            for (const el of r.querySelectorAll('a,button,input[type=button],input[type=submit],[onclick],span,td,div')) {
              const c = C(ownText(el)); if (!c) continue;
              let s = c === t ? 3 : (c.startsWith(t) ? 2 : (c.includes(t) ? 1 : 0));
              if (args.mode === 'exact' && s < 3) continue;
              if (s > bestScore || (s === bestScore && best && best.contains(el))) { best = el; bestScore = s; }
            }
            if (best) best.setAttribute('data-botmark', args.mark);
            return best ? 1 : 0; }""", {"target": text, "mode": mode, "mark": mark})
        if not n:
            raise UIError(f"'{text}' not found in the selected row/area")
        loc = root.locator(f'[data-botmark="{mark}"]')
        loc.scroll_into_view_if_needed()
        loc.click()

    # ----------------------------------------------------------- misc
    def page_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def screenshot(self, path: str):
        try:
            self.page.screenshot(path=path, full_page=True)
        except Exception as e:  # pragma: no cover
            log.warning("screenshot failed: %s", e)
