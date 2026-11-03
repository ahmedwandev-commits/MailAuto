"""The portal's two languages. English is how every page is written; Arabic is applied in the browser by static/i18n.js
from the dictionary static/i18n-ar.js, and the page is laid out right to left. Each person's choice is kept on their
account (``lang``); before signing in it comes from a cookie set by the switch on the sign-in page."""
from flask import g, request

LANGS = ("en", "ar")
COOKIE = "ias-lang"
COOKIE_AGE = 400 * 24 * 3600


def current_lang():
    user = g.get("user")
    lang = (user or {}).get("lang") or request.cookies.get(COOKIE)
    return lang if lang in LANGS else "en"


def template_globals():
    lang = current_lang()
    return {"lang": lang, "text_dir": "rtl" if lang == "ar" else "ltr", "other_lang": "en" if lang == "ar" else "ar"}
