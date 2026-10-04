"""Security Code Request portal: login, re-assign, read tickets, close tickets."""
from __future__ import annotations

import logging

from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PWTimeout

from .config import Config
from .models import Plan, Ticket
from .rules import Rules
from .textnorm import compact, match_score, norm
from .web import Table, UIError, Web

log = logging.getLogger("cem_bot")


class Portal:
    def __init__(self, web: Web, cfg: Config, rules: Rules, context: BrowserContext):
        self.web = web
        self.cfg = cfg
        self.rules = rules
        self.context = context
        self.ui = cfg.ui["portal"]
        self.s = cfg.settings["portal"]

    @property
    def page(self):
        return self.web.page

    # ------------------------------------------------------------ login
    def login(self):
        ui = self.ui["login"]
        log.info("Portal: login as %s", self.cfg.creds.portal_username)
        self.web.goto(self.cfg.portal_url("portal_login"))
        user = self.web.by_selectors(ui["username_selectors"]) or self.web.field_by_label("كود المستخدم", "input")
        pwd = self.web.by_selectors(ui["password_selectors"])
        if user is None or pwd is None:
            raise UIError("Portal login fields not found")
        self.web.fill(user, self.cfg.creds.portal_username)
        self.web.fill(pwd, self.cfg.creds.portal_password)
        self.web.click_text(ui["button_text"])
        self.web.settle()
        if "login.aspx" in self.page.url.lower():
            raise UIError("Portal login failed (still on the login page) - check PORTAL_USERNAME / PORTAL_PASSWORD")

    def open_provisioning(self):
        """Click 'دخول' on the 'User Provisioning Portal-موظف اسناد - مستخدم السرية' row."""
        keywords = [compact(k) for k in self.s["user_group_keywords"]]
        table, pos = None, None
        for t in self.web.tables():
            for i, row in enumerate(t.rows):
                text = compact(" ".join(row))
                if all(k in text for k in keywords):
                    table, pos = t, i
                    break
            if table:
                break
        if table is None:
            log.warning("Portal: user group row not found - opening the portal home page directly")
            self.web.goto(self.cfg.portal_url("portal_home"))
            return
        row = self.web.row_locator(table, pos)
        try:
            with self.context.expect_page(timeout=6000) as info:
                self.web.click_in(row, self.ui["groups"]["enter_text"])
            new_page = info.value
            new_page.wait_for_load_state("domcontentloaded")
            self.web.page = new_page
            log.info("Portal: provisioning portal opened in a new window")
        except PWTimeout:
            log.info("Portal: provisioning portal opened in the same window")
        self.web.settle()
        if "security_assign_maker" not in self.page.url.lower():
            self.web.goto(self.cfg.portal_url("portal_home"))

    # ------------------------------------------------------------ navigation
    def _go_menu(self, text: str, url_key: str):
        target = self.cfg.settings["paths"][url_key].lower()
        try:
            self.web.click_text(text, timeout_ms=5000)
            self.web.settle()
        except UIError:
            pass
        if target not in self.page.url.lower():
            self.web.goto(self.cfg.portal_url(url_key))

    def _search_system(self):
        ui = self.ui["search"]
        sel = self.web.field_by_label(ui["system_label"], "select")
        self.web.choose(sel, self.s["system_search_text"])
        self.web.click_text(ui["search_button_text"])
        self.web.settle()

    def _set_page_size(self):
        loc = self.web.by_selectors(["css=.dataTables_length select", "css=select[name$='_length']", "css=#pageSize"],
                                    timeout_ms=800)
        if loc is None:
            return
        for want in self.s.get("page_size_preference", []):
            try:
                opts = loc.evaluate("(s) => Array.from(s.options).map(o => o.text)")
                if any(match_score(o, want) == 3 for o in opts):
                    self.web.choose(loc, want)
                    return
            except Exception:
                return

    # ------------------------------------------------------------ re-assign
    def reassign_all(self, only_user: str | None = None) -> int:
        """إعادة اسناد: assign CEM tickets to the operator.

        With *only_user* set, only that user's row(s) are re-assigned (and the
        "select all" header checkbox is NOT used). If the user's row cannot be
        located on the re-assign page, nothing is re-assigned - never everyone."""
        ui = self.ui
        op = self.cfg.creds.operator_id
        log.info("Portal: re-assigning %s to %s", f"user {only_user}" if only_user else "all CEM tickets", op)
        self._go_menu(ui["menu"]["reassign_text"], "portal_reassign")
        self._search_system()
        self._set_page_size()
        table = self.web.find_table([ui["columns"]["ticket_type"], ui["columns"]["system"]],
                                    selectors=ui["search"]["results_table_selectors"], timeout_ms=10000,
                                    required=False)
        data_rows = [i for i, r in enumerate(table.rows) if len(r) > 1] if table else []
        if not data_rows:
            log.info("Portal: nothing to re-assign")
            return 0
        if only_user:
            rows = self._rows_for_user(table, data_rows, only_user)
            if not rows:
                log.warning("Portal: no row for user %s on the re-assign page - nothing re-assigned "
                            "(assign it manually, or run without --user)", only_user)
                return 0
        else:
            rows = data_rows
        tloc = self.web.table_locator(table)
        if not only_user:   # tick everything via the header checkbox
            head_cb = tloc.locator("xpath=(./thead//input[@type='checkbox'] | ./tbody/tr[1]/th//input[@type='checkbox'])[1]")
            if head_cb.count():
                self.web.set_checkbox(head_cb.first, True)
        for i in rows:   # make sure the wanted row(s) are ticked
            cb = self.web.row_locator(table, i).locator("input[type=checkbox]")
            if cb.count() and not cb.first.is_checked():
                self.web.set_checkbox(cb.first, True)
        assignee = self.web.by_selectors(ui["reassign"]["assignee_select_selectors"], timeout_ms=500) \
            or tloc.locator("xpath=following::select[1]").first
        self.web.choose(assignee, op, type_text=op)
        self._click_after(assignee, ui["reassign"]["execute_text"])
        self.web.settle()
        log.info("Portal: re-assigned %d ticket(s)", len(rows))
        return len(rows)

    def _rows_for_user(self, table: Table, data_rows: list[int], user_id: str) -> list[int]:
        """Row positions whose user-id cell equals *user_id* (already normalised).
        The re-assign grid may label the id column رقم المستخدم / رقم الموظف rather
        than رقم الوظيفى, so several candidate headers are tried."""
        c = self.ui["columns"]
        candidates, seen = [], set()
        for name in [c["user_id"], "رقم المستخدم", "رقم الموظف"]:
            if name and compact(name) not in seen:
                seen.add(compact(name))
                idx = table.col(name, required=False)
                if idx is not None:
                    candidates.append(idx)
        if not candidates:
            return []
        return [i for i in data_rows
                if any(ci < len(table.rows[i]) and norm(table.rows[i][ci]) == user_id for ci in candidates)]

    def _click_after(self, anchor, text: str):
        """Click the first element with *text* that comes after *anchor* in the page."""
        for cand in self.web.find_text_all(text, "click", "exact"):
            follows = anchor.evaluate("(a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)",
                                      cand.element_handle())
            if follows:
                cand.scroll_into_view_if_needed()
                cand.click()
                return
        self.web.click_text(text)

    # ------------------------------------------------------------ tickets
    def open_making(self):
        self._go_menu(self.ui["menu"]["making_text"], "portal_making")
        self.refresh()

    def refresh(self):
        self._search_system()
        self._set_page_size()

    def _results_table(self, required=False) -> Table | None:
        c = self.ui["columns"]
        return self.web.find_table([c["user_id"], c["ticket_type"]],
                                   selectors=self.ui["search"]["results_table_selectors"],
                                   timeout_ms=8000, required=required)

    def list_tickets(self) -> list[Ticket]:
        table = self._results_table()
        if table is None:
            return []
        c = self.ui["columns"]
        cols = {"user_id": c["user_id"], "user_name": c["user_name"], "ticket_type": c["ticket_type"],
                "branch": c["branch"], "job_title": c["job_title"]}
        recs = table.records(cols)
        opt = {"request_date": c["request_date"], "system": c["system"]}
        opt_idx = {k: table.col(v, required=False) for k, v in opt.items()}
        tickets = []
        data_rows = [r for r in table.rows if len(r) > 1]
        for rec, row in zip(recs, data_rows):
            extra = {k: (row[i] if i is not None and i < len(row) else "") for k, i in opt_idx.items()}
            t = Ticket(user_id=norm(rec["user_id"]), user_name=rec["user_name"].strip(),
                       ticket_type_raw=rec["ticket_type"].strip(), branch_raw=rec["branch"].strip(),
                       job_title_raw=rec["job_title"].strip(), request_date=extra["request_date"].strip(),
                       system=extra["system"].strip(), raw=dict(zip(table.headers, row)))
            if t.system and compact(self.s["system_search_text"]) not in compact(t.system):
                continue   # not a CEM ticket
            t.ticket_type = self.rules.classify(t.ticket_type_raw)
            tickets.append(t)
        return tickets

    def _find_row(self, ticket: Ticket):
        table = self._results_table(required=True)
        c = self.ui["columns"]
        iu, it = table.col(c["user_id"]), table.col(c["ticket_type"])
        idt = table.col(c["request_date"], required=False)
        for pos, row in enumerate(table.rows):
            if len(row) <= max(iu, it):
                continue
            if norm(row[iu]) != ticket.user_id or compact(row[it]) != compact(ticket.ticket_type_raw):
                continue
            if idt is not None and ticket.request_date and compact(row[idt]) != compact(ticket.request_date):
                continue
            return table, pos
        return None, None

    def submit_decision(self, ticket: Ticket, plan: Plan):
        ui = self.ui["decision"]
        table, pos = self._find_row(ticket)
        if table is None:
            raise UIError(f"Ticket row for {ticket.user_id} no longer in the list")
        self.web.click_in(self.web.row_locator(table, pos), ui["choose_text"])
        self.web.settle()

        dec = self.web.field_by_label(ui["decision_label"], "select", selectors=ui["decision_select_selectors"])
        self.web.choose(dec, plan.decision)
        if plan.reason:
            rs = self.web.field_by_label(ui["reasons_label"], "select", selectors=ui["reasons_select_selectors"])
            self.web.choose(rs, plan.reason)
        if plan.notes:
            ta = self.web.field_by_label(ui["notes_label"], "textarea", selectors=ui["notes_selectors"])
            self.web.fill(ta, plan.notes)
        if plan.confirm_matrix:
            self.web.set_checkbox(self._matrix_checkbox(), True)
        self._click_after(dec, ui["execute_text"])
        self.web.settle()

    def _matrix_checkbox(self):
        ui = self.ui["decision"]
        loc = self.web.by_selectors(ui["matrix_checkbox_selectors"], timeout_ms=500)
        if loc is not None:
            return loc
        label = self.web.find_text(ui["matrix_checkbox_text"], "label", "prefix")
        mark = label.evaluate(r"""(el) => {
            let box = null;
            if (el.tagName === 'LABEL' && el.htmlFor) box = document.getElementById(el.htmlFor);
            let p = el;
            for (let i = 0; !box && p && i < 4; i++, p = p.parentElement)
                box = p.querySelector('input[type=checkbox]');
            if (!box) return null;
            box.setAttribute('data-botmark', 'matrixbox');
            return true; }""")
        if not mark:
            raise UIError("Matrix confirmation checkbox not found")
        return self.page.locator('[data-botmark="matrixbox"]').first

    def verify_closed(self, ticket: Ticket):
        self.refresh()
        if any(t.key == ticket.key for t in self.list_tickets()):
            msg = self._page_message()
            raise UIError("Ticket is still open after 'تنفيذ'" + (f" - page says: {msg}" if msg else ""))

    def _page_message(self) -> str:
        text = self.web.page_text()
        for line in text.splitlines():
            if any(norm(e) in norm(line) for e in self.ui["decision"]["error_texts"]) and len(line) < 200:
                return line.strip()
        return ""
