"""The user guide (v5.17): each person sees only the sections of their role; the rule numbers are the ones in Settings."""
import re

from .support import add_user, login


def sections(page):
    return set(re.findall(r'<section id="([a-z]+)"', page))


def test_an_employee_sees_only_their_own_pages(app, world, repo):
    page = login(app, "101").get("/guide").get_data(as_text=True)
    assert sections(page) == {"myatt", "mine", "allow"}
    assert "×1.5" in page and "70.4" in page and "100" in page                          # the numbers come from Settings
    assert 'href="#settings"' not in page and 'href="#hr"' not in page and 'href="#roles"' not in page


def test_a_team_manager_sees_their_teams_work_and_their_own_month(app, world, repo):
    add_user(repo, "301", "manager", ["Team A"], name="Mona Boss")                         # a manager with an HR number
    page = login(app, "301").get("/guide").get_data(as_text=True)
    assert sections(page) == {"today", "plan", "leave", "team", "myatt", "mine", "allow", "analysis"}
    assert "مدير الفريق" in page and "المدير العام</h3>" not in page and "الشئون الإدارية</h3>" not in page
    assert "تبويب الفريق" in page and "المشرف" in page


def test_the_general_manager_sees_reviews_not_imports_or_settings(app, world, gm_b):
    page = gm_b.get("/guide").get_data(as_text=True)
    assert {"today", "plan", "team", "allow", "analysis"} <= sections(page)
    assert not {"hr", "settings", "roles", "myatt", "mine"} & sections(page)
    assert "المدير العام</h3>" in page and "مدير الفريق</h3>" not in page


def test_the_administrator_sees_everything_they_run(app, world, admin_b):
    page = admin_b.get("/guide").get_data(as_text=True)
    assert {"roles", "today", "plan", "leave", "team", "allow", "hr", "analysis", "settings"} <= sections(page)


def test_the_menu_opens_the_guide_page(app, world, repo):
    page = login(app, "101").get("/my").get_data(as_text=True)
    assert 'href="/attendance/guide"' in page
