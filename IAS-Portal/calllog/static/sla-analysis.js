"use strict";
/* The SLA Analysis (Insights › Analysis › SLA): the security team's own work, from the portal's own status history
   («الحالة التاريخية للطلب») read by the collector at every scan - GET /api/ops/sla-analysis ("glance"): what came,
   what was executed, not executed, rejected and confirmed, every hour, how long it takes, each team and each employee;
   the kept day-by-day numbers (GET /api/ops/sla-days) and, for administrators, the applications with no team.

   SLA_ANALYSIS.mount(panel, {admin}) draws into ``panel`` and keeps it live (every minute while the period reaches
   today and the panel is shown). Built with CL.h / CL.svgEl only - never innerHTML; Arabic text carries dir="auto". */
(function () {
  const { h, api, svgEl, fmtNum } = CL;
  const PATHS = {
    alert: "M12 3 2 20h20L12 3zM12 10v4M12 17h.01",
    info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01",
    download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  };
  const state = {
    analysis: null,          // the latest /api/ops/sla-analysis answer
    slaFilter: null,         // {date_from, date_to, executor, checker, team, ...} - defaults to today on first open
    admin: false,
    today: "",               // the server's day (from its answers)
  };
  let panel = null;

  function glyph(name) {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "sx-ic" });
    svg.append(svgEl("path", { d: PATHS[name] }));
    return svg;
  }

  /** Re-render a block without losing keyboard focus on the control that was used. */
  function keepFocus(root, render) {
    const active = document.activeElement;
    const id = active && root.contains(active) ? active.dataset.focusId : null;
    render();
    if (id) {
      const again = [...root.querySelectorAll("[data-focus-id]")].find((n) => n.dataset.focusId === id);
      if (again) again.focus({ preventScroll: true });
    }
  }

  // ------------------------------------------------------------------ the SLA Analysis (the live status history)
  const ANALYSIS_URL = "/api/ops/sla-analysis";
  const ANALYSIS_REFRESH_MS = 60 * 1000;
  const pctChip = (pct) => OPS.chip(OPS.fmtPct(pct), pct >= 80 ? "ok" : pct >= 50 ? "warn" : "danger");

  const fmtMin = (m) => (m == null ? "—" : m >= 90 ? `${Math.round(m / 60 * 10) / 10} h` : `${Math.round(m)} min`);

  // ---- the filter bar: by date (default = today), employee (executor), checker, team ----
  function slaToday() {                                      // the server's day; before its first answer, this PC's day
    const d = new Date(), two = (n) => String(n).padStart(2, "0");
    return state.today || `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  }
  function slaSelect(label, value, options) {
    const sel = h("select", { "aria-label": label });
    sel.append(h("option", { value: "" }, `${label}: all`));
    (options || []).forEach((o) => sel.append(h("option", { value: o, selected: o === value }, o)));
    sel.value = value || "";
    return sel;
  }
  const OUTCOME_TEXT = { completed: "Completed", not_done: "Not executed", rejected: "Rejected", in_progress: "In progress" };
  const EMPTY_FILTER = { date_from: "", date_to: "", executor: "", checker: "", team: "", system: "", branch: "",
    request_type: "", outcome: "", returned: "" };
  function filterBar(d) {
    const f = state.slaFilter || {}, opt = d.options || {};
    const fromI = h("input", { type: "date", "aria-label": "From date", value: f.date_from || "" });
    const toI = h("input", { type: "date", "aria-label": "To date", value: f.date_to || "" });
    const execSel = slaSelect("Employee", f.executor, opt.executors);
    const checkSel = slaSelect("Checker", f.checker, opt.checkers);
    const teamSel = slaSelect("Team", f.team, opt.teams);
    const sysSel = slaSelect("Application", f.system, opt.systems);
    const branchSel = slaSelect("Branch", f.branch, opt.branches);
    const typeSel = slaSelect("Request type", f.request_type, opt.request_types);
    const outSel = h("select", { "aria-label": "Outcome" }, h("option", { value: "" }, "Outcome: all"),
      Object.entries(OUTCOME_TEXT).map(([k, v]) => h("option", { value: k, selected: k === f.outcome }, v)));
    const backBox = h("input", { type: "checkbox", checked: !!f.returned, "aria-label": "Only requests sent back to the branch" });
    const apply = () => {
      state.slaFilter = { date_from: fromI.value, date_to: toI.value, executor: execSel.value, checker: checkSel.value,
        team: teamSel.value, system: sysSel.value, branch: branchSel.value, request_type: typeSel.value,
        outcome: outSel.value, returned: backBox.checked ? "1" : "" };
      loadAnalysis(true);
    };
    [fromI, toI, execSel, checkSel, teamSel, sysSel, branchSel, typeSel, outSel, backBox].forEach((c) => { c.onchange = apply; });
    const today = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { const t = slaToday(); state.slaFilter = { ...state.slaFilter, date_from: t, date_to: t }; loadAnalysis(true); } }, "Today");
    const clear = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.slaFilter = { ...EMPTY_FILTER }; loadAnalysis(true); } }, "Clear");
    const csv = h("a", { class: "btn btn-ghost btn-sm", href: `/export/ops/sla-analysis.csv${slaQuery()}`, download: "" }, glyph("download"), "CSV");
    const xlsx = h("a", { class: "btn btn-ghost btn-sm", href: `/export/ops/sla-analysis.xlsx${slaQuery()}`, download: "" }, glyph("download"), "Excel");
    const fld = (lbl, ctrl) => h("label", { class: "sx-filter-f" }, h("span", {}, lbl), ctrl);
    return h("div", { class: "sx-filter" }, fld("From", fromI), fld("To", toI), fld("Employee", execSel),
      fld("Checker", checkSel), fld("Team", teamSel), fld("Application", sysSel), fld("Branch", branchSel),
      fld("Request type", typeSel), fld("Outcome", outSel),
      h("label", { class: "sx-filter-f sx-filter-check" }, backBox, h("span", {}, "Sent back only")),
      h("span", { class: "grow" }), today, clear, csv, xlsx);
  }

  // ---- the stages of a request's life, each against its own target (the bank's lifecycle tool's view) ----

  // ---- hour by hour: requests that reached the security team vs requests finished, every team and each team ----
  const hh = (n) => `${String(n).padStart(2, "0")}:00`;

  // ---- the hour-of-day chart: when requests are assigned, executed and confirmed ----


  // ---- compared with the portal's own «الاستفسار عن الطلبات» results of the period (Third Party never shown) ----

  // ---- the main view: the security team's own work in the period - what reached it, what it did, how long it took,
  //      each team and each security user; never a request still with the branch or of another team ----
  function periodText(d) {
    const f = d.filter || {}, t = state.today, day = (x) => (x === t ? "today" : `${x.slice(8, 10)}/${x.slice(5, 7)}/${x.slice(0, 4)}`);
    if (!f.date_from && !f.date_to) return "every day recorded";
    if (f.date_from === f.date_to) return day(f.date_from);
    return `${f.date_from ? day(f.date_from) : "…"} – ${f.date_to ? day(f.date_to) : "…"}`;
  }
  function glanceKpis(d) {
    const G = d.glance || {};
    return h("div", { class: "sx-kpis", "data-glance": "" },
      OPS.kpi({ key: "g-came", label: "Came to us", value: fmtNum(G.came || 0), sub: "requests of these dates with the security team", icon: "activity", hero: true }),
      OPS.kpi({ key: "g-exec", label: "Executed", value: fmtNum(G.executed || 0), sub: "«تم التنفيذ» / «التعزيز على التنفيذ»", tone: "ok", icon: "check" }),
      OPS.kpi({ key: "g-notexec", label: "Not executed", value: fmtNum(G.not_executed || 0), sub: "«لم يتم التنفيذ» / «التعزيز على عدم التنفيذ»", tone: G.not_executed ? "warn" : null, icon: "alert" }),
      OPS.kpi({ key: "g-rejected", label: "Rejected", value: fmtNum(G.rejected || 0), sub: "«رفض» by the assigner", tone: G.rejected ? "warn" : null, icon: "x" }),
      OPS.kpi({ key: "g-open", label: "Still with a security user", value: fmtNum(G.open || 0), sub: "of these requests, not executed yet", icon: "user" }),
      OPS.kpi({ key: "g-awaiting", label: "Waiting for confirmation", value: fmtNum(G.waiting || 0), sub: "executed or not, the reviewer has not confirmed yet", tone: G.waiting ? "warn" : null, icon: "clock" }),
      OPS.kpi({ key: "g-confirmed", label: "Confirmed", value: fmtNum(G.confirmed || 0), sub: `«تم التعزيز» · ${fmtNum(G.confirmed_executed || 0)} executed · ${fmtNum(G.confirmed_not || 0)} not executed`, icon: "shield" }),
      OPS.kpi({ key: "g-now", label: "With us now", value: fmtNum(G.now || 0), sub: "the Live tickets (Re-Assign page), any date", icon: "user" }));
  }
  function glanceNote(d) {
    const G = d.glance || {}, R = (d.recorder || {}).stats || {}, T = R.timing || {};
    const bits = ["Counted as the portal's «الاستفسار عن الطلبات» lists the requests of these «تاريخ الطلب» dates, by their status now: came = executed + not executed + rejected + still with a security user"];
    if (G.pre) bits.push(`${fmtNum(G.pre)} request(s) of these dates still with the branch (registration, supervisor): not counted`);
    if (R.at) {
      bits.push(`last scan ${String(R.at).slice(11, 16)}: ${fmtNum(R.opened || 0)} status histor(ies) read, ${fmtNum(R.left || 0)} waiting to be read (times and hours come from them)`
        + (T.stopped ? ` - stopped: ${T.stopped}` : ""));
    }
    if ((G.logged_days || []).length) bits.push(`days before the inquiry window come from the numbers kept those days`);
    return h("p", { class: "hint", "data-glance-note": "" }, bits.join(" · ") + ".");
  }
  function glanceHours(d) {
    const hours = ((d.glance || {}).hours) || [];
    const active = hours.filter((x) => x.came || x.done || x.confirmed);
    const hint = "from the read status histories: came = first assigned · done = executed, not executed or rejected · the rest under “No time yet”";
    if (!active.length) return OPS.card("Every hour", { hint }, h("p", { class: "hint" }, "Nothing came or was done in this period yet."));
    const lo = Math.min(8, ...active.map((x) => x.hour)), hi = Math.max(16, ...active.map((x) => x.hour));
    const span = hours.slice(lo, hi + 1);
    const max = Math.max(1, ...span.map((x) => Math.max(x.came, x.done)));
    const W = span.length * 4;
    const svg = svgEl("svg", { class: "sx-hourly", viewBox: `0 0 ${W} 100`, preserveAspectRatio: "none", role: "img",
      "aria-label": "Requests that came and requests done, hour by hour", "data-glance-hours": "" });
    span.forEach((x, i) => {
      [["came", "sx-hb-a", "came"], ["done", "sx-hb-c", "done"]].forEach(([k, cls, lbl], j) => {
        const bh = x[k] ? Math.max(1.5, (x[k] / max) * 88) : 0;
        const rect = svgEl("rect", { x: i * 4 + 0.4 + j * 1.6, y: 100 - bh, width: 1.5, height: bh, class: `${cls} sx-grow` });
        const t = svgEl("title", {});
        t.textContent = `${hh(x.hour)} – ${lbl}: ${fmtNum(x[k])}`;
        rect.append(t);
        svg.append(rect);
      });
    });
    const axis = h("div", { class: "sx-hours-axis hint", "aria-hidden": "true" }, ...span.filter((_, i) => i % 2 === 0).map((x) => h("span", {}, hh(x.hour))));
    const legend = h("div", { class: "sx-daily-legend hint" }, h("i", { class: "sx-key sx-hb-a" }), "Came", h("i", { class: "sx-key sx-hb-c" }), "Done");
    const U = (d.glance || {}).untimed || {}, someUntimed = !!(U.came || U.done || U.confirmed);
    const sum = (k) => span.reduce((a, x) => a + (x[k] || 0), 0) + (U[k] || 0);
    const line = (label, k) => h("tr", {}, h("th", { scope: "row" }, label), ...span.map((x) => h("td", { class: "num" }, x[k] ? fmtNum(x[k]) : "·")),
      someUntimed ? h("td", { class: "num", title: "Counted from the inquiry's status: the hour follows when the history is read" }, U[k] ? fmtNum(U[k]) : "·") : null,
      h("td", { class: "num" }, fmtNum(sum(k))));
    const table = h("div", { class: "table-wrap" }, h("table", { class: "data sx-hour-table", "data-glance-hour-table": "" },
      h("caption", { class: "sr-only" }, "Requests per hour: came, done, confirmed"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Hour"), ...span.map((x) => h("th", { scope: "col", class: "num" }, hh(x.hour))),
        someUntimed ? h("th", { scope: "col", class: "num" }, "No time yet") : null, h("th", { scope: "col", class: "num" }, "Total"))),
      h("tbody", {}, line("Came", "came"), line("Done", "done"), line("Confirmed", "confirmed"))));
    return OPS.card("Every hour", { hint }, h("div", { class: "sx-stack" }, h("div", { class: "sx-daily-wrap" }, svg, axis, legend), table));
  }
  function glanceTimes(d) {
    const T = (d.glance || {}).times || {};
    const rows = [["execution", "Execution", "assignment → «تم التنفيذ» / «لم يتم التنفيذ» / «رفض»"],
      ["confirmation", "Confirmation", "execution → «تم التعزيز»"], ["whole", "Whole request", "last assignment → «تم التعزيز»"]];
    const body = rows.map(([k, label, what]) => { const t = T[k] || {};
      return h("tr", { "data-time": k }, h("th", { scope: "row" }, label, h("span", { class: "hint", dir: "auto" }, ` · ${what}`)),
        h("td", { class: "num" }, fmtNum(t.n || 0)), h("td", { class: "num" }, fmtMin(t.average_minutes)), h("td", { class: "num" }, fmtMin(t.median_minutes)),
        h("td", { class: "num" }, fmtMin(t.target_minutes)), h("td", { class: "num" }, t.n ? pctChip(t.within_pct) : h("span", { class: "hint" }, "—"))); });
    return OPS.card("How long it takes", { hint: "the requests of this period whose history is read · targets: System › Settings › Ticketing" },
      h("div", { class: "table-wrap" }, h("table", { class: "data", "data-glance-times": "" },
        h("caption", { class: "sr-only" }, "Execution and confirmation times"),
        h("thead", {}, h("tr", {}, ...["", "Requests", "Average", "Median", "Target", "Within target"].map((c, i) => h("th", { scope: "col", class: i ? "num" : "" }, c)))),
        h("tbody", {}, ...body))));
  }
  function glanceTable(rows, key, label, caption) {
    rows = rows || [];
    if (!rows.length) return h("p", { class: "hint" }, "Nothing in this period.");
    const n = (k) => ({ key: k[0], label: k[1], num: true, sort: true, render: (r) => fmtNum(r[k[0]] || 0) });
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 15, pageSizes: [15, 50, 200], caption, sort: { key: "came", desc: true },
      columns: [
        { key, label, sort: true, render: (r) => h("span", { dir: "auto" }, String(r[key])), text: (r) => String(r[key]) },
        ...[["came", "Came"], ["executed", "Executed"], ["not_executed", "Not executed"], ["rejected", "Rejected"],
          ["open", "Still with them"], ["waiting", "Waiting confirmation"], ["confirmed", "Confirmed"], ["now", "With them now (Live)"]].map(n),
        { key: "exec_median_minutes", label: "Execution (median)", num: true, sort: true, render: (r) => fmtMin(r.exec_median_minutes) },
        { key: "exec_within_pct", label: "Within target", num: true, sort: true, render: (r) => (r.exec_n ? pctChip(r.exec_within_pct) : h("span", { class: "hint" }, "—")) },
      ], rows,
    }).el;
  }
  function glanceSection(d) {
    const G = d.glance || {};
    return [
      h("h2", { class: "sx-glance-title" }, `The security team · ${periodText(d)}`),
      glanceKpis(d), glanceNote(d), glanceHours(d), glanceTimes(d),
      OPS.card("Each team", { hint: "the same numbers, team by team" }, glanceTable(G.by_team, "team", "Team", "Each team")),
      OPS.card("Each employee", { hint: "مستخدم السرية of each request · a rejection counts for the assigner who rejected it" }, glanceTable(G.by_person, "person", "Employee", "Each employee")),
    ];
  }
  // ---- day by day: the numbers kept at every scan (performance.read_day_logs) - the team picked, or one employee ----
  function dayByDayCard() {
    const body = h("div", { "data-days": "" }, h("p", { class: "hint" }, "Loading…"));
    const f = state.slaFilter || {}, q = new URLSearchParams();
    if (f.team) q.set("team", f.team);
    if (f.executor) q.set("executor", f.executor);
    const seq = ++daysSeq;
    api("GET", `/api/ops/sla-days${q.toString() ? "?" + q.toString() : ""}`).then((a) => {
      if (seq !== daysSeq) return;
      const rows = a.days || [];
      body.replaceChildren(!rows.length ? h("p", { class: "hint", "data-days-empty": "" }, "No day kept yet: the numbers are kept at every scan from this update on.")
        : OPS.table({ search: false, copy: true, pageSize: 15, pageSizes: [15, 31, 90], caption: "Day by day", sort: { key: "day", desc: true },
          columns: [
            { key: "day", label: "Day", sort: true, render: (r) => `${r.day.slice(8, 10)}/${r.day.slice(5, 7)}/${r.day.slice(0, 4)}`, text: (r) => r.day },
            ...[["came", "Came"], ["executed", "Executed"], ["not_executed", "Not executed"], ["rejected", "Rejected"], ["open", "Still open"],
              ["waiting", "Waiting confirmation"], ["confirmed", "Confirmed"]]
              .map(([k, l]) => ({ key: k, label: l, num: true, sort: true, render: (r) => fmtNum(r[k] || 0) })),
            { key: "exec_average_minutes", label: "Execution (average)", num: true, sort: true, render: (r) => fmtMin(r.exec_average_minutes) },
            { key: "exec_within_pct", label: "Within target", num: true, sort: true, render: (r) => (r.exec_n ? pctChip(r.exec_within_pct) : h("span", { class: "hint" }, "—")) },
            { key: "best", label: "Best performer", render: (r) => (r.best ? h("span", { dir: "auto" }, `${r.best.name} (${fmtNum(r.best.total)})`) : "—"), text: (r) => (r.best ? r.best.name : "") },
          ], rows }).el);
    }).catch((e) => { if (seq === daysSeq) body.replaceChildren(h("p", { class: "hint" }, e.message)); });
    const who = f.executor ? ` · ${f.executor}` : f.team ? ` · ${f.team}` : "";
    return OPS.card("Day by day", { hint: `kept at every scan, the last 30 days${who}` }, body);
  }
  let daysSeq = 0;

  // ---- the applications no rule gives a team (administrators): give each its team here - Auto Assign › Applications ----
  function noTeamCard(d) {
    const apps = d.no_team_apps || [];
    if (!state.admin || !apps.length) return null;
    const names = d.team_names || [];
    const rows = apps.map((a) => {
      const sel = h("select", { "aria-label": `Team of ${a.system}`, "data-classify": a.system },
        h("option", { value: "" }, "Choose a team…"), ...names.map((t) => h("option", { value: t }, t)));
      const save = h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: async () => {
        if (!sel.value) { CL.toast("Choose the team first.", "error"); return; }
        save.disabled = true;
        try {
          await api("POST", "/api/ops/auto-assign/apps", { name: a.system, team: sel.value });
          CL.toast(`${a.system} → ${sel.value}`);
          loadAnalysis(true, true);
        } catch (e) { CL.toast(e.message, "error"); save.disabled = false; }
      } }, "Save");
      return h("tr", { "data-no-team": a.system }, h("td", { dir: "auto" }, a.system), h("td", { class: "num" }, fmtNum(a.requests)), h("td", {}, sel), h("td", {}, save));
    });
    return OPS.card("Applications with no team", { hint: "their requests count under “(no team)”: give each application its team (saved in Auto Assign › Applications)" },
      h("div", { class: "table-wrap" }, h("table", { class: "data", "data-no-team-table": "" },
        h("caption", { class: "sr-only" }, "Applications with no team"),
        h("thead", {}, h("tr", {}, ...["Application", "Requests", "Team", ""].map((c, i) => h("th", { scope: "col", class: i === 1 ? "num" : "" }, c)))),
        h("tbody", {}, ...rows))));
  }

  function renderAnalysis(d) {
    if (!d || !d.available) {
      const R = (d && d.recorder) || {};
      const why = R.enabled === false
        ? "Reading the status history is switched off in System › Settings › Ticketing › SLA history."
        : (R.error && R.error.message) ? `The last scan could not read it: ${R.error.message}`
        : "Nothing is uploaded: at every scan the collector opens «الاستفسار عن الطلبات» on the portal and reads each request's «الحالة التاريخية للطلب» — who assigned, executed and confirmed it, and exactly when. The analysis appears after the next scan.";
      panel.replaceChildren(OPS.emptyState({ title: "No SLA history recorded yet", icon: "shield", text: why }));
      return;
    }
    panel.replaceChildren(h("div", { class: "sx-stack" }, ...[filterBar(d), ...glanceSection(d), dayByDayCard(), noTeamCard(d)].filter(Boolean)));
  }

  function slaQuery() {
    const f = state.slaFilter || {}, p = new URLSearchParams();
    if (f.date_from) p.set("from", f.date_from);
    if (f.date_to) p.set("to", f.date_to);
    for (const k of ["executor", "checker", "team", "system", "branch", "request_type", "outcome", "returned"]) if (f[k]) p.set(k, f[k]);
    const s = p.toString();
    return s ? "?" + s : "";
  }

  let analysisSeq = 0;
  async function loadAnalysis(force, quiet) {
    if (state.analysis && !force) {                         // back on the tab: show what we have, then bring it up to date
      renderAnalysis(state.analysis);
      force = quiet = true;
    }
    const first = !state.slaFilter;                         // first open: today - the SERVER's day, not this PC's
    const seq = ++analysisSeq;
    if (!quiet) panel.replaceChildren(h("p", { class: "empty" }, "Loading the SLA analysis…"));
    let answer;
    try { answer = await api("GET", ANALYSIS_URL + (first ? "?today=1" : slaQuery())); }
    catch (e) {
      if (seq === analysisSeq && !quiet) panel.replaceChildren(OPS.emptyState({ title: "SLA analysis unavailable", icon: "alert", text: e.message }));
      return;
    }
    if (seq !== analysisSeq) return;                        // a newer request (another filter, a newer scan) wins
    state.today = String(answer.server_time || "").slice(0, 10) || state.today;
    if (first && !state.slaFilter) state.slaFilter = { ...EMPTY_FILTER, ...(answer.filter || { date_from: state.today, date_to: state.today }) };
    state.analysis = answer;
    if (quiet) keepFocus(panel, () => renderAnalysis(answer));
    else renderAnalysis(answer);
  }

  // ------------------------------------------------------------------ mount
  let timer = 0;
  function mount(root, opts) {
    const o = opts || {};
    panel = root;
    state.admin = !!o.admin;
    const q = new URLSearchParams(location.search);       // /analysis?tab=sla&from=YYYY-MM-DD&to=YYYY-MM-DD
    const day = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(v || "") ? v : "");
    if (!state.slaFilter && (day(q.get("from")) || day(q.get("to")))) {
      state.slaFilter = { ...EMPTY_FILTER, date_from: day(q.get("from")), date_to: day(q.get("to")) };
    }
    loadAnalysis();
    if (!timer) {
      // live: while it is shown and its period reaches today, it refreshes itself every minute
      timer = setInterval(() => {
        const f = state.slaFilter || {};
        if (panel && panel.isConnected && !panel.hidden && !document.hidden && state.analysis
            && (!f.date_to || f.date_to >= slaToday())) loadAnalysis(true, true);
      }, ANALYSIS_REFRESH_MS);
      // back on the browser tab: bring it up to date at once (a scan may have recorded new requests meanwhile)
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden && panel && panel.isConnected && state.analysis) loadAnalysis(true, true);
      });
    }
  }

  window.SLA_ANALYSIS = { mount };
})();
