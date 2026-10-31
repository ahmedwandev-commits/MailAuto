"""Match the first-name style labels of the shift schedule ("محمد ناصر") to HR employee records.

Order of trust: saved alias (a human confirmed it before) > unique exact token match > everything else, which
always needs a human confirmation, except an Arabic name of two or more words that sounds like exactly one English-named employee
(«محمد صلاح» = "Mohamed Salah ...", shown as such in the preview and the name mapping).
"""
from difflib import SequenceMatcher

from .textnorm import tokens


def build_index(employees):
    """employees: iterable of {'id', 'name', ...} -> list of {'id', 'name', 'tokens'}."""
    return [{"id": e["id"], "name": e["name"], "tokens": tokens(e["name"])} for e in employees]


_AR = {"ب": "b", "ت": "t", "ث": "t", "ج": "j", "ح": "h", "خ": "K", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "س": "s", "ش": "X", "ص": "s", "ض": "d",
       "ط": "t", "ظ": "z", "غ": "j", "ف": "f", "ق": "k", "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h"}       # the vowels (ا و ي ى ع ء ة) are dropped
_LAT = (("kh", "K"), ("sh", "X"), ("th", "t"), ("gh", "j"), ("ph", "f"), ("ch", "X"), ("ck", "k"))


def sound(token):
    """A rough consonant skeleton that is the same for «صلاح» and «Salah», «مصطفي» and «Moustafa», «حمدي» and «Hamdy» - used to match a schedule
    name written in Arabic to an employee whose name is stored in English."""
    if any("؀" <= ch <= "ۿ" for ch in token):
        out = "".join(_AR.get(ch, "") for ch in token)
    else:
        t = token.lower()
        for a, b in _LAT:
            t = t.replace(a, b)
        out = "".join({"c": "k", "q": "k", "g": "j", "v": "f", "p": "b", "x": "ks"}.get(ch, ch) for ch in t if ch.isalpha() and ch not in "aeiouyw")
    return "".join(ch for i, ch in enumerate(out) if i == 0 or ch != out[i - 1])


def _by_sound(q, index):
    """Employees whose name has a token that sounds like every token of the query (an Arabic name for an English-named employee)."""
    sq = [sound(t) for t in q]
    if any(len(s) < 2 for s in sq):
        return []
    latin = lambda t: not any("؀" <= ch <= "ۿ" for ch in t)         # noqa: E731 - only a name written in English (a typo between two Arabic spellings is a human's call)
    return [e for e in index if all(any(latin(t) and sound(t) == s for t in e["tokens"]) for s in sq)]


def _tok_score(a, b):
    if a == b:
        return 1.0
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return 0.85
    return SequenceMatcher(None, a, b).ratio()


def _score(query, emp_tokens):
    """Mean of the best score of every query token; also the weakest one."""
    best = [max((_tok_score(q, t) for t in emp_tokens), default=0.0) for q in query]
    return (sum(best) / len(best), min(best)) if best else (0.0, 0.0)


def match_name(name_norm, index, aliases=None):
    """-> {'status': alias|auto|ambiguous|suggested|unmatched, 'employee_id': int|None, 'candidates': [...]}"""
    aliases = aliases or {}
    if name_norm in aliases:
        return {"status": "alias", "employee_id": aliases[name_norm], "candidates": []}
    q = tokens(name_norm)
    if not q:
        return {"status": "unmatched", "employee_id": None, "candidates": []}
    exact = [e for e in index if all(t in e["tokens"] for t in q)]
    if len(exact) == 1:
        return {"status": "auto", "employee_id": exact[0]["id"], "candidates": []}
    if len(exact) > 1:
        # "عمر احمد" also occurs inside "عبدالله احمد نورالدين احمد عمر": a name that STARTS with the schedule label wins
        # only when exactly one does (HR names begin with the first name).
        prefix = [e for e in exact if e["tokens"][:len(q)] == q]
        if len(prefix) == 1:
            return {"status": "auto", "employee_id": prefix[0]["id"], "candidates": []}
        return {"status": "ambiguous", "employee_id": None,
                "candidates": [{"id": e["id"], "name": e["name"], "score": 1.0} for e in exact[:6]]}
    heard = _by_sound(q, index)                                             # «محمد صلاح» -> "Mohamed Salah Abdelmonem"
    if len(heard) == 1 and len(q) >= 2:
        return {"status": "auto", "employee_id": heard[0]["id"], "via": "sound", "candidates": []}
    if heard:
        return {"status": "suggested" if len(q) < 2 else "ambiguous", "employee_id": None,
                "candidates": [{"id": e["id"], "name": e["name"], "score": 0.9} for e in heard[:6]]}
    scored = []
    for e in index:
        mean, worst = _score(q, e["tokens"])
        if worst >= 0.8:
            scored.append((mean, e))
    scored.sort(key=lambda x: -x[0])
    if scored:
        return {"status": "suggested", "employee_id": None,
                "candidates": [{"id": e["id"], "name": e["name"], "score": round(s, 2)} for s, e in scored[:4]]}
    return {"status": "unmatched", "employee_id": None, "candidates": []}


def match_all(parsed_entries, employees, aliases):
    """Unique names from a parsed schedule -> {name_norm: match result + 'display' + 'count'}."""
    index = build_index(employees)
    out = {}
    for e in parsed_entries:
        n = e["name_norm"]
        if n not in out:
            out[n] = {**match_name(n, index, aliases), "display": e["name_raw"], "count": 0}
        out[n]["count"] += 1
    return out
