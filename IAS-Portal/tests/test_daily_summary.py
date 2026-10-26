"""The daily summary (calllog/ops/engines/daily_summary.py): every team's summary to To / Cc and each team of
``summary.team_recipients`` its OWN summary with only its own calls, e-mails, tickets and change requests; the change
requests: implementations completed TODAY only, implementation requests pending ALL. Made-up people and data only."""
from datetime import datetime

from calllog.ops import settings as hub_settings
from calllog.ops.engines import common, daily_summary as ds
from calllog.ops.store import OpsStore
from calllog.storage import Store
from test_ops_api import email_data, email_row, replied_row

NOW = datetime(2026, 10, 11, 13, 5)
DAY = "2026-10-11"


class Mailer:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, to, cc=(), subject="", html_body="", inline_images=None, display_only=False, **kw):
        if any(t in self.fail_for for t in to):
            raise common.MailError("Outlook is busy")
        self.sent.append({"to": list(to), "cc": list(cc), "subject": subject, "html": html_body})


def _user(uid, team, email):
    return {"user_id": uid, "name": f"Person {uid}", "role": "user", "active": True, "team": team, "email": email}


def world(tmp_path):
    """Flex Cube: 2 calls, 2 mails (1 replied), 2 live tickets, an ITSM role with 4 implementation requests waiting and
    2 completed today + 5 completed earlier; Card Management: 1 call, 1 mail, 1 ticket, a role with 2 waiting."""
    data = tmp_path / "data"
    calls = Store(str(data))
    calls.init()
    calls.create_user(_user("7001", "Flex Cube", "f1@example.test"))
    calls.create_user(_user("7002", "Card Management", "c1@example.test"))
    reason = calls.reasons()[0]["key"]
    for i, uid in enumerate(("7001", "7001", "7002")):
        calls.add_record({"id": f"r{i}", "date": DAY, "user_id": uid, "time": f"{DAY}T09:0{i}:00", "reason": reason})
    ops = OpsStore(str(data)).init()
    ops.write_snapshot("email", email_data([
        email_row("<f1@x>"), replied_row("<f2@x>", "f1@example.test"),
        email_row("<c1@x>", team="Card Management", all_teams=["Card Management"], teams_pending=["Card Management"])]),
        generated_at=f"{DAY}T13:00:00")
    ops.write_snapshot("sla", {"requests": [
        {"key": "t1", "security_user": "S1", "status": "within", "fields": {"النظام": "Flexcube"}},
        {"key": "t2", "security_user": "S1", "status": "breached", "fields": {"النظام": "Flexcube"}},
        {"key": "t3", "security_user": "S2", "status": "within", "fields": {"النظام": "CardSys"}}]},
        generated_at=f"{DAY}T13:00:00")
    ops.update_auto_assign(lambda d: d.update(applications=[{"name": "Flexcube", "team": "Flex Cube"},
                                                            {"name": "CardSys", "team": "Card Management"}]))
    roles = [{"role": "IMP Flex role", "error": None, "categories": {
                 "Implementation Requests": {"count": 4, "rows": []}, "Implementation Completed": {"count": 7, "rows": []},
                 "Rollback Requests": {"count": 1, "rows": []}}},
             {"role": "IMP Card role", "error": None, "categories": {
                 "Implementation Requests": {"count": 2, "rows": []}, "Implementation Completed": {"count": 30, "rows": []}}}]
    totals = {"Implementation Requests": 6, "Implementation Completed": 37, "Rollback Requests": 1}
    ops.write_snapshot("itsm", {"roles": roles, "totals": totals, "categories": list(totals)}, generated_at=f"{DAY}T13:00:00")
    records = {f"F{i}": {"number": f"F{i}", "role": "IMP Flex role", "conclusion": "completed",
                         "completed_at": f"{DAY}T10:00:00" if i < 2 else "2026-10-08T10:00:00", "first_seen": "2026-10-08T09:00:00"}
               for i in range(7)}
    records["C0"] = {"number": "C0", "role": "IMP Card role", "conclusion": "completed", "completed_at": f"{DAY}T11:00:00",
                     "first_seen": f"{DAY}T08:00:00"}
    ops.update_itsm_records(lambda d: d.update(records=records))
    return ops, data


def ctx_for(ops, data, summary, mailer, options=None):
    raw = {"email": {"teams": ["Flex Cube", "Card Management", "Unclassified"]},
           "itsm": {"role_teams": {"IMP Flex role": "Flex Cube", "IMP Card role": "Card Management"}},
           "summary": {"enabled": True, **summary}}
    hub, errors = hub_settings.validate(raw)
    assert errors == [], errors
    return common.JobContext(job="daily_summary", settings=hub["summary"], general=hub["general"], store=ops,
                             now=lambda: NOW, log=lambda m: None, mailer=mailer, data_dir=str(data),
                             options=options or {"slot": "13:00"}, hub=hub)


def test_each_team_gets_its_own_summary_with_only_its_own_numbers(tmp_path):
    ops, data = world(tmp_path)
    mailer = Mailer()
    res = ds.run(ctx_for(ops, data, {"to": ["boss@example.test"], "team_recipients": {
        "Flex Cube": {"to": ["flex.lead@example.test"], "cc": []},
        "Card Management": {"to": ["card.lead@example.test"], "cc": ["card.cc@example.test"]}}}, mailer))
    assert res.ok and res.emails_sent == 3, res.error
    by_to = {m["to"][0]: m for m in mailer.sent}
    every, flex, card = by_to["boss@example.test"], by_to["flex.lead@example.test"], by_to["card.lead@example.test"]
    assert "Flex Cube" in flex["subject"] and "Card Management" in card["subject"] and card["cc"] == ["card.cc@example.test"]
    assert "Calls 3" in every["subject"] and "Calls 2" in flex["subject"] and "Calls 1" in card["subject"]
    assert "Mails 3" in every["subject"] and "Mails 2" in flex["subject"] and "Mails 1" in card["subject"]
    assert "CRs pending 6" in every["subject"] and "CRs pending 4" in flex["subject"] and "CRs pending 2" in card["subject"]
    assert "By team" in every["html"] and "By team" not in flex["html"]          # a team's mail is about that team only
    assert "Card Management" not in flex["html"] and "Flex Cube only" in flex["html"]


def test_the_numbers_of_one_team(tmp_path):
    ops, data = world(tmp_path)
    ctx = ctx_for(ops, data, {"to": ["boss@example.test"]}, Mailer())
    flex = ds.collect(ctx, NOW, "Flex Cube")
    assert flex["calls"]["total"] == 2 and flex["email"]["business_emails"] == 2 and flex["email"]["replied"] == 1
    assert flex["tickets"]["live"] == {"open": 2, "within": 1, "reminder": 0, "breached": 1}
    card = ds.collect(ctx, NOW, "Card Management")
    assert card["calls"]["total"] == 1 and card["tickets"]["live"]["open"] == 1


def test_change_requests_completed_today_only_and_pending_all(tmp_path):
    """Implementations completed: only today's (2 of Flex Cube's 7, whatever the board's completed column says);
    implementation requests pending: every one waiting on the board (4), whatever day it came."""
    ops, data = world(tmp_path)
    ctx = ctx_for(ops, data, {"to": ["boss@example.test"]}, Mailer())
    flex = ds.collect(ctx, NOW, "Flex Cube")["crs"]
    assert flex["pending"] == 4 and flex["completed_today"] == 2 and flex["rollback"] == 1
    assert "Implementation Completed" not in flex["board"]                      # not the earlier days' completed ones
    every = ds.collect(ctx, NOW)["crs"]
    assert every["pending"] == 6 and every["completed_today"] == 3
    html = ds.build_html(ds.collect(ctx, NOW, "Flex Cube"), ctx.settings, "13:00", NOW)
    assert "Implementation requests pending (all)" in html and "Implementations completed today" in html


def test_a_team_mail_outlook_refuses_is_sent_at_the_retry_and_the_others_never_twice(tmp_path):
    ops, data = world(tmp_path)
    summary = {"to": ["boss@example.test"], "team_recipients": {"Flex Cube": {"to": ["flex.lead@example.test"], "cc": []}}}
    first = Mailer(fail_for={"flex.lead@example.test"})
    res = ds.run(ctx_for(ops, data, summary, first))
    assert not res.ok and res.emails_sent == 1 and "Flex Cube: Outlook is busy" in res.error
    again = Mailer()
    res = ds.run(ctx_for(ops, data, summary, again))
    assert res.ok and [m["to"] for m in again.sent] == [["flex.lead@example.test"]]   # only the one that failed
    assert ds.run(ctx_for(ops, data, summary, Mailer())).skipped                       # the checkpoint is done


def test_team_summaries_alone_are_enough_to_switch_it_on_and_a_team_must_exist():
    ok, errors = hub_settings.validate({"summary": {"enabled": True, "team_recipients": {
        "Flex Cube": {"to": ["flex.lead@example.test"], "cc": []}}}})
    assert errors == []
    _, errors = hub_settings.validate({"summary": {"enabled": True, "team_recipients": {
        "No Such Team": {"to": ["x@example.test"], "cc": []}}}})
    assert any(e["path"] == "summary.team_recipients.No Such Team" for e in errors)
    _, errors = hub_settings.validate({"summary": {"enabled": True}})
    assert any(e["path"] == "summary.to" for e in errors)


def test_tickets_count_what_reached_the_security_team_not_every_row_registered_today(tmp_path):
    """The owner's summary said «Arrived today 3034, closed 0, executed 0, not executed 0» and counted registrations as
    open with a security user. Now: rows still with the branch are never arrived nor open; a request confirmed today
    whose details are not read yet still counts (from the inquiry's status); «لم يتم التنفيذ» and «تم التعزيز على عدم
    التنفيذ» are both not executed."""
    from calllog.ops import sla_history as sh
    ops, data = world(tmp_path)
    head = list(sh.GRID_COLUMNS.values())

    def row(status, system, executor="مستخدم اول", day="11/10/2026", employee="موظف"):
        v = {"date": day, "form": "نموذج 5", "branch": "فرع", "employee": employee, "employee_no": "700201",
             "user_no": "700301", "system": system, "group": "", "request_type": "اضافة صلاحية", "assigner": "",
             "executor": executor, "checker": "", "notes": "", "status": status}
        return [v[k] for k in sh.GRID_COLUMNS]
    rows = [row("في انتظار اعتماد مشرف الفرع", "Flexcube", executor="", employee=f"موظف {i}") for i in range(40)]
    rows += [row("تم التعزيز على التنفيذ", "Flexcube", employee="أ"), row("تم التعزيز على عدم التنفيذ", "Flexcube", employee="ب"),
             row("لم يتم التنفيذ", "Flexcube", employee="ج"), row("اسناد لمستخدم السرية", "Flexcube", employee="د")]
    items = sh.grid_rows(head, rows)
    grid = sh.grid_state(items, {}, f"{DAY}T08:00:00", None, None)
    ops.update_sla_history_state(lambda d: d.update(grid=grid, index={}, open={}, last_history=f"{DAY}T13:00:00"))
    ctx = ctx_for(ops, data, {"to": ["boss@example.test"]}, Mailer())
    tix = ds.collect(ctx, NOW)["tickets"]
    assert tix["received_today"] == 4                     # the four with the security team - not the 40 at the branch
    assert tix["now"]["pre"] == 40 and tix["now"]["awaiting"] == 1
    assert tix["now"]["open"] == 4 and tix["now"]["live_only"] == 3     # the grid's one + the 3 Live tickets it lacks
    assert tix["closed_today"] == 2 and tix["closed_executed"] == 1 and tix["closed_not"] == 1
    assert tix["executed"] == 1 and tix["not_executed"] == 2 and tix["from_grid"] == 3
    html = ds.build_html(ds.collect(ctx, NOW), ctx.settings, "13:00", NOW)
    assert "Reached the security team today" in html and "Still with the branch now" in html
    assert "Arrived today" not in html and "counted from the inquiry" in html
