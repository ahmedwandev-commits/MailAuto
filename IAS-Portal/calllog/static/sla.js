"use strict";
/* SLA page (/sla): security requests waiting for the "security users" (docs/HUB-DESIGN.md sections 5.1 and 7).

   Reads GET /api/ops/sla (the last good scan + collector health) every 15 s with ?since=, so an unchanged scan costs
   almost nothing, and draws: KPI tiles, a per-security-user table, the request list (filters, sorting, paging) and
   today's timeline. Ages tick live between scans from "assigned_at" (else "sent_at") and the server clock. "Run scan now" asks the
   collector for a scan and waits (visibly, with a time limit) until a newer scan appears.

   Every scraped text goes in with textContent (CL.h) - never innerHTML - and Arabic text carries dir="auto". */
(function () {
  const { h, $, api, toast, bidi, svgEl, normAr, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";

  const POLL_MS = 15000;          // how often the page asks for news
  const WAIT_POLL_MS = 3000;      // ... while "Run scan now" is waiting
  const WAIT_LIMIT_MS = 4 * 60 * 1000;
  const TICK_MS = 10000;          // how often ages / "x min ago" are recomputed
  const PAGE_SIZES = [25, 50, 100];
  const VIEW_KEY = "ops.sla.view";

  const STATUSES = ["breached", "reminder", "within", "no_date"];
  const LABELS = { breached: "Breached", reminder: "Reminder", within: "Within SLA", no_date: "No sent date" };
  const RANK = { breached: 0, reminder: 1, within: 2, no_date: 3 };
  const STATUS_ICON = { breached: "alert", reminder: "clock", within: "check", no_date: "dash" };
  const PATHS = {
    refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5", alert: "M12 3 2 20h20L12 3zM12 10v4M12 17h.01",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", check: "M20 6 9 17l-5-5", dash: "M6 12h12",
    x: "M18 6 6 18M6 6l12 12", info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01", download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    wifi: "M2 9a15 15 0 0 1 20 0M5.5 12.5a10 10 0 0 1 13 0M9 16a5 5 0 0 1 6 0M12 20h.01",
  };

  const state = {
    snap: null,              // last full answer (envelope with data)
    meta: null,              // collector health that came with it
    generatedAt: "",
    history: null,           // today's {points, first_breach}
    today: "",
    failure: null,
    waiting: null,           // {since, generated, lastRun} while "Run scan now" waits
    filters: { status: "", user: "", q: "" },
    sort: { key: "priority", desc: false },
    userSort: { key: "breached", desc: true },
    page: 1,
    pageSize: PAGE_SIZES[0],
    viewAsTable: false,
    tab: "live",             // "live" (open tickets) | "analysis" (closed-request SLA analysis)
    analysis: null,          // the latest /api/ops/sla-analysis answer
    slaFilter: null,         // {date_from, date_to, executor, checker, team} - defaults to today on first open
    admin: false,
  };
  const el = {};
  let skew = 0;              // server clock minus this browser's clock, in ms

  // ------------------------------------------------------------------ small helpers
  function glyph(name) {
    const svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "sx-ic" });
    svg.append(svgEl("path", { d: PATHS[name] }));
    return svg;
  }

  /** Dates in the snapshots are naive local time of the PCs: read them as plain numbers, never through the browser's zone. */
  function naiveMs(iso) {
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?/.exec(String(iso || ""));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
  }
  function noteServerTime(iso) {
    const t = naiveMs(iso);
    if (t != null) skew = t - Date.now();
  }
  const serverNow = () => Date.now() + skew;
  const hhmm = (iso) => String(iso || "").slice(11, 16);
  const pad2 = (n) => String(n).padStart(2, "0");

  function fmtAge(min) {
    if (min == null) return "—";
    if (min < 60) return `${min} min`;
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return `${hrs} h ${pad2(min % 60)} min`;
    return `${Math.floor(hrs / 24)} d ${hrs % 24} h`;
  }
  const fmtLimit = (min) => (min && min % 60 === 0 ? `${min / 60} h` : `${min} min`);

  /** Minutes since the request reached the security user holding it, counted now: from its LAST assignment when the
      scan matched it to the SLA history ("assigned_at"), else from «تاريخ الارسال للسرية» (null with neither). */
  function ageOf(r) {
    const t = r._t === undefined ? (r._t = naiveMs(r.assigned_at || r.sent_at)) : r._t;
    if (t == null) return null;
    return Math.max(0, Math.floor((serverNow() - t) / 60000));
  }
  function minutesAgo(iso) {
    const t = naiveMs(iso);
    return t == null ? null : Math.max(0, Math.floor((serverNow() - t) / 60000));
  }
  const limits = () => {
    const t = (state.snap && state.snap.data.thresholds) || {};
    return { warn: t.warning_minutes || 60, breach: t.breach_minutes || 120 };
  };

  /** The status a request has reached by the clock now; the stored one is the status at the last scan. */
  function statusNow(r) {
    const age = ageOf(r);
    if (age == null) return r.status;
    const { warn, breach } = limits();
    const live = age >= breach ? "breached" : age >= warn ? "reminder" : "within";
    return RANK[live] < RANK[r.status] ? live : r.status;
  }

  function loadView() {
    try {
      const v = JSON.parse(sessionStorage.getItem(VIEW_KEY) || "{}");
      if (STATUSES.includes(v.filters && v.filters.status)) state.filters.status = v.filters.status;
      if (v.filters && typeof v.filters.user === "string") state.filters.user = v.filters.user.slice(0, 200);
      if (v.filters && typeof v.filters.q === "string") state.filters.q = v.filters.q.slice(0, 100);
      if (v.sort && ["priority", "age", "user"].includes(v.sort.key)) state.sort = { key: v.sort.key, desc: !!v.sort.desc };
      if (v.userSort && ["user", "total", "within", "reminder", "breached", "oldest"].includes(v.userSort.key)) state.userSort = { key: v.userSort.key, desc: !!v.userSort.desc };
      if (PAGE_SIZES.includes(v.pageSize)) state.pageSize = v.pageSize;
    } catch (e) { /* no storage (private window): the page works without it */ }
  }
  function saveView() {
    try {
      sessionStorage.setItem(VIEW_KEY, JSON.stringify({ filters: state.filters, sort: state.sort, userSort: state.userSort, pageSize: state.pageSize }));
    } catch (e) { /* ignore */ }
  }

  /** The page's own stylesheet. A template may link it itself; if not, it is added here and the page waits for it (no unstyled flash). */
  function ensureStyles() {
    if (document.querySelector('link[href*="ops-sla.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-sla.css", SELF).href : "/static/ops-sla.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
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

  // ------------------------------------------------------------------ banner (freshness / offline)
  function failureBanner(snap) {
    const f = state.failure, lead = f.network ? "Cannot reach the server" : `The server reported a problem (${f.message})`;
    return { level: "warn", icon: "wifi", text: `${lead} - retrying.${snap ? ` Showing the scan from ${hhmm(snap.generated_at)}.` : ""}` };
  }
  function freshness() {
    const m = state.meta, snap = state.snap;
    if (state.failure) return failureBanner(snap);
    if (!m) return { level: "info", icon: "clock", text: "Loading…" };
    if (m.problem) return { level: "danger", icon: "alert", text: `The scan file cannot be read: ${m.problem}` };
    if (!snap) {
      if (m.enabled === false) return { level: "info", icon: "dash", text: "Security-request monitoring is switched off in Settings." };
      return { level: "info", icon: "clock", text: m.online ? "No scan yet - the collector is running and the first scan will appear here." : "No scan yet - the collector has not run." };
    }
    const when = hhmm(snap.generated_at), ago = minutesAgo(snap.generated_at);
    const agoText = ago == null ? "" : ago < 1 ? "just now" : `${fmtAge(ago)} ago`;
    if (m.enabled === false) return { level: "info", icon: "dash", text: `Monitoring is switched off. Showing the last scan from ${when}.` };
    if (m.last_status === "error") {
      return { level: "danger", icon: "alert", text: `The last scan failed${m.last_run_at ? ` (${hhmm(m.last_run_at)})` : ""}: ${m.last_error || "unknown error"}. Showing the scan from ${when}.` };
    }
    if (!m.online) return { level: "danger", icon: "alert", text: `The collector is offline. Showing the scan from ${when} (${agoText}).` };
    if (m.stale) return { level: "warn", icon: "clock", text: `The data is ${fmtAge(ago)} old although scans run every ${m.interval_minutes} min. Showing the scan from ${when}.` };
    if (!m.active_now) return { level: "info", icon: "clock", text: `Outside working hours - showing the last scan from ${when}.` };
    // a manager gets no collector alarms (the server hides them), but old data is never called "Live"
    if (ago != null && ago > 3 * Math.max(1, m.interval_minutes || 15)) return { level: "info", icon: "clock", text: `Last scan ${when} (${agoText}).` };
    const next = m.next_run_at ? ` · next about ${hhmm(m.next_run_at)}` : "";
    return { level: "ok", icon: "check", text: `Live · last scan ${when} (${agoText})${next}${m.host ? ` · ${m.host}` : ""}` };
  }

  function renderBanner() {
    const f = freshness(), key = `${f.level}|${f.text}`;
    if (el.banner._key === key) return;                           // a live region must not repeat itself every few seconds
    el.banner._key = key;
    el.banner.className = `sx-banner sx-fresh ${f.level}`;
    el.banner.replaceChildren(glyph(f.icon), h("span", { class: "sx-banner-text", dir: "auto" }, f.text));
  }

  // ------------------------------------------------------------------ KPI tiles
  function renderKpis() {
    const { data } = state.snap, t = data.totals || {}, lim = limits();
    const open = t.open || 0, pct = (n) => (open ? `${Math.round((n / open) * 100)}% of open` : "");
    const users = (data.by_user || []).length;
    const oldest = oldestRequest();
    const tile = (key, label, value, sub, status) => {
      const pressed = key === "open" ? !state.filters.status : state.filters.status === key;
      const body = [h("span", { class: "sx-kpi-label" }, status ? glyph(STATUS_ICON[status]) : null, label),
        h("strong", { class: "sx-kpi-value", "data-kpi-value": key }, value), h("span", { class: "sx-kpi-sub" }, sub || "")];
      if (key === "oldest") return h("div", { class: "sx-kpi static", "data-kpi": key, "data-minutes": oldest.minutes == null ? "" : String(oldest.minutes) }, body);
      return h("button", { type: "button", class: `sx-kpi${status ? ` st-${status}` : ""}`, "data-kpi": key, "data-value": String(value), "data-focus-id": `kpi-${key}`,
        "aria-pressed": String(pressed), title: key === "open" ? "Show all requests" : `Show only: ${label}`,
        onclick: () => setFilter({ status: key === "open" || state.filters.status === key ? "" : key }) }, body);
    };
    el.kpis.replaceChildren(
      tile("open", "Open requests", open, open ? `waiting for ${fmtNum(users)} security user${users === 1 ? "" : "s"}` : "nothing waiting"),
      tile("within", "Within SLA", t.within || 0, [`under ${fmtLimit(lim.warn)}`, pct(t.within || 0)].filter(Boolean).join(" · "), "within"),
      tile("reminder", `Reminder ≥ ${fmtLimit(lim.warn)}`, t.reminder || 0, pct(t.reminder || 0), "reminder"),
      tile("breached", `Breached ≥ ${fmtLimit(lim.breach)}`, t.breached || 0, pct(t.breached || 0), "breached"),
      tile("no_date", "No sent date", t.no_date || 0, "not counted in the SLA", "no_date"),
      tile("oldest", "Oldest", oldest.minutes == null ? "—" : fmtAge(oldest.minutes), oldest.user || ""));
  }

  /** Oldest open request by the clock now (falls back to the scan's number when the rows carry no dates). */
  function oldestRequest() {
    let best = { minutes: null, user: "" };
    for (const r of state.snap.data.requests || []) {
      const age = ageOf(r);
      if (age != null && (best.minutes == null || age > best.minutes)) best = { minutes: age, user: r.security_user };
    }
    if (best.minutes == null && (state.snap.data.totals || {}).oldest_minutes) best.minutes = state.snap.data.totals.oldest_minutes;
    return best;
  }

  // ------------------------------------------------------------------ per-user table
  const USER_COLUMNS = [["user", "Security user", false], ["total", "Total", true], ["within", "Within", true],
    ["reminder", "Reminder", true], ["breached", "Breached", true], ["oldest", "Oldest", true]];

  function liveOldestByUser() {
    const out = new Map();
    for (const r of state.snap.data.requests || []) {
      const age = ageOf(r);
      if (age != null && age > (out.get(r.security_user) ?? -1)) out.set(r.security_user, age);
    }
    return out;
  }

  function sortedUsers() {
    const live = liveOldestByUser();
    const rows = (state.snap.data.by_user || []).map((u) => ({ ...u, oldest: live.has(u.user) ? live.get(u.user) : u.total > (u.no_date || 0) ? u.oldest_minutes : null }));
    const { key, desc } = state.userSort;
    const val = (u) => (key === "user" ? normAr(u.user) : u[key] == null ? -1 : u[key]);
    rows.sort((a, b) => {
      const x = val(a), y = val(b);
      const c = typeof x === "string" ? x.localeCompare(y) : x - y;
      return (desc ? -c : c) || b.breached - a.breached || b.reminder - a.reminder || b.total - a.total;      // a tie keeps the scan's order
    });
    return rows;
  }

  function userHead() {
    return h("tr", {}, USER_COLUMNS.map(([key, label, num]) => sortHeader(label, key, num, state.userSort, (k) => {
      state.userSort = { key: k, desc: state.userSort.key === k ? !state.userSort.desc : k !== "user" };
      saveView();
      renderUsers();
    }, "u")));
  }

  function sortHeader(label, key, num, sort, onSort, scope) {
    const active = sort.key === key;
    const dir = !active ? "none" : sort.desc ? "descending" : "ascending";
    return h("th", { scope: "col", class: num ? "num" : "", "aria-sort": dir },
      h("button", { type: "button", class: "sx-sort", "data-focus-id": `sort-${scope}-${key}`, onclick: () => onSort(key) },
        label, h("span", { class: "sx-arrow", "aria-hidden": "true" }, active ? (sort.desc ? "▼" : "▲") : "")));
  }

  function renderUsers() {
    const users = sortedUsers();
    const selected = state.filters.user;
    const count = (n, status) => h("td", { class: "num" }, n ? h("span", { class: `sx-num has st-${status}` }, fmtNum(n)) : h("span", { class: "sx-num zero" }, "0"));
    keepFocus(el.userCard, () => {
      el.userHead.replaceChildren(userHead());
      el.userBody.replaceChildren(...users.map((u) => h("tr", { class: u.user === selected ? "is-selected" : "", "data-user": u.user },
        h("th", { scope: "row", class: "sx-user" },
          h("button", { type: "button", class: "sx-link", "data-focus-id": `user-${u.user}`, "aria-pressed": String(u.user === selected),
            title: u.user === selected ? "Show all users again" : "Show only this user's requests",
            onclick: () => setFilter({ user: u.user === selected ? "" : u.user }) }, bidi(u.user))),
        h("td", { class: "num" }, h("strong", {}, fmtNum(u.total))), count(u.within, "within"), count(u.reminder, "reminder"), count(u.breached, "breached"),
        h("td", { class: "num sx-oldest", "data-oldest-user": u.user }, fmtAge(u.oldest)))));
    });
    el.userEmpty.hidden = users.length > 0;
    el.userTable.hidden = users.length === 0;
  }

  // ------------------------------------------------------------------ request list
  function visibleRequests() {
    const f = state.filters, words = normAr(f.q).split(/\s+/).filter(Boolean);
    const rows = (state.snap.data.requests || []).filter((r) => {
      if (f.status && r.status !== f.status) return false;
      if (f.user && r.security_user !== f.user) return false;
      if (!words.length) return true;
      const hay = r._hay || (r._hay = normAr([r.security_user, ...Object.values(r.fields || {})].join(" ")));
      return words.every((w) => hay.includes(w));
    });
    const { key, desc } = state.sort;
    const byAge = (a, b) => {                       // oldest first; requests without a date go last
      const x = ageOf(a), y = ageOf(b);
      return x == null && y == null ? 0 : x == null ? 1 : y == null ? -1 : y - x;
    };
    rows.sort((a, b) => {
      if (key === "age") {
        const x = ageOf(a), y = ageOf(b);
        if (x == null || y == null) return byAge(a, b);
        return desc ? y - x : x - y;
      }
      if (key === "user") return (desc ? -1 : 1) * normAr(a.security_user).localeCompare(normAr(b.security_user)) || RANK[a.status] - RANK[b.status] || byAge(a, b);
      return (desc ? -1 : 1) * (RANK[a.status] - RANK[b.status]) || byAge(a, b);       // priority: breached first
    });
    return rows;
  }

  function detailColumns() {
    const cols = [];
    for (const r of state.snap.data.requests || []) {
      for (const k of Object.keys(r.fields || {})) if (!cols.includes(k)) cols.push(k);
      if (cols.length >= 8) break;
    }
    return cols.slice(0, 8);
  }

  function chip(status) {
    return h("span", { class: `sx-chip st-${status}` }, glyph(STATUS_ICON[status]), LABELS[status]);
  }

  /** "now over 2 h" - shown while the clock has passed a limit that the last scan has not seen yet. */
  function cueText(r) {
    const now = statusNow(r);
    if (now === r.status) return "";
    return now === "breached" ? `now over ${fmtLimit(limits().breach)}` : `now over ${fmtLimit(limits().warn)}`;
  }

  /** Which clock the age counts from: "assigned 09:30" (the last assignment, from the SLA history) or "sent 09:30"
      (the Re-Assign page's sent date, when the history has not read the request yet). */
  function sinceText(r) {
    const at = r.assigned_at || r.sent_at;
    if (!at) return "";
    const word = r.assigned_at ? "assigned" : "sent";
    const t = hhmm(at), today = state.snap.generated_at.slice(0, 10);
    return at.slice(0, 10) === today ? `${word} ${t}` : `${word} ${at.slice(8, 10)}/${at.slice(5, 7)} ${t}`;
  }
  const sinceTitle = (r) => (r.assigned_at
    ? "Age from the last assignment to this security user (SLA history)"
    : "Age from «تاريخ الارسال للسرية»: the status history of this request is not read yet");

  function requestRow(r, cols) {
    const cue = cueText(r);
    const tr = h("tr", { class: `st-${r.status}`, "data-key": r.key, "data-status": r.status },
      h("td", { class: "sx-status" }, chip(r.status), h("span", { class: "sx-cue hint", "data-cue": "" }, cue)),
      h("td", { class: "sx-who" }, bidi(r.security_user)),
      h("td", { class: "sx-age nowrap" }, h("strong", { "data-age": "" }, fmtAge(ageOf(r))),
        h("span", { class: "hint", "data-since": r.assigned_at ? "assignment" : "sent", title: sinceTitle(r) }, sinceText(r))),
      ...cols.map((c) => h("td", { class: "sx-field" }, (r.fields || {})[c] ? bidi(r.fields[c]) : h("span", { class: "sx-none" }, "—"))));
    tr._req = r;
    return tr;
  }

  function renderRequests() {
    const all = state.snap.data.requests || [];
    el.reqCard.hidden = false;
    el.reqEmpty.hidden = all.length > 0;
    el.reqTools.hidden = el.reqWrap.hidden = el.pager.hidden = all.length === 0;
    if (!all.length) return;
    const rows = visibleRequests(), cols = detailColumns();
    const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    state.page = Math.min(Math.max(1, state.page), pages);
    const slice = rows.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);

    el.reqHead.replaceChildren(h("tr", {},
      sortHeader("Status", "priority", false, state.sort, sortRequests, "r"),
      sortHeader("Security user", "user", false, state.sort, sortRequests, "r"),
      sortHeader("Age", "age", false, state.sort, sortRequests, "r"),
      ...cols.map((c) => h("th", { scope: "col" }, bidi(c)))));
    el.reqBody.replaceChildren(...slice.map((r) => requestRow(r, cols)));
    el.noMatch.hidden = rows.length > 0;
    el.reqWrap.hidden = rows.length === 0;
    el.count.textContent = rows.length === all.length ? `${fmtNum(all.length)} open request${all.length === 1 ? "" : "s"}` : `${fmtNum(rows.length)} of ${fmtNum(all.length)} requests`;
    renderPager(rows.length, pages);
    syncControls();
  }

  function sortRequests(key) {
    state.sort = { key, desc: state.sort.key === key ? !state.sort.desc : key === "age" };
    state.page = 1;
    saveView();
    keepFocus(el.reqCard, renderRequests);
  }

  function renderPager(total, pages) {
    const from = total ? (state.page - 1) * state.pageSize + 1 : 0, to = Math.min(total, state.page * state.pageSize);
    el.pager.hidden = total === 0;
    el.pagerText.textContent = total ? `${from}–${to} of ${fmtNum(total)}` : "";
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= pages;
    el.pageSize.value = String(state.pageSize);
    el.pageInfo.textContent = pages > 1 ? `Page ${state.page} of ${pages}` : "";
  }

  function fillUserFilter() {
    const names = new Set((state.snap.data.by_user || []).map((u) => u.user));
    for (const r of state.snap.data.requests || []) names.add(r.security_user);
    if (state.filters.user) names.add(state.filters.user);
    const sorted = [...names].sort((a, b) => normAr(a).localeCompare(normAr(b)));
    el.userSelect.replaceChildren(h("option", { value: "" }, "All security users"), ...sorted.map((n) => h("option", { value: n }, n)));
    el.userSelect.value = state.filters.user;
  }

  function syncControls() {
    const f = state.filters;
    el.statusSelect.value = f.status;
    el.userSelect.value = f.user;
    if (document.activeElement !== el.search) el.search.value = f.q;
    el.clear.hidden = !(f.status || f.user || f.q);
  }

  function setFilter(patch) {
    Object.assign(state.filters, patch);
    state.page = 1;
    saveView();
    keepFocus(el.kpis, renderKpis);
    renderUsers();
    keepFocus(el.reqCard, renderRequests);
  }

  // ------------------------------------------------------------------ timeline
  const SERIES = [{ key: "open", label: "Open", cls: "ln-open" }, { key: "breached", label: "Breached", cls: "ln-breached" }];

  /** Today's scans as [{t (ms), ts, v: {open, within, reminder, breached}}], oldest first. */
  function timelinePoints() {
    const hist = state.history;
    if (!hist) return [];
    return (hist.points || []).map((p) => ({ ts: p.ts, t: naiveMs(p.ts), v: p })).filter((p) => p.t != null).sort((a, b) => a.t - b.t);
  }

  /** Requests that first crossed the breach limit today, grouped by the scan that saw it (a diamond on the breached line). */
  function breachMarkers(points) {
    const by = new Map();
    const open = new Map((state.snap.data.requests || []).map((r) => [r.key, r]));
    for (const [key, ts] of Object.entries((state.history && state.history.first_breach) || {})) {
      const t = naiveMs(ts);
      if (t == null) continue;
      if (!by.has(t)) by.set(t, { t, ts, count: 0, users: [] });
      const m = by.get(t);
      m.count += 1;
      const r = open.get(key);
      if (r && !m.users.includes(r.security_user)) m.users.push(r.security_user);
    }
    return [...by.values()].sort((a, b) => a.t - b.t).map((m) => {
      const at = [...points].reverse().find((p) => p.t <= m.t) || points[0];
      return { ...m, y: at ? at.v.breached || 0 : 0,
        text: () => `${m.count} request${m.count === 1 ? "" : "s"} passed ${fmtLimit(limits().breach)} here` };
    });
  }

  function timelineModel(points) {
    return { points, series: SERIES, markers: breachMarkers(points), note: (p) => `Within ${p.v.within || 0} · Reminder ${p.v.reminder || 0}`,
      ariaLabel: `Open and breached tickets today, ${points.length} scans from ${hhmm(points[0].ts)} to ${hhmm(points[points.length - 1].ts)}` };
  }

  function drawTimeline() {
    const host = el.chart, points = timelinePoints();
    host._model = null;
    if (!points.length) {
      host.replaceChildren(h("p", { class: "empty" }, "The timeline fills in as scans happen during the day."));
      el.chartLegend.hidden = true;
      return;
    }
    el.chartLegend.hidden = false;
    host._model = timelineModel(points);
    paintLines(host, host._model);
    if (!host._ro) {
      host._ro = new ResizeObserver(() => { if (host._model && host.clientWidth !== host._width) paintLines(host, host._model); });
      host._ro.observe(host);
    }
    fillChartTable(points, host._model.markers);
  }

  const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  function niceTop(max) {
    const step = NICE.find((n) => max / n <= 5) || Math.ceil(max / 5);
    return { top: Math.max(step, Math.ceil(max / step) * step), step };
  }

  /** Line chart over the hours of today. m = {points: [{t, ts, v: {key: n}}], series: [{key, label, cls}], note(p), ariaLabel}. */
  function paintLines(host, m) {
    const { points, series } = m;
    host._width = host.clientWidth;
    const W = Math.max(host.clientWidth, 320), H = 250;
    const pad = { l: 34, r: 118, t: 14, b: 28 };
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    let t0 = points[0].t, t1 = points[points.length - 1].t;
    if (t1 - t0 < 45 * 60000) { const mid = (t0 + t1) / 2; t0 = mid - 22.5 * 60000; t1 = mid + 22.5 * 60000; }
    const { top, step } = niceTop(Math.max(1, ...points.flatMap((p) => series.map((s) => p.v[s.key] || 0))));
    const X = (t) => pad.l + ((t - t0) / (t1 - t0)) * pw, Y = (v) => pad.t + ph - (v / top) * ph;

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": m.ariaLabel });
    for (let v = 0; v <= top; v += step) {
      svg.append(svgEl("line", { class: v === 0 ? "axis-line" : "grid-line", x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v) }));
      const t = svgEl("text", { class: "ax", x: pad.l - 6, y: Y(v) + 4, "text-anchor": "end" });
      t.textContent = v;
      svg.append(t);
    }
    const stepMin = [10, 15, 30, 60, 120, 180].find((s) => ((t1 - t0) / 60000 / s) <= Math.max(3, pw / 70)) || 240;
    for (let t = Math.ceil(t0 / (stepMin * 60000)) * stepMin * 60000; t <= t1; t += stepMin * 60000) {
      const d = new Date(t), tx = svgEl("text", { class: "ax", x: X(t), y: H - 8, "text-anchor": "middle" });
      tx.textContent = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
      svg.append(tx);
    }
    for (const s of series) {
      svg.append(svgEl("path", { class: `ln ${s.cls}`, d: points.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.v[s.key] || 0).toFixed(1)}`).join(" ") }));
    }
    const last = points[points.length - 1];
    const ends = series.map((s) => Y(last.v[s.key] || 0)).sort((a, b) => a - b);
    const crowded = ends.some((y, i) => i && y - ends[i - 1] < 16);        // colliding labels are left to the legend
    for (const s of series) {
      const y = Y(last.v[s.key] || 0);
      svg.append(svgEl("circle", { class: `dot-end ${s.cls}`, cx: X(last.t), cy: y, r: 4 }));
      if (!crowded) {
        const t = svgEl("text", { class: "end-label", x: X(last.t) + 10, y: y + 4 });
        t.textContent = `${s.label.split(" ")[0]} ${last.v[s.key] || 0}`;
        svg.append(t);
      }
    }
    const cross = svgEl("line", { class: "crosshair", y1: pad.t, y2: pad.t + ph, x1: 0, x2: 0, visibility: "hidden" });
    svg.append(cross);
    for (const mk of m.markers || []) {
      const cx = X(mk.t), cy = Y(mk.y);
      svg.append(svgEl("path", { class: "mark-break", d: `M${cx},${cy - 6} L${cx + 6},${cy} L${cx},${cy + 6} L${cx - 6},${cy} Z` }));
    }
    const hit = svgEl("rect", { class: "hit", x: pad.l, y: pad.t, width: pw, height: ph + 6, tabindex: "0", role: "group",
      "aria-label": "Chart values. Use the left and right arrow keys to move between scans." });
    svg.append(hit);

    const tip = host._tip || (host._tip = h("div", { class: "tooltip sx-tip", hidden: true, role: "status" }));
    let idx = points.length - 1;
    const show = (i) => {
      idx = Math.max(0, Math.min(points.length - 1, i));
      const p = points[idx], note = m.note ? m.note(p) : "";
      tip.replaceChildren(h("div", { class: "tt-title" }, `Scan at ${hhmm(p.ts)}`),
        ...series.map((s) => h("div", { class: "tt-row" }, h("i", { class: `key ${s.cls}` }), h("strong", {}, p.v[s.key] || 0), h("span", { class: "label" }, s.label))),
        note ? h("div", { class: "tt-note" }, note) : null,
        ...(m.markers || []).filter((mk) => mk.t === p.t).map((mk) => h("div", { class: "tt-note" }, mk.text(), ...mk.users.slice(0, 3).map((u, n) => [n ? ", " : ": ", bidi(u)]))));
      tip.hidden = false;
      const x = X(p.t);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      tip.style.left = `${Math.min(Math.max(x - tip.offsetWidth / 2, 0), W - tip.offsetWidth)}px`;
      tip.style.top = `${Math.max(0, Y(Math.max(...series.map((s) => p.v[s.key] || 0))) - tip.offsetHeight - 12)}px`;
    };
    const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
    const nearest = (ev) => {
      const box = svg.getBoundingClientRect(), x = ((ev.clientX - box.left) / box.width) * W;
      let best = 0;
      points.forEach((p, i) => { if (Math.abs(X(p.t) - x) < Math.abs(X(points[best].t) - x)) best = i; });
      return best;
    };
    hit.addEventListener("pointermove", (ev) => show(nearest(ev)));
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => show(idx));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") { ev.preventDefault(); show(idx + (ev.key === "ArrowRight" ? 1 : -1)); }
    });
    host.replaceChildren(svg, tip);
  }

  function fillChartTable(points, markers) {
    const rows = [...points].reverse().map((p) => {
      const first = markers.filter((mk) => mk.t === p.t).reduce((n, mk) => n + mk.count, 0);
      return h("tr", {}, h("th", { scope: "row" }, hhmm(p.ts)), h("td", { class: "num" }, p.v.open), h("td", { class: "num" }, p.v.within),
        h("td", { class: "num" }, p.v.reminder), h("td", { class: "num" }, p.v.breached), h("td", { class: "num" }, first || ""));
    });
    el.chartTable.replaceChildren(h("table", { class: "data sx-table" },
      h("caption", { class: "sr-only" }, "Tickets at each scan today"),
      h("thead", {}, h("tr", {}, ["Scan", "Open", "Within", "Reminder", "Breached", "First over limit"].map((c, i) => h("th", { scope: "col", class: i ? "num" : "" }, c)))),
      h("tbody", {}, rows)));
  }

  function toggleChartView() {
    state.viewAsTable = !state.viewAsTable;
    applyChartView();
  }
  function applyChartView() {
    const has = !!(el.chart._model);
    el.chartToggle.textContent = state.viewAsTable ? "View as chart" : "View as table";
    el.chartToggle.hidden = !has;
    el.chart.hidden = has && state.viewAsTable;
    el.chartTable.hidden = !(has && state.viewAsTable);
  }

  async function loadHistory() {
    try {
      const answer = await api("GET", "/api/ops/sla/history?days=1");
      const day = (answer.days || []).find((d) => d.day === state.today);
      state.history = day ? { points: day.points || [], first_breach: day.first_breach || day.first_break || {} } : { points: [], first_breach: {} };
    } catch (e) {
      state.history = null;                    // the timeline is a bonus: the page works without it
    }
  }

  // ------------------------------------------------------------------ "Run scan now"
  /** The sentence is a polite live region and changes only with the phase; the running clock beside it is silent. */
  function setWaiting(text, clock) {
    el.runLine.hidden = !text;
    if (el.runText.textContent !== (text || "")) el.runText.textContent = text || "";
    el.runTimer.textContent = clock || "";
  }
  function updateRunUI() {
    const w = state.waiting;
    el.runBtn.disabled = !!w;
    el.runBtn.replaceChildren(glyph("refresh"), w ? "Scanning…" : "Run scan now");
    el.runBtn.classList.toggle("is-busy", !!w);
    if (!w) return;
    const secs = Math.floor((Date.now() - w.since) / 1000), m = state.meta || {};
    const clock = `${Math.floor(secs / 60)}:${pad2(secs % 60)}`;
    const phase = m.running ? `The collector${m.host ? ` (${m.host})` : ""} is scanning now`
      : m.online === false ? "The collector looks offline - the request waits until it is back" : "Waiting for the collector to pick up the request";
    setWaiting(`${phase}…`, clock);
  }

  async function runNow() {
    if (state.waiting) return;
    state.waiting = { since: Date.now(), generated: state.generatedAt, lastRun: state.meta ? state.meta.last_run_at : null, timer: null };
    updateRunUI();
    try {
      const answer = await api("POST", "/api/ops/jobs/sla/run", {});
      if (answer.already_pending) toast("A scan was already requested - waiting for it.");
    } catch (e) {
      state.waiting = null;
      updateRunUI();
      toast(e.message, "error");
      return;
    }
    state.waiting.timer = setInterval(updateRunUI, 1000);
    schedule(500);
  }

  function endWaiting(message, kind) {
    if (!state.waiting) return;
    clearInterval(state.waiting.timer);
    state.waiting = null;
    updateRunUI();
    setWaiting(message || "");
    if (message) toast(message, kind);
    if (message && kind !== "error") setTimeout(() => { if (!state.waiting) setWaiting(""); }, 6000);
  }

  function checkWaiting() {
    const w = state.waiting, m = state.meta || {};
    if (!w) return;
    if (state.generatedAt && state.generatedAt !== w.generated) {
      const t = (state.snap && state.snap.data.totals) || {};
      endWaiting(`Scan finished at ${hhmm(state.generatedAt)}: ${fmtNum(t.open || 0)} open, ${fmtNum(t.breached || 0)} breached.`);
    } else if (m.last_run_at && m.last_run_at !== w.lastRun && m.last_status === "error") {
      endWaiting(`The scan failed: ${m.last_error || "unknown error"}.`, "error");
    } else if (Date.now() - w.since > WAIT_LIMIT_MS) {
      endWaiting("The collector has not delivered a new scan yet. The request stays queued and this page updates by itself.", "error");
    }
  }

  // ------------------------------------------------------------------ data loop
  let timer = null, failures = 0;

  let inflight = null;
  function refresh(force) {
    if (!inflight) inflight = load(force).finally(() => { inflight = null; });
    return inflight;
  }

  async function load(force) {
    const since = !force && state.generatedAt ? `?since=${encodeURIComponent(state.generatedAt)}` : "";
    const answer = await api("GET", `/api/ops/sla${since}`);
    state.failure = null;
    state.meta = answer.meta;
    noteServerTime(answer.meta.server_time);
    state.today = String(answer.meta.server_time || "").slice(0, 10);
    if (!answer.unchanged) {
      const fresh = answer.data ? answer : null;
      const isNew = (fresh && fresh.generated_at) !== state.generatedAt;
      state.snap = fresh;
      state.generatedAt = fresh ? fresh.generated_at : "";
      if (isNew) await loadHistory();
      if (isNew && state.tab === "analysis" && state.analysis) loadAnalysis(true, true);   // the analysis is live too
    }
    render();
    tick();                                         // the clock offset may have changed: ages move even when the scan did not
    checkWaiting();
  }

  function schedule(ms) {
    clearTimeout(timer);
    const delay = ms != null ? ms : (state.waiting ? WAIT_POLL_MS : Math.min(POLL_MS * 2 ** Math.min(failures, 2), 60000)) + Math.random() * 1500;
    timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (document.hidden) return;                    // visibilitychange restarts the loop
    try {
      await refresh(false);
      failures = 0;
    } catch (e) {
      failures += 1;
      state.failure = { network: !!e.network, message: e.message };
      if (!e.network && failures === 1) toast(e.message, "error");
      renderBanner();
    }
    schedule();
  }

  /** Between scans only the clock moves: ages, cues and "x min ago" are rewritten in place. */
  function tick() {
    if (document.hidden || !state.snap) return;
    renderBanner();
    for (const tr of el.reqBody.querySelectorAll("tr")) {
      const r = tr._req;
      if (!r) continue;
      tr.querySelector("[data-age]").textContent = fmtAge(ageOf(r));
      tr.querySelector("[data-cue]").textContent = cueText(r);
    }
    const oldest = oldestRequest(), tile = el.kpis.querySelector('[data-kpi="oldest"]');
    if (tile) {
      tile.querySelector("[data-kpi-value]").textContent = oldest.minutes == null ? "—" : fmtAge(oldest.minutes);
      tile.dataset.minutes = oldest.minutes == null ? "" : String(oldest.minutes);
    }
    const live = liveOldestByUser();
    for (const cell of el.userBody.querySelectorAll("[data-oldest-user]")) {
      if (live.has(cell.dataset.oldestUser)) cell.textContent = fmtAge(live.get(cell.dataset.oldestUser));
    }
  }

  // ------------------------------------------------------------------ page
  // ------------------------------------------------------------------ SLA Analysis tab (closed-request history)
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
    return OPS.table({
      search: rows.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Open requests, longest waiting first",
      sort: { key: "minutes", desc: true },
      columns: [
        { key: "id", label: "Request", sort: true, render: (r) => String(r.id), text: (r) => String(r.id) },
        { key: "holder", label: "Security user", sort: true, render: (r) => h("span", { dir: "auto" }, r.holder || "—"), text: (r) => r.holder || "" },
        { key: "system", label: "System", sort: true, render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
        { key: "requester", label: "Requester", sort: true, render: (r) => h("span", { dir: "auto" }, r.requester || "—"), text: (r) => r.requester || "" },
        { key: "assigned_at", label: "Assigned", sort: true, render: (r) => fmtStamp(r.assigned_at), text: (r) => r.assigned_at || "" },
        { key: "minutes", label: "Waiting", num: true, sort: true,
          render: (r) => (r.minutes == null ? h("span", { class: "hint", title: "Its status history is read at the next scan" }, "not read yet")
            : r.minutes > sla ? OPS.chip(fmtMin(r.minutes), "danger") : fmtMin(r.minutes)) },
        { key: "on_hold", label: "", sort: false, render: (r) => (r.on_hold ? OPS.chip("on hold", "warn") : "") },
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
      OPS.kpi({ key: "an-open", label: "Open now", value: fmtNum(N.open || 0), sub: N.unread ? `with the security user · ${fmtNum(N.unread)} read at the next scan` : "with the security user (مستخدم السرية)", icon: "clock", hero: true }),
      OPS.kpi({ key: "an-await-now", label: "Awaiting the reviewer", value: fmtNum(N.awaiting || 0), sub: "executed · not confirmed yet (مراجع السرية)", tone: N.awaiting ? "warn" : null, icon: "alert" }),
      OPS.kpi({ key: "an-past", label: `Past ${fmtMin(sla)}`, value: fmtNum(N.breached || 0), sub: "open longer than the SLA target", tone: N.breached ? "danger" : null, icon: "shield" }),
      OPS.kpi({ key: "an-hold", label: "On hold", value: fmtNum(N.on_hold || 0), sub: "«معلق» right now", icon: "clock" }),
      OPS.kpi({ key: "an-oldest", label: "Oldest open", value: fmtMin(N.oldest_minutes), sub: "waiting the longest", tone: (N.oldest_minutes != null && N.oldest_minutes > sla) ? "danger" : null, icon: "clock" }),
      OPS.kpi({ key: "an-pre", label: "With the branch", value: fmtNum(N.pre || 0), sub: "registered · not assigned yet", icon: "user" }));
    const parts = [kpis];
    if (N.open) {
      parts.push(h("div", { class: "sx-two" },
        OPS.card("By security user (open)", { hint: "مستخدم السرية · who holds the open requests now" }, holderTable(N.by_holder)),
        OPS.card("Open requests", { hint: "longest waiting first" }, openTable(d.open_now, sla))));
    }
    if ((d.awaiting_now || []).length) {
      parts.push(OPS.card("Awaiting the reviewer", { hint: "مراجع السرية · executed, waiting for «تم التعزيز»" }, OPS.table({
        search: d.awaiting_now.length > 12, copy: true, pageSize: 12, pageSizes: [12, 25, 100], caption: "Executed requests waiting for confirmation",
        sort: { key: "minutes", desc: true },
        columns: [
          { key: "id", label: "Request", sort: true, render: (r) => String(r.id), text: (r) => String(r.id) },
          { key: "executor", label: "Executed by", sort: true, render: (r) => h("span", { dir: "auto" }, r.executor || "—"), text: (r) => r.executor || "" },
          { key: "system", label: "System", sort: true, render: (r) => h("span", { dir: "auto" }, r.system || "—"), text: (r) => r.system || "" },
          { key: "executed_at", label: "Executed", sort: true, render: (r) => fmtStamp(r.executed_at), text: (r) => r.executed_at || "" },
          { key: "outcome", label: "Outcome", sort: true, render: (r) => (r.outcome === "not_executed" ? OPS.chip("not executed", "warn") : r.outcome ? OPS.chip("executed", "ok") : "—"), text: (r) => r.outcome || "" },
          { key: "minutes", label: "Waiting", num: true, sort: true, render: (r) => (r.minutes == null ? h("span", { class: "hint" }, "not read yet") : fmtMin(r.minutes)) },
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

  function renderAnalysis(d) {
    if (!d || !d.available) {
      const R = (d && d.recorder) || {};
      const why = R.enabled === false
        ? "Reading the status history is switched off in System › Settings › Ticketing › SLA history."
        : (R.error && R.error.message) ? `The last scan could not read it: ${R.error.message}`
        : "Nothing is uploaded: at every scan the collector opens «الاستفسار عن الطلبات» on the portal and reads each request's «الحالة التاريخية للطلب» — who assigned, executed and confirmed it, and exactly when. The analysis appears after the next scan.";
      el.analysisPanel.replaceChildren(OPS.emptyState({ title: "No SLA history recorded yet", icon: "shield", text: why }));
      return;
    }
    const stack = [analysisHead(d), liveStrip(d), unmappedNote(d), filterBar(d)];
    if (!d.meta || !d.meta.requests) {
      stack.push(OPS.card("This period", {}, OPS.emptyState({ title: "No request matches this filter", icon: "shield",
        text: "“Right now” above is always current. Widen the dates, pick another employee / checker / team, or press Clear." })));
      el.analysisPanel.replaceChildren(h("div", { class: "sx-stack" }, ...stack.filter(Boolean)));
      return;
    }
    const o = d.overall, m = d.meta, b = d.buckets || {};
    const timed = o.median_minutes != null;                // at least one closed request has a clock
    const slaTone = !timed ? null : o.sla_pct >= 80 ? "ok" : o.sla_pct >= 50 ? "warn" : "danger";
    const slaH = Math.round(m.sla_minutes / 60 * 10) / 10;
    const kpis = h("div", { class: "sx-kpis" },
      OPS.kpi({ key: "an-closed", label: "Closed", value: fmtNum(b.closed || 0), sub: "confirmed by the reviewer in this period", icon: "check", hero: true }),
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
    el.analysisPanel.replaceChildren(h("div", { class: "sx-stack" }, ...stack.filter(Boolean)));
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
    if (!state.slaFilter) {                                 // first open: default the date to today (current data)
      const t = slaToday();
      state.slaFilter = { ...EMPTY_FILTER, date_from: t, date_to: t };
    }
    const seq = ++analysisSeq;
    if (!quiet) el.analysisPanel.replaceChildren(h("p", { class: "empty" }, "Loading the SLA analysis…"));
    let answer;
    try { answer = await api("GET", ANALYSIS_URL + slaQuery()); }
    catch (e) {
      if (seq === analysisSeq && !quiet) el.analysisPanel.replaceChildren(OPS.emptyState({ title: "SLA analysis unavailable", icon: "alert", text: e.message }));
      return;
    }
    if (seq !== analysisSeq) return;                        // a newer request (another filter, a newer scan) wins
    state.analysis = answer;
    if (quiet) keepFocus(el.analysisPanel, () => renderAnalysis(answer));
    else renderAnalysis(answer);
  }

  function syncTools() {
    const live = state.tab === "live";
    if (el.runBtn) el.runBtn.hidden = !live;
    if (el.exportLink) el.exportLink.hidden = !(live && state.snap);
    if (el.scanInfo) el.scanInfo.hidden = !live;
  }

  function setTab(tab) {
    state.tab = tab === "analysis" ? "analysis" : "live";
    const live = state.tab === "live";
    if (el.livePanel) el.livePanel.hidden = !live;
    if (el.analysisPanel) el.analysisPanel.hidden = live;
    if (el.tabLive) el.tabLive.setAttribute("aria-selected", String(live));
    if (el.tabAnalysis) el.tabAnalysis.setAttribute("aria-selected", String(!live));
    syncTools();
    if (!live) loadAnalysis();
  }

  function render() {
    renderBanner();
    const snap = state.snap;
    el.content.hidden = !snap;
    el.empty.hidden = !!snap;
    syncTools();
    el.scanInfo.textContent = snap ? `Scan of ${snap.generated_at.replace("T", " ").slice(0, 16)}${snap.duration_seconds ? ` · took ${Math.round(snap.duration_seconds)} s` : ""}` : "";
    if (!snap) return;
    if (el.content.dataset.rendered !== snap.generated_at) {
      el.content.dataset.rendered = snap.generated_at;
      fillUserFilter();
      renderKpis();
      renderUsers();
      renderRequests();
      drawTimeline();
      applyChartView();
    }
  }

  function build() {
    el.runBtn = h("button", { type: "button", class: "btn btn-primary", onclick: runNow }, glyph("refresh"), "Run scan now");
    el.exportLink = h("a", { class: "btn btn-ghost", href: "/export/ops/sla.xlsx", download: "" }, glyph("download"), "Export Excel");
    el.scanInfo = h("span", { class: "hint sx-scan" });
    el.banner = h("div", { class: "sx-banner sx-fresh info", role: "status" });
    el.runText = h("span");
    el.runTimer = h("span", { class: "sx-timer", "aria-hidden": "true" });
    el.runLine = h("p", { class: "sx-runline", role: "status", "aria-live": "polite", hidden: true }, el.runText, " ", el.runTimer);
    el.kpis = h("div", { class: "sx-kpis", role: "group", "aria-label": "Ticket totals" });

    el.userHead = h("thead");
    el.userBody = h("tbody");
    el.userTable = h("div", { class: "table-wrap" }, h("table", { class: "data sx-table sx-users" }, h("caption", { class: "sr-only" }, "Requests per security user"), el.userHead, el.userBody));
    el.userEmpty = h("p", { class: "empty", hidden: true }, "No security user has an open request.");
    el.userCard = h("section", { class: "card", "aria-labelledby": "sx-users-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-users-h" }, "By security user"), h("span", { class: "hint" }, "click a name to filter the list")), el.userTable, el.userEmpty);

    el.chart = h("div", { class: "chart sx-chart" });
    el.chartTable = h("div", { class: "table-wrap sx-chart-table", hidden: true });
    el.chartToggle = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: toggleChartView }, "View as table");
    el.chartLegend = h("div", { class: "legend sx-legend" },
      h("span", {}, h("i", { class: "key ln-open" }), "Open"), h("span", {}, h("i", { class: "key ln-breached" }), "Breached"),
      h("span", {}, h("i", { class: "key mark-key" }), "First went over the limit"));
    el.chartCard = h("section", { class: "card chart-card", "aria-labelledby": "sx-time-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-time-h" }, "Today"), el.chartToggle), el.chartLegend, el.chart, el.chartTable);

    el.statusSelect = h("select", { "aria-label": "Status", onchange: () => setFilter({ status: el.statusSelect.value }) },
      h("option", { value: "" }, "All statuses"), ...STATUSES.map((s) => h("option", { value: s }, LABELS[s])));
    el.userSelect = h("select", { "aria-label": "Security user", onchange: () => setFilter({ user: el.userSelect.value }) }, h("option", { value: "" }, "All security users"));
    let typing = 0;
    el.search = h("input", { type: "search", class: "search", placeholder: "Search name, user number, branch…", "aria-label": "Search requests", dir: "auto",
      oninput: () => { clearTimeout(typing); typing = setTimeout(() => setFilter({ q: el.search.value }), 150); } });
    el.clear = h("button", { type: "button", class: "btn btn-ghost btn-sm", hidden: true, onclick: () => setFilter({ status: "", user: "", q: "" }) }, glyph("x"), "Clear filters");
    el.count = h("span", { class: "sx-count", role: "status" });
    el.reqTools = h("div", { class: "sx-filters" }, el.statusSelect, el.userSelect, el.search, el.clear, h("span", { class: "grow" }), el.count);
    el.reqHead = h("thead");
    el.reqBody = h("tbody");
    el.reqWrap = h("div", { class: "table-wrap" }, h("table", { class: "data sx-table sx-requests" }, h("caption", { class: "sr-only" }, "Open tickets"), el.reqHead, el.reqBody));
    el.noMatch = h("div", { class: "empty", hidden: true }, h("p", {}, "No requests match these filters."),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => setFilter({ status: "", user: "", q: "" }) }, "Clear filters"));
    el.reqEmpty = h("div", { class: "sx-calm ok", hidden: true }, glyph("check"), h("div", {}, h("strong", {}, "No open requests"), h("p", { class: "muted" }, "Nothing is waiting for the security users.")));
    el.prev = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.page -= 1; renderRequests(); } }, "Previous");
    el.next = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { state.page += 1; renderRequests(); } }, "Next");
    el.pageSize = h("select", { "aria-label": "Rows per page", onchange: () => { state.pageSize = +el.pageSize.value; state.page = 1; saveView(); renderRequests(); } },
      PAGE_SIZES.map((n) => h("option", { value: String(n) }, `${n} per page`)));
    el.pagerText = h("span");
    el.pageInfo = h("span", { class: "hint" });
    el.pager = h("div", { class: "pager sx-pager" }, el.pagerText, el.pageInfo, el.pageSize, el.prev, el.next);
    el.reqCard = h("section", { class: "card", "aria-labelledby": "sx-req-h" },
      h("div", { class: "card-head" }, h("h2", { id: "sx-req-h" }, "Requests")), el.reqTools, el.reqWrap, el.noMatch, el.reqEmpty, el.pager);

    el.content = h("div", { class: "sx-stack", hidden: true }, el.kpis, h("div", { class: "sx-two" }, el.userCard, el.chartCard), el.reqCard);
    el.empty = h("section", { class: "card sx-calm big", hidden: true }, glyph("clock"),
      h("div", {}, h("strong", {}, "No scan yet"), h("p", { class: "muted" }, "The collector writes the first security-request scan here as soon as it has run. You can ask for one now with the button above.")));

    const root = document.getElementById("sla-page") || document.getElementById("app") || $("main") || document.body;
    state.admin = (root.dataset || {}).role === "admin";       // only admins run scans / see collector errors
    const tabBtn = (key, label) => h("button", { type: "button", class: "sx-tab", role: "tab", id: `sx-tab-${key}`,
      "aria-selected": "false", onclick: () => setTab(key) }, label);
    el.tabLive = tabBtn("live", "Live tickets");
    el.tabAnalysis = tabBtn("analysis", "SLA Analysis");
    el.tabBar = h("div", { class: "sx-tabs", role: "tablist", "aria-label": "Ticketing views" }, el.tabLive, el.tabAnalysis);
    el.livePanel = h("div", { class: "sx-tabpanel", role: "tabpanel", "aria-labelledby": "sx-tab-live" }, el.banner, el.runLine, el.empty, el.content);
    el.analysisPanel = h("div", { class: "sx-tabpanel", role: "tabpanel", "aria-labelledby": "sx-tab-analysis", hidden: true });
    root.replaceChildren(
      h("div", { class: "sx-page" },
        h("div", { class: "sx-titlebar" }, h("div", {}, h("h1", {}, "Ticketing"), el.scanInfo), h("span", { class: "grow" }), state.admin && el.runBtn, el.exportLink),
        el.tabBar, el.livePanel, el.analysisPanel));
    setTab(state.tab);
  }

  function applyLink() {
    // deep links win over the view remembered for this browser tab (Home and the other dashboards make them):
    //   /sla?q=<text>   /sla?user=<security user>   /sla?status=breached|reminder|within|no_date   /sla?tab=analysis (#analysis)
    const p = new URLSearchParams(location.search);
    if (p.has("q") || p.has("user") || p.has("status")) {
      state.filters = { status: STATUSES.includes(p.get("status")) ? p.get("status") : "",
        user: (p.get("user") || "").slice(0, 200), q: (p.get("q") || "").slice(0, 100) };
    }
    const tab = p.get("tab") || location.hash.slice(1);
    if (tab === "live" || tab === "analysis") state.tab = tab;
  }

  async function start() {
    await ensureStyles();
    loadView();
    applyLink();
    build();
    refresh(true).catch((e) => { state.failure = { network: !!e.network, message: e.message }; renderBanner(); if (!e.network) toast(e.message, "error"); }).finally(() => schedule());
    setInterval(tick, TICK_MS);
    // the SLA Analysis is live too: while it is open and its period reaches today, it refreshes itself every minute
    setInterval(() => {
      const f = state.slaFilter || {};
      if (state.tab === "analysis" && !document.hidden && state.analysis && (!f.date_to || f.date_to >= slaToday())) loadAnalysis(true, true);
    }, ANALYSIS_REFRESH_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); schedule(0); } });
  }

  start();
})();
