"""The welcome after sign-in (calllog/welcome.py, templates/welcome.html, static/welcome.js): a positive message for
the person's role, shown once per sign-in, different from one person to the next, then the portal opens by itself.
Made-up accounts only."""
import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from calllog import welcome
from conftest import Browser
from test_ops_views import FORBIDDEN_JS, Tab, chromium, server  # noqa: F401

STATIC = Path(__file__).resolve().parent.parent / "calllog" / "static"


def _account(admin, uid, role, name, **extra):
    body = {"user_id": uid, "name": name, "department": "IT", "role": role, "password": "Welcome2026!", **extra}
    assert admin.post("/api/admin/users", body).status_code in (200, 201)


def _signed_in(app, uid, password="Welcome2026!"):
    b = Browser(app)
    resp = b.login(uid, password)
    return b, resp


def test_signing_in_opens_the_welcome_once_with_a_message_for_the_role(app, admin):
    _account(admin, "700501", "manager", "Mariam Test", team="Flex Cube")
    app.extensions["store"].update_user("700501", {"must_change_password": False})   # an account already in use
    b, resp = _signed_in(app, "700501")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/welcome")
    page = b.client.get("/welcome")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'id="welcome"' in html and 'data-next="/"' in html
    assert any(ar in html for ar, _ in welcome.MESSAGES["manager"])
    assert "Mariam" in html and "مدير" in html and "welcome.js" in html
    again = b.client.get("/welcome")                                          # shown once per sign-in
    assert again.status_code == 302 and again.headers["Location"].endswith("/")


def test_a_new_account_changes_its_password_first_then_sees_the_welcome(app, admin):
    _account(admin, "700502", "user", "Omar Test")
    b, resp = _signed_in(app, "700502")
    assert resp.headers["Location"].endswith("/welcome")
    assert b.client.get("/welcome").headers["Location"].endswith("/account/password")
    with b.client.session_transaction() as s:
        token = s["csrf"]
    done = b.client.post("/account/password", data={"current_password": "Welcome2026!", "new_password": "Fresh-Pass-2026x",
                                                      "confirm_password": "Fresh-Pass-2026x", "csrf_token": token})
    assert done.status_code == 302 and done.headers["Location"].endswith("/welcome")
    html = b.client.get("/welcome").get_data(as_text=True)
    assert any(ar in html for ar, _ in welcome.MESSAGES["user"]) and "Omar" in html


def test_colleagues_see_different_messages_and_a_person_a_new_one_on_other_days():
    day = dt.datetime(2026, 10, 25, 9, 0)
    seen = {welcome.message_for({"user_id": f"7006{i:02d}", "role": "user", "name": "Test"}, day)["ar"] for i in range(30)}
    assert len(seen) >= 6                                                     # 8 messages, 30 colleagues
    me = {"user_id": "700601", "role": "user", "name": "Test"}
    over_a_week = {welcome.message_for(me, day + dt.timedelta(days=d))["ar"] for d in range(7)}
    assert len(over_a_week) >= 3
    assert welcome.message_for(me, day) == welcome.message_for(me, day.replace(hour=11))      # the same all day


@pytest.mark.parametrize("role", ["user", "manager", "admin"])
def test_every_role_has_its_own_positive_messages_in_arabic_and_english(role):
    msgs = welcome.MESSAGES[role]
    assert len(msgs) >= 8 and len({ar for ar, _ in msgs}) == len(msgs)
    assert all(re.search(r"[؀-ۿ]", ar) and en and en[0].isupper() for ar, en in msgs)
    other = [m for r, ms in welcome.MESSAGES.items() if r != role for m in ms]
    assert not set(msgs) & set(other)
    hello = welcome.message_for({"user_id": "x", "role": role, "name": "Sara Test"}, dt.datetime(2026, 10, 25, 18, 0))
    assert hello["greeting_en"] == "Good evening" and hello["name"] == "Sara"


def test_an_unknown_role_gets_the_employee_messages_and_a_missing_name_is_fine():
    m = welcome.message_for({"user_id": "x", "role": "auditor"}, dt.datetime(2026, 10, 25, 8, 0))
    assert (m["ar"], m["en"]) in welcome.MESSAGES["user"] and m["name"] == "" and m["greeting_en"] == "Good morning"


def test_the_welcome_follows_the_csp_and_moves_slowly_unless_reduced_motion():
    js = (STATIC / "welcome.js").read_text(encoding="utf-8")
    assert js.startswith('"use strict";')
    for pattern in FORBIDDEN_JS:
        assert not re.search(pattern, js), pattern
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    block = css[css.index("welcome after sign-in"):css.index("/* ---------- log a call")]
    assert "@keyframes welcome-rise" in block and "animation-delay: 2.9s" in block
    assert f"animation: welcome-fill {welcome.SECONDS}s linear" in block                 # the bar = the wait
    assert "prefers-reduced-motion: reduce" in block
    html = (Path(__file__).resolve().parent.parent / "calllog" / "templates" / "welcome.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html and "onclick" not in html


def test_welcome_js_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(STATIC / "welcome.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------------------------- in a real browser
def _sign_in(chromium, server, uid, password, **context):
    return Tab(chromium, server, width=1280, height=800, **context).login(uid, password)


def test_the_message_rises_slowly_then_the_portal_opens_by_itself(chromium, server, admin):
    t = _sign_in(chromium, server, "admin", "AdminPass123")
    page = t.page
    assert page.url.endswith("/welcome")
    page.wait_for_selector(".welcome-ar")
    assert any(ar == page.inner_text(".welcome-ar").strip() for ar, _ in welcome.MESSAGES["admin"])
    assert page.evaluate("getComputedStyle(document.querySelector('.welcome-ar')).animationName") == "welcome-rise"
    page.wait_for_timeout(300)
    assert float(page.evaluate("getComputedStyle(document.querySelector('.welcome-en')).opacity")) < 0.5   # not yet
    page.wait_for_url(re.compile(r"/hub$"), timeout=welcome.SECONDS * 1000 + 5000)
    assert not t.problems, t.problems
    t.close()


def test_continue_opens_the_portal_at_once_and_reduced_motion_keeps_it_still(chromium, server, admin):
    t = _sign_in(chromium, server, "admin", "AdminPass123", reduced_motion="reduce")
    page = t.page
    page.wait_for_selector("#welcomeGo")
    assert page.evaluate("getComputedStyle(document.querySelector('.welcome-ar')).animationName") == "none"
    assert float(page.evaluate("getComputedStyle(document.querySelector('.welcome-en')).opacity")) == 1
    page.click("#welcomeGo")
    page.wait_for_url(re.compile(r"/hub$"), timeout=3000)
    page.goto(f"{server}/welcome")                                            # only once per sign-in
    assert page.url.endswith("/hub")
    assert not t.later_problems(), t.problems
    t.close()
