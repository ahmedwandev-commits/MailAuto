# -*- coding: utf-8 -*-
"""Ticketing auto-assign: a native CDP RPA that assigns NEW security requests to the team's employees.

This is the executor behind the Auto-Assign page's *ticketing* target. A manager presses "Run now", types THEIR portal
username + password (used for that one run only - never stored), and this engine walks the portal exactly the way the
stand-alone NBE auto-authorizer bot (``nbe_auto_assign.py``) proved on the live site:

  1. log in, then on the packages page enter the SUPERVISOR package («مشرف البيانات بوحدة الأكواد السرية») - found by
     its NAME, never a row position; when the account does not hold it the run stops and lists the packages it does hold;
  2. click the «اسناد الطلبات الجديدة» menu link (``Request_Assign.aspx``);
  3. for every «نوع النموذج» (all the options the page offers, or the ones set in Settings): select it, Search, switch
     the grid to "show all", and read every request row;
  4. keep only the rows the bot would assign - by default NEVER a future-dated request (the «من» date of «الفتره من /
     الى» after today) - and plan who gets what with ``auto_assign.plan_ticketing`` (the manager's rules: which
     applications, which employees, which daily window - balanced across the chosen employees);
  5. for each employee: choose the decision «اسناد لمستخدم السرية», then that employee in «الموظفين» (only if the portal
     offers them for this form type), re-read the grid (the post-backs re-render it), tick EXACTLY their planned rows,
     check the tick count matches, press Execute (تنفيذ) once, and verify the commit by the grid shrinking.

Nothing is ever executed when there is nothing eligible to assign. Every step is written to the run log, including why
nothing was planned (no active rule, no portal number, no rule for these applications, ...), so an "assigned 0" run
explains itself.

It is driven over the DevTools protocol (``calllog.ops.engines.cdp``) - no Playwright, no Selenium, no browser download
- and reuses the SLA engine's login helpers. The browser work is behind two injectable seams (``scraper`` /
``assigner``) so the orchestration is unit-tested with a fake portal; the pure helpers (date filter, signature) are
tested on their own. Element ids / values live in ``settings["assign"]`` (the stand-alone bot's ids by default) so an
admin can tune them from System › Settings without a code change. No secret is ever logged, put in an error text, a
file name or a saved page.
"""
import hashlib
import json
import re
from collections import Counter
from contextlib import suppress
from datetime import date, datetime

from . import cdp, email_rules, sla_engine as S
from .. import auto_assign, overlay
from ..settings import DEFAULTS, deep_merge
from .common import JobResult

SETTLE_MS = 1500
POSTBACK_MS = 1500          # wait after an ASP.NET post-back (form type / decision / employee, search, execute)
MAX_FORM_TYPES = 20
PLACEHOLDER_VALUES = ("", "0", "-1")

# A JavaScript ``norm`` that reproduces common.norm_arabic EXACTLY (Arabic-Indic digits -> ASCII, tatweel + diacritics
# removed, alef/taa-marbuta/alef-maqsura folded, whitespace collapsed, lower-cased) so a row signature computed in the
# page matches one computed in Python (row_sig). Any drift here means a planned row is never found again.
_JS_NORM = r"""
  const _AR = {'٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'};
  const norm = s => (s || '').replace(/[٠-٩]/g, d => _AR[d]).replace(/ـ/g, '')
      .replace(/[ً-ْ]/g, '').replace(/[إأآ]/g, 'ا')
      .replace(/ة/g, 'ه').replace(/ى/g, 'ي').replace(/\s+/g, ' ').trim().toLowerCase();
"""


class AssignError(RuntimeError):
    pass


class SessionEnded(AssignError):
    """The portal sent its login page in the middle of the run (it ends sessions mid-cycle)."""


# the portal ended the session: its login page is back instead of the assign page
JS_ON_LOGIN = ("(() => /login\\.aspx/i.test(location.href) || (!!document.querySelector(\"input[type=password]\") "
               "&& !document.querySelector('#ContentPlaceHolder1_ddlFormType')))()")


def _check_session(browser, what):
    if browser.eval(JS_ON_LOGIN):
        raise SessionEnded(f"The portal session ended {what} (the portal showed its login page).")


def _success_text(browser, cfg):
    """The portal's own result label after Execute («تم التنفيذ بنجاح»), or ''."""
    sel = (cfg.get("selectors_assign") or {}).get("success_label") or "#ContentPlaceHolder1_lblStatusSuccess"
    text = browser.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); "
                        "return e ? (e.textContent || '').trim() : ''; })()")
    return str(text or "")


# --------------------------------------------------------------------------- config
def _config(ctx):
    return deep_merge(DEFAULTS["assign"], ctx.settings or {})


def row_sig(cells):
    """A stable signature of a grid row from its visible cell texts, so a row can be matched again after an ASP.NET
    post-back re-renders the grid (the check-box ids are positional and re-index)."""
    raw = "|".join(S.norm(c) for c in cells)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- the date filter (ported from the bot)
_DMY = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
_YMD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_ddmmyyyy(text):
    """First DD/MM/YYYY (or DD-MM-YYYY / DD.MM.YYYY) date in ``text`` -> date, else None. Arabic-Indic digits allowed."""
    m = _DMY.search(S.norm(text))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def start_date(period):
    """The request's START date = the «من» of a «من / الى» cell ("DD/MM/YYYY - DD/MM/YYYY"), or None when the request
    has no validity period ("-" or blank)."""
    if not period:
        return None
    return parse_ddmmyyyy(re.split(r"\s[-–]\s", S.norm(period), maxsplit=1)[0])


def looks_like_date(period):
    """True when the «من» part has date-like digits we could not read - such a row is skipped for a human to check."""
    if not period:
        return False
    return bool(re.search(r"\d{1,2}\s*[/.\-]\s*\d{1,2}", re.split(r"\s[-–]\s", S.norm(period), maxsplit=1)[0]))


def resolve_cutoff(value, today):
    """``assign_cutoff`` ('today' | DD/MM/YYYY | YYYY-MM-DD) -> date. Anything unreadable -> today (never wider)."""
    v = str(value or "today").strip()
    if v.lower() in ("", "today"):
        return today
    d = parse_ddmmyyyy(v)
    if d is None:
        m = _YMD.match(v)
        if m:
            with suppress(ValueError):
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return d or today


def classify(rows, cutoff, mode="date"):
    """Split grid rows into those to assign vs skipped - the stand-alone bot's rule. ``mode`` "all" takes every row;
    "date" (default) skips a request whose start date is AFTER ``cutoff`` (never assign a future-dated request) and one
    whose start date cannot be read; a request with no start date is assigned. Returns (eligible, stats)."""
    eligible, future, unreadable, no_start, no_period = [], 0, 0, 0, 0
    for row in rows:
        if mode == "all":
            eligible.append(row)
            continue
        if row.get("period_missing"):         # the grid has no period column: the start date cannot be checked
            no_period += 1
            continue
        period = (row.get("period") or "").strip()
        frm = start_date(period)
        if frm is not None and frm > cutoff:
            future += 1
        elif frm is not None:
            eligible.append(row)
        elif looks_like_date(period):
            unreadable += 1
        else:
            no_start += 1
            eligible.append(row)
    return eligible, {"total": len(rows), "eligible": len(eligible), "future": future, "unreadable": unreadable,
                      "no_start": no_start, "no_period": no_period}


# --------------------------------------------------------------------------- browser JavaScript
def js_select_value(selector, value):
    """Set a <select> to ``value`` and fire input/change so the ASP.NET AutoPostBack runs. Returns the selected value,
    or null when that option is not offered."""
    return ("(() => {"
            + "const el = document.querySelector(" + json.dumps(selector) + ");"
            + "if (!el) return null;"
            + "const want = " + json.dumps(str(value)) + ";"
            + "const opt = [...el.options].find(o => String(o.value).trim() === want);"
            + "if (!opt) return null;"
            + "el.value = opt.value;"
            + "el.dispatchEvent(new Event('input', {bubbles:true}));"
            + "el.dispatchEvent(new Event('change', {bubbles:true}));"
            + "return el.value; })()")


def js_select_options(selector):
    """[{value, text}] of a <select>'s options (null when the select is not on the page)."""
    return ("(() => { const el = document.querySelector(" + json.dumps(selector) + ");"
            + "if (!el) return null;"
            + "return [...el.options].map(o => ({value: String(o.value || '').trim(),"
            + " text: (o.text || '').replace(/\\s+/g, ' ').trim()})); })()")


def js_exists(selector):
    return "!!document.querySelector(" + json.dumps(selector) + ")"


def js_landing_packages():
    """The package names on the after-login packages page (rows that carry an Enter link) - only to tell the manager
    which packages their account holds when the supervisor one is missing."""
    return ("(() => { const out = [];"
            + "for (const tr of document.querySelectorAll('table tr')) {"
            + "  if (!tr.querySelector(\"a[id*='btnSubmit'], a, input[type=submit], button\")) continue;"
            + "  const t = (tr.innerText || tr.textContent || '').replace(/\\s+/g, ' ').trim();"
            + "  if (t && t.length < 200 && !out.includes(t)) out.push(t); }"
            + "return out.slice(0, 15); })()")


def js_show_all(length_selector, grid_selector):
    """Switch the grid's DataTables page length to "All" (-1), through its length <select> when present, else through
    the DataTables API. Returns how it was done, or '' when neither was possible (then only the first page is read)."""
    return ("(() => {"
            + "const s = document.querySelector(" + json.dumps(length_selector) + ");"
            + "if (s && [...s.options].some(o => String(o.value) === '-1')) {"
            + "  if (String(s.value) !== '-1') { s.value = '-1';"
            + "    s.dispatchEvent(new Event('change', {bubbles:true})); }"
            + "  return 'select'; }"
            + "const g = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "try { if (g && window.jQuery && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(g)) {"
            + "  jQuery(g).DataTable().page.len(-1).draw(); return 'api'; } } catch (e) {}"
            + "return ''; })()")


def js_busy(processing_selector):
    """Is the DataTables 'processing' overlay showing (a server-side reload still running)?"""
    return ("(() => { const p = document.querySelector(" + json.dumps(processing_selector) + ");"
            + "return !!(p && (p.offsetWidth || p.offsetHeight || p.getClientRects().length)"
            + " && getComputedStyle(p).display !== 'none'); })()")


def js_grid_rows(grid_selector, app_header, period_header):
    """Read the request grid: its header names, which column is the application (``app_header``) and which the
    «الفتره من / الى» period (``period_header``), and for every row that carries a ``ch_Select`` check-box: the box id,
    the cell texts, the application and the period."""
    return ("(() => {" + _JS_NORM
            + "const grid = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "if (!grid) return {error: 'grid-not-found'};"
            + "const cell = n => { if (!n) return ''; const c = n.cloneNode(true);"
            + "  c.querySelectorAll('br').forEach(b => b.replaceWith(' '));"
            + "  c.querySelectorAll('script,style').forEach(s => s.remove());"
            + "  return (c.textContent || '').replace(/\\s+/g,' ').trim(); };"
            + "let head = [...grid.querySelectorAll('thead th, thead td')].map(cell);"
            + "if (!head.filter(Boolean).length) { const first = grid.querySelector('tr');"
            + "  head = first ? [...first.cells].map(cell) : []; }"
            + "const find = w => { const want = norm(w); if (!want) return -1;"
            + "  let i = head.findIndex(h => norm(h) === want); if (i < 0) i = head.findIndex(h => norm(h).includes(want));"
            + "  return i; };"
            + "const appIdx = find(" + json.dumps(app_header) + "), perIdx = find(" + json.dumps(period_header) + ");"
            + "const rows = [];"
            + "for (const tr of grid.querySelectorAll('tr')) {"
            + "  const cb = tr.querySelector(\"input[type=checkbox][id*='ch_Select']\");"
            + "  if (!cb) continue;"
            + "  const cells = [...tr.cells].map(cell);"
            + "  rows.push({cb: cb.id, cells: cells, system: appIdx >= 0 ? (cells[appIdx] || '') : '',"
            + "             period: perIdx >= 0 ? (cells[perIdx] || '') : ''}); }"
            + "return {headers: head, app_col: appIdx, period_col: perIdx, rows: rows}; })()")


def js_tick_ids(grid_selector, ids, check_all_selector):
    """Tick EXACTLY these check-box ids (a manual tick - the server reads the posted boxes), untick every other row box
    and the select-all box, then return how many row boxes are ticked."""
    return ("(() => {"
            + "const grid = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "if (!grid) return -1;"
            + "const want = new Set(" + json.dumps(list(ids)) + ");"
            + "const boxes = [...grid.querySelectorAll(\"input[type=checkbox][id*='ch_Select']\")];"
            + "for (const b of boxes) b.checked = want.has(b.id);"
            + "const all = document.querySelector(" + json.dumps(check_all_selector) + ");"
            + "if (all) all.checked = false;"
            + "return boxes.filter(b => b.checked).length; })()")


JS_ARM_DIALOGS = ("(() => { window.confirm = () => true; window.alert = () => {}; window.prompt = () => '';"
                  " return true; })()")


# --------------------------------------------------------------------------- browser orchestration
def open_browser(ctx, cfg, headless):
    bcfg = {"browser_executable": cfg.get("browser_executable") or "",
            "cdp_port": cfg.get("cdp_port") or 0, "profile_dir": cfg.get("profile_dir") or ""}
    return cdp.CdpBrowser(bcfg, log=ctx.log, headless=headless, profile_leaf="assign_profile")


def _settle(browser, cfg, ms=None):
    """Let an ASP.NET post-back / DataTables reload finish: a short pause, the document ready, the overlay gone."""
    S._sleep_ms(POSTBACK_MS if ms is None else ms)
    with suppress(Exception):
        browser.wait_ready(int(cfg["timeout_seconds"]))
    _wait_js(browser, "!(" + js_busy(cfg["selectors_assign"]["processing"]) + ")", 15)


def _wait_js(browser, expr, timeout_s, step_ms=300):
    """Re-run ``expr`` until it is truthy or ``timeout_s`` passes. Returns the last value (falsy on timeout)."""
    loops = max(1, int(timeout_s * 1000 / step_ms))
    value = None
    for _ in range(loops):
        with suppress(Exception):
            value = browser.eval(expr)
            if value:
                return value
        S._sleep_ms(step_ms)
    return value


def _login_and_open(ctx, browser, cfg, username, password):
    """Log in, enter the supervisor package by NAME, and open the assign-new-requests page."""
    timeout_s = int(cfg["timeout_seconds"])
    base = cfg["base_url"].rstrip("/")
    sel = cfg.get("selectors") or {}
    sa = cfg["selectors_assign"]
    login_marker = S.url_marker(cfg["login_path"], "login.aspx", keep_extension=True)

    browser.navigate(base + cfg["login_path"], timeout=timeout_s)
    browser.eval(S.js_fill_login(sel.get("username"), sel.get("password"), username, password))
    if sel.get("login_button"):
        S._click(browser, S.js_click_selector(sel["login_button"]))
    elif not S._click(browser, S.js_click_text(cfg["login_button_text"])):
        S._press_enter(browser)
    if browser.wait_url(lambda u: login_marker not in u.lower(), timeout=timeout_s) is None:
        raise S.LoginError("Login failed - still on the login page. Check the portal user and password you typed.")
    browser.wait_ready(timeout_s)
    ctx.log("login OK")

    package = cfg.get("package_text") or ""
    if package:
        S._sleep_ms(SETTLE_MS)
        pkg = browser.eval(S.js_open_package(package, cfg.get("package_link_text")))
        if not isinstance(pkg, dict):
            held = browser.eval(js_landing_packages()) or []
            raise AssignError(f"Your portal account does not hold the «{package}» package, which the assign page lives "
                              f"in. Packages found: {' | '.join(held) or 'none'}. Run with an account that has it, or "
                              f"set the package name in System › Settings › Ticketing auto-assign.")
        browser.click_at(pkg["x"], pkg["y"])
        try:
            browser.attach((cfg.get("app_url_fragment") or "").lower(), timeout=timeout_s)
        except cdp.CdpError:
            ctx.log("the package did not open a recognisable new window - continuing in the current one")
        browser.wait_ready(timeout_s)
        ctx.log(f"entered the «{package}» package")

    S._sleep_ms(SETTLE_MS)
    opened = S._click(browser, S.js_click_selector(cfg["assign_link"])) if cfg.get("assign_link") else False
    if not opened and cfg.get("assign_menu_text"):
        opened = S._click(browser, S.js_click_text(cfg["assign_menu_text"]))
    if not opened and cfg.get("assign_path"):
        browser.navigate(base + cfg["assign_path"], timeout=timeout_s)
        opened = True
    if not opened:
        raise AssignError(f"Could not find the «{cfg.get('assign_menu_text')}» menu link on "
                          f"{S.scrub(browser.current_url(), [password])}.")
    _settle(browser, cfg, SETTLE_MS)
    if not _wait_js(browser, js_exists(sa["form_type"]) + " || " + js_exists(sa["grid"]), timeout_s):
        raise AssignError("The assign page did not open (no «نوع النموذج» list and no request grid) - ended on "
                          + S.scrub(browser.current_url(), [password]))
    ctx.log("assign page open")


def _form_types(ctx, browser, cfg):
    """The «نوع النموذج» values to run: the ones set in Settings, else every real option the page offers."""
    configured = [str(t).strip() for t in (cfg.get("form_types") or []) if str(t).strip()]
    options = browser.eval(js_select_options(cfg["selectors_assign"]["form_type"])) if browser else None
    labels = {}
    real = []
    for o in options or []:
        value, text = o.get("value", ""), o.get("text", "")
        if value in PLACEHOLDER_VALUES or "اختر" in S.norm(text) or "select" in text.lower():
            continue
        labels[value] = text
        real.append(value)
    chosen = configured or real or [""]
    ctx.log("form types: " + ", ".join(f"{v} ({labels.get(v, '?')})" if v else "(page default)" for v in chosen))
    return chosen[:MAX_FORM_TYPES], labels


def _read_grid(browser, cfg):
    sa = cfg["selectors_assign"]
    info = browser.eval(js_grid_rows(sa["grid"], cfg["app_header"], cfg["period_header"]))
    if not isinstance(info, dict) or info.get("error"):
        return [], {}
    missing = info.get("period_col", -1) < 0
    rows = [{"id": row_sig(r.get("cells") or []), "cb": r.get("cb"), "system": r.get("system") or "",
             "period": r.get("period") or "", "period_missing": missing} for r in info.get("rows") or []]
    return rows, info


def _show_all_and_read(browser, cfg):
    """Switch the grid to "show all" (an ASP.NET post-back resets it to its first page, so this is redone after every
    post-back - as the stand-alone bot does) and read it. Returns (rows, info, how)."""
    sa = cfg["selectors_assign"]
    how = browser.eval(js_show_all(sa["length"], sa["grid"]))
    if how:
        _settle(browser, cfg)
    rows, info = _read_grid(browser, cfg)
    return rows, info, how


def _search_form(ctx, browser, cfg, form_type, notes=None):
    """Select the form type, Search, show every row, and read the grid. Returns the rows ([{id, cb, system, period}]).
    ``notes`` (a per-run set) makes each grid warning appear once per run."""
    notes = notes if notes is not None else set()

    def note(key, text):
        if key not in notes:
            notes.add(key)
            ctx.log(text)
    sa = cfg["selectors_assign"]
    _check_session(browser, "before searching")
    if form_type and browser.eval(js_select_value(sa["form_type"], form_type)) is None:
        ctx.log(f"form type {form_type} is not offered on the assign page - skipped")
        return []
    if form_type:
        _settle(browser, cfg)
    if not S._click(browser, S.js_click_selector(sa["search_button"])):
        S._click(browser, S.js_click_text(cfg["search_button_text"]))
    _settle(browser, cfg, SETTLE_MS)
    _check_session(browser, "while searching")                 # a login page is not an empty grid
    rows, info, how = _show_all_and_read(browser, cfg)
    if rows and info.get("app_col", -1) < 0:
        note("app", f"warning: no «{cfg['app_header']}» column on the grid, so rules limited to applications cannot "
                    f"match - columns seen: {', '.join(h for h in info.get('headers') or [] if h)}")
    if rows and cfg.get("assign_filter") != "all" and info.get("period_col", -1) < 0:
        note("period", f"warning: no «{cfg['period_header']}» period column on the grid, so a request's start date "
                       f"cannot be checked and NOTHING is assigned (like the stand-alone bot). Fix the period column "
                       f"name in System › Settings, or choose 'Every request the search returns'.")
    if not how and rows:
        note("length", "note: could not switch the grid to 'show all' - only the rows on its first page are read")
    return rows


def _assign_group(ctx, browser, cfg, group, form_type, offered_logged, notes=None):
    """Decision -> employee -> re-read -> tick exactly this employee's rows -> verify the count -> Execute once ->
    verify by the grid shrinking. Returns the number committed."""
    sa = cfg["selectors_assign"]
    who = f"{group.get('name') or '?'} ({group['portal_id']})"
    _check_session(browser, "before choosing the decision")
    if browser.eval(js_select_value(sa["decision"], cfg["decision_value"])) is None:
        raise AssignError(f"The decision value {cfg['decision_value']} («اسناد لمستخدم السرية») is not offered.")
    _settle(browser, cfg)
    _check_session(browser, "after choosing the decision")
    wait_s = int(cfg.get("employee_wait_seconds") or 8)
    options = _wait_js(browser, "(" + js_select_options(sa["employee"]) + " || []).filter(o => o.value).length ? "
                       + js_select_options(sa["employee"]) + " : null", wait_s) or []
    offered = {o["value"]: o["text"] for o in options if o.get("value") and o["value"] not in PLACEHOLDER_VALUES}
    if not offered_logged.get(form_type):
        offered_logged[form_type] = True
        ctx.log("offered employees: " + (", ".join(f"{v} - {t}" for v, t in offered.items()) or "none"))
    if group["portal_id"] not in offered:
        ctx.log(f"{who} is not in «الموظفين» for this form type - skipped (set the right 'Number in the security "
                f"portal' in Admin › Users; the offered values are listed above)")
        return 0
    browser.eval(js_select_value(sa["employee"], group["portal_id"]))
    _settle(browser, cfg)
    _check_session(browser, "after choosing the employee")

    rows, _info, _how = _show_all_and_read(browser, cfg)      # the post-backs re-rendered the grid: show all, re-read
    want, ids = set(group["ids"]), []
    for r in rows:
        if r["id"] in want and r["cb"]:
            ids.append(r["cb"])
            want.discard(r["id"])                              # one box per planned request
    if not ids:
        ctx.log(f"{who}: none of the {len(group['ids'])} planned request(s) is on the grid any more - skipped")
        return 0
    ticked = browser.eval(js_tick_ids(sa["grid"], ids, sa["check_all"]))
    if ticked != len(ids):
        ctx.log(f"{who}: selection mismatch - {ticked} ticked but {len(ids)} intended; Execute NOT pressed")
        return 0
    if ctx.dry_run:
        ctx.log(f"DRY-RUN: would assign {len(ids)} request(s) to {who}")
        return len(ids)

    before = len(rows)
    browser.eval(JS_ARM_DIALOGS)                               # accept an "are you sure?" the button may raise
    # Execute fires EXACTLY once: after the click the assignment may already be committed, so nothing may re-click.
    if not S._click(browser, S.js_click_selector(sa["execute_button"])):
        if not S._click(browser, S.js_click_text(cfg["execute_button_text"])):
            raise AssignError("The Execute (تنفيذ) button was not found.")
    ctx.log(f"Execute pressed: {len(ids)} request(s) -> {who}")
    _settle(browser, cfg, SETTLE_MS)
    committed = len(ids)
    if browser.eval(JS_ON_LOGIN):                              # Execute was sent, its answer never seen
        raise SessionEnded(f"The portal session ended right after Execute for {who} - check on the portal whether "
                           f"those {len(ids)} request(s) were assigned.")
    said = _success_text(browser, cfg)
    if cfg.get("execute_success_text") and S.norm(cfg["execute_success_text"]) in S.norm(said):
        ctx.log(f"confirmed by the portal: {said} - {len(ids)} request(s) -> {who}")
        return committed                                       # the portal's word; the queue may already have grown
    with suppress(Exception):
        after = len(_search_form(ctx, browser, cfg, form_type, notes))   # a fresh search: assigned rows are gone
        dropped = before - after
        if dropped >= len(ids):
            ctx.log(f"verified: {len(ids)} assigned to {who} (grid {before} -> {after})")
        elif dropped > 0:
            committed = dropped
            ctx.log(f"partly verified: grid {before} -> {after} for {len(ids)} ticked (new arrivals can mask this)")
        else:
            committed = 0
            ctx.log(f"warning: Execute may NOT have committed for {who} - the grid did not shrink "
                    f"({before} -> {after}); check the portal")
    return committed


# --------------------------------------------------------------------------- planning (pure, across the run's teams)
def plan_run(ctx, requests):
    """Combine the manager's team(s) into one plan over the grid's ``requests`` ([{id, system}]). Returns the plan and
    a per-team tally."""
    teams = (ctx.options or {}).get("teams") or {}
    now = ctx.now()
    canon = application_names(ctx)
    load, plan, per_team = {}, [], {}
    for team, info in teams.items():
        taken = {p["id"] for p in plan}
        picks = auto_assign.plan_ticketing(info.get("block") or {}, [r for r in requests if r["id"] not in taken],
                                           now, info.get("members") or {}, load, canon=canon)
        for item in picks:
            load[item["portal_id"]] = load.get(item["portal_id"], 0) + 1
        plan.extend(picks)
        per_team[team] = len(picks)
    return plan, per_team


def application_names(ctx):
    """Application name -> the name the rules use: the e-mail classification's aliases (built-in + the administrator's),
    so the portal's 'Flex Cube' / 'صناديق الإستثمار FCIS' and a rule's 'Flexcube' are the same application."""
    return email_rules.build_rules((ctx.options or {}).get("email_settings") or {}).canonical_application


def why_nothing_planned(ctx, requests):
    """One line on why no request was planned - so an 'assigned 0' run explains itself."""
    now = ctx.now()
    systems = sorted({r.get("system") or "?" for r in requests})
    out = []
    for team, info in ((ctx.options or {}).get("teams") or {}).items():
        block, members = info.get("block") or {}, info.get("members") or {}
        rules = auto_assign.active_rules(block, now)
        if not block.get("enabled"):
            out.append(f"{team}: ticketing auto-assign is off")
        elif not rules:
            out.append(f"{team}: no rule is active at {now:%H:%M} (check the rules' time windows)")
        elif not any(str((members.get(overlay.normalize_email(e)) or {}).get("portal_id") or "").strip()
                     for r in rules for e in r.get("employees") or []):
            out.append(f"{team}: the employees in the active rules have no 'Number in the security portal'")
        else:
            out.append(f"{team}: no active rule covers these applications: {', '.join(systems[:12])}")
    return "; ".join(out) or "no team was given"


# --------------------------------------------------------------------------- entry point
def run(ctx, scraper=None, assigner=None):
    """One ticketing auto-assign pass. ``scraper(ctx, browser, form_type) -> [{id, system, period}]`` and
    ``assigner(ctx, browser, group) -> committed`` replace the real browser in tests."""
    cfg = _config(ctx)
    opts = ctx.options or {}
    username = str(opts.get("username") or "").strip()
    password = opts.get("password") or ""
    teams = opts.get("teams") or {}
    if not teams:
        return _fail(ctx, "No team with ticketing auto-assign enabled was given for this run.")
    live = scraper is None or assigner is None
    if live and (not username or not password):
        return _fail(ctx, "Type the portal username and password to run the ticketing auto-assign.", "auth")
    secrets = [password] if password else []
    cutoff = resolve_cutoff(cfg.get("assign_cutoff"), ctx.now().date())
    mode = "all" if cfg.get("assign_filter") == "all" else "date"

    browser = None
    assigned, per_employee, per_team_total, found = 0, {}, {}, 0
    try:
        if live:
            browser = open_browser(ctx, cfg, headless=not ctx.headed)
            _login_and_open(ctx, browser, cfg, username, password)
            form_types, labels = _form_types(ctx, browser, cfg)
        else:
            form_types = [str(t) for t in cfg.get("form_types") or []] or [""]
            labels = {}
        offered_logged, notes = {}, set()
        for form_type in form_types:
            name = f"form {form_type} ({labels[form_type]})" if form_type in labels else (
                f"form {form_type}" if form_type else "the grid")
            rows = scraper(ctx, browser, form_type) if scraper else _search_form(ctx, browser, cfg, form_type, notes)
            found += len(rows)
            if not rows:
                ctx.log(f"{name}: no new requests")
                continue
            eligible, stats = classify(rows, cutoff, mode)
            skipped = [f"{stats[k]} {label}" for k, label in (("future", "future-dated"),
                                                              ("unreadable", "with an unreadable start date"),
                                                              ("no_period", "whose start date cannot be read"))
                       if stats[k]]
            ctx.log(f"{name}: {stats['total']} new request(s), {stats['eligible']} to assign"
                    + (f" (skipped {', '.join(skipped)})" if skipped else ""))
            if not eligible:
                ctx.log(f"{name}: nothing to assign - Execute not pressed")
                continue
            plan, per_team = plan_run(ctx, eligible)
            for team, n in per_team.items():
                per_team_total[team] = per_team_total.get(team, 0) + n
            if not plan:
                ctx.log(f"{name}: nothing planned - {why_nothing_planned(ctx, eligible)}")
                continue
            groups = auto_assign.group_by_employee(plan)
            ctx.log(f"{name}: planned " + ", ".join(f"{len(g['ids'])} -> {g.get('name') or g['portal_id']}"
                                                    for g in groups))
            for group in groups:
                got = (assigner(ctx, browser, group) if assigner
                       else _assign_group(ctx, browser, cfg, group, form_type, offered_logged, notes))
                assigned += got
                if got:
                    label = group.get("name") or group["portal_id"]
                    per_employee[label] = per_employee.get(label, 0) + got
    except S.LoginError as exc:
        return _fail(ctx, S.clean_error(exc, secrets), "auth", partial(assigned, found, per_employee, per_team_total))
    except Exception as exc:                                     # noqa: BLE001
        if ctx.debug:
            import traceback
            ctx.log(S.scrub(traceback.format_exc(), secrets))
        return _fail(ctx, S.clean_error(exc, secrets), None, partial(assigned, found, per_employee, per_team_total))
    finally:
        if browser is not None:
            with suppress(Exception):
                browser.close()

    summary = (f"assigned {assigned} of {found} new request(s)" + (" (dry-run)" if ctx.dry_run else "")
               + (" - " + ", ".join(f"{k}: {v}" for k, v in sorted(per_employee.items())) if per_employee else ""))
    ctx.log(summary)
    return JobResult(ok=True, summary=summary,
                     snapshot_data={"assigned": assigned, "found": found, "by_employee": per_employee,
                                    "by_team": per_team_total, "dry_run": bool(ctx.dry_run)})


def partial(assigned, found, per_employee, per_team):
    """What a run that stopped half-way had already done (the requests an Execute committed stay assigned)."""
    if not assigned:
        return None
    return {"assigned": assigned, "found": found, "by_employee": per_employee, "by_team": per_team, "partial": True}


def _fail(ctx, message, kind=None, done=None):
    if done:
        message += f" Already assigned before it stopped: {done['assigned']}."
    ctx.log(f"Ticketing auto-assign failed: {message}")
    return JobResult(ok=False, error=message, error_kind=kind, snapshot_data=done)
