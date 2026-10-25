"use strict";
/* E-mail page (/email): the web replacement for the old staff + manager Outlook dashboards
   (docs/HUB-DESIGN.md sections 5.3 and 7, brief docs/handoff/briefs/page-email.md).

   Reads GET /api/ops/email every 15 s through OPS.loader (?since=, random start offset, back-off, paused while the tab
   is hidden). A manager only ever receives the rows of their own team(s) - the API scopes them; an employee never
   reaches this page (the /email route sends them to /my). Six tabs: Overview, Pending, Replied, Quality, Trends and
   Unclassified. Actions (assign, no-action, change team, quality review, create/remove a classification rule) confirm,
   call the API, toast and re-fetch. Every scraped / e-mail text goes in with textContent (CL.h) and Arabic carries
   dir="auto"; there is no innerHTML and no inline style (strict CSP). */
(function () {
  const { h, $, api, toast, bidi, normAr, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";
  const page = $("#email-page");
  const ROLE = page.dataset.role || "manager";
  const MY_TEAMS = (page.dataset.teams || page.dataset.team || "").split(";").map((t) => t.trim()).filter(Boolean);
  const VIEW_KEY = "ops.email.view";
  const WAIT_LIMIT_MS = 4 * 60 * 1000;

  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "pending", label: "Pending" },
    { key: "replied", label: "Replied" },
    { key: "quality", label: "Quality" },
    { key: "trends", label: "Trends" },
    { key: "unclassified", label: "Unclassified" },
  ];

  const state = {
    answer: null, data: null, options: null, meta: null, generatedAt: "",
    tab: "overview",
    filters: { team: "", system: "", assigned: "", carryover: false, escalated: false, q: "" },
    history: null, historyLoaded: false,
    waiting: null, sugSig: null, linkQuery: "",
    quality: { person: "", reviewer: "" },
  };
  const el = {};
  const tables = {};

  // ------------------------------------------------------------------ small helpers
  const canActOnTeam = (team) => ROLE === "admin" || MY_TEAMS.includes("__all__")
    || MY_TEAMS.some((t) => normAr(t) === normAr(team));
  const systemsOf = (row) => String(row.detected_systems || "").split(/\s*;\s*/).filter(Boolean);
  const isAssigned = (row) => !!(row.assigned_to_name || row.assigned_to_email);
  const rowKey = (row) => row.message_id || row.row_id || row.entry_id || "";

  function loadView() {
    try {
      const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || "{}");
      if (TABS.some((t) => t.key === v.tab)) state.tab = v.tab;
      if (v.filters && typeof v.filters === "object") Object.assign(state.filters, v.filters);
    } catch (e) { /* private window: the page still works */ }
  }
  function saveView() {
    try { sessionStorage.setItem(VIEW_KEY, JSON.stringify({ tab: state.tab, filters: state.filters })); } catch (e) { /* ignore */ }
  }

  function ensureStyles() {
    if (document.querySelector('link[href*="ops-email.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-email.css", SELF).href : "/static/ops-email.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
  }

  const stats = () => (state.data && state.data.stats) || {};
  const rows = () => (state.data && state.data.rows) || [];
  const teams = () => (state.options && state.options.teams) || (state.data && state.data.teams) || [];
  const peopleByTeam = () => {                          // the accounts of Admin › Users a mail may go to: a manager's own team(s) only
    const groups = new Map();
    for (const p of (state.options && state.options.people) || []) {
      const key = p.team || "—";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    }
    const mine = (t) => (ROLE !== "admin" && canActOnTeam(t) ? 0 : 1);
    return new Map([...groups.entries()].sort((a, b) => mine(a[0]) - mine(b[0]) || a[0].localeCompare(b[0])));
  };

  // ------------------------------------------------------------------ actions (confirm -> API -> toast -> refetch)
  async function afterAction(msg) {
    toast(msg);
    try { await loader.refresh(); } catch (e) { /* the poll will catch up */ }
  }

  async function assignDialog(row) {
    const byTeam = peopleByTeam();
    if (!byTeam.size) {
      toast(ROLE === "admin" ? "No account has an e-mail address yet: add it in Admin › Users."
        : "No account of your team has an e-mail address yet: ask the administrator to add it in Admin › Users.", "error");
      return;
    }
    const select = h("select", { "aria-label": "Assign to", required: true },
      h("option", { value: "" }, "Choose a person…"),
      [...byTeam.entries()].map(([team, people]) => h("optgroup", { label: team },
        people.map((p) => h("option", { value: p.email }, `${p.name || p.email}${p.email ? ` · ${p.email}` : ""}`)))));
    if (isAssigned(row) && row.assigned_to_email) select.value = row.assigned_to_email;
    const note = h("textarea", { rows: "3", maxlength: "300", "aria-label": "Note (optional)", placeholder: "Optional note for the assignee", dir: "auto" });
    note.value = row.assigned_note || "";
    const people = (state.options && state.options.people) || [];
    await OPS.modal({
      title: "Assign this e-mail", submitLabel: "Assign",
      body: h("div", {},
        h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)")),
        h("label", { class: "em-field" }, h("span", {}, "Assign to"), select),
        h("label", { class: "em-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        const address = select.value;
        if (!address) throw new Error("Choose a person to assign to.");
        const match = people.find((p) => p.email === address);
        await api("POST", "/api/ops/email/assign", { message_id: rowKey(row), assigned_to_email: address,
          assigned_to_name: match ? match.name : "", note: note.value.trim() });
        await afterAction("E-mail assigned.");
      },
    });
  }

  async function noActionDialog(row) {
    const ok = await CL.confirmDialog({ title: "Mark as needing no action",
      message: `Mark "${row.subject || "(no subject)"}" as needing no reply? It will stop counting as pending.`,
      confirmLabel: "Mark no action", danger: false });
    if (!ok) return;
    try {
      await api("POST", "/api/ops/email/no-action", { message_id: rowKey(row) });
      await afterAction("Marked as needing no action.");
    } catch (e) { toast(e.message, "error"); }
  }

  async function changeTeamDialog(row) {
    const choices = teams();          // any manager / admin may re-file a mail to ANY team (not only their own)
    const select = h("select", { "aria-label": "Team", required: true },
      choices.map((t) => h("option", { value: t, selected: t === row.team }, t)));
    await OPS.modal({
      title: "Change the team", submitLabel: "Change team",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)")),
        h("label", { class: "em-field" }, h("span", {}, "Team"), select)),
      onSubmit: async () => {
        await api("POST", "/api/ops/email/team", { message_id: rowKey(row), team: select.value });
        await afterAction("Team changed.");
      },
    });
  }

  async function reviewDialog(row) {
    const ratings = (state.options && state.options.ratings) || ["Excellent", "Very Good", "Good", "Needs Improvement"];
    const reasons = (state.options && state.options.quality_reasons) || [];
    const ratingSel = h("select", { "aria-label": "Rating", required: true },
      h("option", { value: "" }, "Choose a rating…"), ratings.map((r) => h("option", { value: r, selected: r === row.quality_rating }, r)));
    const boxes = reasons.map((r) => h("label", { class: "em-check" },
      h("input", { type: "checkbox", value: r, checked: String(row.quality_reasons || "").includes(r) }), r));
    const note = h("textarea", { rows: "3", maxlength: "500", "aria-label": "Note", dir: "auto" });
    note.value = row.quality_note || "";
    await OPS.modal({
      title: "Review the reply quality", submitLabel: "Save review",
      body: h("div", {},
        h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)"), row.first_reply_by ? ` · replied by ${row.first_reply_by}` : ""),
        h("label", { class: "em-field" }, h("span", {}, "Rating"), ratingSel),
        reasons.length ? h("div", { class: "em-field" }, h("span", {}, "Reasons"), h("div", { class: "em-reasons" }, boxes)) : null,
        h("label", { class: "em-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        if (!ratingSel.value) throw new Error("Choose a rating.");
        const picked = boxes.filter((b) => b.querySelector("input").checked).map((b) => b.querySelector("input").value);
        await api("POST", "/api/ops/email/quality", { message_id: rowKey(row), rating: ratingSel.value, note: note.value.trim(), reasons: picked });
        await afterAction("Quality review saved.");
      },
    });
  }

  async function ruleDialog(hintKeyword) {
    const choices = teams().filter((t) => t !== "Unclassified" && (ROLE === "admin" || canActOnTeam(t)));
    const keyword = h("input", { type: "text", maxlength: "80", required: true, "aria-label": "Keyword or application", dir: "auto" });
    keyword.value = hintKeyword || "";
    const teamSel = h("select", { "aria-label": "Team", required: true },
      h("option", { value: "" }, "Choose a team…"), choices.map((t) => h("option", { value: t }, t)));
    await OPS.modal({
      title: "Create a classification rule", submitLabel: "Create rule",
      body: h("div", {},
        h("p", { class: "muted" }, "E-mails whose detected words contain this keyword will be put in the chosen team from the next scan on."),
        h("label", { class: "em-field" }, h("span", {}, "Keyword / application"), keyword),
        h("label", { class: "em-field" }, h("span", {}, "Team"), teamSel)),
      onSubmit: async () => {
        if (!keyword.value.trim()) throw new Error("Enter a keyword.");
        if (!teamSel.value) throw new Error("Choose a team.");
        await api("POST", "/api/ops/email/classification/rule", { keyword: keyword.value.trim(), team: teamSel.value });
        await afterAction("Rule created - it applies from the next scan.");
      },
    });
  }

  async function removeRule(keyword) {
    const ok = await CL.confirmDialog({ title: "Remove the rule", message: `Remove the rule for "${keyword}"?`, confirmLabel: "Remove" });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/email/classification/rule?keyword=${encodeURIComponent(keyword)}`);
      await afterAction("Rule removed.");
    } catch (e) { toast(e.message, "error"); }
  }

  // open the shared mail viewer (subject, body, attachments, Download, Done/Reopen); refresh the page after a change
  function openMail(row) {
    OPS.mailViewer(rowKey(row), { onChange: () => loader.refresh() });
  }

  async function doneDialog(row) {
    const note = h("textarea", { rows: "2", maxlength: "300", dir: "auto", placeholder: "Optional note for the manager" });
    await OPS.modal({
      title: "Mark as done", submitLabel: "Mark done",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(row.subject || "(no subject)")),
        h("label", { class: "em-field" }, h("span", {}, "Note (optional)"), note)),
      onSubmit: async () => {
        await api("POST", "/api/ops/email/done", { message_id: rowKey(row), note: note.value.trim() });
        await afterAction("Marked as done.");
      },
    });
  }
  async function reopenRow(row) {
    const ok = await CL.confirmDialog({ title: "Reopen the e-mail",
      message: `Reopen "${row.subject || "(no subject)"}"? It will no longer show as done.`, confirmLabel: "Reopen", danger: false });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/email/done?message_id=${encodeURIComponent(rowKey(row))}`);
      await afterAction("Reopened.");
    } catch (e) { toast(e.message, "error"); }
  }

  function rowActionButtons(row) {
    const btns = [h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); openMail(row); } }, OPS.icon("mail"), "Open")];
    if (canActOnTeam(row.team)) {
      if (row.status !== "Replied") {
        btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); assignDialog(row); } }, "Assign"));
        btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); noActionDialog(row); } }, "No action"));
      }
      btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); changeTeamDialog(row); } }, "Team"));
      if (isAssigned(row)) {
        btns.push(row.assignment_done
          ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reopenRow(row); } }, "Reopen")
          : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); doneDialog(row); } }, OPS.icon("check"), "Done"));
      }
    }
    return h("div", { class: "op-row-actions" }, btns);
  }

  function assignedCell(row) {
    if (!row.assigned_to_name) return h("span", { class: "hint" }, "—");
    return h("div", { class: "em-assignee" }, bidi(row.assigned_to_name),
      row.assignment_done ? OPS.chip("Done", "ok", "check") : null);
  }

  // ------------------------------------------------------------------ row drawer
  function detailDrawer(row) {
    const field = (label, value) => (value == null || value === "" ? null
      : [h("dt", {}, label), h("dd", { dir: "auto" }, bidi(String(value)))]);
    const body = h("div", {},
      h("dl", { class: "em-drawer-grid" },
        field("Subject", row.subject), field("Customer", row.customer_name), field("Team", row.team),
        field("Status", row.status), field("SLA", row.sla_status), field("Received", OPS.fmtWhen(row.received_time, state.meta && state.meta.server_time)),
        field("Waiting", row.current_waiting_time), field("First reply by", row.first_reply_by),
        field("First reply", OPS.fmtWhen(row.first_reply_time, state.meta && state.meta.server_time)),
        field("Response", row.response_minutes != null ? OPS.fmtDuration(row.response_minutes) : null),
        field("Detected systems", row.detected_systems), field("Match method", row.match_method),
        field("Assigned to", row.assigned_to_name), field("Quality", row.quality_rating),
        field("No-action reason", row.no_action_reason)),
      row.body_preview ? h("div", {}, h("div", { class: "hint" }, "Message preview"), h("div", { class: "em-preview", dir: "auto" }, row.body_preview)) : null,
      h("div", { class: "em-drawer-actions" }, rowActionButtons(row)));
    OPS.modal({ title: "E-mail details", wide: true, body });
  }

  // ------------------------------------------------------------------ Overview tab
  function kpiTiles() {
    const s = stats();
    const n = (v) => fmtNum(v || 0);
    const tile = (label, value, sub, tone, icon) => OPS.kpi({ label, value, sub, tone, icon });
    el.kpis.replaceChildren(
      tile("Business e-mails", n(s.business_emails), "to the teams today", null, "mail"),
      tile("Replied", n(s.replied), OPS.fmtPct(s.reply_rate) + " reply rate", s.replied ? "ok" : null, "check"),
      tile("Pending", n(s.pending), "need a reply", s.pending ? "warn" : null, "clock"),
      tile("No action", n(s.no_action_needed), "marked no reply", null, "dash"),
      tile("Carry-over", n(s.carryover_count), s.carryover_count ? `oldest ${s.carryover_oldest_days} d` : "none", s.carryover_count ? "warn" : null, "clock"),
      tile("Past SLA", n(s.sla_breached), OPS.fmtPct(s.sla_compliance_rate) + " on time", s.sla_breached ? "danger" : "ok", "alert"),
      tile("Escalated", n(s.escalated_count), "still no reply", s.escalated_count ? "danger" : null, "alert"),
      tile("Assigned", n(s.assigned_count), "to a person", null, "user"),
      tile("Avg first reply", OPS.fmtDuration(s.average_response), s.fastest_response ? `fastest ${OPS.fmtDuration(s.fastest_response)}` : "", null, "activity"));
  }

  function statusDonut() {
    const s = stats();
    return OPS.donut([
      { label: "Replied", value: s.replied || 0, cls: "op-t-ok" },
      { label: "Pending", value: s.pending || 0, cls: "op-t-warn" },
      { label: "No action", value: s.no_action_needed || 0, cls: "op-t-na" },
    ], { size: 150, thickness: 18, centerValue: OPS.fmtPct(s.reply_rate).replace(/\.\d+/, ""), centerLabel: "replied" });
  }

  function teamTable() {
    const ts = stats().team_stats || {};
    const data = Object.entries(ts).map(([team, t]) => ({
      team, business_emails: t.business_emails || 0, replied: t.replied || 0, pending: t.pending || 0,
      no_action: t.no_action_needed || 0, escalated: t.escalated_count || 0, carryover: t.carryover_count || 0,
      breached: t.sla_breached_count || 0, sent: t.team_emails_sent || 0,
      avg: t.response_count ? Math.round((t.response_minutes_sum || 0) / t.response_count) : null,
    }));
    return OPS.table({
      search: false, copy: true, caption: "E-mail by team", pageSize: 10, pageSizes: [10, 25, 50],
      sort: { key: "pending", desc: true },
      columns: [
        { key: "team", label: "Team", sort: true, render: (r) => bidi(r.team) },
        { key: "business_emails", label: "E-mails today", num: true,       // like the headline: carry-over has its own column
          sort: (r) => r.business_emails - r.carryover, render: (r) => fmtNum(r.business_emails - r.carryover),
          text: (r) => String(r.business_emails - r.carryover) },
        { key: "replied", label: "Replied", num: true, sort: true },
        { key: "pending", label: "Pending", num: true, sort: true },
        { key: "no_action", label: "No action", num: true, sort: true },
        { key: "escalated", label: "Escalated", num: true, sort: true, render: (r) => (r.escalated ? h("strong", { class: "op-t-danger" }, fmtNum(r.escalated)) : "0") },
        { key: "breached", label: "Past SLA", num: true, sort: true },
        { key: "carryover", label: "Carry-over", num: true, sort: true },
        { key: "avg", label: "Avg reply", num: true, sort: true, render: (r) => OPS.fmtDuration(r.avg) },
        { key: "sent", label: "Replies sent", num: true, sort: true },
      ],
      rows: data,
      empty: { title: "No team data yet", icon: "mail", text: "Teams appear after the first scan." },
    });
  }

  function arrivalsSparkline() {
    // today's arrivals only: a carry-over row is a mail of an earlier day that is still waiting (it is not in the
    // Business e-mails tile either, so the chart and the tile add up to the same number)
    const buckets = new Array(24).fill(0), day = String((state.data && state.data.date) || "");
    for (const r of rows()) {
      const when = String(r.received_time || "");
      const m = /T(\d\d):/.exec(when);
      if (m && !r.is_carryover && (!day || when.startsWith(day))) buckets[+m[1]] += 1;
    }
    const first = buckets.findIndex((n) => n > 0), last = 23 - [...buckets].reverse().findIndex((n) => n > 0);
    if (first < 0) return h("p", { class: "hint" }, "No arrival times in this scan.");
    const series = buckets.slice(first, last + 1);
    return h("div", {}, OPS.sparkline(series, { width: 260, height: 44, label: "E-mails per hour today" }),
      h("span", { class: "hint em-arrivals" }, `per hour, ${String(first).padStart(2, "0")}:00–${String(last).padStart(2, "0")}:00`
        + ` · ${fmtNum(buckets.reduce((a, b) => a + b, 0))} e-mails`));
  }

  function topApps() {
    const vol = stats().system_volume || {};
    const items = Object.entries(vol).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([label, value]) => ({ label, value }));
    if (!items.length) return h("p", { class: "hint" }, "No application was detected in this scan.");
    return OPS.hbars(items, { max: Math.max(1, ...items.map((i) => i.value)) });
  }

  function slowestList() {
    const items = (stats().top_slowest_replies || []).slice(0, 6);
    if (!items.length) return h("p", { class: "hint" }, "No replies to rank yet.");
    return h("ul", { class: "op-list" }, items.map((i) => h("li", {},
      h("span", { dir: "auto" }, bidi(i.subject || "(no subject)")),
      h("span", { class: "hint" }, ` ${i.team || ""} · ${OPS.fmtDuration(i.response_minutes)}${i.first_reply_by ? " · " + i.first_reply_by : ""}`))));
  }

  function escalatedList() {
    const items = (stats().escalated_items || []).slice(0, 8);
    if (!items.length) return OPS.emptyState({ title: "Nothing escalated", icon: "check", text: "No e-mail is waiting past escalation." });
    const more = (stats().escalated_count || items.length) - items.length;     // the tile counts them all: say what is not listed
    return h("div", {}, h("ul", { class: "op-list" }, items.map((i) => h("li", {},
      OPS.icon("alert"), h("span", { dir: "auto" }, bidi(i.subject || "(no subject)")),
      h("span", { class: "hint" }, ` ${i.team || ""} · waiting ${i.current_waiting_time || ""}`)))),
      more > 0 ? h("a", { class: "link", href: "/email?escalated=1" }, `and ${fmtNum(more)} more - show all ${fmtNum(more + items.length)}`) : null);
  }

  function renderOverview() {
    kpiTiles();
    const matched = stats();
    el.panels.overview.replaceChildren(
      el.kpis,
      h("div", { class: "em-three" },
        OPS.card("Status", {}, h("div", { class: "op-viz op-viz-donut" }, statusDonut(),
          h("div", { class: "op-legend op-legend-col" },
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-ok" }), `${fmtNum(matched.replied || 0)} replied`),
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-warn" }), `${fmtNum(matched.pending || 0)} pending`),
            h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-na" }), `${fmtNum(matched.no_action_needed || 0)} no action`)))),
        OPS.card("Arrivals", { hint: "when e-mails came in today" }, arrivalsSparkline()),
        OPS.card("Top applications", { hint: "carry-over included" }, topApps())),
      OPS.card("E-mails by team", {}, teamTable().el),
      h("div", { class: "em-pair" },
        OPS.card("Slowest first replies", {}, slowestList()),
        OPS.card("Escalated (no reply)", {}, escalatedList())),
      matchDiagnostic());
  }

  function matchDiagnostic() {
    const s = stats();
    const parts = [["ConversationID", s.conversation_matches], ["Index", s.conversation_index_matches],
      ["Subject", s.subject_matches], ["No match", s.no_matches]].filter(([, n]) => n);
    if (!parts.length) return null;
    return OPS.card("How replies were matched", { hint: "diagnostic" },
      h("p", { class: "hint" }, parts.map(([label, n]) => `${label}: ${fmtNum(n)}`).join(" · ")));
  }

  // ------------------------------------------------------------------ Pending / Replied filters
  function pendingFilter() {
    const f = state.filters;
    return (r) => {
      if (f.team && !(normAr(r.team) === normAr(f.team) || (r.all_teams || []).some((t) => normAr(t) === normAr(f.team)))) return false;
      if (f.system && !systemsOf(r).some((sys) => normAr(sys) === normAr(f.system))) return false;
      if (f.carryover && !r.is_carryover) return false;
      if (f.escalated && !r.escalated) return false;
      if (f.assigned === "no" && isAssigned(r)) return false;
      if (f.assigned === "open" && !(isAssigned(r) && !r.assignment_done)) return false;
      if (f.assigned === "done" && !(isAssigned(r) && r.assignment_done)) return false;
      return true;
    };
  }

  function filterBar(onChange) {
    const f = state.filters;
    const teamSel = h("select", { "aria-label": "Team", onchange: () => { f.team = teamSel.value; onChange(); } },
      h("option", { value: "" }, "All teams"), teams().map((t) => h("option", { value: t, selected: t === f.team }, t)));
    const sysSet = new Set();
    for (const r of rows()) systemsOf(r).forEach((s) => sysSet.add(s));
    const sysSel = h("select", { "aria-label": "Application", onchange: () => { f.system = sysSel.value; onChange(); } },
      h("option", { value: "" }, "All applications"), [...sysSet].sort().map((s) => h("option", { value: s, selected: s === f.system }, s)));
    const assignedSel = h("select", { "aria-label": "Assignment", onchange: () => { f.assigned = assignedSel.value; onChange(); } },
      h("option", { value: "", selected: !f.assigned }, "Any assignment"),
      h("option", { value: "open", selected: f.assigned === "open" }, "Assigned · open"),
      h("option", { value: "done", selected: f.assigned === "done" }, "Assigned · done"),
      h("option", { value: "no", selected: f.assigned === "no" }, "Unassigned"));
    const carry = h("input", { type: "checkbox", checked: f.carryover, onchange: () => { f.carryover = carry.checked; onChange(); } });
    const esc = h("input", { type: "checkbox", checked: f.escalated, onchange: () => { f.escalated = esc.checked; onChange(); } });
    return h("div", { class: "em-filters" }, teamSel, sysSel, assignedSel,
      h("label", { class: "em-check" }, carry, "Carry-over only"),
      h("label", { class: "em-check" }, esc, "Escalated only"));
  }

  function pendingTable() {
    const now = state.meta && state.meta.server_time;
    return OPS.table({
      search: true, searchPlaceholder: "Search subject, customer, system…", copy: true,
      caption: "Pending e-mails", pageSize: 10, pageSizes: [10, 25, 50], rowKey, onRowClick: openMail,
      sort: { key: "received", desc: false },
      toolbar: filterBar(() => { tables.pending.setFilter(pendingFilter()); saveView(); }),
      filter: pendingFilter(),
      columns: [
        { key: "status", label: "SLA", render: (r) => OPS.statusChip(r.escalated ? "Breached" : r.sla_status || r.status), text: (r) => r.sla_status || r.status },
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "team", label: "Team", render: (r) => bidi(r.team), text: (r) => r.team },
        { key: "received", label: "Waiting", sort: (r) => OPS.parseNaive(r.received_time) || 0, render: (r) => r.current_waiting_time || OPS.fmtWhen(r.received_time, now), text: (r) => r.current_waiting_time },
        { key: "assigned_to_name", label: "Assigned", render: assignedCell, text: (r) => r.assigned_to_name + (r.assignment_done ? " done" : "") },
        { key: "actions", label: "", render: rowActionButtons },
      ],
      rows: rows().filter((r) => r.status === "Pending Reply"),
      empty: (filtered) => ({ title: filtered ? "No pending e-mail matches" : "Nothing pending", icon: "check",
        text: filtered ? "Try clearing the filters." : "Every business e-mail has a reply or is marked no-action." }),
    });
  }

  function renderPending() {
    if (!tables.pending) {
      tables.pending = pendingTable();
      el.panels.pending.replaceChildren(OPS.card("Pending e-mails", { hint: "click a row for details and actions" }, tables.pending.el));
      if (state.linkQuery) { tables.pending.setQuery(state.linkQuery); state.linkQuery = ""; }
    } else {
      tables.pending.setRows(rows().filter((r) => r.status === "Pending Reply"));
    }
  }

  // another team's reply on a shared mail arrives without its person, text or rating (the server hides it)
  function replierCell(r) {
    if (r.reply_other_team) return h("span", { class: "hint", dir: "auto" }, bidi(`${r.reply_team || "Another"} team`));
    return bidi(r.first_reply_by || "—");
  }
  function ratingCell(r) {
    if (!r.quality_rating) return h("span", { class: "hint" }, "—");
    return h("div", { class: "em-rating" },
      OPS.chip(r.quality_rating, ratingTone(r.quality_rating)),
      r.rating_withdrawn ? OPS.chip("Withdrawn", "na") : null,
      r.quality_reviewed_by ? h("span", { class: "hint", dir: "auto" }, bidi(`by ${r.quality_reviewed_by}`)) : null);
  }
  /** The same rule as the server: a reply written by a team member (not a manager), of a team the viewer looks after. */
  const canRate = (r) => r.status === "Replied" && !r.reply_by_manager && !r.reply_other_team
    && [r.team, ...(r.all_teams || [])].some((t) => t && canActOnTeam(t));

  function repliedTable() {
    const now = state.meta && state.meta.server_time;
    const canReview = ROLE === "admin" || ROLE === "manager";
    return OPS.table({
      search: true, searchPlaceholder: "Search subject, customer, replier…", copy: true,
      caption: "Replied e-mails", pageSize: 10, pageSizes: [10, 25, 50], rowKey, onRowClick: openMail,
      sort: { key: "response_minutes", desc: true },
      columns: [
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "team", label: "Team", render: (r) => bidi(r.team), text: (r) => r.team },
        { key: "first_reply_by", label: "Replied by", render: replierCell, text: (r) => (r.reply_other_team ? `${r.reply_team} team` : r.first_reply_by) },
        { key: "response_minutes", label: "Response", num: true, sort: true, render: (r) => OPS.fmtDuration(r.response_minutes) },
        { key: "sla_status", label: "SLA", render: (r) => OPS.statusChip(r.sla_status), text: (r) => r.sla_status },
        { key: "quality_rating", label: "Quality", render: ratingCell,
          text: (r) => [r.quality_rating, r.quality_reviewed_by].filter(Boolean).join(" by ") },
        { key: "actions", label: "", render: (r) => h("div", { class: "op-row-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); openMail(r); } }, OPS.icon("mail"), "Open"),
          (canReview && canRate(r)
            ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reviewDialog(r); } }, r.quality_rating ? "Re-review" : "Review")
            : null)) },
      ],
      rows: rows().filter((r) => r.status === "Replied"),
      empty: (filtered) => ({ title: filtered ? "No replied e-mail matches" : "No replies yet", icon: "mail",
        text: filtered ? "Try clearing the search." : "Replies appear here as the teams answer." }),
    });
  }

  function renderReplied() {
    if (!tables.replied) {
      tables.replied = repliedTable();
      el.panels.replied.replaceChildren(OPS.card("Replied e-mails", { hint: "managers can review the reply quality" }, tables.replied.el));
    } else {
      tables.replied.setRows(rows().filter((r) => r.status === "Replied"));
    }
  }

  // ------------------------------------------------------------------ Quality tab
  function ratingTone(rt) { return rt === "Needs Improvement" ? "warn" : rt === "Excellent" || rt === "Very Good" ? "ok" : "info"; }

  async function decideAppeal(row, decision) {
    const accept = decision === "Accepted";
    const ok = await CL.confirmDialog({
      title: accept ? "Accept the appeal" : "Reject the appeal",
      message: accept ? `Accept the appeal and withdraw the rating for "${row.subject || "(no subject)"}"?`
                      : `Reject the appeal and keep the rating for "${row.subject || "(no subject)"}"?`,
      confirmLabel: accept ? "Accept" : "Reject", danger: !accept,
    });
    if (!ok) return;
    try {
      await api("POST", "/api/ops/email/quality/decision", { message_id: rowKey(row), decision });
      toast(accept ? "Appeal accepted — rating withdrawn." : "Appeal rejected — rating kept.");
      await loader.refresh();
    } catch (e) { toast(e.message, "error"); }
  }

  /** The appeals card: every person who disagreed with a rating, the ones still waiting with Accept / Reject, then the decided ones. */
  function appealsCard(keep) {
    const all = rows().filter((r) => r.quality_appeal && (!keep || keep(r)));
    if (!all.length) return null;
    const waiting = all.filter((r) => !r.appeal_decision);
    const decided = all.filter((r) => r.appeal_decision);
    const one = (r, withActions) => h("div", { class: "em-appeal" },
      h("div", { class: "em-appeal-head" },
        OPS.chip(r.quality_rating || "—", ratingTone(r.quality_rating)),
        h("strong", { class: "em-appeal-subj", dir: "auto" }, bidi(r.subject || "(no subject)")),
        r.first_reply_by ? h("span", { class: "hint", dir: "auto" }, bidi(r.first_reply_by)) : null,
        r.team ? h("span", { class: "hint" }, bidi(r.team)) : null,
        r.quality_reviewed_by ? h("span", { class: "hint", dir: "auto" }, bidi(`rated by ${r.quality_reviewed_by}`)) : null),
      r.quality_reasons ? h("p", { class: "em-appeal-reasons hint", dir: "auto" }, bidi(r.quality_reasons)) : null,
      h("p", { class: "em-appeal-text", dir: "auto" }, bidi(r.quality_appeal)),
      withActions
        ? ([r.team, ...(r.all_teams || [])].some((t) => t && canActOnTeam(t))   // the same rule as the server
            ? h("div", { class: "op-row-actions" },
                h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: () => decideAppeal(r, "Accepted") }, OPS.icon("check"), "Accept"),
                h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => decideAppeal(r, "Rejected") }, "Reject"))
            : h("span", { class: "hint" }, "Another team decides this one."))
        : OPS.chip(`Appeal ${r.appeal_decision.toLowerCase()}`, r.appeal_decision === "Accepted" ? "ok" : "warn"));
    const body = [];
    body.push(waiting.length
      ? h("div", { class: "em-appeals" }, waiting.map((r) => one(r, true)))
      : OPS.emptyState({ title: "No appeals waiting", icon: "check", text: "When someone disagrees with a rating, it shows here for a decision." }));
    if (decided.length) body.push(h("details", { class: "em-appeals-decided" },
      h("summary", {}, `${fmtNum(decided.length)} already decided`),
      h("div", { class: "em-appeals" }, decided.map((r) => one(r, false)))));
    return OPS.card("Quality appeals", { hint: waiting.length ? `${fmtNum(waiting.length)} waiting for a decision` : "all decided" }, body);
  }

  const personKey = (r) => String(r.first_reply_email || r.first_reply_by || "").toLowerCase();

  /** Employee + reviewer pickers of the Quality tab: the people who replied (of the viewer's own teams - another team's
      reply never reaches this page) and the managers who rated. */
  function qualityFilters(replied, rated) {
    const people = new Map();
    for (const r of replied) if (personKey(r) && !people.has(personKey(r))) people.set(personKey(r), r.first_reply_by || r.first_reply_email);
    const reviewers = [...new Set(rated.map((r) => r.quality_reviewed_by).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    const qf = state.quality;
    if (qf.person && !people.has(qf.person)) qf.person = "";
    if (qf.reviewer && !reviewers.includes(qf.reviewer)) qf.reviewer = "";
    const personSel = h("select", { "aria-label": "Employee", onchange: (e) => { qf.person = e.target.value; renderQuality(); } },
      h("option", { value: "" }, "Every employee"),
      [...people.entries()].sort((a, b) => String(a[1]).localeCompare(String(b[1])))
        .map(([k, name]) => h("option", { value: k, selected: k === qf.person }, name)));
    const reviewerSel = h("select", { "aria-label": "Rated by", onchange: (e) => { qf.reviewer = e.target.value; renderQuality(); } },
      h("option", { value: "" }, "Every reviewer"),
      reviewers.map((n) => h("option", { value: n, selected: n === qf.reviewer }, n)));
    const clear = (qf.person || qf.reviewer)
      ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { qf.person = ""; qf.reviewer = ""; renderQuality(); } }, "Clear")
      : null;
    return h("div", { class: "em-qfilters" },
      h("label", { class: "em-field em-inline" }, h("span", {}, "Employee"), personSel),
      h("label", { class: "em-field em-inline" }, h("span", {}, "Rated by"), reviewerSel), clear);
  }

  function ratedTable(list) {
    return OPS.table({
      search: true, searchPlaceholder: "Search subject, employee, reviewer…", copy: true, caption: "Rated replies",
      pageSize: 10, pageSizes: [10, 25, 50], rowKey, onRowClick: openMail,
      sort: { key: "quality_reviewed_at", desc: true },
      columns: [
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "first_reply_by", label: "Replied by", render: replierCell, text: (r) => r.first_reply_by },
        { key: "team", label: "Team", render: (r) => bidi(r.team), text: (r) => r.team },
        { key: "quality_rating", label: "Rating", render: (r) => h("div", { class: "em-rating" },
          OPS.chip(r.quality_rating, ratingTone(r.quality_rating)), r.rating_withdrawn ? OPS.chip("Withdrawn", "na") : null),
          text: (r) => r.quality_rating },
        { key: "quality_reasons", label: "Reasons", render: (r) => (r.quality_reasons ? h("span", { dir: "auto" }, bidi(r.quality_reasons)) : h("span", { class: "hint" }, "—")), text: (r) => r.quality_reasons },
        { key: "quality_reviewed_at", label: "Rated by", render: (r) => h("div", {}, bidi(r.quality_reviewed_by || "—"),
          r.quality_reviewed_at ? h("div", { class: "hint" }, OPS.fmtWhen(r.quality_reviewed_at, state.meta && state.meta.server_time)) : null),
          text: (r) => r.quality_reviewed_by, sort: (r) => String(r.quality_reviewed_at || "") },
        { key: "quality_appeal", label: "Appeal", render: (r) => (r.quality_appeal
          ? OPS.chip(r.appeal_decision ? `Appeal ${r.appeal_decision.toLowerCase()}` : "Appeal waiting", r.appeal_decision === "Accepted" ? "ok" : "info")
          : h("span", { class: "hint" }, "—")), text: (r) => (r.quality_appeal ? r.appeal_decision || "waiting" : "") },
        { key: "actions", label: "", render: (r) => (canRate(r)
          ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reviewDialog(r); } }, "Re-review")
          : null) },
      ],
      rows: list,
      empty: (filtered) => ({ title: filtered ? "No rated reply matches" : "No rated replies", icon: "check",
        text: filtered ? "Try clearing the search." : "Ratings given from the Replied tab show here, with who rated them." }),
    }).el;
  }

  function renderQuality() {
    const q = (state.data && state.data.quality) || {};
    const qf = state.quality;
    const replied = rows().filter((r) => r.status === "Replied" && !r.reply_other_team && !r.reply_by_manager);
    const ratedAll = rows().filter((r) => r.quality_rating && !r.reply_other_team);
    const filters = qualityFilters(replied, ratedAll);
    const keep = (r) => (!qf.person || personKey(r) === qf.person) && (!qf.reviewer || r.quality_reviewed_by === qf.reviewer);
    const rated = ratedAll.filter(keep);
    let sq = stats().quality || {};
    if (qf.person || qf.reviewer) {                     // the KPIs of the chosen employee / reviewer, from the rows
      const counted = rated.filter((r) => !r.rating_withdrawn);
      const base = replied.filter((r) => !qf.person || personKey(r) === qf.person).length;
      sq = { reviewed: counted.length, coverage_rate: base ? (100 * counted.length) / base : 0,
        excellent: counted.filter((r) => r.quality_rating === "Excellent").length,
        very_good: counted.filter((r) => r.quality_rating === "Very Good").length,
        good: counted.filter((r) => r.quality_rating === "Good").length,
        needs_improvement: counted.filter((r) => r.quality_rating === "Needs Improvement").length,
        appeals: rated.filter((r) => r.quality_appeal).length };
    }
    const kpis = h("div", { class: "em-kpis" },
      OPS.kpi({ label: "Reviewed", value: fmtNum(sq.reviewed || 0), sub: OPS.fmtPct(sq.coverage_rate) + " coverage", icon: "check" }),
      OPS.kpi({ label: "Excellent", value: fmtNum(sq.excellent || 0), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Very good", value: fmtNum(sq.very_good || 0), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Good", value: fmtNum(sq.good || 0), icon: "check" }),
      OPS.kpi({ label: "Needs improvement", value: fmtNum(sq.needs_improvement || 0), tone: sq.needs_improvement ? "warn" : null, icon: "alert" }),
      OPS.kpi({ label: "Appeals waiting", value: fmtNum(rows().filter((r) => r.quality_appeal && !r.appeal_decision && keep(r)).length),
        sub: `${fmtNum(sq.appeals || 0)} in all`, icon: "info" }));        // the same "waiting" number as Home's attention list
    const personName = qf.person ? (replied.find((r) => personKey(r) === qf.person) || {}).first_reply_by : "";
    const scorecard = (Array.isArray(q.scorecard) ? q.scorecard : []).filter((e) => !qf.person || e.name === personName);
    let board;
    if (!scorecard.length) {
      board = OPS.emptyState({ title: "No quality reviews yet", icon: "activity",
        text: "Once replies are reviewed, a per-person scorecard, team index and leaderboard appear here." });
    } else {
      board = OPS.table({
        search: true, copy: true, caption: "Quality scorecard", pageSize: 10, pageSizes: [10, 25, 50],
        sort: { key: "reviewed", desc: true },
        columns: [
          { key: "name", label: "Person", render: (r) => bidi(r.name || "—"), text: (r) => r.name },
          { key: "team", label: "Team", render: (r) => bidi(r.team || "—"), text: (r) => r.team },
          { key: "reviewed", label: "Reviewed", num: true, sort: true },
          { key: "excellent", label: "Excellent", num: true, sort: true },
          { key: "very_good", label: "Very good", num: true, sort: true, render: (r) => fmtNum(r.very_good || 0) },
          { key: "good", label: "Good", num: true, sort: true },
          { key: "needs", label: "Needs improvement", num: true, sort: true },     // the scorecard's own field name
        ],
        rows: scorecard,
      }).el;
    }
    const appeals = appealsCard(keep);
    const ratedCard = OPS.card("Rated replies", { hint: `${fmtNum(rated.length)} rated · who rated each one` }, ratedTable(rated));
    el.panels.quality.replaceChildren(...[filters, kpis, appeals, ratedCard,
      OPS.card("Quality scorecard", { hint: "per person, from the reviews" }, board)].filter(Boolean));
  }

  // ------------------------------------------------------------------ Trends tab
  async function ensureHistory() {
    if (state.historyLoaded) return;
    state.historyLoaded = true;
    try {
      const answer = await api("GET", "/api/ops/email/history?days=14");
      state.history = answer.days || [];
    } catch (e) { state.history = null; }
  }
  function trendChart(label, key, tone) {
    const days = state.history || [];
    // a day's number: the report's final count, else (today, before the report ran) the day's latest scan
    const last = (d) => ((d.points || [])[(d.points || []).length - 1] || {});
    const series = days.map((d) => (d.final && d.final[key] != null ? d.final[key] : last(d)[key]) || 0);
    return OPS.card(label, {}, series.length
      ? h("div", {}, OPS.sparkline(series, { width: 260, height: 50, label, tone }),
        h("p", { class: "hint" }, `${days.length} day${days.length === 1 ? "" : "s"}, latest ${fmtNum(series[series.length - 1] || 0)}`))
      : h("p", { class: "hint" }, "No history yet."));
  }
  async function renderTrends() {
    el.panels.trends.replaceChildren(h("p", { class: "empty" }, "Loading the history…"));
    await ensureHistory();
    if (state.history === null) {
      el.panels.trends.replaceChildren(OPS.emptyState({ title: "History unavailable", icon: "alert", text: "The daily history could not be read." }));
      return;
    }
    el.panels.trends.replaceChildren(h("div", { class: "em-trend-grid" },
      trendChart("Replied per day", "replied", "op-t-ok"),
      trendChart("Pending per day", "pending", "op-t-warn"),
      trendChart("Past SLA per day", "sla_breached", "op-t-danger")));
  }

  // ------------------------------------------------------------------ Unclassified tab
  function unclassifiedRows() {
    // a manager's own view holds only their teams; the server sends the Unclassified mails separately for them
    if (state.answer && Array.isArray(state.answer.unclassified)) return state.answer.unclassified;
    return rows().filter((r) => normAr(r.team) === normAr("Unclassified"));
  }
  function renderUnclassified() {
    const list = unclassifiedRows();
    const ruleMap = (state.options && state.options.system_team_map) || {};
    const ruleEntries = Object.entries(ruleMap);
    const unclTeam = (state.options && state.options.unclassified_team) || "";

    const table = list.length ? OPS.table({
      search: true, copy: true, caption: "Unclassified e-mails", pageSize: 10, pageSizes: [10, 25, 50], rowKey, onRowClick: openMail,
      columns: [
        { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
        { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
        { key: "detected_systems", label: "Detected words", render: (r) => bidi(r.classification_hint || r.detected_systems || "—"), text: (r) => r.detected_systems },
        { key: "mailbox", label: "Sent to", render: (r) => bidi(r.mailbox || "—"), text: (r) => r.mailbox },
        { key: "actions", label: "", render: (r) => h("div", { class: "op-row-actions" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); changeTeamDialog(r); } }, "Assign team"),
          r.status !== "Replied" ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); assignDialog(r); } }, "Assign person") : null,
          r.status !== "Replied" ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); noActionDialog(r); } }, "No action") : null,
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); ruleDialog(systemsOf(r)[0] || ""); } }, "Create rule")) },
      ],
      rows: list,
    }).el : OPS.emptyState({ title: "Nothing unclassified", icon: "check",
      text: "Every e-mail could be put in a team. New ones without a team appear here." });

    const rulesBox = h("div", {},
      h("div", { class: "card-head" }, h("h3", {}, "Classification rules"),
        h("span", { class: "grow" }), h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => ruleDialog("") }, OPS.icon("plus"), "New rule")),
      ruleEntries.length
        ? h("ul", { class: "em-rules" }, ruleEntries.sort((a, b) => a[0].localeCompare(b[0])).map(([kw, team]) => h("li", { class: "em-rule" },
          h("span", { dir: "auto" }, h("code", {}, kw), " → ", bidi(team)),
          h("span", { class: "grow" }),
          canActOnTeam(team) ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => removeRule(kw) }, OPS.icon("trash"), "Remove") : null)))
        : h("p", { class: "hint" }, "No custom keyword rules yet."),
      h("p", { class: "hint" }, unclTeam
        ? `E-mails with no detected team currently go to "${unclTeam}" (change it in System › Settings › Classification).`
        : "Where e-mails with no detected team go is set in System › Settings › Classification."));

    if (!el.sugBox) el.sugBox = h("div", { class: "em-suggest" }, h("p", { class: "hint" }, "Loading suggestions…"));
    el.panels.unclassified.replaceChildren(
      OPS.card("Unclassified e-mails", { hint: "assign a team or create a rule" }, table),
      OPS.card("Suggested classifications", { hint: "learned from employees' replies — approve to file the mail and teach the rule" }, el.sugBox),
      OPS.card("", {}, rulesBox));
    loadSuggestions();
  }

  // When an employee replies to an Unclassified e-mail, the scan proposes filing it under that employee's team and
  // learning a keyword rule. The admin (or the team's manager) approves here; nothing changes until they do.
  async function loadSuggestions() {
    const box = el.sugBox;
    if (!box) return;
    let data;
    try { data = await api("GET", "/api/ops/email/classification/suggestions"); }
    catch (e) { state.sugSig = null; box.replaceChildren(OPS.emptyState({ title: "Could not load suggestions", icon: "alert", text: e.message })); return; }
    const list = data.suggestions || [];
    const sig = JSON.stringify(list.map((s) => [s.message_id, s.suggested_team, s.keyword]));
    if (sig === state.sugSig && box.childElementCount) return;     // unchanged: keep the inputs the user may be editing
    state.sugSig = sig;
    if (!list.length) {
      box.replaceChildren(h("p", { class: "hint" }, "No suggestions waiting. When an employee replies to an unclassified e-mail it is proposed here, with the team to file it under."));
      return;
    }
    box.replaceChildren(h("ul", { class: "em-suggest-list" }, list.map(suggestionItem)));
  }

  function suggestionItem(s) {
    const canAct = ROLE === "admin" || canActOnTeam(s.suggested_team);
    const kw = h("input", { type: "text", class: "em-suggest-kw", maxlength: "80", "aria-label": "Keyword to learn", dir: "auto" });
    kw.value = s.keyword || "";
    const choices = teams().filter((t) => t !== "Unclassified" && (ROLE === "admin" || canActOnTeam(t)));
    if (s.suggested_team && !choices.includes(s.suggested_team)) choices.unshift(s.suggested_team);
    const teamSel = h("select", { "aria-label": "Team" }, choices.map((t) => h("option", { value: t, selected: t === s.suggested_team }, t)));
    const info = h("div", { class: "em-suggest-info" },
      h("button", { type: "button", class: "link em-suggest-subj", dir: "auto", onclick: () => OPS.mailViewer(s.message_id, { onChange: () => loader.refresh() }) }, bidi(s.subject || "(no subject)")),
      h("div", { class: "hint", dir: "auto" }, bidi(`Replied by ${s.reply_by || "a team member"}${s.customer ? " · from " + s.customer : ""}`)));
    if (!canAct) return h("li", { class: "em-suggest-item" }, info, h("div", { class: "hint" }, `Suggested team: ${s.suggested_team}`));
    return h("li", { class: "em-suggest-item" }, info,
      h("div", { class: "em-suggest-act" },
        h("label", { class: "em-field em-suggest-f" }, h("span", {}, "Keyword"), kw),
        h("label", { class: "em-field em-suggest-f" }, h("span", {}, "Team"), teamSel),
        h("div", { class: "em-suggest-btns" },
          h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: () => approveSuggestion(s, kw, teamSel) }, OPS.icon("check"), "Approve"),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => dismissSuggestion(s) }, "Dismiss"))));
  }

  async function approveSuggestion(s, kw, teamSel) {
    const keyword = (kw.value || "").trim(), team = teamSel.value;
    if (keyword.length < 2) { toast("Enter a keyword of at least 2 characters.", "error"); return; }
    if (!team) { toast("Choose a team.", "error"); return; }
    try {
      await api("POST", "/api/ops/email/classification/suggestion", { message_id: s.message_id, keyword, team });
      state.sugSig = null;
      await afterAction(`Filed under ${team}; future mail with "${keyword}" will classify itself.`);
    } catch (e) { toast(e.message, "error"); }
  }

  async function dismissSuggestion(s) {
    const ok = await CL.confirmDialog({ title: "Dismiss suggestion",
      message: "The e-mail stays unclassified and is not suggested again.", confirmLabel: "Dismiss" });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/email/classification/suggestion?message_id=${encodeURIComponent(s.message_id)}`);
      state.sugSig = null;
      await afterAction("Dismissed.");
    } catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------------ tabs
  function badges() {
    const pending = rows().filter((r) => r.status === "Pending Reply").length;
    const uncl = unclassifiedRows().length;
    setBadge("pending", pending, "warn");
    setBadge("unclassified", uncl, "info");
  }
  function setBadge(key, n, tone) {
    const btn = el.tabEls[key];
    if (!btn) return;
    let badge = btn.querySelector(".em-badge");
    if (!n) { if (badge) badge.remove(); return; }
    if (!badge) { badge = h("span", { class: "em-badge" }); btn.append(badge); }
    badge.className = "em-badge " + (tone || "");
    badge.textContent = fmtNum(n);
  }

  function showTab(key) {
    state.tab = key;
    saveView();
    for (const t of TABS) {
      el.tabEls[t.key].setAttribute("aria-selected", String(t.key === key));
      el.panels[t.key].hidden = t.key !== key;
    }
    renderTab(key);
  }

  function renderTab(key) {
    if (!state.data) return;
    if (key === "overview") renderOverview();
    else if (key === "pending") renderPending();
    else if (key === "replied") renderReplied();
    else if (key === "quality") renderQuality();
    else if (key === "trends") renderTrends();
    else if (key === "unclassified") renderUnclassified();
  }

  // ------------------------------------------------------------------ run scan now
  async function runNow() {
    if (state.waiting) return;
    state.waiting = { since: Date.now(), generated: state.generatedAt };
    updateRunUI();
    try {
      await api("POST", "/api/ops/jobs/email/run", {});
    } catch (e) {
      state.waiting = null; updateRunUI(); toast(e.message, "error"); return;
    }
    state.waiting.timer = setInterval(tickRun, 2000);
  }
  function checkRun() {
    // every answer only checks whether the wait is over; asking again is the 2 s timer's job (tickRun). Asking from
    // inside an answer would chain one request onto the next, back to back, until the scan arrives
    const w = state.waiting;
    if (!w) return false;
    if (state.generatedAt && state.generatedAt !== w.generated) { endRun(`New scan at ${OPS.fmtClock(state.generatedAt)}.`); return false; }
    if (Date.now() - w.since > WAIT_LIMIT_MS) { endRun("The collector has not delivered a scan yet - it stays queued.", "error"); return false; }
    updateRunUI();
    return true;
  }
  function tickRun() {
    if (checkRun()) loader.refresh().catch(() => {});
  }
  function endRun(msg, kind) {
    if (state.waiting) clearInterval(state.waiting.timer);
    state.waiting = null;
    updateRunUI();
    if (msg) toast(msg, kind);
  }
  function updateRunUI() {
    el.runBtn.disabled = !!state.waiting;
    el.runBtn.replaceChildren(OPS.icon("refresh"), state.waiting ? "Scanning…" : "Run scan now");
  }

  // ------------------------------------------------------------------ data in
  function onData(answer) {
    state.answer = answer;
    state.data = answer.data || null;
    state.options = answer.options || state.options;
    state.generatedAt = answer.generated_at || "";
    el.scanInfo.textContent = answer.generated_at
      ? `Scan of ${String(answer.generated_at).replace("T", " ").slice(0, 16)}${answer.duration_seconds ? ` · ${Math.round(answer.duration_seconds)} s` : ""}` : "";
    el.empty.hidden = !!state.data;
    el.content.hidden = el.tabbar.hidden = !state.data;
    el.exportLink.hidden = !state.data;
    if (!state.data) return;
    badges();
    renderTab(state.tab);
    if (state.openOnce) { const id = state.openOnce; state.openOnce = ""; OPS.mailViewer(id, { onChange: () => loader.refresh() }); }
    if (state.waiting) checkRun();
  }
  function onMeta(meta) {
    if (!meta) return;
    state.meta = meta;
    el.banner.update(meta);
  }
  function onError(e) {
    if (!state.data) el.banner.update(e.network ? { network: true, module: "email" } : { problem: e.message, module: "email", enabled: true });
    if (!e.network && !state.data) toast(e.message, "error");
  }

  // ------------------------------------------------------------------ shell
  function build() {
    el.scanInfo = h("span", { class: "hint" });
    el.runBtn = h("button", { type: "button", class: "btn btn-primary", onclick: runNow }, OPS.icon("refresh"), "Run scan now");
    el.exportLink = h("a", { class: "btn btn-ghost", href: "/export/ops/email.xlsx", download: "", hidden: true }, OPS.icon("download"), "Export Excel");
    const fb = OPS.freshnessBanner();
    el.banner = fb;
    el.kpis = h("div", { class: "em-kpis" });

    el.tabEls = {};
    el.panels = {};
    const tabbar = el.tabbar = h("div", { class: "em-tabs", role: "tablist", "aria-label": "E-mail views", hidden: true });
    for (const t of TABS) {
      const btn = h("button", { type: "button", class: "em-tab", role: "tab", id: `em-tab-${t.key}`,
        "aria-selected": String(t.key === state.tab), "aria-controls": `em-panel-${t.key}`, onclick: () => showTab(t.key) }, t.label);
      el.tabEls[t.key] = btn;
      tabbar.append(btn);
      el.panels[t.key] = h("div", { class: "em-panel", role: "tabpanel", id: `em-panel-${t.key}`, "aria-labelledby": `em-tab-${t.key}`, hidden: t.key !== state.tab });
    }
    el.content = h("div", { hidden: true }, ...TABS.map((t) => el.panels[t.key]));
    el.empty = OPS.emptyState({ title: "No scan yet", icon: "mail",
      text: "The collector writes the first e-mail scan here as soon as it has run. You can ask for one with the button above." });

    page.replaceChildren(
      h("div", { class: "op-titlebar" },
        h("div", {}, h("h1", {}, "E-mail"), el.scanInfo),
        h("span", { class: "grow" }), ROLE === "admin" && el.runBtn, el.exportLink),
      tabbar, fb.el, el.content, el.empty);          // the same order on every dashboard: title, tabs, freshness, data
  }

  const loader = OPS.loader({ url: "/api/ops/email", interval: 15000, onData, onUnchanged: () => { if (state.waiting) checkRun(); }, onMeta, onError });

  function applyLink() {
    // deep links win over the view remembered for this browser tab (Home, System and the notifications make them):
    //   /email#unclassified   /email?tab=pending&open=<id>   /email?team=<team>   /email?q=<subject>&team=<team>
    //   /email?escalated=1   /email?carryover=1
    const params = new URLSearchParams(location.search);
    const team = params.get("team") || "", q = params.get("q") || "";
    const escalated = params.get("escalated") === "1", carryover = params.get("carryover") === "1";
    if (team || q || escalated || carryover) {                    // exactly the linked rows: the pending mails behind a number
      Object.assign(state.filters, { team, system: "", assigned: "", carryover, escalated });
      state.tab = "pending";
    }
    const wanted = params.get("tab") || decodeURIComponent(location.hash.slice(1));
    if (TABS.some((t) => t.key === wanted)) state.tab = wanted;
    state.linkQuery = q;
    state.openOnce = params.get("open") || "";
  }

  async function start() {
    await ensureStyles();
    loadView();
    applyLink();
    build();
    loader.start();
    document.addEventListener("visibilitychange", () => { /* OPS.loader handles pause/resume */ });
  }
  start();
})();
