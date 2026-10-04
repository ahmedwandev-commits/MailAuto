"""SEDCO CEM: find / create users, flags, roles, branches and branch grants."""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Locator

from .config import Config
from .rules import Rules
from .textnorm import best_match, compact, norm
from .web import UIError, Web

log = logging.getLogger("cem_bot")

_JS_STATE = r"""(el) => {
  const row = el.closest('tr');
  let html = '';
  if (row) html = row.innerHTML;
  else { let s = el.nextElementSibling; html = s ? s.outerHTML : (el.parentElement ? el.parentElement.innerHTML : ''); }
  html = html.toLowerCase();
  const neg = ['fa-times', 'fa-close', 'glyphicon-remove', '✖', '✗', '✘', '×', 'icon-remove', 'unchecked', 'false'];
  const pos = ['fa-check', 'glyphicon-ok', '✔', '✓', 'icon-ok', 'checked', 'true'];
  if (neg.some(n => html.includes(n))) return false;
  if (pos.some(p => html.includes(p))) return true;
  return null; }"""


class Cem:
    def __init__(self, web: Web, cfg: Config, rules: Rules):
        self.web = web
        self.cfg = cfg
        self.rules = rules
        self.ui = cfg.ui["cem"]
        self.tb = self.ui["toolbar"]
        self.read_url: str | None = None

    @property
    def page(self):
        return self.web.page

    # ------------------------------------------------------------ basics
    def login(self):
        ui = self.ui["login"]
        log.info("CEM: login as %s", self.cfg.creds.cem_username)
        self.web.goto(self.cfg.cem_url("cem_login"))
        user = self.web.by_selectors(ui["username_selectors"])
        pwd = self.web.by_selectors(ui["password_selectors"])
        if user is None or pwd is None:
            raise UIError("CEM login fields not found")
        self.web.fill(user, self.cfg.creds.cem_username)
        self.web.fill(pwd, self.cfg.creds.cem_password)
        self.web.click_text(ui["button_text"])
        self.web.settle()
        if "/login" in self.page.url.lower():
            raise UIError("CEM login failed (still on the login page) - check CEM_USERNAME / CEM_PASSWORD")

    def toolbar(self, key: str, timeout_ms: int | None = None):
        self.web.click_text(self.tb[key], exclude_sidebar=True, timeout_ms=timeout_ms)
        self.web.settle()

    def go_users(self):
        """'Click on users' - back to the users list."""
        self.web.goto(self.cfg.cem_url("cem_users"))

    def back_to_user(self):
        if not self.read_url:
            raise UIError("No CEM user is open")
        self.web.goto(self.read_url)

    # ------------------------------------------------------------ search
    def find_user(self, user_id: str) -> dict | None:
        """Search the users list; only an exact Login Name match counts."""
        user_id = norm(user_id)
        if self.cfg.settings["cem"].get("user_search_mode", "searchbox") == "url":
            self.web.goto(self.cfg.cem_url("cem_users_filter", user_id=user_id))
        else:
            self.go_users()
            box = self.web.by_selectors(self.ui["users_search_selectors"], timeout_ms=4000)
            if box is None:
                log.debug("CEM: search box not found, using filter URL")
                self.web.goto(self.cfg.cem_url("cem_users_filter", user_id=user_id))
            else:
                self.web.fill(box, user_id)
                box.press("Enter")
                self.web.settle()
        return self._match_user_row(user_id)

    def _match_user_row(self, user_id: str, wait_s: float = 6.0) -> dict | None:
        cols = self.ui["columns"]
        deadline = time.time() + wait_s
        while True:
            table = self.web.find_table([cols["login_name"]], timeout_ms=4000, required=False)
            if table:
                il, inm = table.col(cols["login_name"]), table.col(cols["name"], required=False)
                rows = [r for r in table.rows if len(r) > il]
                # wait until the grid shows filtered results (AJAX grids)
                if all(user_id in compact(" ".join(r)) for r in rows) or time.time() > deadline:
                    for pos, r in enumerate(table.rows):
                        if len(r) > il and norm(r[il]) == user_id:
                            return {"table": table, "pos": pos, "name": r[inm] if inm is not None else user_id}
                    return None
            elif time.time() > deadline:
                return None
            time.sleep(0.4)

    def open_user(self, user_id: str):
        hit = self.find_user(user_id)
        if not hit:
            raise UIError(f"User {user_id} not found in CEM")
        row = self.web.row_locator(hit["table"], hit["pos"])
        try:
            self.web.click_in(row, hit["name"])
        except UIError:
            row.click()
        self.web.settle()
        if "/user/read" not in self.page.url.lower():
            raise UIError(f"Opening user {user_id} did not show the user page ({self.page.url})")
        self.read_url = self.page.url

    # ------------------------------------------------------------ edit
    def set_flags(self, active: bool | None, locked: bool | None):
        self.back_to_user()
        self.toolbar("edit")
        f = self.ui["fields"]
        if active is not None:
            self.web.set_checkbox(self.web.field_by_label(f["user_active"], "checkbox"), active)
        if locked is not None:
            self.web.set_checkbox(self.web.field_by_label(f["user_locked"], "checkbox"), locked)
        self._click_ok()
        if "/edit" in self.page.url.lower():
            raise UIError("CEM did not save the user (still on the Edit page): " + self._error_text())
        log.info("    CEM: user active=%s locked=%s", active, locked)

    def _click_ok(self):
        ok = self.web.find_text(self.tb["ok"], "click", "exact", exclude_css=self.ui["sidebar_css"])
        ok.scroll_into_view_if_needed()
        ok.click()
        self.web.settle()

    def _error_text(self) -> str:
        for sel in ["css=.err", "css=.error", "css=.validation-summary-errors", "css=.alert-danger", "css=.text-danger"]:
            loc = self.web.by_selectors([sel], timeout_ms=200)
            if loc is not None:
                return loc.inner_text().strip()[:200]
        return ""

    # ------------------------------------------------------------ roles / branches lists
    def _main_grid(self, first_col: str = "Name", extra: str | None = None):
        keys = [first_col] + ([extra] if extra else [])
        return self.web.find_table(keys, timeout_ms=8000)

    def _data_rows(self, table):
        return [i for i, r in enumerate(table.rows) if len(r) > 1 and any(c.strip() for c in r)]

    def remove_all(self, extra_col: str | None = None) -> int:
        table = self._main_grid(self.ui["columns"]["name"], extra_col)
        rows = self._data_rows(table)
        if not rows:
            return 0
        for i in rows:
            cb = self.web.row_locator(table, i).locator("input[type=checkbox]")
            if cb.count():
                self.web.set_checkbox(cb.first, True)
            else:
                self.web.row_locator(table, i).click()
        self.toolbar("remove_from_user")
        deadline = time.time() + 10
        while time.time() < deadline:
            t = self._main_grid(self.ui["columns"]["name"], extra_col)
            if not self._data_rows(t):
                return len(rows)
            time.sleep(0.4)
        raise UIError("Items were not removed from the user")

    def _open_modal(self, title: str) -> Locator:
        self.toolbar("add_existing")
        deadline = time.time() + self.web.timeout_ms / 1000
        while time.time() < deadline:
            for css in self.ui["modal"]["container_selectors"]:
                loc = self.page.locator(css).filter(has_text=title)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            time.sleep(0.3)
        raise UIError(f"Dialog '{title}' did not open")

    def add_existing(self, title: str, column: str, value: str, exact: bool = True) -> str:
        """Add Existing -> pick the row whose *column* matches *value* -> OK.
        Returns the Name of the added item."""
        modal = self._open_modal(title)
        search = modal.locator("input[type=text], input[type=search]")
        if search.count() and search.first.is_visible():
            search.first.fill(value)
            search.first.press("Enter")
            time.sleep(0.6)
        cols = self.ui["columns"]
        deadline = time.time() + 10
        pick = None
        while time.time() < deadline and pick is None:
            handle = modal.element_handle()
            data = self.page.evaluate(
                "(m) => Array.from(m.querySelectorAll('table')).map(t => Array.from(document.querySelectorAll('table')).indexOf(t))",
                handle)
            for t in self.web.tables():
                if t.index not in data:
                    continue
                ic = t.col(column, required=False)
                inm = t.col(cols["name"], required=False)
                if ic is None:
                    continue
                rows = [(p, r) for p, r in enumerate(t.rows) if len(r) > ic]
                if exact:
                    hit = next(((p, r) for p, r in rows if compact(r[ic]) == compact(value)), None)
                else:
                    hit = best_match(rows, value, key=lambda pr: _role_key(pr[1][ic]))
                    if hit is None:
                        hit = best_match(rows, _role_key(value), key=lambda pr: _role_key(pr[1][ic]))
                if hit:
                    pick = (t, hit[0], hit[1][inm] if inm is not None else hit[1][ic])
                    break
            if pick is None:
                time.sleep(0.4)
        if pick is None:
            self.web.click_in(modal, self.tb["cancel"])
            raise UIError(f"'{value}' not found in '{title}' (it may already be assigned)")
        table, pos, name = pick
        row = self.web.row_locator(table, pos)
        cb = row.locator("input[type=checkbox]")
        if cb.count():
            self.web.set_checkbox(cb.first, True)
        else:
            row.click()
        self.web.click_in(modal, self.tb["ok"])
        self.web.settle()
        return name.strip()

    def reset_roles(self, role: str | None, clear: bool = True):
        """Roles: remove everything, then add *role* (if any)."""
        self.back_to_user()
        self.toolbar("roles")
        removed = self.remove_all() if clear else 0
        if removed:
            log.info("    CEM: removed %d role(s)", removed)
        if role:
            name = self.add_existing(self.ui["modal"]["roles_title"], self.ui["columns"]["name"], role, exact=False)
            t = self._main_grid()
            if not any(compact(r[c]) == compact(name) for r in t.rows for c in range(len(r))):
                raise UIError(f"Role '{role}' was not added")
            log.info("    CEM: role '%s' added", name)

    def set_branch(self, branch_id: str, grants: list[str], clear: bool):
        self.back_to_user()
        self.toolbar("branches")
        g = self.ui["grants"]
        any_grant_col = g["serving"]["setting"]
        if clear:
            removed = self.remove_all(any_grant_col)
            if removed:
                log.info("    CEM: removed %d branch(es)", removed)
        name = self.add_existing(self.ui["modal"]["branches_title"], self.ui["columns"]["identity"], branch_id)
        log.info("    CEM: branch %s (%s) added", branch_id, name)
        table = self._main_grid(self.ui["columns"]["name"], any_grant_col)
        inm = table.col(self.ui["columns"]["name"])
        pos = next((p for p, r in enumerate(table.rows) if len(r) > inm and compact(r[inm]) == compact(name)), None)
        if pos is None:
            raise UIError(f"Branch '{name}' not shown after adding it")
        row = self.web.row_locator(table, pos)
        try:
            self.web.click_in(row, name)
        except UIError:
            row.click()
        self.web.settle()
        for key in grants:
            self.grant(key)
        log.info("    CEM: granted %s", ", ".join(grants))

    def grant(self, key: str):
        labels = self.ui["grants"][key]
        if self.web.exists_text(labels["deny"], exclude_sidebar=True) or self._setting_state(labels["setting"]) is True:
            log.debug("    %s already granted", key)
            return
        self.web.click_text(labels["grant"], exclude_sidebar=True)
        self.web.settle()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.web.exists_text(labels["deny"], exclude_sidebar=True) or self._setting_state(labels["setting"]) is True:
                return
            time.sleep(0.4)
        raise UIError(f"'{labels['grant']}' did not take effect")

    def _setting_state(self, label: str):
        for lab in self.web.find_text_all(label, "label", "exact", exclude_css=self.ui["sidebar_css"]):
            try:
                st = lab.evaluate(_JS_STATE)
            except Exception:
                continue
            if st is not None:
                return st
        return None

    # ------------------------------------------------------------ create
    def create_user(self, user_id: str, display_name: str):
        f = self.ui["fields"]
        nr = self.cfg.rules["new_user"]
        self.go_users()
        try:
            self.toolbar("new", timeout_ms=5000)
        except UIError:
            self.web.goto(self.cfg.cem_url("cem_user_create"))
        if "/create" not in self.page.url.lower():
            self.web.goto(self.cfg.cem_url("cem_user_create"))

        self.web.fill(self.web.field_by_label(f["name_en"], "input"), display_name)
        self.web.fill(self.web.field_by_label(f["name_ar"], "input"), display_name)
        self.web.choose(self.web.field_by_label(f["auth_type"], "select"), nr["authentication_type"])
        self.web.fill(self.web.field_by_label(f["login_name"], "input"), user_id)
        self.web.click_text(self.tb["verify"], exclude_sidebar=True)
        self._wait_verified(user_id)

        dept = self.web.field_by_label(f["department"], "select", timeout_ms=3000, required=False)
        if dept is not None:
            self.web.choose(dept, nr["department"])
        else:   # type-ahead combobox
            box = self.web.field_by_label(f["department"], "input")
            self.web.fill(box, nr["department"])
            self.web.click_text(nr["department"], exclude_sidebar=True, timeout_ms=5000)
        self.web.set_checkbox(self.web.field_by_label(f["user_active"], "checkbox"), True)
        self.web.set_checkbox(self.web.field_by_label(f["user_locked"], "checkbox"), False)
        self._click_ok()
        if "/create" in self.page.url.lower():
            raise UIError("CEM did not create the user: " + (self._error_text() or "unknown error"))
        self.read_url = self.page.url
        log.info("    CEM: user %s created ('%s')", user_id, display_name)

    def _wait_verified(self, user_id: str):
        v = self.ui["verify"]
        deadline = time.time() + v.get("wait_ms", 8000) / 1000
        while time.time() < deadline:
            if self.web.by_selectors(v["failure_selectors"], timeout_ms=0) is not None:
                raise UIError(f"Login name {user_id} could not be verified in CEM")
            if self.web.by_selectors(v["success_selectors"], timeout_ms=0) is not None:
                return
            time.sleep(0.3)
        raise UIError(f"No 'verified' confirmation for login name {user_id} - check cem.verify selectors in ui.yaml")


def _role_key(text: str) -> str:
    """'Branch Supervisor' and 'Branch Superviser' are the same role."""
    return compact(text).replace("supervisor", "superviser")
