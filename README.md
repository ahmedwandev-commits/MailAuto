# CEM Provisioning Bot

Automates the procedure in **Logic.docx**: it takes the CEM tickets from the
*Security Code Request* portal ("الطلبات الجديدة-مستخدم سرية"), applies them in
**CEM** (SEDCO), then closes each ticket with the right decision. The job codes
come from **Mapping.xlsx**.

> **ملخص بالعربي:** الأداة تفتح بوابة طلبات الأكواد السرية، تعيد إسناد طلبات CEM لك،
> ثم تمر على كل طلب (تعديل صلاحية، إعادة تشغيل، الغاء، انشاء): تبحث عن المستخدم في
> CEM، وتنفذ المطلوب (التفعيل/الإيقاف، الأدوار، الفرع والصلاحيات حسب ملف الـ Mapping)،
> ثم ترجع للبوابة وتختار القرار ("تم التنفيذ" أو "لم يتم التنفيذ" مع السبب) وتضغط تنفيذ.
> في آخر التشغيل يتم حفظ تقرير Excel بكل طلب ونتيجته وصور الشاشة عند حدوث أي خطأ.
> **ابدأ دائماً بالتجربة على النظام الوهمي (Mock) ثم Dry-Run قبل التشغيل الفعلي.**

---

## 1. Quick start

Needs Python 3.10+. Open a terminal in the project folder.

**Setup (once):**

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

On a locked-down PC where the browser download is blocked, skip the
`playwright install` line and set `channel: "chrome"` (or `"msedge"`) in
`config/settings.yaml` to use the browser that is already installed.

**Use:**

| Step | Command | What happens |
|---|---|---|
| 1 | `python -m cem_bot demo` | Starts the **fake** portal + **fake** CEM and runs the bot on them. Watch the browser work, then open `runs\<date>\report.xlsx` |
| 2 | edit `.env` | Put your real user code / password |
| 3 | `python -m cem_bot run --profile real --dry-run` | Real system, **changes nothing**: lists tickets, checks users in CEM, writes what it *would* do |
| 4 | `python -m cem_bot run --profile real --max 1` | Real run, one ticket only. Asks you to type `YES` |
| 5 | `python -m cem_bot run --profile real` | Full run |

More commands:

```bash
python -m cem_bot run --profile real --types MODIFY,REACTIVATE   # only some ticket types
python -m cem_bot check-mapping               # validate Mapping.xlsx, show rules
python -m mock.server                         # just the fake systems, to click around yourself
python -m pytest tests                        # 33 tests incl. full browser run on the mocks
```

Other `run` options: `--no-reassign`, `--headless`, `--slowmo 300`, `--yes`, `-v`.

---

## 2. What the bot does

1. **Portal login** → row *User Provisioning Portal-موظف اسناد - مستخدم السرية* → **دخول** (popup handled).
2. **إعادة اسناد**: النظام = cem → بحث → select all → اختر = your code (`OPERATOR_ID`) → تنفيذ.
3. **CEM login** in a second tab.
4. **الطلبات الجديدة-مستخدم سرية**: النظام = cem → بحث, then ticket types in this order:
   تعديل صلاحية → إعادة تشغيل → الغاء → انشاء. For each ticket:

| نوع الطلب | User in CEM? | What the bot does in CEM | Decision in the portal |
|---|---|---|---|
| تعديل صلاحية (+ بصفه استثنائيه) | no | – | لم يتم التنفيذ / ليس لدية كود |
| | yes | Edit: Active ON, Locked OFF · Roles: remove all, add role of the category · Branches: remove old (Maker/Checker), add branch, grant permissions | تم التنفيذ |
| إعادة تشغيل | no | – | لم يتم التنفيذ / ليس لدية كود |
| | yes | Active ON, Locked OFF | تم التنفيذ |
| الغاء | no | – | لم يتم التنفيذ / ليس لدية كود |
| | yes | Active OFF, Locked OFF | تم التنفيذ |
| انشاء (+ بصفه استثنائيه) | yes | – | لم يتم التنفيذ / Other / "لديه كود بالفعل برجاء ارسال طلب تعديل صلاحية وليس إنشاء" |
| | no | New user: Name (En/Ar) = `id - name`, Login = id, Verify, Windows, Department Main · role · branch · permissions | تم التنفيذ |

Category (column **المسمى الوظيفى** → job code → `Word File` in Mapping.xlsx):

| Category | Role | Branch permissions |
|---|---|---|
| Maker | none (roles list emptied) | Grant Serving |
| Checker | Branch Superviser | Grant Serving, Grant Monitoring, Grant Alerts Receiving |
| Manager | Branch Manager (Component) | Grant Supervising, Grant Monitoring, Grant Alerts Receiving |

**Branch id** = the number in **الفرع / الادارة**, first 3 digits: `حلوان - 15100` → `151`
(matched exactly against the *Identity* column in CEM, so `1510` is never picked for `151`).

"تم الاطلاع على مصفوفة الصلاحيات و لا يوجد تعارض" is ticked for تعديل / انشاء tickets.

**Left open for a human (SKIPPED):** job code not in Mapping.xlsx, unknown ticket type,
missing branch number. **ERROR:** anything unexpected on screen → screenshot, ticket left
open, the bot recovers and continues with the next ticket.

A user only counts as "found" when **Login Name equals the id exactly** (searching 300104
must not match 1300104).

---

## 3. Files you may want to change

| File | Purpose |
|---|---|
| `.env` | Usernames / passwords / `OPERATOR_ID`. Never share it. |
| `data/Mapping.xlsx` | Job code → Maker/Checker/Manager. Replace with a newer version any time (same columns). |
| `config/rules.yaml` | Business rules: ticket-type texts, role + permissions per category, decision/reason/notes texts, name format, branch digits. |
| `config/settings.yaml` | `profile` (mock/real), URLs, browser, processing order, `max_tickets`, `stop_on_error`. |
| `config/ui.yaml` | Screen texts and optional element selectors (see §5). |

## 4. Output

Each run creates `runs/<date_time>/`:
- `report.xlsx` — one row per ticket: result (DONE / REJECTED / SKIPPED / ERROR / PLANNED), user, branch id, job code, category, action, decision, details, steps done, screenshot. Plus a Summary sheet.
- `run.log` — detailed log.
- `screenshots/` — portal + CEM screenshots for every error.

## 5. Adapting to the real screens

The bot finds buttons, fields and columns **by their visible text** (Arabic spelling
differences such as أ/ا, ة/ه, ى/ي are ignored), so it is not tied to element ids. It
handles select2 dropdowns (type "cem" + Enter), native dropdowns, styled on/off
switches, confirm pop-ups, popup windows and ASP.NET post-backs.

If something on the real system is not found, the run stops that ticket with an
`UIError` that names the missing text, plus screenshots. Then:

1. Check the text on the screen matches `config/ui.yaml` (e.g. column names, button labels).
2. Or give the element directly: put a selector in the matching `*_selectors` list,
   e.g. `assignee_select_selectors: ["css=#ctl00_ContentPlaceHolder1_ddlUsers"]`.
   Find it with Chrome → right-click → Inspect, or record clicks with
   `python -m playwright codegen https://nbesrv.nbe.ahly.bank/...`.
3. Things most likely to need a look on the real CEM: the **Verify** success marker
   (`cem.verify.success_selectors`) and the Department field on the *New user* page.

After any change, run `python -m cem_bot run --profile real --dry-run`, then
`python -m cem_bot run --profile real --max 1`.

## 6. Points to confirm (differences found between Logic.docx and Mapping.xlsx)

1. **انشاء + Checker permissions** — Logic.docx says *serving + supervising + alerts*,
   Mapping.xlsx (and the تعديل section) say *serving + monitoring + alerts*. The bot uses
   Mapping.xlsx. To follow the document instead, uncomment the `overrides` block in `rules.yaml`.
2. **Job 100056192** (Manager) has *grant monitoring* twice and no *grant supervising* in
   Mapping.xlsx. The bot gives it the normal Manager permissions; `check-mapping` lists it.
3. **تعديل صلاحية + Manager** — the document does not say to remove old branches, so the
   bot keeps them (`clear_existing_branches: false`). Set `true` if they should be removed.
4. **إعادة تشغيل / الغاء** — the document does not mention the matrix checkbox, so it is not
   ticked (`confirm_matrix_for` in `rules.yaml`).

## 7. Safety

- Credentials only in `.env`, never logged.
- `--dry-run` changes nothing (no re-assign, no CEM edits, no decisions).
- Live runs on the real profile require typing `YES`; `--max N` limits the run.
- After each decision the list is searched again to confirm the ticket really closed.
- Unclear cases are skipped, not guessed.

## 8. The mock systems (for testing)

`python -m mock.server` starts a fake portal on http://127.0.0.1:8701 and a fake CEM on
http://127.0.0.1:8702 built from the screenshots in Logic.docx (all data invented). The seed
data covers every ticket type × category, users found / not found, look-alike ids, a
look-alike branch, an unmapped job code, an unknown ticket type, a ticket from another
system, and one ticket that **fails on purpose** (login name 900116 cannot be verified) to
show the error handling. `/__state` shows the current data, `POST /__reset` restores it.

## 9. Project layout

```
cem_bot/        the bot
  rules.py        decision engine (pure Python, unit-tested)
  mapping.py      reads Mapping.xlsx
  portal.py       Security Code Request portal steps
  cem.py          CEM steps
  web.py          text-based element finding, select2, tables, checkboxes
  runner.py       orchestration, error recovery
  report.py       Excel report + log
config/         settings.yaml, rules.yaml, ui.yaml
data/           Mapping.xlsx
mock/           fake portal + fake CEM (Flask)
tests/          unit tests + end-to-end browser test on the mocks
```
