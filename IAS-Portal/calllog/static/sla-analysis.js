"use strict";
/* The SLA Analysis (Insights › Analysis › SLA): the security requests' SLA from the portal's own status history
   («الحالة التاريخية للطلب»), read by the collector at every scan - GET /api/ops/sla-analysis. Moved here from the
   Ticketing page, which keeps the live tickets. The period is the ASSIGNMENT day; "Right now" is always current; the
   "Compared with the portal" card puts the portal's own «الاستفسار عن الطلبات» counts next to what is recorded.

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
  const isSunday = (d) => { const t = new Date(String(d) + "T12:00:00"); return !Number.isNaN(t.getTime()) && t.getDay() === 0; };

  const fmtMin = (m) => (m == null ? "—" : m >= 90 ? `${Math.round(m / 60 * 10) / 10} h` : `${Math.round(m)} min`);

  function dailyChart(days) {
    if (!days || !days.length) return h("p", { class: "hint" }, "No daily data yet.");
    const max = Math.max(1, ...days.map((d) => d.n || 0));
    const svg = svgEl("svg", { class: "sx-daily", viewBox: `0 0 ${days.length} 100`, preserveAspectRatio: "none",
      role: "img", "aria-label": "Confirmed requests per day, Sundays highlighted" });
    days.forEach((d, i) => {
      const bh = Math.max(0.6, ((d.n || 0) / max) * 100);
      const rect = svgEl("rect", { x: i + 0.08, y: 100 - bh, width: 0.84, height: bh,
        class: isSunday(d.date) ? "sx-bar sx-bar-sun" : "sx-bar" });
      const t = svgEl("title", {});
      t.textContent = `${d.date}${isSunday(d.date) ? " (Sunday)" : ""}: ${fmtNum(d.n)} confirmed · ${d.sla_pct}% within SLA`;
      rect.append(t);
      svg.append(rect);
    });
    return h("div", { class: "sx-daily-wrap" }, svg,
      h("div", { class: "sx-daily-legend hint" }, h("i", { class: "sx-key sx-key-sun" }), "Sunday", h("i", { class: "sx-key sx-key-day" }), "other days"));
  }

  function rollupTable(rows, keyCol, label, caption, sort) {
    rows = rows || [];
    const numbered = rows.some((r) => r.employee_no);     // a person: show the employee number of the status history
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption,
      sort: sort || { key: "n", desc: true },
      columns: [
        { key: keyCol, label, sort: true, render: (r) => h("span", { dir: "auto" }, String(r[keyCol])), text: (r) => String(r[keyCol]) },
        ...(numbered ? [{ key: "employee_no", label: "Employee no.", sort: true, render: (r) => r.employee_no || "—", text: (r) => r.employee_no || "" }] : []),
        { key: "n", label: "Requests", num: true, sort: true, render: (r) => fmtNum(r.n) },
        { key: "executed", label: "Executed", num: true, sort: true, render: (r) => (r.executed + r.not_executed ? fmtNum(r.executed) : "—") },
        { key: "executed_pct", label: "Executed %", num: true, sort: true,
          render: (r) => (r.executed + r.not_executed ? OPS.fmtPct(r.executed_pct) : h("span", { class: "hint", title: "Left the queue - executed or not comes from the export" }, "—")) },
        { key: "median_minutes", label: "Median", num: true, sort: true, render: (r) => fmtMin(r.median_minutes) },
        { key: "sla_pct", label: "Within SLA %", num: true, sort: true,     // no clock in the group: "—", not a red 0 %
          render: (r) => (r.median_minutes == null ? h("span", { class: "hint", title: "No request here has a start and an end time" }, "—") : pctChip(r.sla_pct)) },
      ], rows,
    }).el;
  }

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
  const OUTCOME_TONE = { completed: "ok", not_done: "warn", rejected: "danger", in_progress: "info" };
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
  const ragTone = (rag) => (rag === "ok" ? "ok" : rag === "warn" ? "warn" : rag === "bad" ? "danger" : null);
  const ragChip = (minutes, rag) => (minutes == null ? h("span", { class: "hint" }, "—") : rag ? OPS.chip(fmtMin(minutes), ragTone(rag)) : fmtMin(minutes));
  const ragOf = (v, target) => (v == null || !target ? "" : v <= target ? "ok" : v <= 1.5 * target ? "warn" : "bad");

  function stageTable(stages) {
    return OPS.table({
      search: false, copy: true, caption: "Stages of a request", pageSize: 10,
      columns: [
        { key: "label", label: "Stage", render: (r) => r.label, text: (r) => r.label },
        { key: "n", label: "Requests", num: true, render: (r) => fmtNum(r.n) },
        { key: "average_minutes", label: "Average", num: true, render: (r) => ragChip(r.average_minutes, r.rag), text: (r) => fmtMin(r.average_minutes) },
        { key: "p50_minutes", label: "Median", num: true, render: (r) => fmtMin(r.p50_minutes) },
        { key: "p90_minutes", label: "90 % within", num: true, render: (r) => ragChip(r.p90_minutes, ragOf(r.p90_minutes, r.target)), text: (r) => fmtMin(r.p90_minutes) },
        { key: "max_minutes", label: "Longest", num: true, render: (r) => fmtMin(r.max_minutes) },
        { key: "target", label: "Target", num: true, render: (r) => fmtMin(r.target) },
        { key: "within_pct", label: "On target", num: true, render: (r) => (r.n ? pctChip(r.within_pct) : h("span", { class: "hint" }, "—")), text: (r) => (r.n ? `${r.within_pct}%` : "") },
        { key: "breached", label: "Over target", num: true, render: (r) => (r.breached ? OPS.chip(fmtNum(r.breached), "danger") : fmtNum(0)), text: (r) => String(r.breached) },
      ], rows: stages || [],
    }).el;
  }

  function waterfall(d) {
    const w = d.waterfall || [];
    if (!w.length) return h("p", { class: "hint" }, "No request in this view has its stages stamped yet.");
    const tones = ["op-t-na", "op-t-info", "op-t-warn", "op-t-ok", "op-t-danger"];
    const b = d.bottleneck;
    return h("div", { class: "sx-stack" },
      OPS.stackbar(w.map((s, i) => ({ label: `${s.label} · ${fmtMin(s.average_minutes)}`, value: s.average_minutes, cls: tones[i % tones.length] }))),
      b ? h("p", { class: "hint", dir: "auto" }, `Bottleneck: ${b.label} — ${fmtMin(b.average_minutes)} on average, ${b.share_pct}% of a request's time.`) : null);
  }

  const FUNNEL_TEXT = { registered: "Registered by the branch", branch_confirmed: "Confirmed by the branch supervisor",
    assigned: "Assigned to a security user", executed: "Executed / not executed", confirmed: "Confirmed by the reviewer" };
  function outcomesCard(d) {
    const o = d.outcomes || {}, total = (d.meta || {}).requests || 0;
    const tiles = h("div", { class: "sx-kpis" },
      ...Object.entries(OUTCOME_TEXT).map(([k, label]) => OPS.kpi({ label, value: fmtNum(o[k] || 0),
        sub: total ? `${Math.round(100 * (o[k] || 0) / total)}% of ${fmtNum(total)}` : "", tone: (o[k] && k !== "completed" && k !== "in_progress") ? OUTCOME_TONE[k] : null,
        icon: k === "completed" ? "check" : k === "in_progress" ? "clock" : "alert" })),
      OPS.kpi({ label: "Sent back to the branch", value: fmtNum(o.returned || 0), sub: `${o.returned_pct || 0}% · «مطلوب تعديل»`, tone: (o.returned_pct || 0) >= 20 ? "danger" : o.returned ? "warn" : null, icon: "refresh" }),
      OPS.kpi({ label: "Re-assigned", value: fmtNum(o.reassigned || 0), sub: "to another security user", icon: "user" }),
      OPS.kpi({ label: "Exception path", value: fmtNum(o.exception || 0), sub: "approved by the region / sector", icon: "shield" }));
    const funnel = h("ol", { class: "sx-funnel" }, ...(d.funnel || []).map((s) => h("li", {},
      h("span", { class: "sx-funnel-label" }, FUNNEL_TEXT[s.key] || s.key),
      h("span", { class: "sx-funnel-bar" }, h("i", { class: `sx-funnel-fill sx-w-${Math.max(0, Math.min(100, Math.round(s.pct / 5) * 5))}` })),
      h("span", { class: "sx-funnel-n" }, `${fmtNum(s.n)} · ${Math.round(s.pct)}%`))));
    return OPS.card("Outcomes and the path", { hint: "every request in this view" }, h("div", { class: "sx-stack" }, tiles,
      h("h4", { class: "sx-sub" }, "How far the requests got"), funnel));
  }

  function attentionTable(d) {
    const rows = d.attention || [];
    if (!rows.length) return h("p", { class: "hint" }, "Nothing needs attention in this view: no SLA breach, rejection or request sent back.");
    return OPS.table({
      search: rows.length > 10, copy: true, pageSize: 10, pageSizes: [10, 25, 100], caption: "Requests that need attention",
      onRowClick: (r) => openRequest(r),
      columns: [
        { key: "reasons", label: "Why", render: (r) => h("div", { class: "sx-chips" }, ...r.reasons.map((x) => OPS.chip(x, /breach|past|Rejected/i.test(x) ? "danger" : "warn"))), text: (r) => r.reasons.join(", ") },
        { key: "id", label: "Request", render: (r) => String(r.id || "—"), text: (r) => String(r.id || "") },
        { key: "requester", label: "Requester", render: (r) => h("span", { dir: "auto" }, r.requester || "—"), text: (r) => r.requester || "" },
        { key: "branch", label: "Branch", render: (r) => h("span", { dir: "auto" }, r.branch || "—"), text: (r) => r.branch || "" },
        { key: "system", label: "Application", render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
        { key: "executor", label: "Security user", render: (r) => h("span", { dir: "auto" }, r.executor || "—"), text: (r) => r.executor || "" },
        { key: "sla", label: "SLA time", num: true, sort: (r) => (r.minutes || {}).sla || r.waiting_minutes || 0,
          render: (r) => ((r.minutes || {}).sla != null ? ragChip(r.minutes.sla, ragOf(r.minutes.sla, (d.meta || {}).sla_minutes)) : r.waiting_minutes != null ? `${fmtMin(r.waiting_minutes)} so far` : "—") },
      ], rows,
    }).el;
  }

  function requestsTable(d) {
    const rows = d.requests_list || [], T = (d.meta || {}).targets || {};
    const stageCol = (key, label) => ({ key: `m_${key}`, label, num: true, sort: (r) => (r.minutes || {})[key] ?? -1,
      render: (r) => ragChip((r.minutes || {})[key], ragOf((r.minutes || {})[key], T[key])), text: (r) => fmtMin((r.minutes || {})[key]) });
    return OPS.table({
      search: true, searchPlaceholder: "Search request, requester, branch, application…", copy: true,
      pageSize: 25, pageSizes: [25, 50, 100], caption: "Requests in this view", onRowClick: (r) => openRequest(r),
      sort: { key: "finished_at", desc: true },
      columns: [
        { key: "id", label: "Request", sort: true, render: (r) => String(r.id || "—"), text: (r) => String(r.id || "") },
        { key: "request_date", label: "Date", sort: true, render: (r) => r.request_date || "—", text: (r) => r.request_date || "" },
        { key: "requester", label: "Requester", sort: true, render: (r) => h("span", { dir: "auto" }, r.requester || "—"), text: (r) => `${r.requester} ${r.requester_no}` },
        { key: "branch", label: "Branch", sort: true, render: (r) => h("span", { dir: "auto" }, r.branch || "—"), text: (r) => r.branch || "" },
        { key: "system", label: "Application", sort: true, render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
        { key: "outcome", label: "Outcome", sort: true, render: (r) => h("div", { class: "sx-chips" }, OPS.chip(OUTCOME_TEXT[r.outcome] || r.outcome, OUTCOME_TONE[r.outcome]),
          r.returned ? OPS.chip(`sent back ×${r.returned}`, "warn") : null, r.region ? OPS.chip("exception", "info") : null), text: (r) => OUTCOME_TEXT[r.outcome] || r.outcome },
        stageCol("branch", "Branch"), stageCol("pickup", "To assign"), stageCol("work", "Security user"),
        stageCol("review", "Reviewer"), stageCol("sla", "SLA"), stageCol("lifetime", "Whole"),
        { key: "finished_at", label: "Finished", sort: (r) => r.finished_at || "9999", render: (r) => r.finished_at || (r.waiting_minutes != null ? `${fmtMin(r.waiting_minutes)} so far` : "—"), text: (r) => r.finished_at || "" },
      ], rows,
    }).el;
  }

  const STEP_TEXT = { registered: "Registered", branch_confirmed: "Confirmed by the branch supervisor",
    region_approved: "Approved above the branch", assigned: "Assigned to a security user", held: "On hold",
    returned: "Sent back to the branch", executed: "Executed", not_executed: "Not executed",
    confirmed_executed: "Confirmed (executed)", confirmed_not: "Confirmed (not executed)", rejected: "Rejected",
    transferred: "Transferred" };
  async function openRequest(row) {
    if (!row.id) {
      CL.toast(row.unread ? "This request's status history is read at the next scan." : "Open it from the Live tickets tab.", "info");
      return;
    }
    let d;
    try {
      const q = new URLSearchParams({ id: row.id || "" });
      if (row.finished_at) q.set("day", row.finished_at);
      d = await api("GET", `/api/ops/sla-analysis/request?${q.toString()}`);
    } catch (e) { CL.toast(e.message, "error"); return; }
    const r = d.request || {};
    const facts = [["Request", r.id], ["Requester", [r.requester, r.requester_no].filter(Boolean).join(" · ")], ["Branch", r.branch],
      ["Application", r.system], ["Team", r.team], ["Request type", r.request_type], ["Status now", r.grid_status || r.last_status],
      ["Outcome", OUTCOME_TEXT[d.outcome] || d.outcome], ["Sent back", r.returned ? `${r.returned} time(s)` : ""],
      ["Re-assigned", r.reassigned ? `${r.reassigned} time(s)` : ""], ["On hold", r.held_minutes ? fmtMin(r.held_minutes) : ""]]
      .filter(([, v]) => v !== "" && v != null);
    const body = h("div", { class: "sx-stack" },
      h("dl", { class: "op-defs" }, ...facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", { dir: "auto" }, String(v))])),
      h("h4", { class: "sx-sub" }, "Stages"),
      OPS.table({ search: false, copy: true, caption: "Stages of this request", pageSize: 10,
        columns: [
          { key: "label", label: "Stage", render: (s) => s.label, text: (s) => s.label },
          { key: "minutes", label: "Took", num: true, render: (s) => ragChip(s.minutes, s.rag), text: (s) => fmtMin(s.minutes) },
          { key: "target", label: "Target", num: true, render: (s) => fmtMin(s.target) },
        ], rows: d.stages || [] }).el,
      h("h4", { class: "sx-sub" }, "Timeline — «الحالة التاريخية للطلب»"),
      (d.trail || []).length ? h("ol", { class: "sx-trail" }, ...d.trail.map((t) => h("li", { class: `sx-trail-${t.step}` },
        h("span", { class: "sx-trail-at" }, fmtStamp(t.at)),
        h("strong", {}, STEP_TEXT[t.step] || t.step),
        h("span", { class: "hint", dir: "auto" }, [t.status, [t.who, t.number].filter(Boolean).join(" · ")].filter(Boolean).join(" — ")),
        t.note ? h("span", { class: "sx-trail-note", dir: "auto" }, t.note) : null)))
        : h("p", { class: "hint" }, "The timeline of this request was read before timelines were kept: it shows at its next change."));
    OPS.modal({ title: `Request ${r.id || ""}`, wide: true, body });
  }

  // ---- hour by hour: requests that reached the security team vs requests finished, every team and each team ----
  const hh = (n) => `${String(n).padStart(2, "0")}:00`;
  const pctTone = (p) => (p == null ? null : p >= 100 ? "ok" : p >= 70 ? "warn" : "danger");
  const pctCell = (p) => (p == null ? h("span", { class: "hint" }, "—") : OPS.chip(`${Math.round(p)}%`, pctTone(p)));
  function hourlyCard(d) {
    const H = d.hourly || {}, hours = H.hours || [];
    const active = hours.filter((x) => x.received || x.done);
    const hint = "arrived = first assigned to a security user · finished = confirmed by the reviewer · live";
    if (!active.length) return OPS.card("Hour by hour — arrived vs finished", { hint }, h("p", { class: "hint" }, "No request arrived or was finished in this view yet."));
    const lo = Math.min(8, ...active.map((x) => x.hour)), hi = Math.max(16, ...active.map((x) => x.hour));
    const span = hours.slice(lo, hi + 1);
    const max = Math.max(1, ...span.map((x) => Math.max(x.received, x.done)));
    const W = span.length * 4;
    const svg = svgEl("svg", { class: "sx-hourly", viewBox: `0 0 ${W} 100`, preserveAspectRatio: "none", role: "img",
      "aria-label": "Requests that arrived and requests finished, hour by hour" });
    span.forEach((x, i) => {
      [["received", "sx-hb-a", "arrived"], ["done", "sx-hb-c", "finished"]].forEach(([k, cls, lbl], j) => {
        const bh = x[k] ? Math.max(1.5, (x[k] / max) * 88) : 0;
        const rect = svgEl("rect", { x: i * 4 + 0.4 + j * 1.6, y: 100 - bh, width: 1.5, height: bh, class: `${cls} sx-grow` });
        const t = svgEl("title", {});
        t.textContent = `${hh(x.hour)} – ${lbl}: ${fmtNum(x[k])}${k === "done" && x.done_pct != null ? ` (${Math.round(x.done_pct)}% of the arrived)` : ""}`;
        rect.append(t);
        svg.append(rect);
      });
    });
    const pts = span.map((x, i) => (x.done_pct == null ? null : `${(i * 4 + 2).toFixed(1)},${(100 - Math.min(x.done_pct, 200) / 200 * 88).toFixed(1)}`)).filter(Boolean);
    if (pts.length > 1) svg.append(svgEl("polyline", { class: "sx-pct-line", points: pts.join(" "), fill: "none" }));
    const axis = h("div", { class: "sx-hours-axis hint", "aria-hidden": "true" }, ...span.filter((_, i) => i % 2 === 0).map((x) => h("span", {}, hh(x.hour))));
    const legend = h("div", { class: "sx-daily-legend hint" }, h("i", { class: "sx-key sx-hb-a" }), "Arrived",
      h("i", { class: "sx-key sx-hb-c" }), "Finished", h("i", { class: "sx-key sx-key-line" }), "Finished ÷ arrived (line: 0–200 %)");
    const head = h("tr", {}, h("th", { scope: "col" }, "All teams"), ...span.map((x) => h("th", { scope: "col", class: "num" }, hh(x.hour))),
      h("th", { scope: "col", class: "num" }, "Total"));
    const sum = (k) => span.reduce((a, x) => a + (x[k] || 0), 0);
    const totR = sum("received"), totD = sum("done"), totS = sum("same_hour");
    const line = (label, cells, total) => h("tr", {}, h("th", { scope: "row" }, label), ...cells, h("td", { class: "num" }, total));
    const table = h("div", { class: "table-wrap" }, h("table", { class: "data sx-hour-table" },
      h("caption", { class: "sr-only" }, "Arrived and finished requests per hour, all teams"), h("thead", {}, head), h("tbody", {},
        line("Arrived", span.map((x) => h("td", { class: "num" }, fmtNum(x.received))), fmtNum(totR)),
        line("Finished", span.map((x) => h("td", { class: "num" }, fmtNum(x.done))), fmtNum(totD)),
        line("Finished ÷ arrived", span.map((x) => h("td", { class: "num" }, pctCell(x.done_pct))), pctCell(totR ? 100 * totD / totR : null)),
        line("Finished the same hour", span.map((x) => h("td", { class: "num" }, x.received ? `${fmtNum(x.same_hour)} · ${Math.round(x.same_hour_pct)}%` : "—")),
          totR ? `${fmtNum(totS)} · ${Math.round(100 * totS / totR)}%` : "—"))));
    const teams = (H.teams || []).slice(0, 20);
    const teamTable = teams.length > 1 ? h("div", { class: "table-wrap" }, h("table", { class: "data sx-hour-table" },
      h("caption", { class: "sr-only" }, "Finished out of arrived per hour, by team"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Team · finished / arrived"), ...span.map((x) => h("th", { scope: "col", class: "num" }, hh(x.hour))),
        h("th", { scope: "col", class: "num" }, "Total"))),
      h("tbody", {}, ...teams.map((t) => h("tr", {}, h("th", { scope: "row", dir: "auto" }, t.team),
        ...span.map((x) => { const c = t.hours[x.hour] || {};
          return h("td", { class: `num sx-hcell ${c.received || c.done ? "" : "sx-hcell-empty"}`, title: c.received ? `${hh(x.hour)}: ${c.done} finished, ${c.received} arrived, ${c.same_hour} the same hour` : "" },
            c.received || c.done ? `${fmtNum(c.done)}/${fmtNum(c.received)}` : "·", c.received ? h("small", { class: `sx-hpct sx-hpct-${pctTone(c.done_pct)}` }, ` ${Math.round(c.done_pct)}%`) : null); }),
        h("td", { class: "num" }, `${fmtNum(t.done)}/${fmtNum(t.received)}`, t.received ? h("small", { class: `sx-hpct sx-hpct-${pctTone(100 * t.done / t.received)}` }, ` ${Math.round(100 * t.done / t.received)}%`) : null)))))) : null;
    return OPS.card("Hour by hour — arrived vs finished", { hint }, h("div", { class: "sx-stack" },
      h("div", { class: "sx-daily-wrap" }, svg, axis, legend), table,
      teamTable ? h("h4", { class: "sx-sub" }, "Each team — finished / arrived in the hour") : null, teamTable));
  }

  function slaRead(d) {
    const o = d.overall || {}, m = d.meta || {}, b = d.bottleneck, oc = d.outcomes || {};
    const bits = [];
    if (o.measured) bits.push(`${OPS.fmtPct(o.sla_pct)} of the confirmed requests met the ${fmtMin(m.sla_minutes)} SLA (${fmtNum(o.breached || 0)} breached, 90 % within ${fmtMin(o.p90_minutes)}).`);
    if (b) bits.push(`Most of a request's time is spent in “${b.label}” (${b.share_pct}%).`);
    if (oc.returned) bits.push(`${oc.returned_pct}% were sent back to the branch at least once.`);
    if (oc.rejected) bits.push(`${fmtNum(oc.rejected)} rejected.`);
    return bits.length ? h("p", { class: "sx-read", dir: "auto" }, bits.join(" ")) : null;
  }

  // ---- the hour-of-day chart: when requests are assigned, executed and confirmed ----
  const HOUR_SERIES = [["assigned", "Assigned", "sx-hb-a"], ["executed", "Executed", "sx-hb-e"], ["confirmed", "Confirmed", "sx-hb-c"]];
  function hourChart(hours) {
    hours = hours || [];
    const max = Math.max(1, ...hours.map((x) => Math.max(x.assigned || 0, x.executed || 0, x.confirmed || 0)));
    if (!hours.some((x) => x.assigned || x.executed || x.confirmed)) return h("p", { class: "hint" }, "No time stamps in this period yet.");
    const svg = svgEl("svg", { class: "sx-hours", viewBox: "0 0 96 100", preserveAspectRatio: "none", role: "img",
      "aria-label": "Assigned, executed and confirmed requests by hour of the day" });
    hours.forEach((x, i) => HOUR_SERIES.forEach(([key, label, cls], j) => {
      const bh = Math.max(x[key] ? 1.5 : 0, ((x[key] || 0) / max) * 100);
      const rect = svgEl("rect", { x: i * 4 + j * 1.2 + 0.2, y: 100 - bh, width: 1.1, height: bh, class: cls });
      const t = svgEl("title", {});
      t.textContent = `${String(x.hour).padStart(2, "0")}:00 – ${label}: ${fmtNum(x[key] || 0)}`;
      rect.append(t);
      svg.append(rect);
    }));
    const peak = (key) => hours.reduce((a, b) => ((b[key] || 0) > (a[key] || 0) ? b : a), hours[0]);
    const pa = peak("assigned"), pe = peak("executed");
    return h("div", { class: "sx-daily-wrap" }, svg,
      h("div", { class: "sx-hours-axis hint", "aria-hidden": "true" }, ...[0, 6, 12, 18, 23].map((n) => h("span", {}, `${String(n).padStart(2, "0")}:00`))),
      h("div", { class: "sx-daily-legend hint" }, ...HOUR_SERIES.flatMap(([, label, cls]) => [h("i", { class: `sx-key ${cls}` }), label]),
        h("span", { class: "grow" }),
        pa.assigned ? `Peak assignment ${String(pa.hour).padStart(2, "0")}:00 (${fmtNum(pa.assigned)})` : "",
        pe.executed ? ` · peak execution ${String(pe.hour).padStart(2, "0")}:00 (${fmtNum(pe.executed)})` : ""));
  }

  const fmtStamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "—");

  function holderTable(rows) {
    rows = rows || [];
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Open requests by security user",
      sort: { key: "breached", desc: true },
      columns: [
        { key: "holder", label: "Security user", sort: true, render: (r) => h("span", { dir: "auto" }, String(r.holder)), text: (r) => String(r.holder) },
        { key: "n", label: "Open now", num: true, sort: true, render: (r) => fmtNum(r.n) },
        { key: "breached", label: "Past SLA", num: true, sort: true, render: (r) => (r.breached ? OPS.chip(fmtNum(r.breached), "danger") : fmtNum(0)) },
        { key: "on_hold", label: "On hold", num: true, sort: true, render: (r) => fmtNum(r.on_hold || 0) },
        { key: "oldest_minutes", label: "Oldest", num: true, sort: true, render: (r) => fmtMin(r.oldest_minutes) },
      ], rows,
    }).el;
  }

  function openTable(rows, sla) {
    rows = rows || [];
    const sentAgo = (r) => {                               // the Live tickets tab's own clock, for a request not read yet
      const t = Date.parse(String(r.sent_at || "").slice(0, 19)), now = Date.parse(String((state.analysis || {}).server_time || "").slice(0, 19));
      return Number.isNaN(t) || Number.isNaN(now) ? null : Math.max(0, Math.round((now - t) / 60000));
    };
    const notRead = (r) => {
      const ago = r.sent_at ? sentAgo(r) : null;
      return h("span", { class: "hint", title: "Its status history is read at the next scan; until then the Live tickets tab counts it from «تاريخ الارسال للسرية»" },
        ago == null ? "not read yet" : `not read yet · sent ${fmtMin(ago)} ago`);
    };
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Open requests, oldest first",
      sort: { key: "minutes", desc: true },
      columns: [
        { key: "id", label: "Request", sort: true, render: (r) => String(r.id || "—"), text: (r) => String(r.id || "") },
        { key: "holder", label: "Security user", sort: true, render: (r) => h("span", { dir: "auto" }, r.holder || "—"), text: (r) => r.holder || "" },
        { key: "system", label: "System", sort: true, render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
        { key: "requester", label: "Requester", sort: true, render: (r) => h("span", { dir: "auto" }, r.requester || "—"), text: (r) => r.requester || "" },
        { key: "first_assigned_at", label: "First assigned", sort: true, render: (r) => fmtStamp(r.first_assigned_at), text: (r) => r.first_assigned_at || "" },
        { key: "minutes", label: "Request age", num: true, sort: true,
          render: (r) => (r.minutes == null ? notRead(r) : r.minutes > sla ? OPS.chip(fmtMin(r.minutes), "danger") : fmtMin(r.minutes)) },
        { key: "holder_minutes", label: "With this user", num: true, sort: true,
          render: (r) => (r.holder_minutes == null ? "—" : fmtMin(r.holder_minutes)) },
        { key: "on_hold", label: "", sort: false, render: (r) => (r.on_hold ? OPS.chip("on hold", "warn") : r.source === "live" ? OPS.chip("Live tickets", "info") : "") },
      ], rows,
    }).el;
  }

  function liveStrip(d) {
    const N = d.now || {}, sla = (d.meta || {}).sla_minutes || 180;
    if (!N.open && !N.awaiting) {
      return OPS.card("Right now", { hint: "live from the portal's status history" },
        h("p", { class: "hint" }, "No request is open with a security user and none is waiting for the reviewer."));
    }
    const kpis = h("div", { class: "sx-kpis" },
      OPS.kpi({ key: "an-open", label: "Open now", value: fmtNum(N.open || 0), sub: N.unread ? `every open request · ${fmtNum(N.unread)} read at the next scan` : "every open request with a security user (مستخدم السرية)", icon: "clock", hero: true }),
      OPS.kpi({ key: "an-await-now", label: "Awaiting the reviewer", value: fmtNum(N.awaiting || 0), sub: "executed · not confirmed yet (مراجع السرية)", tone: N.awaiting ? "warn" : null, icon: "alert" }),
      OPS.kpi({ key: "an-past", label: `Past ${fmtMin(sla)}`, value: fmtNum(N.breached || 0), sub: "open longer than the SLA target", tone: N.breached ? "danger" : null, icon: "shield" }),
      OPS.kpi({ key: "an-hold", label: "On hold", value: fmtNum(N.on_hold || 0), sub: "«معلق» right now", icon: "clock" }),
      OPS.kpi({ key: "an-oldest", label: "Oldest open", value: fmtMin(N.oldest_minutes), sub: "since its first «اسناد لمستخدم السرية»", tone: (N.oldest_minutes != null && N.oldest_minutes > sla) ? "danger" : null, icon: "clock" }),
      OPS.kpi({ key: "an-pre", label: "With the branch", value: fmtNum(N.pre || 0), sub: "registered · not assigned yet", icon: "user" }));
    const parts = [kpis];
    if (N.open) {
      parts.push(OPS.card("By security user (open)", { hint: "مستخدم السرية · who holds the open requests now" }, holderTable(N.by_holder)),
        OPS.card("Open requests", { hint: N.live_only ? `oldest first · ${fmtNum(N.live_only)} only on the Live tickets tab so far` : "oldest first · age since the first «اسناد لمستخدم السرية»" },
          openTable(d.open_now, sla)));
    }
    if ((d.awaiting_now || []).length) {
      parts.push(OPS.card("Awaiting the reviewer", { hint: "مراجع السرية · executed, waiting for «تم التعزيز»" }, OPS.table({
        search: d.awaiting_now.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Executed requests waiting for confirmation",
        sort: { key: "minutes", desc: true },
        columns: [
          { key: "id", label: "Request", sort: true, render: (r) => String(r.id || "—"), text: (r) => String(r.id || "") },
          { key: "executor", label: "Executed by", sort: true, render: (r) => h("span", { dir: "auto" }, r.executor || "—"), text: (r) => r.executor || "" },
          { key: "system", label: "System", sort: true, render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
          { key: "executed_at", label: "Executed", sort: true, render: (r) => fmtStamp(r.executed_at), text: (r) => r.executed_at || "" },
          { key: "outcome", label: "Outcome", sort: true, render: (r) => (r.outcome === "not_executed" ? OPS.chip("not executed", "warn") : r.outcome ? OPS.chip("executed", "ok") : "—"), text: (r) => r.outcome || "" },
          { key: "employee_minutes", label: "Security user", num: true, sort: true, render: (r) => (r.employee_minutes == null ? "—" : fmtMin(r.employee_minutes)) },
          { key: "minutes", label: "Waiting for the reviewer", num: true, sort: true, render: (r) => (r.minutes == null ? h("span", { class: "hint" }, "not read yet") : fmtMin(r.minutes)) },
          { key: "request_minutes", label: "Request age", num: true, sort: true, render: (r) => (r.request_minutes == null ? "—" : fmtMin(r.request_minutes)) },
        ], rows: d.awaiting_now,
      }).el));
    }
    return OPS.card("Right now", { hint: "live from the portal's status history · not limited by the dates below" }, h("div", { class: "sx-stack" }, ...parts));
  }

  // where the data comes from, and whether the last read worked
  function recorderLine(d) {
    const R = d.recorder || {}, st = R.stats || {}, err = R.error || {};
    if (R.enabled === false) {
      return h("div", { class: "sx-banner warn", role: "note" }, glyph("alert"), h("span", { class: "sx-banner-text" },
        "Reading the status history is switched off (System › Settings › Ticketing › SLA history). The analysis below stops at the last read."));
    }
    if (err.message && (!R.last_read || String(err.at || "") > String(R.last_read))) {
      return h("div", { class: "sx-banner warn", role: "alert" }, glyph("alert"), h("span", { class: "sx-banner-text", dir: "auto" },
        `The last scan could not read the status history (${fmtStamp(err.at)}): ${err.message} The live tickets are not affected; the analysis shows the last good read.`));
    }
    const bits = [`Read live from «الاستفسار عن الطلبات» and each request's «الحالة التاريخية للطلب» at every scan`];
    if (R.last_read) bits.push(`last read ${fmtStamp(R.last_read)}`);
    if (st.rows != null) bits.push(`${fmtNum(st.rows)} request(s) on the page, ${fmtNum(st.opened || 0)} history(ies) read`);
    if (st.left) bits.push(`${fmtNum(st.left)} more next scan`);
    const out = [...((d.left_out || {}).teams || []), ...((d.left_out || {}).applications || [])];
    if (out.length) bits.push(`left out of the SLA: ${out.join(", ")}`);
    return h("p", { class: "hint sx-analysis-note", dir: "auto" }, bits.join(" · ") + ".");
  }

  function analysisHead(d) {
    const m = d.meta || {}, slaH = Math.round((m.sla_minutes || 180) / 60 * 10) / 10;
    return h("div", { class: "sx-stack" }, recorderLine(d),
      h("p", { class: "hint sx-analysis-note", dir: "auto" }, `${fmtNum(m.requests || 0)} request(s) in this view. Security user (مستخدم السرية): «اسناد» → «تم التنفيذ / لم يتم التنفيذ»; reviewer (مراجع السرية): → «تم التعزيز». SLA target ${slaH} h, first assignment → confirmation. Settings: System › Settings › Ticketing.`));
  }

  function unmappedNote(d) {
    if (!d.unmapped) return null;
    const n = fmtNum(d.unmapped), what = d.unmapped === 1 ? "request has" : "requests have";
    return h("div", { class: "sx-banner info", role: "note" }, glyph("info"), h("span", { class: "sx-banner-text" }, state.admin
      ? `${n} recorded ${what} no team: the application is not in Auto Assign › Applications and the security user is on no account's "security name". Only administrators see them.`
      : `${n} recorded ${what} no team yet, so ${d.unmapped === 1 ? "it is" : "they are"} not in your view. An administrator gives them a team in Auto Assign › Applications.`));
  }

  function assignerTable(rows) {
    rows = rows || [];
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Requests by who assigned them",
      sort: { key: "n", desc: true },
      columns: [
        { key: "assigner", label: "Assigned by", sort: true, render: (r) => h("span", { dir: "auto" }, String(r.assigner)), text: (r) => String(r.assigner) },
        { key: "n", label: "Requests", num: true, sort: true, render: (r) => fmtNum(r.n) },
      ], rows,
    }).el;
  }

  // ---- compared with the portal's own «الاستفسار عن الطلبات» results of the period (Third Party never shown) ----
  const PORTAL_FACTS = [
    ["rows", "Inquiry rows", "every row the portal lists for these request dates (one per application of a request)"],
    ["with_security", "With the security team", ""],
    ["with_branch", "Still with the branch", "registered / confirmed by the branch supervisor: no SLA step yet"],
    ["rejected", "Rejected", ""],
    ["read", "Status history read", "its «الحالة التاريخية للطلب» is recorded"],
    ["not_read", "Not read yet", "read at the next scans"],
  ];
  function portalCard(d) {
    const p = d.portal;
    if (!p) return null;
    const f = state.slaFilter || {};
    const period = f.date_from || f.date_to ? `request dates ${f.date_from || "…"} – ${f.date_to || "…"}` : "every request date of the latest read";
    const sub = { with_security: `${fmtNum(p.closed || 0)} of them confirmed by the reviewer` };
    const facts = PORTAL_FACTS.flatMap(([key, label, note]) => [h("dt", {}, label),
      h("dd", { "data-portal": key }, h("strong", {}, fmtNum(p[key] || 0)), (sub[key] || note) ? h("span", { class: "hint" }, ` · ${sub[key] || note}`) : null)]);
    return OPS.card("Compared with the portal", { hint: `«الاستفسار عن الطلبات» · ${period}${d.scope ? ` · ${d.scope}` : ""}${p.last_read ? ` · read ${fmtStamp(p.last_read)}` : ""}` },
      h("dl", { class: "op-defs sx-portal" }, ...facts),
      h("p", { class: "hint" }, "Search the same dates on the portal: its number of results should be «Inquiry rows». Third Party is left out here, as everywhere in the SLA."));
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
    const stack = [analysisHead(d), liveStrip(d), unmappedNote(d), filterBar(d), portalCard(d)];
    if (!d.meta || !d.meta.requests) {
      stack.push(OPS.card("This period", {}, OPS.emptyState({ title: "No request matches this filter", icon: "shield",
        text: "“Right now” above is always current. Widen the dates, pick another employee / checker / team, or press Clear." })));
      panel.replaceChildren(h("div", { class: "sx-stack" }, ...stack.filter(Boolean)));
      return;
    }
    const o = d.overall, m = d.meta, b = d.buckets || {};
    const timed = o.median_minutes != null;                // at least one closed request has a clock
    const slaTone = !timed ? null : o.sla_pct >= 80 ? "ok" : o.sla_pct >= 50 ? "warn" : "danger";
    const slaH = Math.round(m.sla_minutes / 60 * 10) / 10;
    const kpis = h("div", { class: "sx-kpis" },
      OPS.kpi({ key: "an-closed", label: "Closed", value: fmtNum(b.closed || 0), sub: m.unread ? `confirmed · ${fmtNum(m.unread)} request(s) of this period not read yet: counted, times follow` : "confirmed by the reviewer in this period", icon: "check", hero: true }),
      OPS.kpi({ key: "an-within", label: `Within ${slaH} h SLA`, value: timed ? OPS.fmtPct(o.sla_pct) : "—",
        sub: timed ? `median ${fmtMin(o.median_minutes)} · assignment → confirmation` : "no closed request yet", tone: slaTone, icon: "shield" }),
      OPS.kpi({ key: "an-exec", label: "Executed", value: fmtNum(o.executed || 0), sub: b.closed ? `${OPS.fmtPct(o.executed_pct)} of the closed` : "«تم التعزيز على التنفيذ»", tone: "ok", icon: "check" }),
      OPS.kpi({ key: "an-notexec", label: "Not executed", value: fmtNum(o.not_executed || 0), sub: "«تم التعزيز على عدم التنفيذ»", tone: o.not_executed ? "warn" : null, icon: "alert" }),
      OPS.kpi({ key: "an-held", label: "Put on hold", value: fmtNum((d.hold || {}).requests || 0), sub: (d.hold || {}).requests ? `${fmtMin((d.hold || {}).minutes)} on hold in all` : "«معلق»", icon: "clock" }),
      OPS.kpi({ key: "an-awaiting", label: "Executed, not confirmed", value: fmtNum(b.awaiting || 0), sub: "in this period, waiting for the reviewer", tone: b.awaiting ? "warn" : null, icon: "alert" }));
    const ec = d.employee_clock || {}, mc = d.manager_clock || {}, bw = d.branch_wait || {};
    const clocks = h("div", { class: "sx-kpis" },
      OPS.kpi({ label: "Security user clock", value: fmtMin(ec.median_minutes), sub: `median · assignment → execution${ec.n ? ` · ${fmtNum(ec.n)} req.` : ""}`, icon: "user" }),
      OPS.kpi({ label: "Reviewer clock", value: fmtMin(mc.median_minutes), sub: `median · execution → confirmation${mc.n ? ` · ${fmtNum(mc.n)} req.` : ""}`, icon: "shield" }),
      OPS.kpi({ label: "Full clock", value: fmtMin(o.median_minutes), sub: "median · first assignment → confirmation", icon: "clock" }),
      OPS.kpi({ label: "Average full clock", value: fmtMin(o.average_minutes), sub: "mean of the closed requests", icon: "clock" }),
      OPS.kpi({ label: "Before the security team", value: fmtMin(bw.median_minutes), sub: "median · branch confirmation → assignment", icon: "activity" }));
    stack.push(slaRead(d), kpis, clocks, hourlyCard(d));
    stack.push(OPS.card("Stages of a request", { hint: "each stage against its own target · green on target, amber up to 1.5×, red beyond" }, stageTable(d.stages)));
    stack.push(h("div", { class: "sx-two" },
      OPS.card("Where the time goes", { hint: "average of each hand-off" }, waterfall(d)),
      OPS.card("Needs attention", { hint: d.attention_total > (d.attention || []).length ? `${fmtNum(d.attention_total)} in all · worst first` : "worst first · click for the timeline" }, attentionTable(d))));
    stack.push(outcomesCard(d));
    if (b.closed) {
      stack.push(OPS.card("Outcome", { hint: "confirmed executed vs confirmed not executed" },
        OPS.stackbar([{ label: "Executed", value: o.executed, cls: "op-t-ok" }, { label: "Not executed", value: o.not_executed, cls: "op-t-warn" }])));
    }
    stack.push(h("div", { class: "sx-two" },
      OPS.card("Confirmed requests per day", { hint: "Sundays highlighted" }, dailyChart(d.by_day)),
      OPS.card("By hour of the day", { hint: "when requests are assigned, executed and confirmed" }, hourChart(d.by_hour))));
    stack.push(h("div", { class: "sx-two" },
      OPS.card("By security user (executor)", { hint: "مستخدم السرية · assignment → execution" }, rollupTable(d.by_employee, "executor", "Security user", "SLA by security user")),
      OPS.card("By reviewer (checker)", { hint: "مراجع السرية · execution → confirmation" }, rollupTable(d.by_checker, "checker", "Reviewer", "SLA by reviewer"))));
    stack.push(h("div", { class: "sx-two" },
      OPS.card("By team", { hint: "full clock" }, rollupTable(d.by_team, "team", "Team", "SLA by team")),
      OPS.card("By assigner", { hint: "موظف الاسناد · who assigned the requests" }, assignerTable(d.by_assigner))));
    stack.push(h("div", { class: "sx-two" },
      OPS.card("By day", { hint: "newest first" }, rollupTable(d.by_day, "date", "Date", "SLA by day", { key: "date", desc: true })),
      OPS.card("By system", { hint: "top 25" }, rollupTable(d.by_system, "system", "System", "SLA by system"))));
    stack.push(h("div", { class: "sx-two" },
      OPS.card("By branch", { hint: "الفرع / الادارة · top 25" }, rollupTable(d.by_branch, "branch", "Branch", "SLA by branch")),
      OPS.card("By request type", { hint: "نوع الطلب · top 25" }, rollupTable(d.by_request_type, "request_type", "Request type", "SLA by request type"))));
    const more = d.requests_total > (d.requests_list || []).length;
    stack.push(OPS.card("Requests", { hint: more ? `newest ${fmtNum(d.requests_list.length)} of ${fmtNum(d.requests_total)} · the CSV / Excel export holds every one` : "click a request for its timeline" }, requestsTable(d)));
    panel.replaceChildren(h("div", { class: "sx-stack" }, ...stack.filter(Boolean)));
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
