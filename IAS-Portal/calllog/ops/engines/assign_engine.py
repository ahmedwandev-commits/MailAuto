# -*- coding: utf-8 -*-
"""Ticketing auto-assign: a native CDP RPA that assigns NEW security requests to the team's employees.

This is the executor behind the Auto-Assign page's *ticketing* target. A manager presses "Run now", types THEIR portal
username + password (used for that one run only - never stored), and this engine:

  1. logs in to the SSO portal (the same login the SLA job uses) and opens the "assign new requests"
     («اسناد الطلبات الجديدة») page;
  2. for each configured form type («نوع النموذج») searches + reads the new-request grid;
  3. plans who gets what with ``auto_assign.plan_ticketing`` (the manager's rules: which applications, which employees,
     which daily window - balanced across the chosen employees);
  4. for each employee selects the decision «اسناد لمستخدم السرية» + that employee in «الموظفين», ticks exactly their
     planned rows, and presses Execute (تنفيذ), verifying the commit by the grid shrinking - the same careful order the
     stand-alone NBE auto-authorizer bot proved on the real portal.

It is driven over the DevTools protocol (``calllog.ops.engines.cdp``) - no Playwright, no Node.js, no browser download -
and reuses the SLA engine's login/table-reading helpers so the two stay consistent. The browser work is behind two
injectable seams (``scraper`` / ``assigner``) so the whole orchestration is unit-tested with a fake portal and the pure
planning logic is tested on its own (``tests/test_auto_assign.py``).

The portal element ids (``ddlExecuteStatus`` = the decision, ``ddlEmployee`` = the security user, ``btnSearch`` /
``btnExecute``, the ``GridView_RequestDetails`` grid, the ``ch_Select`` row check-boxes) and the decision value ("6" =
«اسناد لمستخدم السرية») are the ones the stand-alone bot uses; they live in ``settings["assign"]`` so an admin can tune
them from System › Settings without a code change. The exact page path / menu for THIS portal must be confirmed against
the live site on first use (see docs: the defaults follow the known layout).

No secret is ever logged, put in an error text, a file name or a saved page (the SLA engine's ``scrub`` is used).
"""
import hashlib
import time
from contextlib import suppress

from . import cdp, sla_engine as S
from .. import auto_assign
from ..settings import DEFAULTS, deep_merge
from .common import CredentialError, JobResult

SETTLE_MS = 1500
POSTBACK_MS = 1200          # wait after an ASP.NET post-back (form-type / decision / employee change, search, execute)
MAX_FORM_TYPES = 20

# A JavaScript ``norm`` that reproduces common.norm_arabic EXACTLY (Arabic-Indic digits -> ASCII, tatweel + diacritics
# removed, alef/taa-marbuta/alef-maqsura folded, whitespace collapsed, lower-cased) so a row signature computed in the
# page (js_tick_rows) matches one computed in Python (row_sig). Any drift here means a row never gets ticked.
_JS_NORM = r"""
  const _AR = {'٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'};
  const norm = s => (s || '').replace(/[٠-٩]/g, d => _AR[d]).replace(/ـ/g, '')
      .replace(/[ً-ْ]/g, '').replace(/[إأآ]/g, 'ا')
      .replace(/ة/g, 'ه').replace(/ى/g, 'ي').replace(/\s+/g, ' ').trim().toLowerCase();
"""


class AssignError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config
def _config(ctx):
    return deep_merge(DEFAULTS["assign"], ctx.settings or {})


def row_sig(cells):
    """A stable signature of a grid row from its visible cell texts, so a row can be matched again after an ASP.NET
    post-back re-renders the grid. The SAME join is computed in JavaScript when ticking, so the two always agree."""
    raw = "|".join(S.norm(c) for c in cells)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- browser JavaScript
def js_select_value(selector, value):
    """Set a <select> (by CSS selector) to ``value`` and fire input/change so the ASP.NET AutoPostBack runs. Returns the
    value actually selected, or null when the option is not offered (e.g. an employee not valid for this form type)."""
    import json
    return ("(() => {"
            + "const el = document.querySelector(" + json.dumps(selector) + ");"
            + "if (!el) return null;"
            + "const want = " + json.dumps(str(value)) + ";"
            + "const opt = [...el.options].find(o => String(o.value) === want);"
            + "if (!opt) return null;"
            + "el.value = opt.value;"
            + "el.dispatchEvent(new Event('input', {bubbles:true}));"
            + "el.dispatchEvent(new Event('change', {bubbles:true}));"
            + "return el.value; })()")


def js_grid_rows(grid_selector, app_header):
    """Read the request grid: for each row that carries a ``ch_Select`` check-box, return its cell texts and the
    detected application/system (the column whose header contains ``app_header``). The caller hashes the cells into the
    same signature as :func:`row_sig`."""
    import json
    return ("(() => {" + S.JS_NORM
            + "const grid = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "if (!grid) return {error: 'grid-not-found'};"
            + "const cell = n => { if (!n) return ''; const c = n.cloneNode(true);"
            + "  c.querySelectorAll('br').forEach(b => b.replaceWith(' '));"
            + "  c.querySelectorAll('script,style').forEach(s => s.remove());"
            + "  return (c.textContent || '').replace(/\\s+/g,' ').trim(); };"
            + "const head = [...grid.querySelectorAll('thead th, thead td')].map(cell);"
            + "const want = norm(" + json.dumps(app_header) + ");"
            + "let appIdx = head.findIndex(h => norm(h) === want);"
            + "if (appIdx < 0) appIdx = head.findIndex(h => norm(h).includes(want));"
            + "const bodies = grid.tBodies.length ? [...grid.tBodies] : [grid];"
            + "const out = [];"
            + "for (const b of bodies) for (const tr of b.rows) {"
            + "  const cb = tr.querySelector(\"input[type=checkbox][id*='ch_Select']\");"
            + "  if (!cb) continue;"
            + "  const cells = [...tr.cells].map(cell);"
            + "  out.push({cells: cells, system: appIdx >= 0 ? (cells[appIdx] || '') : ''});"
            + "}"
            + "return {rows: out}; })()")


def js_tick_rows(grid_selector, sigs):
    """Tick the ``ch_Select`` box of every row whose signature is in ``sigs`` (and untick the rest), returning how many
    were ticked. The signature is recomputed in JS from the row's cell texts with the same normalisation + join + a
    SHA-256 as :func:`row_sig`, so Python and the page agree on which row is which."""
    import json
    return ("(async () => {" + _JS_NORM
            + "const grid = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "if (!grid) return -1;"
            + "const cell = n => { if (!n) return ''; const c = n.cloneNode(true);"
            + "  c.querySelectorAll('br').forEach(b => b.replaceWith(' '));"
            + "  c.querySelectorAll('script,style').forEach(s => s.remove());"
            + "  return (c.textContent || '').replace(/\\s+/g,' ').trim(); };"
            + "const enc = new TextEncoder();"
            + "const sha = async s => { const h = await crypto.subtle.digest('SHA-256', enc.encode(s));"
            + "  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2,'0')).join('').slice(0,16); };"
            + "const wanted = new Set(" + json.dumps(list(sigs)) + ");"
            + "const bodies = grid.tBodies.length ? [...grid.tBodies] : [grid];"
            + "let n = 0;"
            + "for (const b of bodies) for (const tr of b.rows) {"
            + "  const cb = tr.querySelector(\"input[type=checkbox][id*='ch_Select']\");"
            + "  if (!cb) continue;"
            + "  const sig = await sha([...tr.cells].map(cell).map(norm).join('|'));"
            + "  const on = wanted.has(sig);"
            + "  if (cb.checked !== on) { cb.click(); }"
            + "  if (on) n++;"
            + "}"
            + "return n; })()")


def js_count_rows(grid_selector):
    import json
    return ("(() => { const g = document.querySelector(" + json.dumps(grid_selector) + ");"
            + "return g ? g.querySelectorAll(\"input[type=checkbox][id*='ch_Select']\").length : 0; })()")


# --------------------------------------------------------------------------- browser orchestration
def open_browser(ctx, cfg, headless):
    bcfg = {"browser_executable": cfg.get("browser_executable") or "",
            "cdp_port": cfg.get("cdp_port") or 0, "profile_dir": cfg.get("profile_dir") or ""}
    return cdp.CdpBrowser(bcfg, log=ctx.log, headless=headless, profile_leaf="assign_profile")


def _login_and_open(ctx, browser, cfg, username, password):
    """Log in to the SSO portal and open the assign-new-requests page. Mirrors sla_engine.scrape's login, then either
    navigates to ``assign_path`` directly or (when it is blank) clicks the menu link ``assign_menu_text``."""
    timeout_s = int(cfg["timeout_seconds"])
    base = cfg["base_url"].rstrip("/")
    sel = cfg.get("selectors") or {}
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
    if cfg.get("package_text"):
        pkg = browser.eval(S.js_open_package(cfg["package_text"], cfg.get("package_link_text")))
        if isinstance(pkg, dict):
            browser.click_at(pkg["x"], pkg["y"])
            browser.attach(cfg["app_url_fragment"].lower(), timeout=timeout_s)
    if cfg.get("assign_path"):
        browser.navigate(base + cfg["assign_path"], timeout=timeout_s)
    elif not S._click(browser, S.js_click_text(cfg["assign_menu_text"])):
        raise AssignError(f"Could not open the assign page (menu '{cfg['assign_menu_text']}' not found).")
    browser.wait_ready(timeout_s)


def _search_form(browser, cfg, form_type):
    """Select the form type and search; return the grid's current rows as ``[{sig, system}]``."""
    sel = cfg["selectors_assign"]
    S._sleep_ms(SETTLE_MS)
    if form_type:
        browser.eval(js_select_value(sel["form_type"], form_type))
        S._sleep_ms(POSTBACK_MS)
    if not S._click(browser, S.js_click_selector(sel["search_button"])):
        S._click(browser, S.js_click_text(cfg["search_button_text"]))
    S._sleep_ms(SETTLE_MS)
    browser.wait_ready(int(cfg["timeout_seconds"]))
    S._sleep_ms(POSTBACK_MS)
    return _read_rows(browser, cfg)


def _read_rows(browser, cfg):
    res = browser.eval(js_grid_rows(cfg["selectors_assign"]["grid"], cfg["app_header"]))
    if not isinstance(res, dict) or res.get("error"):
        return []
    out = []
    for r in res.get("rows") or []:
        out.append({"id": row_sig(r.get("cells") or []), "system": r.get("system") or ""})
    return out


def _assign_group(ctx, browser, cfg, group):
    """Select the decision + this employee, tick their planned rows, press Execute, and verify by the grid shrinking.
    Returns the number committed (verified by the drop in row count, else the number ticked)."""
    sel = cfg["selectors_assign"]
    if browser.eval(js_select_value(sel["decision"], cfg["decision_value"])) is None:
        raise AssignError(f"The decision value {cfg['decision_value']} is not offered on the assign page.")
    S._sleep_ms(POSTBACK_MS)
    if browser.eval(js_select_value(sel["employee"], group["portal_id"])) is None:
        ctx.log(f"employee {group['portal_id']} ({group.get('name')}) is not offered for this form type - skipped")
        return 0
    S._sleep_ms(POSTBACK_MS)
    before = browser.eval(js_count_rows(sel["grid"]))
    ticked = browser.eval(js_tick_rows(sel["grid"], group["ids"]))
    if not isinstance(ticked, int) or ticked <= 0:
        ctx.log(f"employee {group['portal_id']}: none of their {len(group['ids'])} planned row(s) are on the grid now - skipped")
        return 0
    if ctx.dry_run:
        ctx.log(f"DRY-RUN: would assign {ticked} request(s) to {group.get('name') or group['portal_id']}")
        return ticked
    # Execute must fire exactly once; after the click the commit may already be server-side, so nothing after may raise.
    S._click(browser, S.js_click_selector(sel["execute_button"])) or S._click(browser, S.js_click_text(cfg["execute_button_text"]))
    S._sleep_ms(SETTLE_MS)
    with suppress(Exception):
        browser.wait_ready(int(cfg["timeout_seconds"]))
    S._sleep_ms(POSTBACK_MS)
    committed = ticked
    with suppress(Exception):
        after = browser.eval(js_count_rows(sel["grid"]))
        if isinstance(before, int) and isinstance(after, int) and before - after > 0:
            committed = min(ticked, before - after)
    ctx.log(f"assigned {committed} request(s) to {group.get('name') or group['portal_id']}")
    return committed


# --------------------------------------------------------------------------- planning (pure, across the run's teams)
def plan_run(ctx, requests):
    """Combine the manager's team(s) into one plan over the grid's ``requests`` ([{id, system}]). Each application
    belongs to one team, so a request is claimed by at most one team's rules. Returns the grouped plan and a per-team
    tally for the report."""
    teams = (ctx.options or {}).get("teams") or {}
    now = ctx.now()
    load, plan, per_team = {}, [], {}
    for team, info in teams.items():
        block = info.get("block") or {}
        members = info.get("members") or {}
        picks = auto_assign.plan_ticketing(block, requests, now, members, load)
        for item in picks:
            load[item["portal_id"]] = load.get(item["portal_id"], 0) + 1
        taken = {p["id"] for p in plan}
        picks = [p for p in picks if p["id"] not in taken]          # a request is assigned once across teams
        plan.extend(picks)
        per_team[team] = len(picks)
    return plan, per_team


# --------------------------------------------------------------------------- entry point
def run(ctx, scraper=None, assigner=None):
    """One ticketing auto-assign pass. ``scraper(ctx, browser, form_type) -> [{id, system}]`` and
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
    form_types = [t for t in (cfg.get("form_types") or [""])][:MAX_FORM_TYPES] or [""]

    browser = None
    assigned, per_employee, per_team_total = 0, {}, {}
    try:
        if live:
            browser = open_browser(ctx, cfg, headless=not ctx.headed)
            _login_and_open(ctx, browser, cfg, username, password)
        for form_type in form_types:
            requests = (scraper(ctx, browser, form_type) if scraper else _search_form(browser, cfg, form_type))
            if not requests:
                continue
            plan, per_team = plan_run(ctx, requests)
            for team, n in per_team.items():
                per_team_total[team] = per_team_total.get(team, 0) + n
            groups = auto_assign.group_by_employee(plan)
            for i, group in enumerate(groups):
                if live and i > 0:
                    _search_form(browser, cfg, form_type)        # Execute removed the last employee's rows: refresh
                got = (assigner(ctx, browser, group) if assigner else _assign_group(ctx, browser, cfg, group))
                assigned += got
                if got:
                    label = group.get("name") or group["portal_id"]
                    per_employee[label] = per_employee.get(label, 0) + got
    except S.LoginError as exc:
        return _fail(ctx, S.clean_error(exc, secrets), "auth")
    except Exception as exc:                                     # noqa: BLE001
        if ctx.debug:
            import traceback
            ctx.log(S.scrub(traceback.format_exc(), secrets))
        return _fail(ctx, S.clean_error(exc, secrets))
    finally:
        if browser is not None:
            with suppress(Exception):
                browser.close()

    summary = (f"assigned {assigned} request(s)" + (" (dry-run)" if ctx.dry_run else "")
               + (" - " + ", ".join(f"{k}: {v}" for k, v in sorted(per_employee.items())) if per_employee else ""))
    ctx.log(summary)
    return JobResult(ok=True, summary=summary,
                     snapshot_data={"assigned": assigned, "by_employee": per_employee, "by_team": per_team_total,
                                    "dry_run": bool(ctx.dry_run)})


def _fail(ctx, message, kind=None):
    ctx.log(f"Ticketing auto-assign failed: {message}")
    return JobResult(ok=False, error=message, error_kind=kind)
