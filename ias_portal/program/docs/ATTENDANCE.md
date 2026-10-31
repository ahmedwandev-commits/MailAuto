# Attendance & Workforce (inside the portal)

Pages live under **`/attendance`** (top-bar link "Attendance"). The module is the package `calllog/attendance/`
(templates `calllog/templates/attendance/`, static `calllog/static/attendance/`, tests `tests/attendance/`).
It is mounted by `calllog/app.py` (`attendance.init_app(app)`); nothing else of the portal was changed except:
`templates/base.html` (an empty `head` block), `templates/_topbar.html` (the Attendance link), `app.py` (registration and the
upload limit of the two Excel imports), `pytest.ini` (the `slow` marker).

## One set of accounts

There is no attendance roster and no attendance user table. The module reads the portal's `users.json` through
`attendance/hub_directory.py`:

| attendance concept | where it comes from |
|---|---|
| **employee** | an *active* portal account whose User ID is the HR employee number (digits only) and whose role is `user` or `manager` (managers are on the roster by default; untick *Include portal managers in the attendance roster* in Attendance → Settings to track only `user` accounts). |
| **team(s)** | the teams on the account (`ops/overlay.user_teams`) - the same values the Calls and E-mail modules use. `__all__` is a scope, not a team. |
| **Administrator** | portal role `admin` - every permission, every team |
| **Manager** | portal role `manager` - only the teams on the account |
| **General Manager** | assigned by an administrator in Attendance → Administration → *Roles & permissions* to any non-admin portal account (stored in `attendance/roles.json`; audited). Sees every team, strictly read-only. |
| anybody else | no access to `/attendance` (403 page) |

Passwords, sign-in, sessions, forced password change, throttling and CSRF are the portal's. Changing a name, a team, a role or
disabling an account on the portal's Users page takes effect in Attendance at once (nothing is copied).

**Deliberate difference from the e-mail module:** a manager with *no team* sees **nobody** in Attendance (the e-mail module shows
such a manager everything). Use `__all__` for a manager who must see every team.

## No approval workflow

Administrators and managers act directly, inside their permissions: schedule changes (weekly plan, monthly calendar), attendance
corrections, forgotten-punch records, absence and permit reviews, leave records / cancellations, compensatory days. Every
write is audited (see below). The General Manager cannot write anything; a manager cannot touch another manager's team (the server
checks the employee on every request - changing `team=` or an id in the URL or form gets a 403 or an empty page).

## Rules (all configurable in Attendance → Administration → Settings)

* **Default shift** - on a scheduled working day with *no* explicit entry (no Morning/Evening/Night, no rest day, no stand-by) an
  employee gets **Normal Day 08:00-16:00**. Working day = the HR calendar when it has a row for the day, otherwise the configured
  weekly rest weekdays (default Fri + Sat) minus holidays. The shift code, its times and the on/off switch are settings. Shown as
  "auto" in the weekly plan and "(default)" on the dashboards; never written as an explicit entry.
* **Attendance permits** - check-in *more than* 15 minutes after the real shift start (Morning 07:00, Evening 14:00, Night 22:00, default
  shift 08:00) is flagged **اذن حضور**; check-out *more than* 15 minutes before the shift end (Night: next morning 07:00) is flagged
  **اذن انصراف**. Exactly 15:00 is not flagged; 15:01 is. The two thresholds are independent. A flag stays visible (Today's overview,
  team dashboard, employee profile, *Permits* report, Excel/PDF) until a manager/admin reviews it (*provided* / *excused* / *rejected*,
  append-only, with a reason). A missing permit **never** makes anyone absent and never changes a score.
* **Dashboard = Day view + Team availability** (every role, scoped to its teams; there is no separate "Today's overview" page - old
  links land on the Day view).
  - *Day view* (`/attendance/planning/day`): the normal day view - shift cards (required / available, no staffing percentage), stand-by and rest
    lists, short-staffed banner. It carries no team table.
  - *Team availability* (`/attendance/team`): one line per team with numbers only (**managers**, employees, present, scheduled on each
    shift, on leave, not arrived, rest) and the day's counters (expected, arrived, not arrived, on leave, rest, permit exceptions). The
    *Managers* column is a number: how many managers the administrator ticked for the team in Administration -> Settings -> *Team
    managers* and how many of them have arrived ("2 · 1 arrived") - no names. **Click a team** to open its people: every person with
    shift, expected hours, check-in / check-out, state and permit flags (managers get *Review permit* for their own team, the General
    Manager reads only), and the names of the team's managers. A manager's own team is open straight away. No staffing percentage.
  - *Arrived by sign-in*: the HR file of the day is hard to get on the same day, so for **today** a person who is expected to work, has no
    HR check-in yet and signed in to the portal is marked **Arrived HH:MM** (their first successful sign-in of the day, from the portal's
    audit log) and counted as present. It is only a mark: lateness, permits and scores are never computed from it, a person on leave
    or on a rest day is not touched, past days are not touched, and as soon as the HR row of the day exists it takes over (HR says no
    check-in = not arrived).* **Modification** (`/attendance/modify`, administrator and managers only) - *Change shift* (reason required), *Add leave*, *Cancel
  leave* for the employees of a team and day; managers only their own teams; audited with previous / new value and reason. No upload.
* **Imports** (administrator only; managers have none; the General Manager can read the history) - *Import now* imports the file at
  once, with no preview or confirmation: date typos are corrected, exact / alias / single confident name matches are used, every other
  name is skipped and listed, an existing month is replaced (the old version stays in the history), HR rows of employee numbers without a
  portal account are skipped and listed, stored days are never overwritten. A file that needs a decision (wrong month, unreadable columns,
  no employees yet) still opens the review page, and "Let me review first" ticks that path on purpose.
  - *Name mapping* (Imports -> Name mapping, administrator): the schedule has first names only and is matched to the portal accounts by
    name. The page lists every name of the latest schedule of each month with its status; pick the account for the ones without a match
    (or "Forget this link"). A choice is remembered for every later import. *Rebuild* re-applies the mapping to a month that is already
    imported (a new version; the old one stays in the history). The HR attendance file is matched by employee number, so it needs no
    mapping: numbers without a portal account are skipped and listed.
  - *HR statistical report* ("تقرير احصائي ببيانات موقف الحضور والانصراف للموظف خلال الفترة من ... الى ..."): imported as is - the day type,
    punches and their source, the leave, permit (أذون) and mission columns are all read. بدل راحة counts as a compensatory rest day (leave
    type comp), اذن خاص حضور / انصراف excuse a late arrival / early departure, an official holiday without punches is a holiday.
    Only people with a portal account are imported: accounts with the role "user" and (while "Include portal managers in the attendance
    roster" is ticked, the default) managers - in the October 2026 report 14 of the 40 people are portal managers. The last days of a
    report exported in the morning are often incomplete; people without any data there show as "Absent (unverified)" for a manager to review, never as a penalty.
* **The employee's own pages** (role *user*, a numeric User ID on the roster). The portal's side menu of an employee is **Log calls /
  My work / E-mail / My schedule**:
  - *My work* (`/my`) is the summary of the day only: my calls today, my e-mails today (numbers only, with a link), my quality reviews of
    today, my tickets and my SLA today. The Team status strip stays.
  - *E-mail* (`/my/email`) has everything about the mails: assigned to me, replied today, my team's open mails (with the same buttons)
    and the full list of my quality reviews with the appeals. The notification links `/my?mail=<id>` open there.
  - *My schedule* (`/attendance/my`): the shifts of the next 14 days and the month with the real check-in / check-out and the status of
    every day. It shows only the signed-in person's rows (the number is the account's User ID, never a parameter). An employee cannot open
    any other attendance page: the server refuses every other URL; an account that also has the shift-allowance export gets that page too.
* **Shift allowance export** (بدل ورديات; `/attendance/allowance`): **counts only - no value and no money anywhere** (page, Excel). Sheets:
  «ورديات» (employee_ID, employee_name, date, shift_type, morning_shift, Evening_shift, night_shift, holidays_GT_6_DAYS, TASK),
  «ملخص» (counts per employee for صباحية / مسائية / ليلية / عطلة) and «غير محسوب» (days with punches that did not count, with the reason).
  `shift_type` is one of four words: صباحية, مسائية, ليلية, عطلة (a weekly rest day or an official holiday worked are both عطلة).
  - **Which shift a day is** (Administration -> Settings -> *Shift allowance*): 1) *Use the portal schedule first* - if the employee has an
    M / E / N entry for that day (the manager may have edited it in Schedule), that shift counts when the hours requirement is met;
    2) otherwise, for people who work shifts that month, a rule by time of day: check-in inside a shift's window, or - for someone who came
    early - who stayed the required hours (default 8) and left after *Evening after* (default 20:00) counts as Evening. A day with no shift in the schedule that does not start at a shift and ends before *Evening after* is a normal working day and is not counted (also for shift workers).
  - **Managers are not paid shifts**; only weekly rest days and official holidays worked count for them (setting *Managers: holidays only*).
  - **Monthly shifts** (`/attendance/monthly-shifts`): at month end each manager sees their team with the shifts exactly as the system
    counts them, can open the schedule to correct, and presses *Approve*. **Only the manager of the team approves** (not the administrator, not the GM, not another team's manager). An approval is per month and team; if the data changes after it,
    the page says *changed after approval* and it must be approved again. **The administrator sees every shift at once** (page and Excel),
    each day marked *Approved / Not approved yet*; a payroll account (allowance role) gets approved teams only.
  Who may use the export: administrators; any other account only after the administrator ticks it in Roles & permissions (such an account
  sees that one page and nothing else). Every export and settings change is audited.
* **Attendance score** (Administration -> Scoring rules; nothing is built in, no penalty until you enter one). All penalties are % of the
  start score. Per component - late arrival, early departure, verified unexcused absence, missing punch, rejected permit - you set the
  base (minute bands and/or a flat % per occurrence, ignore-under-N-minutes), a maximum, and a **repeat rule**: count occurrences in the
  period, calendar month or a rolling N days; by occurrence number add an extra % (`3-5=+1; 6-=+2`) or multiply (`2-=x1.5`), and add an extra
  % for runs of N consecutive scheduled days. Plus an overall cap, categories and the minimum number of evaluated days. Every save is a new
  version (old periods keep their rules). The Scoring rules page offers **ready-made sets** (Lenient / Standard / Strict) that fill the form, **rows instead of typed text** for bands, repeat steps and categories (the text boxes stay behind *Show text*), and a **live preview** (`POST /admin/scoring/preview`, the real scoring code on a sample month) before saving. The employee **scorecard** shows the score, category, the equation, where each point went (base +
  repeat) and the rules in plain words. A missing or unreviewed permit never lowers the score.* **Short staffed** - *Staffing % = expected available ÷ required × 100* where required = employees planned for the team and shift and
  expected available = planned minus approved leave / compensatory day. A global, per-team and per-shift minimum can be set;
  nothing is flagged until one is configured. Shown to the manager (own teams) and to the GM / administrator (all teams).
* **Scoring** is unconfigured until management enters rules; there is no invented formula.

## Data

`<data_dir>/attendance/*.json` (see the header of `attendance/repo/json_repo.py`). Written atomically under one module lock; a unit of
work is all-or-nothing. Back it up with the rest of `data/`. A hand-edited file is read even with a Notepad BOM; a damaged one gives a
clear error instead of silently losing data. Sized for hundreds of employees (monthly attendance files of a few MB).

HR attendance rows whose employee number has no portal account are **skipped and counted** in the import report (create the account,
then import the file again - already-stored days are never overwritten). Teams are never changed by an import.

## Audit

Every attendance action is written to the portal's audit log (`data/logs/audit-*.jsonl`) as `attendance.<action>` with the user, the
time, the entity, and `previous` / `new` / `reason` where they apply. *Attendance → Administration → Audit log* shows the same
entries.

## Tests

`python -m pytest tests/attendance` (attendance only) or `python -m pytest` (whole portal). The attendance tests cover shared
accounts and team permissions, the default shift, the 15 / >15 minute permit thresholds, team availability, short-staffing and
direct (approval-free) administrator / manager actions, the JSON storage, and the two real HR workbooks when they are available
(`ATTENDANCE_SAMPLES=<folder>`; skipped otherwise).

## Deployment, backup and rollback

* **No database and no migration.** The first start creates `data\attendance\` (shift types, leave types, counters). No new Python
  package is needed (Flask, waitress and openpyxl are already required). PDF export of reports stays optional.
* **Before deploying:** stop the server and copy the whole `data\` folder (this is also the backup of Calls, E-mail and Attendance) and
  the program folder. To restore, stop the server and copy them back.
* **Deploy:** copy the new program files (`calllog\attendance\`, `calllog\templates\attendance\`, `calllog\static\attendance\`, the
  three changed portal files (`calllog\app.py`, `calllog\templates\base.html`, `calllog\templates\_topbar.html`) over the old ones and start the server (`python run.py --service`). Sign in
  as an administrator, open Attendance -> Settings and check the rules, then give managers their teams on the portal's Users page.
* **Backup of Attendance only:** `data\attendance\` (it also keeps the original HR/schedule workbooks under `uploads\` - they contain
  employee data; treat the folder like `users.json`).
* **Rollback:** stop the server, restore the three changed portal files and delete `calllog\attendance\` (and its templates/static
  folders). `data\attendance\` can stay or be deleted; Calls, accounts and E-mail data are not touched by the module.

## Schedule names
* A name written in the schedule «محمد صلاح (MFA)» counts for the **MFA team only** for that shift (the mark is kept with the entry); a name under a column
  «OPEN + MFA» without the mark counts for both teams.
* An Arabic name of two or more words that sounds like exactly one employee stored in English («محمد صلاح» = *Mohamed Salah ...*) is matched automatically.
* A name that matched nobody when the file was imported is skipped. When the employee is added / renamed / mapped later, the **Day view** (administrator)
  shows «N people of the schedule are missing» with **Add them now**: it plans only the person-days nothing was planned for - a manager's changes are never touched.
  Names that still match nobody are listed under it (add the employee to the portal, or link the name in Name mapping).

## Day view
The shift cards (Morning / Evening / Night / Normal day) are followed, in the same grid, by **Annual Leaves & Rest days**: everybody away that day (rest day, annual or other leave, holiday, or not planned), once.

## Reports and Analytics
* **Reports** (`/attendance/reports`) is the only place for tables: summary, month-end checklist (*what needs a decision*), daily detail,
  schedule changes, shift approvals, teams, departments, lateness, ranking, permits, trends... Each has a description, a totals row, and
  the same content in Excel / PDF.
* **Score reports:** *Score summary* (one line per person, lowest first: score, points lost per problem, the reason in words, and how the score is built) and
  *Score details* (every late arrival / absence / missing punch ... that cost points, with date, base, repeat extra and points).
* A **double shift** (e.g. Morning + Evening the same day) counts in both shifts in the Day view, Team availability and staffing.
* **Analytics** (`/attendance/analytics`) is the picture only: indicators and charts (where the planned days went, lateness by weekday,
  teams, score categories, 6-month trend, most late); every card links to its report. A manager sees only their teams.
