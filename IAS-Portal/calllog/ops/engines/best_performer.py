"""The "best_performer" job: once a day (``summary.best_performer.send_times``, 15:30 by default, on a working day) the
day's best performer gets a celebration e-mail, with the Cc set in System › Settings › Daily summary.

The best performer is the first of Insights › Analysis' "Best performers" for that day (``performance.people_day``:
the day's score in security requests - executed, not executed or rejected + 2 per call answered + 3 per e-mail
replied; ties by tickets, then calls) - the same ranking as the daily summary's section. The mail goes to the address of their account; without one, to the
Cc only (the result says so).

The mail is a celebration: confetti, a trophy, their numbers of the day and a word of encouragement - the owner's own
lines (``messages``, one a day in turn, ``{name}`` = their name) or a built-in one, a different one each day. Outlook
safe: tables, inline styles, no image but the logo. The Arabic lines are written in forms spelled the same for a man
and a woman. Never raises."""
from datetime import datetime

from ..settings import DEFAULTS, deep_merge
from .common import FONT_STACK, NBE_GOLD, NBE_GREEN, JobResult, OutlookMailer, esc, outlook_error_text
from . import daily_summary as ds
from . import email_report as er

# (English, Arabic) - a different one each day; the Arabic is spelled the same whoever reads it
MESSAGES = (
    ("Today you set the pace for the whole team. Thank you for the effort behind every call, every e-mail and every "
     "request.", "النهارده مجهودك هو اللي حدد إيقاع الفريق كله. شكراً لك على كل مكالمة وكل إيميل وكل طلب."),
    ("Great work is never an accident, and today your work proved it. Keep shining!",
     "الشغل المميز عمره ما بيكون صدفة، وشغلك النهارده أكبر دليل. نتمنى لك التألق دايماً."),
    ("Every customer helped today had a better day thanks to you. That is what being the best looks like.",
     "كل عميل اتساعد النهارده يومه بقى أحسن بفضلك. هو ده معنى الأفضل بجد."),
    ("Commitment, speed and care: today your work brought all three together. We are proud of you!",
     "الالتزام والسرعة والاهتمام: التلاتة اجتمعوا في شغلك النهارده. كلنا فخورين بك!"),
    ("Today the bar went up thanks to your effort. Tomorrow is a new chance to shine even brighter.",
     "النهارده المستوى ارتفع بفضل مجهودك. وبكرة فرصة جديدة لتألق أكبر."),
    ("Behind these numbers is real dedication. Thank you for making the team stronger every day.",
     "ورا الأرقام دي التزام حقيقي. شكراً لك على إضافتك للفريق كل يوم."),
    ("Champions are made one task at a time, and today nobody did more. Bravo!",
     "الأبطال بيتصنعوا مهمة ورا مهمة، والنهارده محدش عمل أكتر منك. برافو!"),
    ("Your effort today made a real difference. The whole team celebrates with you.",
     "مجهودك النهارده عمل فرق حقيقي. والفريق كله بيحتفل معك."),
)
CONFETTI = (("★", NBE_GOLD), ("●", er.GREEN), ("◆", er.BLUE), ("▲", er.RED), ("✦", er.PURPLE), ("●", er.AMBER))


def message_of(cfg, day, name):
    """The day's word of encouragement as [paragraph]: the owner's own lines in turn (``{name}`` = their name), else the
    built-in English + Arabic pair - a different one each day."""
    n = datetime.strptime(day, "%Y-%m-%d").toordinal()
    own = [str(x).strip() for x in (cfg.get("messages") or []) if str(x).strip()]
    if own:
        return [own[n % len(own)].replace("{name}", name)]
    return list(MESSAGES[n % len(MESSAGES)])


def _confetti(count=24):
    cells = "".join(f'<td align="center" style="font-size:17px;line-height:22px;color:{color};font-family:{FONT_STACK};">'
                    f'{symbol}</td>' for symbol, color in (CONFETTI[i % len(CONFETTI)] for i in range(count)))
    return ('<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"><tr>'
            + cells + '</tr></table>')


def build_html(star, day, now, words):
    """The celebration mail of ``star`` (a ``performance.people_day`` row)."""
    name, team = esc(star["name"]), esc(star.get("team") or "")
    emails = "—" if star.get("emails") is None else star["emails"]
    trophy = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin:14px 0 6px 0;">'
        f'<tr><td align="center" bgcolor="#FFF8E7" style="background:#FFF8E7;border:2px solid {NBE_GOLD};padding:22px 16px;'
        f'font-family:{FONT_STACK};">'
        f'<div style="font-size:54px;line-height:64px;">&#127942;</div>'
        f'<div style="font-size:15px;color:{er.MUTED};margin-top:6px;letter-spacing:0.5px;">&#127881; CONGRATULATIONS &#127881;</div>'
        f'<div style="font-size:28px;font-weight:700;color:{NBE_GREEN};margin-top:8px;" dir="auto">{name}</div>'
        + (f'<div style="font-size:13px;color:{er.MUTED};margin-top:3px;" dir="auto">{team}</div>' if team else "")
        + f'<div style="font-size:19px;font-weight:700;color:{NBE_GREEN};margin-top:12px;" dir="rtl">'
          f'ألف مبروك! الأفضل أداءً النهارده &#129351;</div>'
        f'<div style="margin-top:12px;"><span style="background:{NBE_GREEN};color:#ffffff;padding:6px 14px;font-size:13px;'
        f'font-weight:700;">&#129351; #1 today &middot; score {star["total"]}</span></div>'
        '</td></tr></table>')
    kpis = er._kpi_grid([er._kpi("Calls answered", star["calls"], er.BLUE),
                         er._kpi("E-mails replied", emails, er.GREEN),
                         er._kpi("Requests handled", star["tickets"], er.AMBER),
                         er._kpi("Score", star["total"], NBE_GOLD)])
    said = "".join(f'<div style="font-size:15px;line-height:1.6;margin:4px 0;" dir="auto">{esc(p)}</div>' for p in words)
    note = er._note(f'<div style="font-weight:700;color:{NBE_GREEN};margin-bottom:6px;">&#10024; A word for you</div>{said}',
                    color=NBE_GOLD, background="#FFFBF0")
    inner = (_confetti() + trophy + er._heading("Today's work") + kpis + note
             + '<div style="height:16px;line-height:16px;">&nbsp;</div>' + _confetti()
             + er._footer(f"Counted by the IAS Portal up to {now:%H:%M} of {now:%A %d/%m/%Y} from today's calls "
                          "logged, e-mails replied and security requests executed, not executed or rejected. Score in "
                          "security requests: a call counts as 2, an e-mail replied as 3. "
                          "With the whole team's congratulations."))
    return er._page("Today's best performer", f"{er._subtitle(now.date())} &nbsp;&bull;&nbsp; &#127881;", inner)


def run(ctx):
    """One run (the runner passes ``options["slot"]``; "Run now" has none and always sends). Never raises."""
    try:
        return _run(ctx)
    except Exception as exc:                                # noqa: BLE001 - the runner must never crash on a celebration
        ctx.log(f"best performer e-mail failed: {type(exc).__name__}: {exc}")
        return JobResult(ok=False, error=f"The best performer e-mail could not be built: {type(exc).__name__}: {exc}")


def _run(ctx):
    from .. import performance
    from ...storage import Store
    cfg = deep_merge(DEFAULTS["summary"]["best_performer"], (ctx.settings or {}).get("best_performer") or {})
    now = ctx.now()
    day = now.date().isoformat()
    users = ds._users(ctx.data_dir)
    rows = performance.people_day(ctx.store, Store(ctx.data_dir), ctx.hub or {}, users, day, now)
    if not rows:
        return JobResult(ok=True, skipped=True, summary="nobody has calls, e-mails or requests done today yet")
    star = rows[0]
    words = message_of(cfg, day, star["name"])
    html = build_html(star, day, now, words)
    counts = (f"{star['name']}: calls {star['calls']}, e-mails {'-' if star['emails'] is None else star['emails']}, "
              f"requests {star['tickets']} = score {star['total']}")
    if not ctx.can_email():
        return JobResult(ok=True, summary=f"best performer e-mail built, e-mail off - {counts}")
    address = str((users.get(str(star["user_id"])) or {}).get("email") or "").strip()
    cc = [a for a in cfg.get("cc") or [] if a and a.lower() != address.lower()]
    to = [address] if address else []
    if not (to or cc):
        return JobResult(ok=False, error=f"{star['name']} has no e-mail address in their account and no Cc is set - "
                                         "add one (Admin › Users) or a Cc (System › Settings › Daily summary)")
    subject = str(cfg.get("subject") or DEFAULTS["summary"]["best_performer"]["subject"]).replace("{name}", star["name"])
    mailer = ctx.mailer or OutlookMailer()
    try:
        ds.send_retrying(mailer, ctx.log, "best performer", {"on": True}, to=to or cc, cc=cc if to else [],
                         subject=subject, html_body=html, inline_images=er.mail_images() or None,
                         display_only=bool(cfg.get("display_only")))
    except Exception as exc:                                # noqa: BLE001 - built, not sent: say why
        ctx.log(f"best performer e-mail built ({counts}) but Outlook could not send it: {outlook_error_text(exc)}")
        return JobResult(ok=False, error=f"The best performer e-mail was built but Outlook could not send it: "
                                         f"{outlook_error_text(exc)}")
    where = f"to {address}" if address else "to the Cc only (no e-mail address in their account)"
    return JobResult(ok=True, emails_sent=1,
                     summary=f"best performer celebrated - {counts} - {where}" + (f", cc {len(cc)}" if to and cc else ""))
