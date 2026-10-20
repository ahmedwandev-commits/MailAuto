"use strict";
/* Team calls (/calls, for a manager): the calls logged by the employees of the manager's own team(s), for one day.
   Reads GET /api/ops/team-calls?date=<day> every 30 s through OPS.loader. An administrator opens the full Call Log
   dashboard (/admin) instead - this page is the manager's team-scoped view. Strict CSP: textContent only (CL.h),
   Arabic carries dir="auto", no innerHTML, no inline style. */
(function () {
  const { h, $, api, toast, bidi, fmtNum } = CL;
  const page = $("#calls-page");
  const STATUS = [["resolved", "Resolved", "resolved"], ["transferred", "Transferred", "transferred"], ["ticket_inquiry", "Ticket inquiry", "ticket_inquiry"]];
  const state = { date: "", today: "", data: null, serverTime: "" };
  const el = {};
  const tables = {};

  // "today" is the SERVER's day (the PC clock may be ahead: asking the server for a future day fails); the first answer sets it
  const todayIso = () => state.today || OPS.localNowIso().slice(0, 10);
  function shiftDate(iso, days) {
    const [y, m, d] = iso.split("-").map(Number);
    const dt = new Date(y, m - 1, d + days, 12);
    const p = (n) => String(n).padStart(2, "0");
    return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
  }
  const prettyDate = (iso) => (iso ? new Date(`${iso}T12:00:00`).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "");

  // ------------------------------------------------------------------ summary + chart
  function kpis(t) {
    el.kpis.replaceChildren(
      OPS.kpi({ label: "Calls on this day", value: fmtNum(t.total), sub: `${fmtNum(t.active_employees)} of ${fmtNum(t.employees)} took calls`, icon: "phone", hero: true }),
      OPS.kpi({ label: "Resolved", value: fmtNum(t.resolved || 0), tone: "ok", icon: "check" }),
      OPS.kpi({ label: "Transferred", value: fmtNum(t.transferred || 0), icon: "arrow" }),
      OPS.kpi({ label: "Ticket inquiry", value: fmtNum(t.ticket_inquiry || 0), icon: "activity" }));
  }
  function hourChart(perHour) {
    const active = (perHour || []).filter((b) => b.total > 0);
    if (!active.length) return h("p", { class: "hint" }, "No calls on this day yet.");
    const lo = Math.max(0, active[0].key - 1), hi = active[active.length - 1].key;
    const series = (perHour || []).filter((b) => b.key >= lo && b.key <= hi).map((b) => b.total);
    return h("div", { class: "op-viz" }, OPS.sparkline(series, { width: 280, height: 48, label: "Calls per hour" }),
      h("span", { class: "hint" }, `per hour, ${String(lo).padStart(2, "0")}:00–${String(hi).padStart(2, "0")}:00`));
  }

  function employeesTable(rows) {
    return OPS.table({
      search: true, searchPlaceholder: "Search an employee…", copy: true, caption: "Calls per employee",
      pageSize: 10, pageSizes: [10, 25, 50], sort: { key: "total", desc: true },
      columns: [
        { key: "name", label: "Employee", sort: true, render: (r) => bidi(r.name), text: (r) => r.name },
        { key: "department", label: "Department", render: (r) => bidi(r.department || "—"), text: (r) => r.department },
        { key: "teams", label: "Team(s)", render: (r) => bidi((r.teams || []).join(", ") || "—"), text: (r) => (r.teams || []).join(", ") },
        { key: "total", label: "Total", num: true, sort: true, render: (r) => h("strong", {}, fmtNum(r.total)) },
        { key: "resolved", label: "Resolved", num: true, sort: true },
        { key: "transferred", label: "Transferred", num: true, sort: true },
        { key: "ticket_inquiry", label: "Ticket", num: true, sort: true },
      ],
      rows,
      empty: { title: "No employees in your team yet", icon: "user", text: "Ask your administrator to add employees to your team." },
    });
  }

  function recordsTable(rows) {
    return OPS.table({
      search: true, searchPlaceholder: "Search caller, reason, location, note…", copy: true, caption: "Team call log",
      pageSize: 10, pageSizes: [10, 25, 50], sort: { key: "time", desc: true },
      columns: [
        { key: "time", label: "Time", sort: true, render: (r) => String(r.time || "").slice(0, 5), text: (r) => r.time },
        { key: "name", label: "Employee", render: (r) => bidi(r.name), text: (r) => r.name },
        { key: "ip_phone", label: "IP phone", render: (r) => h("div", {}, h("div", {}, r.ip_phone || "—"), r.ip_unit ? h("div", { class: "hint", dir: "auto" }, bidi(r.ip_unit)) : null), text: (r) => `${r.ip_phone || ""} ${r.ip_unit || ""}` },
        { key: "reason", label: "Reason", render: (r) => h("div", {}, h("span", { class: "status-chip" }, h("i", { class: `dot s-${r.status}` }), bidi(r.reason_ar || r.reason_en || r.reason)), r.reason_en && r.reason_ar ? h("div", { class: "hint" }, r.reason_en) : null), text: (r) => `${r.reason_ar || ""} ${r.reason_en || ""}` },
        { key: "branch_name", label: "Branch", render: (r) => (r.branch_name ? bidi(`${r.branch_name}${r.region ? " · " + r.region : ""}`) : h("span", { class: "hint" }, "—")), text: (r) => r.branch_name },
        { key: "note", label: "Note", render: (r) => (r.note ? bidi(r.note) : h("span", { class: "hint" }, "—")), text: (r) => r.note },
      ],
      rows,
      empty: (filtered) => ({ title: filtered ? "No call matches" : "No calls on this day", icon: "phone",
        text: filtered ? "Try clearing the search." : "Your team's calls for this day will show here." }),
    });
  }

  // ------------------------------------------------------------------ render
  function render(d) {
    state.data = d;
    state.serverTime = d.server_time || "";
    if (state.serverTime) state.today = state.serverTime.slice(0, 10);
    if (!state.date) state.date = d.date;                  // the first answer: the server's today
    el.dateInput.max = todayIso();
    el.dateInput.value = state.date;
    el.next.disabled = state.date >= todayIso();
    const teams = d.scope === "all" ? "all teams" : (d.scope_teams || []).join(", ");
    el.sub.textContent = `${prettyDate(state.date)} · ${teams}`;
    kpis(d.totals);
    if (!tables.employees) {
      tables.employees = employeesTable(d.employees);
      tables.records = recordsTable(d.records);
      el.chartBody = h("div", {}, hourChart(d.per_hour));
      el.body.replaceChildren(
        el.kpis,
        OPS.card("When the calls came in", {}, el.chartBody),
        h("div", { class: "op-calls-two" },
          OPS.card("Calls per employee", {}, tables.employees.el),
          OPS.card("Team call log", { hint: "every call your team logged this day" }, tables.records.el)));
    } else {
      tables.employees.setRows(d.employees);
      tables.records.setRows(d.records);
      el.chartBody.replaceChildren(hourChart(d.per_hour));
    }
  }

  function showError(e) {
    if (!state.data) toast(e.network ? "Cannot reach the server - retrying." : e.message, "error");
  }

  const loader = OPS.loader({ url: () => `/api/ops/team-calls${state.date ? `?date=${encodeURIComponent(state.date)}` : ""}`,
    interval: 30000, since: false, onData: render, onError: showError });

  function setDate(iso) {
    const max = todayIso();
    state.date = iso > max ? max : iso;
    el.dateInput.value = state.date;
    el.next.disabled = state.date >= max;
    tables.employees = null;                               // the day changed: rebuild the tables (fresh page 1)
    tables.records = null;
    el.body.replaceChildren(h("p", { class: "empty" }, "Loading…"));
    loader.refresh();
  }

  function build() {
    el.kpis = h("div", { class: "op-kpis" });
    el.sub = h("p", { class: "hint" });
    el.body = h("div", { class: "my-stack" }, h("p", { class: "empty" }, "Loading your team's calls…"));
    el.dateInput = h("input", { type: "date", "aria-label": "Day", max: todayIso(), value: state.date,
      onchange: () => setDate(el.dateInput.value || todayIso()) });
    el.prev = h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Previous day", onclick: () => setDate(shiftDate(state.date, -1)) }, "‹");
    el.next = h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-label": "Next day", onclick: () => setDate(shiftDate(state.date, 1)) }, "›");
    el.today = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => setDate(todayIso()) }, "Today");
    page.replaceChildren(
      h("div", { class: "op-titlebar" }, h("div", {}, h("h1", {}, "Team calls"), el.sub),
        h("span", { class: "grow" }),
        h("div", { class: "op-calls-filters" }, el.prev, el.dateInput, el.next, el.today)),
      el.body);
  }

  async function start() {
    build();                                               // state.date stays empty: the server answers for its own today
    loader.start();
  }
  start();
})();
