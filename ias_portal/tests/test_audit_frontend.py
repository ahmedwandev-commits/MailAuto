"""Front-end audit regressions: static contracts between the templates, the page scripts and the backend routes, plus
the strict-CSP rules (no inline script / handler / style). Pure file + url_map checks - no browser needed."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "program" / "calllog"
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
PAGE_JS = sorted(p for p in STATIC.glob("*.js") if p.name != "i18n-ar.js")
TOP_TEMPLATES = sorted(TEMPLATES.glob("*.html"))


def _read(path):
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ strict CSP
@pytest.mark.parametrize("tpl", TOP_TEMPLATES, ids=lambda p: p.name)
def test_templates_have_no_inline_script_handler_or_style(tpl):
    html = _read(tpl)
    inline_scripts = [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m]
    assert not inline_scripts, f"inline <script> in {tpl.name}"
    assert not re.search(r"\son[a-z]+\s*=", html), f"inline event handler in {tpl.name}"
    assert not re.search(r"\sstyle\s*=", html), f"inline style attribute in {tpl.name}"


@pytest.mark.parametrize("js", PAGE_JS, ids=lambda p: p.name)
def test_scripts_never_use_html_injection_or_inline_style(js):
    src = _read(js)
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)                    # the header comments name the rules
    for bad in (".innerHTML", "insertAdjacentHTML", "outerHTML =", "document.write(", "eval(", "new Function(",
                'setAttribute("style"', "setAttribute('style'", ".cssText"):
        assert bad not in code, f"{js.name} uses {bad}"


def test_rendered_pages_carry_no_inline_script(admin):
    for url in ("/admin", "/hub", "/email", "/sla", "/itsm", "/system", "/analysis", "/auto-assign", "/account/picture"):
        resp = admin.get(url)
        assert resp.status_code == 200, url
        html = resp.get_data(as_text=True)
        assert not [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m], url
        assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


# ------------------------------------------------------------------------------------- element ids JS <-> HTML
def test_element_ids_used_by_scripts_exist_in_their_templates():
    shared = "".join(_read(TEMPLATES / n) for n in ("base.html", "_topbar.html", "_lang_toggle.html", "_theme_toggle.html"))
    problems = []
    for tpl in TOP_TEMPLATES:
        html = _read(tpl)
        for name in re.findall(r"filename='([\w.-]+\.js)'", html):
            path = STATIC / name
            if not path.is_file() or name == "i18n-ar.js":
                continue
            src = _read(path)
            ids = set(re.findall(r"getElementById\(\s*[\"']([\w-]+)[\"']\s*\)(?!\s*\|\|)", src))   # `a || b` = a fallback
            ids |= set(re.findall(r"""\$\(\s*["'`]#([\w-]+)["'`]\s*\)""", src))
            for i in ids:
                created = re.search(r"""id:\s*["'`]%s["'`]""" % re.escape(i), src)
                if not created and not re.search(r"""id=["']%s["']""" % re.escape(i), html + shared):
                    problems.append(f"{name} needs #{i} in {tpl.name}")
    assert not problems, problems


# ------------------------------------------------------------------------------------------ API URLs <-> routes
_CALL = re.compile(r"""\bapi\(\s*["'](GET|POST|PUT|DELETE)["']\s*,\s*([`"'])(/[^`"']*)\2""")
_LOADER = re.compile(r"""url:\s*(?:\(\)\s*=>\s*)?([`"'])(/api/[^`"']*)\1""")
_FETCH = re.compile(r"""fetch\(\s*([`"'])(/api/[^`"']*)\1(?:\s*,\s*\{\s*method:\s*["'](\w+)["'])?""")


def _concrete(url):
    """A template literal as a path the router can match: ${...} -> a sample segment, the query string dropped."""
    url = re.sub(r"(?<=/)\$\{[^}]*\}", "x", url)            # a path segment: /records/${id}
    url = re.sub(r"\$\{[^}]*\}", "", url)                  # glued on: /api/ops/sla${since} = an optional query string
    url = url.split("${")[0]                                  # an optional part (${q ? `?a=${q}` : ""}) cut by the regex
    url = url.split("?")[0]
    return url.rstrip("/") or "/"


def _calls():
    out = []
    for js in PAGE_JS:
        src = _read(js)
        out += [(js.name, m.group(1), m.group(3)) for m in _CALL.finditer(src)]
        out += [(js.name, "GET", m.group(2)) for m in _LOADER.finditer(src)]
        out += [(js.name, (m.group(3) or "GET").upper(), m.group(2)) for m in _FETCH.finditer(src)]
    return out


def test_every_api_call_of_the_pages_hits_a_route_with_that_method(app):
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    adapter = app.url_map.bind("localhost")
    calls = _calls()
    assert len(calls) > 60                                   # the extractor still finds the calls
    problems = []
    for name, method, url in calls:
        path = _concrete(url)
        if path.endswith("/x") and "/api/ops/" in path and path.count("/") == 3:
            path = path[:-2] + "/email"                      # /api/ops/${module} -> one of email|sla|itsm
        try:
            adapter.match(path, method=method)
        except (NotFound, MethodNotAllowed) as exc:
            problems.append(f"{name}: {method} {url} -> {type(exc).__name__}")
    assert not problems, problems


def test_unsafe_requests_send_the_csrf_header():
    """Every hand-written fetch() that changes something sends X-CSRF-Token (CL.api always does)."""
    for js in PAGE_JS:
        src = _read(js)
        for m in re.finditer(r"fetch\(", src):
            call = src[m.start(): m.start() + 500].split(");")[0]          # the whole call, options included
            if re.search(r"method:\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", call):
                assert "X-CSRF-Token" in call, f"{js.name}: {call[:80]}"
    common = _read(STATIC / "common.js")
    assert '"X-CSRF-Token": csrf()' in common


# ---------------------------------------------------------------------------------------- audit fixes (regressions)
def test_no_utc_date_from_toisostring():
    """toISOString() is UTC: in Cairo it gives yesterday's date for the first hours after midnight."""
    for js in PAGE_JS:
        assert not re.search(r"toISOString\(\)\.slice\(0,\s*10\)", _read(js)), js.name


def test_tall_dialogs_scroll_instead_of_hiding_their_buttons():
    """The Edit call dialog has no inner scroll (its branch list must not be clipped): on a short window the dialog
    itself has to scroll, or Save changes ends up below the screen, out of reach."""
    css = _read(STATIC / "app.css")
    rule = re.search(r"dialog\.dlg\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"overflow-y:\s*auto", rule)
    assert "scroll: false" in _read(STATIC / "common.js")    # the dialog this protects


@pytest.mark.parametrize("name", ["my.js", "email.js"])
def test_reopen_is_not_offered_for_a_mail_answered_in_outlook(name):
    """The server refuses to reopen a mail the assignee answered in Outlook (409): no Reopen button for done_auto."""
    src = _read(STATIC / name)
    i = src.index('"answered in Outlook"')
    window = src[max(0, i - 400): i + 400]
    assert "done_auto" in window and "Reopen" in window


def test_team_calls_day_buttons_work_before_the_first_answer():
    src = _read(STATIC / "calls-team.js")
    assert "shiftDate(state.date || todayIso(), -1)" in src and "shiftDate(state.date || todayIso(), 1)" in src
