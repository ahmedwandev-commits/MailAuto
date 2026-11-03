"""Arabic-aware text normalisation shared by the parsers and the name matcher."""
import re

_DIACRITICS = re.compile(r"[ً-ْـ]")
_TRANS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})
_PAREN = re.compile(r"\([^)]*\)")


def norm(text) -> str:
    """Normalise a name or label for comparison (not for display)."""
    if text is None:
        return ""
    s = _DIACRITICS.sub("", str(text))
    s = _PAREN.sub(" ", s)
    s = s.translate(_TRANS).lower()
    return re.sub(r"\s+", " ", s).strip()


def norm_keep(text) -> str:
    """Like norm() but keeps the text inside parentheses (used to tell "موقف ( الأذون )" from "موقف ( المهمات )")."""
    if text is None:
        return ""
    s = _DIACRITICS.sub("", str(text)).translate(_TRANS).lower()
    return re.sub(r"\s+", " ", s).strip()


def tokens(text) -> list:
    return [t for t in norm(text).split(" ") if t]


WEEKDAYS = {  # python weekday(): Monday=0
    "الاثنين": 0, "الإثنين": 0, "الثلاثاء": 1, "الاربعاء": 2, "الأربعاء": 2,
    "الخميس": 3, "الجمعة": 4, "السبت": 5, "الاحد": 6, "الأحد": 6,
}
_WEEKDAYS_NORM = {norm(k): v for k, v in WEEKDAYS.items()}


def weekday_from_name(text):
    return _WEEKDAYS_NORM.get(norm(text))


MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "أبريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "اغسطس": 8, "أغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "أكتوبر": 10,
    "نوفمبر": 11, "ديسمبر": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTHS_NORM = {norm(k): v for k, v in MONTHS.items()}


def month_from_text(text):
    n = norm(text)
    for name, num in _MONTHS_NORM.items():
        if name in n:
            return num
    return None
