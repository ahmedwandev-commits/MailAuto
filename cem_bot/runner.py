"""Orchestrates one run: portal -> CEM -> portal, ticket by ticket."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from .cem import Cem
from .config import Config
from .mapping import load_mapping
from .models import Action, Outcome, Plan, Ticket, TicketResult, TicketType
from .portal import Portal
from .report import Report, new_run_dir, setup_logging
from .rules import Rules
from .web import Web

log = logging.getLogger("cem_bot")


class Runner:
    def __init__(self, cfg: Config, dry_run: bool = False, max_tickets: int | None = None,
                 types: list[str] | None = None, reassign: bool | None = None, headless: bool | None = None,
                 slow_mo: int | None = None, verbose: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        run = cfg.settings["run"]
        self.max_tickets = run.get("max_tickets", 0) if max_tickets is None else max_tickets
        order = [TicketType(t) for t in run["processing_order"]]
        self.order = [t for t in order if not types or t.value in types]
        self.reassign = cfg.settings["portal"].get("do_reassign", True) if reassign is None else reassign
        b = cfg.settings["browser"]
        self.headless = b.get("headless", False) if headless is None else headless
        self.slow_mo = b.get("slow_mo_ms", 0) if slow_mo is None else slow_mo
        self.stop_on_error = run.get("stop_on_error", False)
        self.run_dir = new_run_dir(cfg.path(run.get("output_dir", "runs")), dry_run)
        setup_logging(self.run_dir, verbose)
        self.mapping = load_mapping(cfg.path(run["mapping_file"]), cfg.rules["categories"])
        self.rules = Rules(cfg.rules, self.mapping)
        self.report = Report(self.run_dir, {
            "Started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Profile": cfg.profile,
            "Portal": cfg.portal_base, "CEM": cfg.cem_base, "Operator": cfg.creds.operator_id,
            "Mode": "DRY-RUN (nothing changed)" if dry_run else "LIVE",
            "Ticket types": ", ".join(t.value for t in self.order), "Mapping": self.mapping.source,
        })
        self._shots = 0

    # ------------------------------------------------------------------ main
    def run(self) -> Report:
        log.info("Run folder: %s", self.run_dir)
        log.info("Mode: %s | profile: %s | mapping: %d job codes %s", "DRY-RUN" if self.dry_run else "LIVE",
                 self.cfg.profile, len(self.mapping.jobs), self.mapping.summary())
        for w in self.mapping.warnings:
            log.warning("Mapping: %s", w)
        b = self.cfg.settings["browser"]
        with sync_playwright() as pw:
            launch = dict(headless=self.headless, slow_mo=self.slow_mo)
            if b.get("channel"):
                launch["channel"] = b["channel"]
            browser = pw.chromium.launch(**launch)
            context = browser.new_context(ignore_https_errors=b.get("ignore_https_errors", True),
                                          viewport=b.get("viewport") or {"width": 1600, "height": 900})
            context.set_default_timeout(b.get("default_timeout_ms", 20000))
            context.set_default_navigation_timeout(b.get("navigation_timeout_ms", 45000))
            context.on("page", self._attach_dialog_handler)
            try:
                self._run_in(context)
            finally:
                self.report.meta["Finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.report.save()
                context.close()
                browser.close()
        c = self.report.counts()
        log.info("Finished: %s", ", ".join(f"{k}={v}" for k, v in c.items() if v))
        log.info("Report: %s", self.report.path)
        return self.report

    @staticmethod
    def _attach_dialog_handler(page):
        def on_dialog(d):
            log.info("    page dialog (%s): %s -> accepted", d.type, d.message)
            try:
                d.accept()
            except Exception:
                pass
        page.on("dialog", on_dialog)

    def _run_in(self, context):
        timeout = self.cfg.settings["browser"].get("default_timeout_ms", 20000)
        portal_page = context.new_page()
        self.portal = Portal(Web(portal_page, timeout), self.cfg, self.rules, context)
        self.portal.login()
        self.portal.open_provisioning()
        if self.reassign and not self.dry_run:
            self.portal.reassign_all()
        elif self.reassign:
            log.info("Portal: re-assign skipped in dry-run")

        cem_page = context.new_page()
        self.cem = Cem(Web(cem_page, timeout, exclude_css=self.cfg.ui["cem"]["sidebar_css"]), self.cfg, self.rules)
        self.cem.login()
        self.portal.web.page.bring_to_front()
        self.portal.open_making()

        handled: set[str] = set()
        processed = 0
        tickets = self.portal.list_tickets()
        log.info("Portal: %d CEM ticket(s) in the list", len(tickets))
        for t in tickets:   # tickets that no rule covers are reported once
            if t.ticket_type is None:
                handled.add(t.key)
                self.report.add(self._skip(t, f"unknown ticket type '{t.ticket_type_raw}' - handle manually"))

        for ttype in self.order:
            log.info("=== %s tickets ===", ttype.value)
            while True:
                todo = [t for t in tickets if t.ticket_type == ttype and t.key not in handled]
                if not todo:
                    break
                t = todo[0]
                handled.add(t.key)
                res = self.process(t)
                self.report.add(res)
                processed += 1
                if res.outcome == Outcome.ERROR and self.stop_on_error:
                    log.error("Stopping (stop_on_error=true)")
                    return
                if self.max_tickets and processed >= self.max_tickets:
                    log.info("Reached max_tickets=%d", self.max_tickets)
                    return
                if not self.dry_run:   # list changes after each decision
                    try:
                        self.portal.refresh()
                        tickets = self.portal.list_tickets()
                    except Exception as e:
                        log.error("Could not refresh the ticket list: %s", e)
                        self._recover()
                        tickets = self.portal.list_tickets()

    # ------------------------------------------------------------------ one ticket
    def _skip(self, t: Ticket, why: str, plan: Plan | None = None) -> TicketResult:
        log.info("SKIP  %s %s: %s", t.user_id, t.ticket_type_raw, why)
        return TicketResult(t, plan or Plan(Action.SKIP, t, skip_reason=why), Outcome.SKIPPED, why,
                            started=datetime.now().strftime("%H:%M:%S"))

    def process(self, t: Ticket) -> TicketResult:
        log.info("--- %s | %s | %s | %s", t.user_id, t.user_name, t.ticket_type_raw, t.branch_raw)
        pre = self.rules.precheck(t)
        if pre:
            return self._skip(t, pre.skip_reason, pre)
        started, t0 = datetime.now().strftime("%H:%M:%S"), time.time()
        res = TicketResult(t, None, Outcome.ERROR, started=started)
        try:
            found = self.cem.find_user(t.user_id)
            res.user_existed = bool(found)
            res.steps.append("CEM lookup: " + ("found" if found else "not found"))
            plan = self.rules.build_plan(t, bool(found))
            res.plan = plan
            log.info("    plan: %s", plan.describe())
            if plan.action == Action.SKIP:
                res.outcome, res.message = Outcome.SKIPPED, plan.skip_reason
            elif self.dry_run:
                res.outcome, res.message = Outcome.PLANNED, plan.describe()
            else:
                self._execute(plan, res.steps)
                self.portal.submit_decision(t, plan)
                res.steps.append(f"portal: {plan.decision}")
                if self.cfg.settings["portal"].get("verify_ticket_closed", True):
                    self.portal.verify_closed(t)
                    res.steps.append("ticket closed (verified)")
                res.outcome = Outcome.DONE if plan.decision == self.cfg.rules["decision_text"]["done"] \
                    else Outcome.REJECTED
                res.message = plan.describe()
        except Exception as e:
            res.outcome, res.message = Outcome.ERROR, f"{type(e).__name__}: {e}"
            log.error("    ERROR: %s", res.message)
            log.debug("traceback", exc_info=True)
            res.screenshot = self._screens(t)
            self._recover()
        res.seconds = time.time() - t0
        log.info("    => %s", res.outcome.value)
        return res

    def _execute(self, p: Plan, steps: list[str]):
        cem, uid = self.cem, p.ticket.user_id
        if p.action in (Action.REJECT_NO_CODE, Action.REJECT_ALREADY_EXISTS):
            return
        if p.action == Action.CREATE_USER:
            cem.create_user(uid, self.rules.new_user_name(p.ticket))
            steps.append("CEM: user created")
        else:
            cem.open_user(uid)
            steps.append("CEM: user opened")
        if p.action in (Action.MODIFY_USER, Action.REACTIVATE_USER, Action.CANCEL_USER):
            cem.set_flags(p.set_active, p.set_locked)
            steps.append(f"CEM: active={p.set_active} locked={p.set_locked}")
        if p.action in (Action.MODIFY_USER, Action.CREATE_USER):
            if p.clear_roles or p.role:
                cem.reset_roles(p.role, clear=p.clear_roles)
                steps.append(f"CEM: roles=[{p.role or ''}]")
            cem.set_branch(p.branch_id, p.grants, p.clear_branches)
            steps.append(f"CEM: branch {p.branch_id} + {'/'.join(p.grants)}")
        cem.go_users()

    def _screens(self, t: Ticket) -> str:
        if not self.cfg.settings["run"].get("screenshot_on_error", True):
            return ""
        self._shots += 1
        names = []
        for tag, obj in (("portal", getattr(self, "portal", None)), ("cem", getattr(self, "cem", None))):
            if obj is None:
                continue
            name = f"screenshots/{self._shots:03d}_{t.user_id}_{tag}.png"
            obj.web.screenshot(str(self.run_dir / name))
            names.append(name)
        return ", ".join(names)

    def _recover(self):
        try:
            self.cem.go_users()
        except Exception as e:
            log.warning("    recover CEM failed: %s", e)
        try:
            self.portal.open_making()
        except Exception as e:
            log.warning("    recover portal failed: %s", e)
