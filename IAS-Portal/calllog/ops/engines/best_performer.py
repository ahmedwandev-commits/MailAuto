"""The "best_performer" job: the Best Performance e-mail. Once a day (``summary.best_performer.send_times``, 15:30 by
default, on a working day) the day's top 3 (``top``) get a celebration e-mail, with the Cc set in System › Settings ›
Daily summary. It is the only place the best performers are mailed (the daily summary does not list them).

The top 3 are the first of Insights › Analysis' "Best performers" for that day (``performance.people_day``: the day's
score in security requests - executed, not executed or rejected + 2 per call answered + 3 per e-mail replied; ties by
tickets, then calls). The mail goes To the addresses of their accounts; one without an address is still celebrated in
the mail (the result says so); none with one: to the Cc only.

The mail is a celebration, in English: confetti, a trophy, a podium with their names, their numbers of the day and a
word of encouragement - the owner's own lines (``messages``, one a day in turn, ``{names}`` = their names) or a
built-in one, a different one each day. Outlook safe: tables, inline styles, no image but the logo. Never raises."""
from datetime import datetime

from ..settings import DEFAULTS, deep_merge
from .common import FONT_STACK, NBE_GOLD, NBE_GREEN, JobResult, OutlookMailer, esc, outlook_error_text
from . import daily_summary as ds
from . import email_report as er

# a different one each day
MESSAGES = (
    "Today you set the pace for the whole team. Thank you for the effort behind every call, every e-mail and every "
    "request.",
    "Great work is never an accident, and today your work proved it. Keep shining!",
    "Every customer you helped today had a better day thanks to you. That is what being the best looks like.",
    "Commitment, speed and care: today your work brought all three together. We are proud of you!",
    "Today the bar went up thanks to your effort. Tomorrow is a new chance to shine even brighter.",
    "Behind these numbers is real dedication. Thank you for making the team stronger every day.",
    "Champions are made one task at a time, and today nobody did more than you. Bravo!",
    "Your effort today made a real difference. The whole team celebrates with you.",
)
CONFETTI = (("★", NBE_GOLD), ("●", er.GREEN), ("◆", er.BLUE), ("▲", er.RED), ("✦", er.PURPLE), ("●", er.AMBER))
# (medal, colour, podium height in px) of places 1, 2, 3
PLACES = (("&#129351;", NBE_GOLD, 96), ("&#129352;", "#A7B1BC", 72), ("&#129353;", "#B87333", 56))


def names_of(stars):
    """"A", "A and B", "A, B and C"."""
    names = [s["name"] for s in stars]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def message_of(cfg, day, names):
    """The day's word of encouragement: the owner's own lines in turn (``{names}`` / ``{name}`` = their names), else a
    built-in one - a different one each day."""
    n = datetime.strptime(day, "%Y-%m-%d").toordinal()
    own = [str(x).strip() for x in (cfg.get("messages") or []) if str(x).strip()]
    if own:
        return own[n % len(own)].replace("{names}", names).replace("{name}", names)
    return MESSAGES[n % len(MESSAGES)]


def _confetti(count=24):
    cells = "".join(f'<td align="center" style="font-size:17px;line-height:22px;color:{color};font-family:{FONT_STACK};">'
                    f'{symbol}</td>' for symbol, color in (CONFETTI[i % len(CONFETTI)] for i in range(count)))
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            + cells + '</tr></table>')


def _podium(stars):
    """The places side by side, the first in the middle and highest (2 - 1 - 3)."""
    order = [i for i in (1, 0, 2) if i < len(stars)]
    cells = []
    for i in order:
        star, (medal, colour, height) = stars[i], PLACES[i]
        team = esc(star.get("team") or "")
        cells.append(
            f'<td width="33%" valign="bottom" align="center" style="padding:0 6px;font-family:{FONT_STACK};">'
            f'<div style="font-size:34px;line-height:40px;">{medal}</div>'
            f'<div style="font-size:17px;font-weight:700;color:{NBE_GREEN};margin-top:4px;" dir="auto">{esc(star["name"])}</div>'
            + (f'<div style="font-size:12px;color:{er.MUTED};margin-top:2px;" dir="auto">{team}</div>' if team else "")
            + f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin-top:8px;">'
              f'<tr><td height="{height}" valign="top" align="center" bgcolor="{colour}" style="background:{colour};'
              f'height:{height}px;color:#ffffff;font-family:{FONT_STACK};padding-top:10px;">'
              f'<div style="font-size:22px;font-weight:700;">#{i + 1}</div>'
              f'<div style="font-size:12.5px;font-weight:700;">score {star["total"]}</div></td></tr></table></td>')
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin-top:14px;">'
            '<tr>' + "".join(cells) + '</tr></table>')


def build_html(stars, now, words):
    """The Best Performance mail of ``stars`` (the first ``performance.people_day`` rows, best first)."""
    title = "TODAY'S TOP 3" if len(stars) == 3 else f"TODAY'S TOP {len(stars)}" if len(stars) > 1 else "TODAY'S BEST"
    trophy = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin:14px 0 6px 0;">'
        f'<tr><td align="center" bgcolor="#FFF8E7" style="background:#FFF8E7;border:2px solid {NBE_GOLD};padding:20px 16px;'
        f'font-family:{FONT_STACK};">'
        f'<div style="font-size:54px;line-height:64px;">&#127942;</div>'
        f'<div style="font-size:15px;color:{er.MUTED};margin-top:6px;letter-spacing:0.5px;">&#127881; CONGRATULATIONS &#127881;</div>'
        f'<div style="font-size:24px;font-weight:700;color:{NBE_GREEN};margin-top:6px;">{title}</div>'
        + _podium(stars) + '</td></tr></table>')
    rows = [er._tr(er._td(f"{PLACES[i][0]} #{s['rank']}", bold=True, raw=True), er._td(s["name"], bold=True),
                   er._td(s.get("team") or "-"), er._td(s["calls"], align="right"),
                   er._td("-" if s.get("emails") is None else s["emails"], align="right"),
                   er._td(s["tickets"], align="right"), er._td(s["total"], bold=True, align="right"))
            for i, s in enumerate(stars)]
    work = er._table(["", "Employee", "Team", "Calls answered", "E-mails replied", "Requests handled", "Score"], rows)
    note = er._note(f'<div style="font-weight:700;color:{NBE_GREEN};margin-bottom:6px;">&#10024; A word for you</div>'
                    f'<div style="font-size:15px;line-height:1.6;" dir="auto">{esc(words)}</div>',
                    color=NBE_GOLD, background="#FFFBF0")
    inner = (_confetti() + trophy + er._heading("Today's work") + work + note
             + '<div style="height:16px;line-height:16px;">&nbsp;</div>' + _confetti()
             + er._footer(f"Counted by the IAS Portal up to {now:%H:%M} of {now:%A %d/%m/%Y} from today's calls logged, "
                          "e-mails replied and security requests executed, not executed or rejected. Score in security "
                          "requests: a call counts as 2, an e-mail replied as 3, a request handled as 1. "
                          "With the whole team's congratulations."))
    return er._page("Best Performance", f"{er._subtitle(now.date())} &nbsp;&bull;&nbsp; &#127881;", inner)


def run(ctx):
    """One run (the runner passes ``options["slot"]``; "Run now" has none and always sends). Never raises."""
    try:
        return _run(ctx)
    except Exception as exc:                                # noqa: BLE001 - the runner must never crash on a celebration
        ctx.log(f"best performance e-mail failed: {type(exc).__name__}: {exc}")
        return JobResult(ok=False, error=f"The best performance e-mail could not be built: {type(exc).__name__}: {exc}")


def _run(ctx):
    from .. import performance
    from ...storage import Store
    cfg = deep_merge(DEFAULTS["summary"]["best_performer"], (ctx.settings or {}).get("best_performer") or {})
    now = ctx.now()
    day = now.date().isoformat()
    users = ds._users(ctx.data_dir)
    stars = performance.people_day(ctx.store, Store(ctx.data_dir), ctx.hub or {}, users, day, now,
                                   top=max(1, int(cfg.get("top") or 3)))
    if not stars:
        return JobResult(ok=True, skipped=True, summary="nobody has calls, e-mails or requests done today yet")
    names = names_of(stars)
    html = build_html(stars, now, message_of(cfg, day, names))
    counts = "; ".join(f"#{s['rank']} {s['name']}: calls {s['calls']}, e-mails {'-' if s['emails'] is None else s['emails']}, "
                       f"requests {s['tickets']} = score {s['total']}" for s in stars)
    if not ctx.can_email():
        return JobResult(ok=True, summary=f"best performance e-mail built, e-mail off - {counts}")
    to, missing = [], []
    for s in stars:
        address = str((users.get(str(s["user_id"])) or {}).get("email") or "").strip()
        if address and address.lower() not in {a.lower() for a in to}:
            to.append(address)
        elif not address:
            missing.append(s["name"])
    cc = [a for a in cfg.get("cc") or [] if a and a.lower() not in {t.lower() for t in to}]
    if not (to or cc):
        return JobResult(ok=False, error=f"{names}: no e-mail address in their accounts and no Cc is set - add one "
                                         "(Admin › Users) or a Cc (System › Settings › Daily summary)")
    subject = str(cfg.get("subject") or DEFAULTS["summary"]["best_performer"]["subject"])
    subject = subject.replace("{names}", names).replace("{name}", names)
    mailer = ctx.mailer or OutlookMailer()
    try:
        ds.send_retrying(mailer, ctx.log, "best performance", {"on": True, "light": False}, to=to or cc, cc=cc if to else [],
                         subject=subject, html_body=html, inline_images=er.mail_images() or None,
                         display_only=bool(cfg.get("display_only")))
    except Exception as exc:                                # noqa: BLE001 - built, not sent: say why
        ctx.log(f"best performance e-mail built ({counts}) but Outlook could not send it: {outlook_error_text(exc)}")
        return JobResult(ok=False, error=f"The best performance e-mail was built but Outlook could not send it: "
                                         f"{outlook_error_text(exc)}")
    where = f"to {len(to)} address(es)" + (f", cc {len(cc)}" if cc else "") if to else "to the Cc only"
    if missing:
        where += f" ({', '.join(missing)}: no e-mail address in their account)"
    return JobResult(ok=True, emails_sent=1, summary=f"top {len(stars)} celebrated - {counts} - {where}")
