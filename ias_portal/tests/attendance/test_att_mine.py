"""My schedule (an employee's own shifts) and the employee's side menu: My work (today), E-mail (own page), My schedule."""
from .support import add_user, login


def test_an_employee_sees_only_their_own_schedule(app, world, repo):
    b = login(app, "101")
    page = b.get("/attendance/my").get_data(as_text=True)
    assert "My schedule" in page and "Coming up" in page and "my-upcoming" in page and "my-month" in page and "Ali A1" in page
    assert "Morning" in page.split("my-upcoming")[1]                                    # 101 is on Morning in October
    assert "Sara A2" not in page and "Omar" not in page and "Nour" not in page
    month = page.split('id="my-month"')[1]
    row = next(r for r in month.split("<tr") if "12 Oct 2026" in r)
    assert "07:10" in row and "15:00" in row                                            # the real check-in / check-out of 12 Oct
    assert "10 min late" in row or "late" in row.lower()
    other = b.get("/attendance/my?emp=201&id=201&user=201").get_data(as_text=True)                # no parameter can show somebody else
    assert "Omar" not in other and "Ali A1" in other
    assert login(app, "201").get("/attendance/my").get_data(as_text=True).count("Ali A1") == 0


def test_the_month_can_be_changed_and_a_bad_month_falls_back(app, world, repo):
    b = login(app, "101")
    assert "November 2026" in b.get("/attendance/my?year=2026&month=11").get_data(as_text=True)
    assert "October 2026" in b.get("/attendance/my?year=2026&month=13").get_data(as_text=True)     # today's month (the clock is in October)
    assert b.get("/attendance/my?year=abc&month=x").status_code == 200


def test_an_employee_cannot_open_any_other_attendance_page(app, world, repo):
    b = login(app, "101")
    nav = b.get("/attendance/my").get_data(as_text=True).split('aria-label="Main"')[1].split("</nav>")[0]   # the portal's menu
    assert "My schedule" in nav and ">Today<" not in nav and ">Schedule<" not in nav and "Attendance settings" not in nav
    assert b.get("/").headers["Location"].endswith("/attendance/my")
    for url in ("/planning/day", "/team", "/employees", "/reports", "/schedule", "/admin/settings", "/admin/roles", "/hr/import", "/modify",
                "/allowance", "/api/overview", "/employee/101"):
        assert b.get(url).status_code == 403, url
    assert b.post("/leave/new", {"emp": "101", "type": "annual", "from": "2026-10-20", "to": "2026-10-20"}).status_code == 403


def test_an_account_that_is_not_an_attendance_employee_has_no_access(app, world, repo):
    add_user(repo, "alice", "user", [])                                                 # not an HR number
    assert login(app, "alice").get("/attendance/my").status_code == 403
    assert login(app, "alice").get("/planning/day").status_code == 403


def test_managers_and_the_allowance_account_keep_their_own_pages(app, world, repo, admin_b, mgr1_b):
    assert mgr1_b.get("/planning/day").status_code == 200 and mgr1_b.get("/team").status_code == 200
    admin_b.post("/admin/roles", {"action": "assign_allowance", "user_id": "101"})
    b = login(app, "101")
    nav = b.get("/attendance/my").get_data(as_text=True).split('aria-label="Main"')[1].split("</nav>")[0]
    assert "My schedule" in nav and "Shift allowance" in nav and ">Today<" not in nav
    assert b.get("/allowance").status_code == 200 and b.get("/planning/day").status_code == 403
    assert login(app, "102").get("/allowance").status_code == 403                      # the others do not get it


def test_the_employee_side_menu_has_my_work_email_and_my_schedule(app, world, repo):
    b = login(app, "101")
    html = b.client.get("/my").get_data(as_text=True)                                  # the portal's own My work page
    menu = html.split('aria-label="Main"')[1].split("</nav>")[0]
    assert 'href="/my"' in menu and 'href="/my/email"' in menu and "My work" in menu and "E-mail" in menu and "My schedule" in menu
    assert 'href="/attendance/my"' in menu and "Attendance<" not in menu               # the link of an employee is called My schedule


def test_my_work_and_the_email_page_are_two_pages(app, world, repo, admin_b):
    b = login(app, "101")
    work, mail = b.client.get("/my").get_data(as_text=True), b.client.get("/my/email").get_data(as_text=True)
    assert 'id="my-page"' in work and 'data-mode="email"' not in work and "/static/my.js" in work
    assert 'id="my-page"' in mail and 'data-mode="email"' in mail and "/static/my.js" in mail and "E-mail · IAS Portal" in mail
    assert b.client.get("/my/email?as=102").headers["Location"].endswith("/my/email")   # an employee only opens their own
    assert admin_b.client.get("/my/email").headers["Location"].endswith("/hub")          # an administrator names a person
    assert admin_b.client.get("/my/email?as=101").status_code == 200
    assert admin_b.client.get("/my/email?as=nobody").status_code == 404


def test_my_js_has_a_summary_mode_and_an_email_mode():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "program" / "calllog" / "static" / "my.js").read_text(encoding="utf-8")
    assert 'page.dataset.mode === "email"' in js and "emailSummaryCard" in js and "My e-mails today" in js
    assert "My quality reviews today" in js and "My SLA today" in js and "/my/email?mail=" in js     # the notification link still works
    assert "qualityCard(d.quality, null)" in js and "qualityCard(d.quality, d.date)" in js            # today on My work, every review (+ appeals) on E-mail
    assert "last 7 days" not in js.lower() and "Yesterday" not in js                                  # My work is only about today
