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


def _pct(v):
    return "-" if v is None else f"{v:g}%"


def team_block(best):
    """The Team of the day (the first ``performance.team_day`` row): its name, its people and why it won."""
    sla = "no e-mail or request with an SLA today" if best.get("sla_pct") is None else f"{_pct(best['sla_pct'])} kept their SLA"
    card = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin:14px 0 6px 0;">'
        f'<tr><td align="center" bgcolor="#F0F7F3" style="background:#F0F7F3;border:2px solid {NBE_GREEN};padding:18px 16px;'
        f'font-family:{FONT_STACK};">'
        f'<div style="font-size:44px;line-height:52px;">&#127941;</div>'
        f'<div style="font-size:15px;color:{er.MUTED};margin-top:4px;letter-spacing:0.5px;">TEAM OF THE DAY</div>'
        f'<div style="font-size:24px;font-weight:700;color:{NBE_GREEN};margin-top:6px;" dir="auto">{esc(best["team"])}</div>'
        f'<div style="font-size:14px;color:{er.MUTED};margin-top:6px;">{best["manpower"]} people worked &nbsp;&bull;&nbsp; '
        f'{best["per_person"]:g} per person &nbsp;&bull;&nbsp; {esc(sla)}</div></td></tr></table>')
    work = er._table(["Team", "Worked today", "Calls answered", "E-mails replied", "Requests handled", "Per person", "E-mails in SLA",
                      "Requests in SLA", "Team score"],
                     [er._tr(er._td(best["team"], bold=True), er._td(f'{best["manpower"]} of {best.get("team_size", best["manpower"])}', align="right"), er._td(best["calls"], align="right"),
                             er._td(best["emails"], align="right"), er._td(best["tickets"], align="right"),
                             er._td(f'{best["per_person"]:g}', align="right"), er._td(_pct(best.get("email_sla_pct")), align="right"),
                             er._td(_pct(best.get("ticket_sla_pct")), align="right"), er._td(f'{best["score"]:g}', bold=True, align="right"))])
    return card + work


def manager_block(best):
    """The Manager of the day (the first ``performance.manager_day`` row)."""
    rated = "nothing of the team to rate today" if best.get("rated_pct") is None else f"{_pct(best['rated_pct'])} of the team's replies rated"
    card = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin:14px 0 6px 0;">'
        f'<tr><td align="center" bgcolor="#F3F1FA" style="background:#F3F1FA;border:2px solid {er.PURPLE};padding:18px 16px;'
        f'font-family:{FONT_STACK};">'
        f'<div style="font-size:44px;line-height:52px;">&#11088;</div>'
        f'<div style="font-size:15px;color:{er.MUTED};margin-top:4px;letter-spacing:0.5px;">MANAGER OF THE DAY</div>'
        f'<div style="font-size:24px;font-weight:700;color:{NBE_GREEN};margin-top:6px;" dir="auto">{esc(best["name"])}</div>'
        f'<div style="font-size:14px;color:{er.MUTED};margin-top:6px;" dir="auto">{esc(best.get("team") or "")} &nbsp;&bull;&nbsp; '
        f'{best["emails"]} e-mails replied &nbsp;&bull;&nbsp; {best["ratings"]} replies rated &nbsp;&bull;&nbsp; {esc(rated)}</div>'
        f'</td></tr></table>')
    work = er._table(["Manager", "Team", "E-mails replied", "Replies rated", "Team replies not rated", "Team rated", "Score"],
                     [er._tr(er._td(best["name"], bold=True), er._td(best.get("team") or "-"), er._td(best["emails"], align="right"),
                             er._td(best["ratings"], align="right"),
                             er._td(best["team_unrated"] if best.get("team_reviewable") else "-", align="right"),
                             er._td(_pct(best.get("rated_pct")), align="right"), er._td(f'{best["score"]:g}', bold=True, align="right"))])
    return card + work


def build_html(stars, now, words, best_team=None, best_manager=None):
    """The Best Performance mail of ``stars`` (the first ``performance.people_day`` rows, best first) and, when given,
    the Team of the day (``best_team``: the first ``performance.team_day`` row)."""
    title = "TODAY'S TOP 3" if len(stars) == 3 else f"TODAY'S TOP {len(stars)}" if len(stars) > 1 else "TODAY'S BEST"
    trophy = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin:14px 0 6px 0;">'
        f'<tr><td align="center" bgcolor="#FFF8E7" style="background:#FFF8E7;border:2px solid {NBE_GOLD};padding:20px 16px;'
        f'font-family:{FONT_STACK};">'
        f'<div style="font-size:54px;line-height:64px;">&#127942;</div>'
        f'<div style="font-size:15px;color:{er.MUTED};margin-top:6px;letter-spacing:0.5px;">&#127881; CONGRATULATIONS &#127881;</div>'
        f'<div style="font-size:24px;font-weight:700;color:{NBE_GREEN};margin-top:6px;">{title}</div>'
        + _podium(stars) + '</td></tr></table>')
    rows = [er._tr(er._td(f"{PLACES[i][0] if i < len(PLACES) else '&#127941;'} #{s['rank']}", bold=True, raw=True), er._td(s["name"], bold=True),
                   er._td(s.get("team") or "-"), er._td(s["calls"], align="right"),
                   er._td("-" if s.get("emails") is None else s["emails"], align="right"),
                   er._td(s["tickets"], align="right"), er._td(s["total"], bold=True, align="right"))
            for i, s in enumerate(stars)]
    work = er._table(["", "Employee", "Team", "Calls answered", "E-mails replied", "Requests handled", "Score"], rows)
    note = er._note(f'<div style="font-weight:700;color:{NBE_GREEN};margin-bottom:6px;">&#10024; A word for you</div>'
                    f'<div style="font-size:15px;line-height:1.6;" dir="auto">{esc(words)}</div>',
                    color=NBE_GOLD, background="#FFFBF0")
    team = (er._heading("Team of the day") + team_block(best_team)) if best_team else ""
    if best_manager:
        team += er._heading("Manager of the day") + manager_block(best_manager)
    inner = (_confetti() + trophy + er._heading("Today's work") + work + team + note
             + '<div style="height:16px;line-height:16px;">&nbsp;</div>' + _confetti()
             + er._footer(f"Counted by the IAS Portal up to {now:%H:%M} of {now:%A %d/%m/%Y} from today's calls logged, "
                          "e-mails replied and security requests executed, not executed or rejected. Score in security "
                          "requests: a call counts as 2, an e-mail replied as 3, a request handled as 1."
                          + (" Team of the day: the team's score per person (its score / the people who worked today - at least "
                             "one call, e-mail or request) x the share of its e-mails and requests that kept their SLA." if best_team else "")
                          + (" Manager of the day: 3 per e-mail replied (always in full) + 1 per reply rated x the share of the "
                             "team's replies of the day that were rated." if best_manager else "")
                          + " With the whole team's congratulations."))
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
    calls = Store(ctx.data_dir)
    numbers = performance.day_numbers(ctx.store, ctx.hub or {}, users, day)
    everyone = performance.people_day(ctx.store, calls, ctx.hub or {}, users, day, now, numbers=numbers,
                                      roles=performance.TEAM_ROLES)    # the managers count for the Team of the day only
    stars = performance.employees_only(everyone, max(1, int(cfg.get("top") or 3)))
    if not stars:
        return JobResult(ok=True, skipped=True, summary="nobody has calls, e-mails or requests done today yet")
    best_team = None
    if cfg.get("team", True):
        try:
            best_team = (performance.team_day(ctx.store, calls, ctx.hub or {}, users, day, now, people=everyone, numbers=numbers) or [None])[0]
        except Exception as exc:                            # noqa: BLE001 - the people are still celebrated
            ctx.log(f"best performance e-mail: the team of the day could not be counted: {type(exc).__name__}: {exc}")
    best_manager = None
    if cfg.get("manager", True):
        try:
            best_manager = (performance.manager_day(ctx.store, users, day, now, everyone) or [None])[0]
        except Exception as exc:                            # noqa: BLE001 - the people are still celebrated
            ctx.log(f"best performance e-mail: the manager of the day could not be counted: {type(exc).__name__}: {exc}")
    names = names_of(stars)
    html = build_html(stars, now, message_of(cfg, day, names), best_team, best_manager)
    counts = "; ".join(f"#{s['rank']} {s['name']}: calls {s['calls']}, e-mails {'-' if s['emails'] is None else s['emails']}, "
                       f"requests {s['tickets']} = score {s['total']}" for s in stars)
    if best_manager:
        counts += f"; manager of the day {best_manager['name']} ({best_manager['emails']} replies, {best_manager['ratings']} rated)"
    if best_team:
        counts += f"; team of the day {best_team['team']} ({best_team['per_person']:g} per person, SLA {_pct(best_team['sla_pct'])})"
    if not ctx.can_email():
        return JobResult(ok=True, summary=f"best performance e-mail built, e-mail off - {counts}")
    to, missing = [], []
    for s in stars:
        address = str((users.get(str(s["user_id"])) or {}).get("email") or "").strip()
        if address and address.lower() not in {a.lower() for a in to}:
            to.append(address)
        elif not address:
            missing.append(s["name"])
    if best_manager:                                       # the Manager of the day is celebrated too
        address = str((users.get(str(best_manager["user_id"])) or {}).get("email") or "").strip()
        if address and address.lower() not in {a.lower() for a in to}:
            to.append(address)
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
