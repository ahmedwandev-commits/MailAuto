"""Arabic/English text normalisation.

Screens in the Security Code Request portal mix Arabic spellings
(e.g. "الوظيفى" vs "الوظيفي", "إعادة" vs "اعادة", "لدية" vs "لديه").
Every comparison the bot makes goes through ``norm`` so that small
spelling differences never break matching.

The exact same algorithm is mirrored in JavaScript (``JS_NORM``) so that
the browser-side element search behaves identically.
"""
from __future__ import annotations

import re
import unicodedata

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_BIDI = re.compile(r"[‎‏‪-‮⁦-⁩]")
_ALEF = re.compile(r"[إأآٱ]")
_WS = re.compile(r"\s+")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm(value) -> str:
    """Return a normalised, lower-cased, whitespace-collapsed string."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = _DIACRITICS.sub("", s)
    s = s.replace("ـ", "")  # tatweel
    s = _ALEF.sub("ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    s = _BIDI.sub("", s)
    s = s.translate(_DIGITS)
    s = _WS.sub(" ", s).strip().lower()
    return s


_COMPACT = re.compile(r"[\s\-–—_/\\.:*]+")


def compact(value) -> str:
    """``norm`` plus removal of spaces, dashes, slashes, dots, colons and '*'
    so that "الفرع / الادارة" == "الفرع/الإدارة" and "الأسباب *" starts with
    "الأسباب"."""
    return _COMPACT.sub("", norm(value))


def match_score(candidate: str, target: str, allow_contains: bool = True) -> int:
    """3 = exact, 2 = starts-with, 1 = contains, 0 = no match (normalised)."""
    c, t = compact(candidate), compact(target)
    if not t:
        return 0
    if c == t:
        return 3
    if c.startswith(t):
        return 2
    if allow_contains and t in c:
        return 1
    return 0


def best_match(candidates, target, key=lambda x: x, allow_contains: bool = True):
    """Pick the candidate that best matches *target* (exact > prefix > contains,
    ties broken by the shortest text). Returns None when nothing matches."""
    best, best_rank = None, None
    for item in candidates:
        text = key(item)
        score = match_score(text, target, allow_contains)
        if score == 0:
            continue
        rank = (score, -len(compact(text)))
        if best_rank is None or rank > best_rank:
            best, best_rank = item, rank
    return best


# JavaScript twin of ``norm`` (keep in sync with the Python version above).
JS_NORM = r"""
(s) => (s == null ? '' : String(s)).normalize('NFKC')
  .replace(/[ؐ-ًؚ-ٰٟۖ-ۭ]/g, '')
  .replace(/ـ/g, '')
  .replace(/[إأآٱ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي')
  .replace(/ؤ/g, 'و').replace(/ئ/g, 'ي')
  .replace(/[‎‏‪-‮⁦-⁩]/g, '')
  .replace(/[٠-٩]/g, d => String(d.charCodeAt(0) - 0x660))
  .replace(/[۰-۹]/g, d => String(d.charCodeAt(0) - 0x6F0))
  .replace(/\s+/g, ' ').trim().toLowerCase()
"""

# JavaScript twin of ``compact``.
JS_COMPACT_SUFFIX = r""".replace(/[\s\-–—_\/\\.:*]+/g, '')"""
