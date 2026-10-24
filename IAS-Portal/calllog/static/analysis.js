"use strict";
/* Analysis page (/analysis): calls, e-mails, tickets and change requests over a period, and the SLA of the security
   requests (the "SLA" tab: static/sla-analysis.js, moved here from Ticketing), for administrators and managers
   (a manager sees only their own team(s)). GET /api/ops/analytics?from&to&team gives, per module: the daily trend, the
   hour-of-day profile, the weekday x hour heat map, the peaks, the split by team and the change against the previous
   period of the same length.

   Built with CL.h / CL.svgEl only (strict CSP: no innerHTML, no inline style); the filters live in the address bar. */
(function () {
  const { h, $, api, toast, svgEl, fmtNum } = CL;
  const MODULES = ["calls", "email", "tickets", "crs"];
  const TONES = { calls: "ax-c0", email: "ax-c1", tickets: "ax-c2", crs: "ax-c3" };
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const PRESETS = [["today", "Today"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"], ["month", "This month"],
    ["last_month", "Last month"], ["365", "12 months"]];
  const page = $("#analysis-page");
  const state = { data: null, tab: "overview", from: "", to: "", team: "", preset: "30", seq: 0 };

  // ------------------------------------------------------------------ dates and the address bar
  const two = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  const parse = (s) => { const [y, m, d] = String(s).split("-").map(Number); return new Date(y, m - 1, d, 12); };
  const fmtDay = (s) => (s ? parse(s).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "—");
  const hourText = (hr) => (hr == null ? "—" : `${two(hr)}:00–${two((hr + 1) % 24)}:00`);
  function presetRange(p, todayIso) {
    const t = parse(todayIso);
    if (p === "today") return [todayIso, todayIso];
    if (p === "month") return [iso(new Date(t.getFullYear(), t.getMonth(), 1, 12)), todayIso];
    if (p === "last_month") return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1, 12)), iso(new Date(t.getFullYear(), t.getMonth(), 0, 12))];
    const n = Number(p) || 30;
    const f = new Date(t);
    f.setDate(f.getDate() - (n - 1));
    return [iso(f), todayIso];
  }
  function readUrl() {
    const q = new URLSearchParams(location.search);
    state.from = q.get("from") || "";
    state.to = q.get("to") || "";
    state.team = q.get("team") || "";
    state.tab = MODULES.includes(q.get("tab")) || ["sla", "quality"].includes(q.get("tab")) ? q.get("tab") : "overview";
    state.preset = q.get("preset") || (state.from ? "" : "30");
  }
  function writeUrl() {
    const q = new URLSearchParams();
    if (state.preset) q.set("preset", state.preset); else { q.set("from", state.from); q.set("to", state.to); }
    if (state.team) q.set("team", state.team);
    if (state.tab !== "overview") q.set("tab", state.tab);
    history.replaceState(null, "", `${location.pathname}?${q.toString()}`);
  }

  // ------------------------------------------------------------------ small drawing kit (SVG geometry only)
  function niceMax(v) {
    if (v <= 5) return 5;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  /** Columns (one per item) with an optional line for a second series. items = [{label, value, title, value2}] */
  function columns(items, opts) {
    const o = opts || {}, W = 760, H = o.height || 190, padL = 38, padB = 22, padT = 10;
    const max = niceMax(Math.max(1, ...items.map((i) => Math.max(i.value || 0, i.value2 || 0))));
    const svg = svgEl("svg", { class: "ax-cols", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.label || "chart",
      preserveAspectRatio: "none" });
    const n = Math.max(1, items.length), slot = (W - padL) / n, bw = Math.max(1, slot * 0.72);
    const y = (v) => padT + (H - padT - padB) * (1 - v / max);
    for (const f of [0, 0.5, 1]) {
      svg.append(svgEl("line", { class: "ax-gridline", x1: padL, x2: W, y1: y(max * f), y2: y(max * f) }));
      const t = svgEl("text", { class: "ax-axis", x: padL - 6, y: y(max * f) + 4, "text-anchor": "end" });
      t.textContent = fmtNum(Math.round(max * f));
      svg.append(t);
    }
    const step = Math.max(1, Math.ceil(n / 12));
    items.forEach((it, i) => {
      const x = padL + i * slot + (slot - bw) / 2, v = it.value || 0;
      const r = svgEl("rect", { class: `ax-bar ${o.tone || "ax-c0"}${it.missing ? " ax-missing" : ""}${it.hot ? " ax-hot" : ""}`,
        x: x.toFixed(1), y: y(v).toFixed(1), width: bw.toFixed(1), height: Math.max(0, H - padB - y(v)).toFixed(1) });
      const title = svgEl("title", {});
      title.textContent = it.title || `${it.label}: ${fmtNum(v)}`;
      r.append(title);
      svg.append(r);
      if (i % step === 0) {
        const t = svgEl("text", { class: "ax-axis", x: (x + bw / 2).toFixed(1), y: H - 6, "text-anchor": "middle" });
        t.textContent = it.short || it.label;
        svg.append(t);
      }
    });
    if (items.some((i) => i.value2 != null)) {
      const pts = items.map((it, i) => `${(padL + i * slot + slot / 2).toFixed(1)},${y(it.value2 || 0).toFixed(1)}`);
      svg.append(svgEl("polyline", { class: "ax-line", points: pts.join(" "), fill: "none" }));
    }
    return h("div", { class: "ax-chart" }, svg);
  }

  function heatmap(cells, label) {
    const max = Math.max(1, ...cells.flat());
    const head = h("tr", {}, h("th", { scope: "col" }, ""), ...Array.from({ length: 24 }, (_, hr) => h("th", { scope: "col", class: "ax-hh" }, hr % 3 === 0 ? two(hr) : "")));
    const rows = cells.map((row, wd) => h("tr", {}, h("th", { scope: "row" }, WEEKDAYS[wd]),
      ...row.map((v, hr) => {
        const lvl = v ? 1 + Math.floor(4.999 * v / max) : 0;
        return h("td", { class: `ax-cell ax-h${lvl}`, title: `${WEEKDAYS[wd]} ${hourText(hr)}: ${fmtNum(v)}`,
          "aria-label": `${WEEKDAYS[wd]} ${hourText(hr)}: ${fmtNum(v)}` }, "");
      })));
    return h("div", { class: "table-wrap ax-heat-wrap" }, h("table", { class: "ax-heat" },
      h("caption", { class: "sr-only" }, label), h("thead", {}, head), h("tbody", {}, rows)),
      h("div", { class: "ax-legend hint" }, "less", ...[0, 1, 2, 3, 4, 5].map((l) => h("i", { class: `ax-key ax-h${l}` })), "more"));
  }

  const changeChip = (pct, invert) => (pct == null ? h("span", { class: "hint" }, "no earlier data")
    : OPS.chip(`${pct > 0 ? "▲" : pct < 0 ? "▼" : "→"} ${Math.abs(pct)}% vs the previous period`,
      pct === 0 ? "na" : (pct > 0) !== !!invert ? "warn" : "ok"));

  // ------------------------------------------------------------------ the filter bar
  function filterBar(d) {
    const presets = h("div", { class: "segmented ax-presets", role: "group", "aria-label": "Period" },
      PRESETS.map(([key, label]) => h("button", { type: "button", "aria-pressed": String(state.preset === key),
        onclick: () => { state.preset = key; [state.from, state.to] = presetRange(key, d.today); load(); } }, label)));
    const from = h("input", { type: "date", value: d.from, max: d.today, "aria-label": "From" });
    const to = h("input", { type: "date", value: d.to, max: d.today, "aria-label": "To" });
    const apply = () => { if (from.value && to.value) { state.preset = ""; state.from = from.value; state.to = to.value; load(); } };
    from.onchange = apply;
    to.onchange = apply;
    const teams = (d.options && d.options.teams) || [];
    const team = h("select", { "aria-label": "Team" }, h("option", { value: "" }, d.scope ? "All my teams" : "All teams"),
      teams.map((t) => h("option", { value: t, selected: t === state.team }, t)));
    team.onchange = () => { state.team = team.value; load(); };
    const fld = (lbl, ctrl) => h("label", { class: "ax-f" }, h("span", {}, lbl), ctrl);
    return h("div", { class: "ax-filter" }, presets, fld("From", from), fld("To", to), teams.length > 1 || !d.scope ? fld("Team", team) : null);
  }

  function tabs() {
    const names = { overview: "Overview", calls: "Calls", email: "E-mails", tickets: "Tickets", crs: "Change requests",
      sla: "SLA", quality: "E-mail quality" };
    return h("div", { class: "sx-tabs ax-tabs", role: "tablist" }, ["overview", ...MODULES, "sla", "quality"].map((k) => h("button", {
      type: "button", role: "tab", class: "sx-tab", id: `ax-tab-${k}`, "aria-selected": String(state.tab === k),
      onclick: () => { state.tab = k; writeUrl(); render(); } }, names[k])));
  }

  // ------------------------------------------------------------------ overview
  function moduleCard(key, m) {
    if (!m || !m.available) return OPS.card(m ? m.label : key, {}, h("p", { class: "hint" }, "Not available yet."));
    const spark = OPS.sparkline((m.daily || []).map((x) => x.n), { width: 220, height: 44, label: `${m.label} per day` });
    const facts = [
      ["Per day", fmtNum(m.avg_per_day)],
      ["Peak hour", m.peak_hour ? hourText(m.peak_hour.hour) : "—"],
      ["Busiest weekday", m.peak_weekday ? `${m.peak_weekday.weekday} (${fmtNum(m.peak_weekday.avg)}/day)` : "—"],
      ["Busiest day", m.busiest_day ? `${fmtDay(m.busiest_day.date)} (${fmtNum(m.busiest_day.n)})` : "—"],
    ];
    if (m.done) facts.push([m.done.label, fmtNum(m.done.total)]);
    return h("section", { class: `card ax-mod ${TONES[key]}` },
      h("div", { class: "card-head" }, h("h2", {}, m.label), h("span", { class: "grow" }),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.tab = key; writeUrl(); render(); } }, "Details")),
      h("div", { class: "ax-mod-top" }, h("div", {}, h("strong", { class: "ax-big" }, fmtNum(m.total)), h("div", { class: "hint" }, m.in_label)), spark),
      h("div", {}, changeChip(m.change_pct)),
      h("dl", { class: "op-defs ax-defs" }, facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
  }

  function overview(d) {
    const mods = d.modules || {};
    const cards = h("div", { class: "ax-grid" }, MODULES.map((k) => moduleCard(k, mods[k])));
    // the workload of every module together, by hour and weekday
    const heat = Array.from({ length: 7 }, () => Array(24).fill(0));
    const hours = Array(24).fill(0);
    for (const k of MODULES) {
      const m = mods[k];
      if (!m || !m.available) continue;
      (m.heatmap || []).forEach((row, wd) => row.forEach((v, hr) => { heat[wd][hr] += v; }));
      (m.hours || []).forEach((v, hr) => { hours[hr] += v; });
    }
    const peak = hours.some(Boolean) ? hours.indexOf(Math.max(...hours)) : null;
    const hourItems = hours.map((v, hr) => ({ label: two(hr), value: v, hot: hr === peak, title: `${hourText(hr)}: ${fmtNum(v)} items` }));
    const rows = MODULES.filter((k) => mods[k] && mods[k].available).map((k) => ({ key: k, ...mods[k] }));
    const table = OPS.table({
      search: false, copy: true, caption: "Peaks by module", sort: { key: "total", desc: true }, pageSize: 10,
      columns: [
        { key: "label", label: "Module", render: (r) => r.label, text: (r) => r.label },
        { key: "total", label: "Total", num: true, sort: true, render: (r) => fmtNum(r.total) },
        { key: "avg_per_day", label: "Per day", num: true, sort: true, render: (r) => fmtNum(r.avg_per_day) },
        { key: "change_pct", label: "Change", num: true, sort: true, render: (r) => (r.change_pct == null ? "—" : `${r.change_pct > 0 ? "+" : ""}${r.change_pct}%`) },
        { key: "peak_hour", label: "Peak hour", render: (r) => (r.peak_hour ? hourText(r.peak_hour.hour) : "—"), text: (r) => (r.peak_hour ? hourText(r.peak_hour.hour) : "") },
        { key: "peak_weekday", label: "Busiest weekday", render: (r) => (r.peak_weekday ? r.peak_weekday.weekday : "—"), text: (r) => (r.peak_weekday ? r.peak_weekday.weekday : "") },
        { key: "busiest_day", label: "Busiest day", render: (r) => (r.busiest_day ? `${fmtDay(r.busiest_day.date)} (${fmtNum(r.busiest_day.n)})` : "—"), text: (r) => (r.busiest_day ? r.busiest_day.date : "") },
      ], rows,
    }).el;
    return [cards, qualitySummary(d.quality),
      h("div", { class: "sx-two" },
        OPS.card("All work by hour of the day", { hint: peak != null ? `peak ${hourText(peak)}` : "" }, columns(hourItems, { label: "All modules by hour", tone: "ax-c4" })),
        OPS.card("All work by weekday and hour", { hint: "darker = busier" }, heatmap(heat, "All modules by weekday and hour"))),
      OPS.card("Peaks by module", {}, table)];
  }

  // ------------------------------------------------------------------ e-mail reply quality (calls are never rated)
  const pct = (v) => (v == null ? "—" : `${fmtNum(v)}%`);
  const roleText = (r) => (r === "admin" ? "Administrator" : r === "manager" ? "Manager" : r ? r : "—");

  function qualitySummary(q) {
    if (!q || !q.available) return null;
    const top = (q.reviewers || [])[0];
    return h("section", { class: "card ax-quality-sum" },
      h("div", { class: "card-head" }, h("h2", {}, "E-mail reply quality"), h("span", { class: "grow" }),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.tab = "quality"; writeUrl(); render(); } }, "Details")),
      h("dl", { class: "op-defs ax-defs" },
        h("dt", {}, "Replies rated"), h("dd", {}, fmtNum(q.rated)),
        h("dt", {}, "Of the e-mails received"), h("dd", {}, q.received ? `${pct(q.coverage_pct)} of ${fmtNum(q.received)}` : "—"),
        h("dt", {}, "Reviewers"), h("dd", {}, fmtNum((q.reviewers || []).length)),
        h("dt", {}, "Most ratings"), h("dd", { dir: "auto" }, top ? `${top.name} (${fmtNum(top.ratings)})` : "—"),
        h("dt", {}, "Average score"), h("dd", {}, q.score == null ? "—" : `${fmtNum(q.score)} / 100`)));
  }

  function qualityView(q, d) {
    if (!q || !q.available) return [OPS.emptyState({ title: "Not available", icon: "info", text: "The quality statistics could not be read." })];
    const kpis = h("div", { class: "sx-kpis" },
      OPS.kpi({ label: "Replies rated", value: fmtNum(q.rated), sub: q.withdrawn ? `${fmtNum(q.withdrawn)} withdrawn after an appeal` : "e-mail replies only", hero: true, icon: "check" }),
      OPS.kpi({ label: "Rated of the e-mails received", value: pct(q.coverage_pct), sub: `${fmtNum(q.received)} received`, icon: "mail" }),
      OPS.kpi({ label: "Excellent", value: fmtNum(q.excellent), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Very good", value: fmtNum(q.very_good || 0), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Good", value: fmtNum(q.good), icon: "check" }),
      OPS.kpi({ label: "Needs improvement", value: fmtNum(q.needs), tone: q.needs ? "warn" : null, icon: "alert" }),
      OPS.kpi({ label: "Average score", value: q.score == null ? "—" : fmtNum(q.score), sub: `${fmtNum(q.appeals_waiting)} appeal(s) waiting of ${fmtNum(q.appeals)}`, icon: "activity" }));
    const dayItems = (q.daily || []).map((x) => ({ label: x.date, short: x.date.slice(5), value: x.n, title: `${fmtDay(x.date)}: ${fmtNum(x.n)} rated` }));
    const reviewers = OPS.table({
      search: (q.reviewers || []).length > 10, copy: true, caption: "Who rated", sort: { key: "ratings", desc: true }, pageSize: 10,
      columns: [
        { key: "name", label: "Reviewer", render: (r) => h("span", { dir: "auto" }, r.name), text: (r) => r.name },
        { key: "role", label: "Role", render: (r) => roleText(r.role), text: (r) => roleText(r.role) },
        { key: "ratings", label: "Ratings", num: true, sort: true, render: (r) => fmtNum(r.ratings) },
        { key: "excellent", label: "Excellent", num: true, sort: true, render: (r) => fmtNum(r.excellent) },
        { key: "very_good", label: "Very good", num: true, sort: true, render: (r) => fmtNum(r.very_good || 0) },
        { key: "good", label: "Good", num: true, sort: true, render: (r) => fmtNum(r.good) },
        { key: "needs", label: "Needs impr.", num: true, sort: true, render: (r) => fmtNum(r.needs) },
        { key: "withdrawn", label: "Withdrawn", num: true, sort: true, render: (r) => fmtNum(r.withdrawn) },
        { key: "teams", label: "Teams rated", render: (r) => h("span", { dir: "auto" }, (r.teams || []).join(", ")), text: (r) => (r.teams || []).join(", ") },
        { key: "last_at", label: "Last rating", sort: (r) => r.last_at || "", render: (r) => (r.last_at ? OPS.fmtWhen(r.last_at, d.server_time) : "—"), text: (r) => r.last_at },
      ], rows: q.reviewers || [],
      empty: () => ({ title: "Nobody rated a reply in this period", icon: "info", text: "Managers rate replies from E-mail › Replied." }),
    }).el;
    const teamsTable = OPS.table({
      search: (q.teams || []).length > 10, copy: true, caption: "Rated by team", sort: { key: "rated", desc: true }, pageSize: 10,
      columns: [
        { key: "team", label: "Team", render: (r) => h("span", { dir: "auto" }, r.team), text: (r) => r.team },
        { key: "received", label: "E-mails received", num: true, sort: true, render: (r) => fmtNum(r.received) },
        { key: "rated", label: "Rated", num: true, sort: true, render: (r) => fmtNum(r.rated) },
        { key: "coverage_pct", label: "Rated %", num: true, sort: (r) => r.coverage_pct || 0, render: (r) => pct(r.coverage_pct) },
        { key: "reviewers", label: "Reviewers", num: true, sort: true, render: (r) => fmtNum(r.reviewers) },
        { key: "score", label: "Score", num: true, sort: (r) => r.score || 0, render: (r) => (r.score == null ? "—" : fmtNum(r.score)) },
        { key: "needs", label: "Needs impr.", num: true, sort: true, render: (r) => fmtNum(r.needs) },
      ], rows: q.teams || [],
    }).el;
    const people = q.people || [];
    const pick = h("select", { "aria-label": "Employee" }, h("option", { value: "" }, "Every employee"),
      people.map((p, i) => h("option", { value: String(i) }, `${p.name}${p.team ? ` · ${p.team}` : ""}`)));
    const peopleBox = h("div", {});
    const drawPeople = () => {
      const rows = pick.value === "" ? people : [people[Number(pick.value)]];
      peopleBox.replaceChildren(OPS.table({
        search: pick.value === "" && people.length > 10, copy: true, caption: "Rated replies by employee", sort: { key: "rated", desc: true }, pageSize: 10,
        columns: [
          { key: "name", label: "Employee", render: (r) => h("span", { dir: "auto" }, r.name), text: (r) => r.name },
          { key: "team", label: "Team", render: (r) => h("span", { dir: "auto" }, r.team), text: (r) => r.team },
          { key: "rated", label: "Rated", num: true, sort: true, render: (r) => fmtNum(r.rated) },
          { key: "excellent", label: "Excellent", num: true, sort: true, render: (r) => fmtNum(r.excellent) },
          { key: "very_good", label: "Very good", num: true, sort: true, render: (r) => fmtNum(r.very_good || 0) },
          { key: "good", label: "Good", num: true, sort: true, render: (r) => fmtNum(r.good) },
          { key: "needs", label: "Needs impr.", num: true, sort: true, render: (r) => fmtNum(r.needs) },
          { key: "score", label: "Score", num: true, sort: (r) => r.score || 0, render: (r) => (r.score == null ? "—" : fmtNum(r.score)) },
        ], rows,
        empty: () => ({ title: "No rated replies", icon: "info", text: "Nobody's reply was rated in this period." }),
      }).el);
    };
    pick.onchange = drawPeople;
    drawPeople();
    return [kpis,
      OPS.card("Ratings per day", { hint: "by the day the e-mail was received" }, columns(dayItems, { label: "Ratings per day", tone: "ax-c1" })),
      OPS.card("Who rated", { hint: "every manager / administrator who rated a reply in the period" }, reviewers),
      OPS.card("Rated of the e-mails each team received", { hint: "a reply counts for the team of the person who wrote it" }, teamsTable),
      OPS.card("By employee", { hint: "filter by employee" }, h("label", { class: "ax-f ax-f-inline" }, h("span", {}, "Employee"), pick), peopleBox),
      h("p", { class: "hint" }, "Only e-mail replies are rated - calls are never rated. Replies written by managers are not rated.")];
  }

  // ------------------------------------------------------------------ hour by hour: what arrived vs what was finished
  const pctTone = (p) => (p == null ? "" : p >= 100 ? "ok" : p >= 70 ? "warn" : "danger");
  function arrivedVsDone(m) {
    const arrived = m.hours || [], done = (m.done && m.done.hours) || [];
    const active = arrived.map((v, i) => (v || done[i] ? i : null)).filter((i) => i != null);
    if (!active.length) return null;
    const lo = Math.min(8, ...active), hi = Math.max(16, ...active);
    const span = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
    const cell = (a, dn) => {
      if (!a && !dn) return h("td", { class: "num sx-hcell-empty" }, "·");
      const p = a ? Math.round(100 * dn / a) : null;
      return h("td", { class: "num", title: `${fmtNum(dn)} ${m.done.label.toLowerCase()} · ${fmtNum(a)} ${m.in_label.toLowerCase()}` },
        `${fmtNum(dn)}/${fmtNum(a)}`, p != null ? h("small", { class: `sx-hpct sx-hpct-${pctTone(p)}` }, ` ${p}%`) : null);
    };
    const sum = (arr) => span.reduce((s, hr) => s + (arr[hr] || 0), 0);
    const row = (label, a, dn) => h("tr", {}, h("th", { scope: "row", dir: "auto" }, label), ...span.map((hr) => cell(a[hr] || 0, dn[hr] || 0)),
      cell(sum(a), sum(dn)));
    const rows = [row("All teams", arrived, done), ...(m.hours_by_team || []).map((t) => row(t.team, t.arrived || [], t.done || []))];
    return OPS.card("Hour by hour — arrived vs finished", { hint: `finished / arrived in each hour (${m.done.label.toLowerCase()} ÷ ${m.in_label.toLowerCase()}) · every team and each team` },
      h("div", { class: "table-wrap" }, h("table", { class: "data sx-hour-table" },
        h("caption", { class: "sr-only" }, `${m.label}: finished out of arrived per hour`),
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Team"), ...span.map((hr) => h("th", { scope: "col", class: "num" }, `${two(hr)}:00`)),
          h("th", { scope: "col", class: "num" }, "Total"))),
        h("tbody", {}, ...rows))));
  }

  // ------------------------------------------------------------------ one module
  function moduleView(key, m, d) {
    if (!m || !m.available) return [OPS.emptyState({ title: "Not available yet", icon: "info", text: "This module has no history for the chosen period yet." })];
    const tone = TONES[key];
    const kpis = h("div", { class: "sx-kpis" },
      OPS.kpi({ label: m.in_label, value: fmtNum(m.total), sub: `${fmtNum(m.previous.total)} in the previous period`, hero: true, icon: "activity" }),
      OPS.kpi({ label: "Per day", value: fmtNum(m.avg_per_day), sub: m.avg_change_pct == null ? "" : `${m.avg_change_pct > 0 ? "+" : ""}${m.avg_change_pct}% vs before`, icon: "clock" }),
      OPS.kpi({ label: "Peak hour", value: m.peak_hour ? `${two(m.peak_hour.hour)}:00` : "—", sub: m.peak_hour ? `${m.peak_hour.share}% of the hours known` : "no hours yet", icon: "clock" }),
      OPS.kpi({ label: "Busiest weekday", value: m.peak_weekday ? m.peak_weekday.weekday : "—", sub: m.peak_weekday ? `${fmtNum(m.peak_weekday.avg)} a day on average` : "", icon: "clock" }),
      OPS.kpi({ label: "Busiest day", value: m.busiest_day ? fmtNum(m.busiest_day.n) : "—", sub: m.busiest_day ? fmtDay(m.busiest_day.date) : "", icon: "alert" }),
      m.done ? OPS.kpi({ label: m.done.label, value: fmtNum(m.done.total), sub: `${fmtNum(m.done.previous_total)} in the previous period`, icon: "check" })
        : OPS.kpi({ label: "Busiest hour of a weekday", value: m.busiest_slot ? `${m.busiest_slot.weekday} ${two(m.busiest_slot.hour)}:00` : "—", sub: m.busiest_slot ? `${fmtNum(m.busiest_slot.n)} in all` : "", icon: "activity" }));
    const doneByDay = m.done ? Object.fromEntries(m.done.daily.map((x) => [x.date, x.n])) : null;
    const dayItems = (m.daily || []).map((x) => ({ label: x.date, short: x.date.slice(5), value: x.n, missing: x.missing,
      value2: doneByDay ? doneByDay[x.date] || 0 : undefined,
      title: `${fmtDay(x.date)}: ${fmtNum(x.n)} ${m.in_label.toLowerCase()}${doneByDay ? ` · ${fmtNum(doneByDay[x.date] || 0)} ${m.done.label.toLowerCase()}` : ""}${x.missing ? " (no data)" : ""}` }));
    const peakH = m.peak_hour ? m.peak_hour.hour : null;
    const hourItems = (m.hours || []).map((v, hr) => ({ label: two(hr), value: v, hot: hr === peakH,
      value2: m.done ? (m.done.hours || [])[hr] || 0 : undefined,
      title: `${hourText(hr)}: ${fmtNum(v)}${m.done ? ` · ${fmtNum((m.done.hours || [])[hr] || 0)} ${m.done.label.toLowerCase()}` : ""}` }));
    const weekdays = OPS.hbars(WEEKDAYS.map((w, i) => ({ label: w, value: (m.weekday_avg || [])[i] || 0, cls: tone })),
      { format: (v) => `${fmtNum(v)}/day` });
    const teamTable = OPS.table({
      search: (m.by_team || []).length > 10, copy: true, caption: `${m.label} by team`, sort: { key: "n", desc: true }, pageSize: 10,
      columns: [
        { key: "team", label: "Team", render: (r) => h("span", { dir: "auto" }, r.team), text: (r) => r.team },
        { key: "n", label: m.in_label, num: true, sort: true, render: (r) => fmtNum(r.n) },
        { key: "share", label: "Share", num: true, sort: (r) => r.n, render: (r) => `${m.total ? Math.round(100 * r.n / m.total) : 0}%` },
      ], rows: m.by_team || [],
    }).el;
    const hourCard = m.done ? arrivedVsDone(m) : null;
    const notes = [];
    if (m.approximate_days) notes.push(`${fmtNum(m.approximate_days)} day(s) come from the older daily history: their total is exact, the hour is not known.`);
    const missing = (m.daily || []).filter((x) => x.missing).length;
    if (missing) notes.push(`${fmtNum(missing)} day(s) of the period have no data yet (before the history started, or nothing was collected).`);
    return [kpis,
      OPS.card(`${m.label} per day`, { hint: m.done ? `bars: ${m.in_label.toLowerCase()} · line: ${m.done.label.toLowerCase()}` : `${fmtDay(d.from)} – ${fmtDay(d.to)}` },
        columns(dayItems, { label: `${m.label} per day`, tone })),
      h("div", { class: "sx-two" },
        OPS.card("By hour of the day", { hint: peakH != null ? `peak ${hourText(peakH)}` : "" }, columns(hourItems, { label: `${m.label} by hour`, tone })),
        OPS.card("By weekday", { hint: "average per day" }, weekdays)),
      hourCard,
      OPS.card("Weekday × hour", { hint: "when the work arrives - darker = busier" }, heatmap(m.heatmap || [], `${m.label} by weekday and hour`)),
      OPS.card("By team", {}, teamTable),
      notes.length ? h("p", { class: "hint" }, notes.join(" ")) : null];
  }

  // ------------------------------------------------------------------ SLA: the security requests' SLA (static/sla-analysis.js)
  let slaPanel = null;
  function slaView() {
    if (!slaPanel) {                                   // mounted once: its own filters and live refresh survive tab changes
      slaPanel = h("div", { class: "ax-sla", role: "tabpanel", "aria-labelledby": "ax-tab-sla" });
      if (window.SLA_ANALYSIS) window.SLA_ANALYSIS.mount(slaPanel, { admin: (page.dataset || {}).role === "admin" });
      else slaPanel.append(OPS.emptyState({ title: "The SLA analysis could not be loaded", icon: "alert", text: "Reload the page." }));
    }
    return [slaPanel];
  }

  // ------------------------------------------------------------------ page
  function render() {
    const d = state.data, sla = state.tab === "sla";
    if (!d && !sla) return;
    const hint = sla ? "SLA of the security requests · from the portal's own status history («الحالة التاريخية للطلب»)"
      : `${fmtDay(d.from)} – ${fmtDay(d.to)}${d.scope ? ` · ${d.scope}` : " · every team"} · compared with ${fmtDay(d.previous.from)} – ${fmtDay(d.previous.to)}`;
    const title = h("div", { class: "op-titlebar" }, h("div", {}, h("h1", {}, "Analysis"), h("p", { class: "hint", dir: "auto" }, hint)));
    const body = sla ? slaView() : state.tab === "overview" ? overview(d)
      : state.tab === "quality" ? qualityView(d.quality, d) : moduleView(state.tab, (d.modules || {})[state.tab], d);
    page.replaceChildren(...[title, sla ? null : filterBar(d), tabs(), ...[].concat(body)].filter(Boolean));
  }

  async function load() {
    writeUrl();
    const q = new URLSearchParams();
    if (state.from) q.set("from", state.from);
    if (state.to) q.set("to", state.to);
    if (state.team) q.set("team", state.team);
    const seq = ++state.seq;
    try {
      const d = await api("GET", `/api/ops/analytics?${q.toString()}`);
      if (seq !== state.seq) return;
      state.data = d;
      state.from = d.from;
      state.to = d.to;
      render();
    } catch (e) {
      if (seq !== state.seq) return;
      if (!state.data) page.replaceChildren(OPS.emptyState({ title: "The analysis cannot be shown", icon: "alert", text: e.message }));
      else toast(e.message, "error");
    }
  }

  readUrl();
  if (state.preset) {
    const today = iso(new Date());
    [state.from, state.to] = presetRange(state.preset, today);
  }
  if (state.tab === "sla") render();                   // the SLA tab does not wait for the other modules' numbers
  load();
  // live: a period that reaches today is brought up to date every minute (the server reuses one answer a minute)
  setInterval(() => { if (!document.hidden && state.data && state.data.to >= state.data.today) load(); }, 60 * 1000);
})();
