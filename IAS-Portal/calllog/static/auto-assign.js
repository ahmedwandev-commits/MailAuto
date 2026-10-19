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
  function ruleEditor(rule, members, apps, onRemove) {
    rule.employees = rule.employees || [];
    rule.applications = rule.applications || [];
    const emp = checkGroup(members.map((m) => ({ value: m.email, label: m.name })), rule.employees, "Employees");
    const appCtrl = apps.length
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
      h("div", { class: "aa-field" }, h("label", {}, "For applications"), appCtrl),
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
    const rulesWrap = h("div", { class: "aa-rules" });
    function drawRules() {
      rulesWrap.replaceChildren(...(block.rules.length
        ? block.rules.map((r, i) => ruleEditor(r, members, apps, () => { block.rules.splice(i, 1); drawRules(); }))
        : [h("p", { class: "muted" }, "No rules yet — add one to start distributing.")]));
    }
    drawRules();
    async function save() {
      try {
        const r = await api("PUT", "/api/ops/auto-assign/team",
          { team, kind, enabled: block.enabled, interval_minutes: block.interval_minutes, rules: block.rules });
        info[kind] = r.block;
        toast(kind === "email" && r.assigned ? `Saved — ${r.assigned} e-mail(s) distributed.` : "Saved.");
        render();
      } catch (e) { toast(e.message, "error"); }
    }
    const applyBtn = kind === "email"
      ? h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: distributeNow }, I("refresh"), "Distribute now")
      : null;
    const note = kind === "ticketing"
      ? h("p", { class: "op-note op-t-info op-block" }, I("info"),
        h("span", {}, "The ticketing collector applies these rules against the portal (assign-only)."))
      : null;
    return h("section", { class: "card op-fieldset aa-target" },
      h("div", { class: "card-head" }, h("h3", {}, kind === "email" ? "E-mail auto-assign" : "Ticketing auto-assign"),
        h("span", { class: "grow" }),
        h("label", { class: "aa-interval-field" }, h("span", {}, "Run every"), interval, h("span", { class: "muted" }, "min")),
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

  // --------------------------------------------------------- the page
  function render() {
    const parts = [h("header", { class: "op-head" }, h("div", { class: "op-head-title" },
      h("h1", {}, "Auto Assign"),
      h("p", { class: "muted" }, "Route pending work to your team's employees automatically, by application and time window.")))];
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
