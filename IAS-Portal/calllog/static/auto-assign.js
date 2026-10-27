"use strict";
/* Auto Assign page (/auto-assign): a manager enables + configures auto-assign for their team(s); an administrator also
   manages the applications and the team each belongs to. Rules are {employees, applications, active window}; the e-mail
   target distributes the team's PENDING e-mails to its employees at once, the ticketing target drives the portal.

   Everything is selectable (checkbox multi-selects, team dropdown, time inputs). Strict CSP: no inline styles / scripts,
   every scraped name goes in with textContent (CL.h). */
(function () {
  const { h, api, toast } = CL;
  const page = document.getElementById("auto-assign-page");
  const IS_ADMIN = page.dataset.role === "admin";
  const I = (name) => OPS.icon(name);
  let state = null;

  async function load() {
    try {
      state = await api("GET", "/api/ops/auto-assign");
      render();
      pollTicketing();                            // show an in-progress / last ticketing run, if any
    } catch (e) {
      page.replaceChildren(OPS.emptyState({ title: "Auto Assign cannot be shown", text: e.message, icon: "alert",
        action: h("button", { class: "btn btn-ghost", type: "button", onclick: load }, I("refresh"), "Try again") }));
    }
  }

  // --------------------------------------------------------- a checkbox multi-select
  function checkGroup(items, selected, label) {
    const box = h("div", { class: "aa-check", role: "group", "aria-label": label });
    if (!items.length) return h("p", { class: "muted" }, "Nobody to choose yet.");
    items.forEach((it) => {
      const cb = h("input", { type: "checkbox", value: it.value });
      cb.checked = selected.includes(it.value);
      cb.addEventListener("change", () => {
        const i = selected.indexOf(it.value);
        if (cb.checked && i < 0) selected.push(it.value);
        if (!cb.checked && i >= 0) selected.splice(i, 1);
      });
      box.appendChild(h("label", { class: "aa-chk" }, cb, h("span", { dir: "auto" }, it.label)));
    });
    return box;
  }

  // --------------------------------------------------------- applications (admin only)
  function applicationsCard() {
    const apps = state.applications || [];
    const nameInput = h("input", { type: "text", class: "search", placeholder: "Application name, e.g. Flexcube", "aria-label": "Application name" });
    const teamSel = h("select", { "aria-label": "Team" }, h("option", { value: "" }, "Choose a team…"),
      ...(state.all_teams || []).map((t) => h("option", { value: t }, t)));
    async function add() {
      const name = nameInput.value.trim(), team = teamSel.value;
      if (!name || !team) { toast("Type an application name and choose a team.", "error"); return; }
      try {
        const r = await api("POST", "/api/ops/auto-assign/apps", { name, team });
        state.applications = r.applications; nameInput.value = ""; teamSel.value = ""; render(); toast("Application added.");
      } catch (e) { toast(e.message, "error"); }
    }
    async function remove(name) {
      try { state.applications = (await api("DELETE", "/api/ops/auto-assign/apps?name=" + encodeURIComponent(name))).applications; render(); }
      catch (e) { toast(e.message, "error"); }
    }
    const rows = apps.length ? apps.map((a) => h("tr", {},
      h("td", { dir: "auto" }, a.name), h("td", {}, a.team),
      h("td", {}, h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => remove(a.name) }, I("trash"), "Remove"))))
      : [h("tr", {}, h("td", { colspan: "3", class: "muted" }, "No applications yet — add the systems your teams handle."))];
    return OPS.card("Applications", { hint: "each application belongs to one team; a team sees only its own" },
      h("div", { class: "op-toolbar" }, nameInput, teamSel,
        h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: add }, I("plus"), "Add application")),
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "Application"), h("th", {}, "Team"), h("th", {}, ""))),
        h("tbody", {}, ...rows))));
  }

  // --------------------------------------------------------- one rule
  function ruleEditor(rule, members, apps, onRemove, kind) {
    rule.employees = rule.employees || [];
    rule.applications = kind === "email" ? [] : (rule.applications || []);   // e-mail: every pending mail of the team
    const emp = checkGroup(members.map((m) => ({ value: m.email, label: m.name })), rule.employees, "Employees");
    const appCtrl = kind === "email" ? null : apps.length
      ? checkGroup(apps.map((a) => ({ value: a, label: a })), rule.applications, "Applications")
      : h("p", { class: "muted" }, "No applications set for this team — this rule covers every item.");
    const from = h("input", { type: "time", value: rule.from || "", "aria-label": "Active from" });
    const to = h("input", { type: "time", value: rule.to || "", "aria-label": "Active to" });
    from.addEventListener("change", () => { rule.from = from.value; });
    to.addEventListener("change", () => { rule.to = to.value; });
    return h("div", { class: "aa-rule" },
      h("div", { class: "aa-rule-head" }, h("strong", {}, "Rule"),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: onRemove }, I("x"), "Remove")),
      h("div", { class: "aa-field" }, h("label", {}, "Send to"), emp),
      appCtrl ? h("div", { class: "aa-field" }, h("label", {}, "For applications"), appCtrl)
        : h("p", { class: "hint" }, "Every pending e-mail of the team is shared between these members (the least loaded first)."),
      h("div", { class: "aa-field aa-times" }, h("label", {}, "Active window"), from, h("span", { class: "muted" }, "to"), to,
        h("span", { class: "hint" }, "leave empty for all day")));
  }

  // --------------------------------------------------------- one target (email / ticketing) of one team
  function targetCard(team, kind, info) {
    const block = JSON.parse(JSON.stringify(info[kind] || { enabled: false, rules: [] }));
    const members = info.members || [];
    const apps = info.applications || [];
    const enable = h("input", { type: "checkbox" });
    enable.checked = !!block.enabled;
    enable.addEventListener("change", () => { block.enabled = enable.checked; });
    const interval = h("input", { type: "number", min: "1", max: "1440", step: "1", class: "aa-interval",
      value: block.interval_minutes || 10, "aria-label": "Run auto-assign every how many minutes" });
    interval.addEventListener("change", () => { block.interval_minutes = Math.max(1, Math.min(1440, parseInt(interval.value, 10) || 10)); });
    const what = kind === "email" ? "e-mails" : "requests";
    const maxPerRun = h("input", { type: "number", min: "0", max: "1000", step: "1", class: "aa-interval aa-max",
      value: block.max_per_run || 0, "aria-label": `At most how many ${what} each run assigns (0 = no limit)`,
      title: `At most this many ${what} are assigned in each run; the rest wait for the next run. 0 = no limit.` });
    maxPerRun.addEventListener("change", () => {
      block.max_per_run = Math.max(0, Math.min(1000, parseInt(maxPerRun.value, 10) || 0));
      maxPerRun.value = block.max_per_run;
    });
    const rulesWrap = h("div", { class: "aa-rules" });
    function drawRules() {
      rulesWrap.replaceChildren(...(block.rules.length
        ? block.rules.map((r, i) => ruleEditor(r, members, apps, () => { block.rules.splice(i, 1); drawRules(); }, kind))
        : [h("p", { class: "muted" }, "No rules yet — add one to start distributing.")]));
    }
    drawRules();
    async function save() {
      try {
        const r = await api("PUT", "/api/ops/auto-assign/team",
          { team, kind, enabled: block.enabled, interval_minutes: block.interval_minutes, max_per_run: block.max_per_run || 0,
            rules: block.rules });
        info[kind] = r.block;
        toast(kind === "email" && r.assigned ? `Saved — ${r.assigned} e-mail(s) distributed.` : "Saved.");
        render();
      } catch (e) { toast(e.message, "error"); }
    }
    const applyBtn = kind === "email"
      ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: distributeNow }, I("refresh"), "Distribute now")
      : h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => runTicketing(team, (info.ticketing || {}).interval_minutes || 10, (info.ticketing || {}).max_per_run || 0) }, I("refresh"), "Run now");
    const note = kind === "ticketing"
      ? h("p", { class: "op-note op-t-info op-block" }, I("info"),
        h("span", {}, "These rules drive the portal. Press Run now and type your own portal login to assign new requests to your team — once, or again every “Run every” minutes until you press Stop (your password is kept only in the server's memory, never saved). “Max per run” caps how many requests each run assigns (0 = no limit); the rest wait for the next run — save it before Run now. Each employee needs a “Number in the security portal” set in Admin › Users."))
      : null;
    return h("section", { class: "card op-fieldset aa-target" },
      h("div", { class: "card-head" }, h("h3", {}, kind === "email" ? "E-mail auto-assign" : "Ticketing auto-assign"),
        h("span", { class: "grow" }),
        h("label", { class: "aa-interval-field" }, h("span", {}, "Run every"), interval, h("span", { class: "muted" }, "min")),
        h("label", { class: "aa-interval-field" }, h("span", {}, "Max per run"), maxPerRun, h("span", { class: "muted" }, "0 = no limit")),
        h("label", { class: "aa-switch" }, enable, h("span", {}, "Enabled"))),
      note, rulesWrap,
      h("div", { class: "op-toolbar" },
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { block.rules.push({ employees: [], applications: [], from: "", to: "" }); drawRules(); } }, I("plus"), "Add rule"),
        h("span", { class: "grow" }), applyBtn,
        h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: save }, I("check"), "Save")));
  }

  async function distributeNow() {
    try { toast(`${(await api("POST", "/api/ops/auto-assign/apply", {})).assigned} e-mail(s) distributed.`); load(); }
    catch (e) { toast(e.message, "error"); }
  }

  // --------------------------------------------------------- ticketing: run the portal RPA with the manager's own login
  const runHost = h("div", {});                 // a persistent panel: survives render(), updated live while a run polls
  let polling = false;

  async function runTicketing(team, every, maxPerRun) {
    const user = h("input", { type: "text", autocomplete: "off", spellcheck: "false", dir: "ltr", maxlength: "60", "aria-label": "Portal username" });
    const pass = h("input", { type: "password", autocomplete: "off", dir: "ltr", maxlength: "200", "aria-label": "Portal password" });
    const dry = h("input", { type: "checkbox" });
    const keep = h("input", { type: "checkbox", checked: true });
    dry.addEventListener("change", () => { keep.disabled = dry.checked; if (dry.checked) keep.checked = false; });
    const body = h("div", { class: "dlg-body" },
      h("p", { class: "muted", dir: "auto" }, `Assign new portal requests for “${team}” by your saved rules. Type your OWN portal login — it is kept only in the server's memory while it runs, never stored.`),
      h("label", { class: "field" }, "Portal username", user),
      h("label", { class: "field" }, "Portal password", pass),
      h("p", { class: "muted" }, maxPerRun ? `Each run assigns at most ${maxPerRun} request(s); the rest wait for the next run.`
        : "Each run assigns every request the rules cover (no “Max per run” is saved)."),
      h("label", { class: "aa-switch" }, keep, h("span", {}, `Keep running: again every ${every} min until I press Stop (or the working hours end)`)),
      h("label", { class: "aa-switch" }, dry, h("span", {}, "Dry run (do everything except the final Execute; runs once)")));
    const ok = await CL.modal({
      title: "Run ticketing auto-assign", submitLabel: "Run", body,
      onSubmit: async () => {
        if (!user.value.trim() || !pass.value) throw new Error("Type your portal username and password.");
        await api("POST", "/api/ops/auto-assign/run-ticketing",
          { team, username: user.value.trim(), password: pass.value, dry_run: dry.checked, repeat: keep.checked && !dry.checked });
        return true;
      },
    });
    if (ok) { toast(keep.checked && !dry.checked ? "Ticketing auto-assign is running until you stop it." : "Ticketing auto-assign started."); pollTicketing(); }
  }

  function drawRun(d) {
    if (!d || (!d.running && !d.done)) { runHost.replaceChildren(); return; }
    const rows = (d.log || []).map((ln) => h("div", { class: "aa-logline", dir: "auto" }, ln));
    const loop = d.repeat && d.running
      ? ` — run ${d.runs || 0}${d.runs ? " done" : ""}, ${d.assigned || 0} assigned so far${d.next_run_at ? `, next at ${d.next_run_at.slice(11, 16)}` : ""}${d.stopping ? " (stopping after this run)" : ""}`
      : "";
    const head = (d.running ? `Running for “${d.team}”…` : (d.error && !d.repeat ? `Failed for “${d.team}”` : `Finished for “${d.team}”`)) + loop;
    const tone = d.running ? "op-t-info" : (d.error ? "op-t-danger" : "op-t-ok");
    const summary = d.error ? d.error : (d.summary || (d.done ? "Done." : ""));
    runHost.replaceChildren(OPS.card("Ticketing auto-assign", { hint: d.started_at ? `started ${d.started_at.replace('T', ' ')}` : "" },
      h("p", { class: "op-note op-block " + tone }, I(d.running ? "refresh" : (d.error ? "alert" : "check")),
        h("span", { dir: "auto" }, head + (summary ? " — " + summary : ""))),
      d.running && d.repeat && !d.stopping
        ? h("div", { class: "op-toolbar" }, h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: stopTicketing }, I("x"), "Stop"))
        : null,
      rows.length ? h("div", { class: "aa-log" }, ...rows) : null));
  }

  async function stopTicketing() {
    try { await api("POST", "/api/ops/auto-assign/run-ticketing/stop", {}); toast("Stopping after the current run."); }
    catch (e) { toast(e.message, "error"); }
    pollTicketing();
  }

  async function pollTicketing() {
    if (polling) return;
    polling = true;
    try {
      for (;;) {
        let d;
        try { d = await api("GET", "/api/ops/auto-assign/run-ticketing"); }
        catch (e) { drawRun({ done: true, error: e.message, team: "" }); break; }
        drawRun(d);
        if (!d.running) break;
        await new Promise((r) => setTimeout(r, 2000));
      }
    } finally { polling = false; }
  }

  // --------------------------------------------------------- the page
  function render() {
    const parts = [h("header", { class: "op-head" }, h("div", { class: "op-head-title" },
      h("h1", {}, "Auto Assign"),
      h("p", { class: "muted" }, "Route pending work to your team's employees automatically, by application and time window.")))];
    parts.push(runHost);
    if (IS_ADMIN) parts.push(applicationsCard());
    const teams = Object.keys(state.teams || {});
    if (!teams.length) {
      parts.push(OPS.emptyState({ title: "No team to configure", icon: "info", text: "You are not a manager of any team yet." }));
    }
    teams.forEach((team) => {
      const info = state.teams[team];
      parts.push(h("section", { class: "op-stack aa-teamcard" },
        h("h2", { class: "aa-team" }, I("user"), h("span", { dir: "auto" }, team)),
        h("div", { class: "op-grid op-grid-2" }, targetCard(team, "email", info), targetCard(team, "ticketing", info))));
    });
    page.replaceChildren(h("div", { class: "op-stack" }, ...parts));
  }

  load();
})();
