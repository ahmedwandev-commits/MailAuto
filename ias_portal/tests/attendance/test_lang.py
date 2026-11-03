"""The عربي / English switch: kept on the account, right to left in Arabic, the dictionary loaded only then."""
from .support import login


def _switch(b, lang, nxt="/attendance/planning/day"):
    return b.client.post("/account/lang", data={"lang": lang, "next": nxt, "csrf_token": b.csrf})


def test_the_choice_is_kept_on_the_account(app, world, repo, admin_b):
    page = admin_b.get("/planning/day").get_data(as_text=True)
    assert '<html lang="en" dir="ltr"' in page and "i18n-ar.js" not in page and 'class="lang-toggle"' in page
    r = _switch(admin_b, "ar")
    assert r.status_code == 302 and r.headers["Location"].endswith("/attendance/planning/day")
    assert repo.directory.store.get_user("admin")["lang"] == "ar"
    page = admin_b.get("/planning/day").get_data(as_text=True)
    assert '<html lang="ar" dir="rtl" class="i18n-pending"' in page and "i18n-ar.js" in page and ">English<" in page
    again = login(app, "admin").get("/planning/day").get_data(as_text=True)      # another sign-in: still Arabic
    assert 'lang="ar"' in again
    _switch(admin_b, "en")
    assert repo.directory.store.get_user("admin")["lang"] == "en"


def test_the_switch_is_safe(app, world, repo, admin_b):
    assert _switch(admin_b, "ar", "//evil.example/x").headers["Location"].endswith("/")      # never another site
    assert _switch(admin_b, "fr").status_code == 302 and repo.directory.store.get_user("admin")["lang"] == "ar"
    assert admin_b.client.post("/account/lang", data={"lang": "en"}).status_code == 400            # CSRF checked


def test_the_sign_in_page_follows_the_cookie(app):
    c = app.test_client()
    assert 'lang="en"' in c.get("/login").get_data(as_text=True)
    c.set_cookie("ias-lang", "ar")
    page = c.get("/login").get_data(as_text=True)
    assert 'lang="ar" dir="rtl"' in page and ">English<" in page
