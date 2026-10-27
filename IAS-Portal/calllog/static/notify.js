"use strict";
/* Notification bell (Feature B4) - on every signed-in page through _topbar.html. Self-contained: no CL / OPS dependency,
   so it works on the Call Log pages and the Hub pages alike. Polls GET /api/ops/notifications, shows an unread badge and
   a popover (newest 20), marks items read and follows the link. Poll every 30 s with a random start offset, back-off on
   errors, paused while the tab is hidden, one shared poll. CSP-safe: textContent only, never innerHTML. */
(function () {
  const host = document.getElementById("opBell");
  if (!host) return;
  const SVG = "http://www.w3.org/2000/svg";
  const INTERVAL = 30000;
  const csrf = () => { const m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ""; };
  const baseTitle = document.title;

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (k === "hidden") n.hidden = !!v;
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  function icon(d) {
    const svg = document.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(k, v);
    const p = document.createElementNS(SVG, "path");
    p.setAttribute("d", d);
    svg.append(p);
    return svg;
  }
  const BELL = "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0";

  function ago(iso) {
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(String(iso || ""));
    if (!m) return "";
    const then = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]);
    const d = new Date(), now = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes());
    const mins = Math.max(0, Math.round((now - then) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    if (mins < 1440) return `${Math.floor(mins / 60)} h ago`;
    return `${Math.floor(mins / 1440)} d ago`;
  }

  const badge = el("span", { class: "op-bell-badge", hidden: true, "aria-hidden": "true" });
  const btn = el("button", { type: "button", class: "op-bell", "aria-label": "Notifications", "aria-haspopup": "true",
    "aria-expanded": "false", onclick: toggle }, icon(BELL), badge);
  const srCount = el("span", { class: "sr-only" });
  const list = el("ul", { class: "op-bell-list" });
  const markAll = el("button", { type: "button", class: "link", onclick: readAll }, "Mark all read");
  const pop = el("div", { class: "op-bell-pop", role: "dialog", "aria-label": "Notifications", hidden: true },
    el("div", { class: "op-bell-head" }, el("strong", {}, "Notifications"), markAll), list);
  host.append(btn, srCount, pop);

  const state = { items: [], unread: 0, ts: "", open: false, fails: 0, timer: null };

  function render() {
    badge.hidden = state.unread <= 0;
    badge.textContent = state.unread > 99 ? "99+" : String(state.unread);
    srCount.textContent = state.unread ? `${state.unread} unread notifications` : "";
    document.title = state.unread ? `(${state.unread}) ${baseTitle}` : baseTitle;
    markAll.hidden = !state.items.some((i) => !i.read);
    if (!state.items.length) {
      list.replaceChildren(el("li", {}, el("div", { class: "op-bell-empty" }, "You are all caught up.")));
      return;
    }
    list.replaceChildren(...state.items.slice(0, 20).map((it) => el("li", {},
      el("button", { type: "button", class: "op-bell-item" + (it.read ? "" : " unread"), onclick: () => openItem(it) },
        el("span", { class: "op-bell-dot", "aria-hidden": "true" }),
        el("span", { class: "op-bell-it-main" },
          el("span", { class: "op-bell-it-title", dir: "auto" }, it.title || "Notification"),
          it.text ? el("span", { class: "op-bell-it-text", dir: "auto" }, it.text) : null,
          el("span", { class: "op-bell-it-time" }, ago(it.ts)))))));
  }

  async function poll() {
    if (document.hidden) return;
    const since = state.ts ? `?since=${encodeURIComponent(state.ts)}` : "";
    try {
      const r = await fetch(`/api/ops/notifications${since}`, { credentials: "same-origin" });
      if (r.status === 401) return;                    // signed out: stop quietly
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "failed");
      state.fails = 0;
      const was = state.unread;
      state.unread = data.unread || 0;
      state.ts = data.ts || state.ts;
      if (!data.unchanged && Array.isArray(data.items)) state.items = data.items;
      render();
      if (state.unread > was && !state.open) {         // a new one arrived while the popover is closed: a gentle ring
        btn.classList.remove("has-unread");
        void btn.offsetWidth;                          // restart the animation
        btn.classList.add("has-unread");
      }
    } catch (e) {
      state.fails += 1;
    }
    schedule();
  }
  function schedule() {
    clearTimeout(state.timer);
    if (document.hidden) return;
    const factor = Math.min(8, 2 ** state.fails);
    state.timer = setTimeout(poll, INTERVAL * factor * (0.9 + Math.random() * 0.3));
  }

  async function readIds(ids) {
    try {
      const r = await fetch("/api/ops/notifications/read", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: JSON.stringify({ ids }) });
      const data = await r.json().catch(() => ({}));
      if (r.ok) { state.unread = data.unread != null ? data.unread : state.unread; state.ts = data.ts || state.ts; }
    } catch (e) { /* the next poll corrects the count */ }
  }
  function openItem(it) {
    if (!it.read) { it.read = true; state.unread = Math.max(0, state.unread - 1); readIds([it.id]); }
    render();
    close();
    if (it.link) location.assign(it.link);
  }
  async function readAll() {
    state.items.forEach((i) => { i.read = true; });
    state.unread = 0;
    render();
    await readIds("all");
  }

  function toggle() { state.open ? close() : open(); }
  function open() {
    state.open = true;
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    btn.classList.remove("has-unread");
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    const first = list.querySelector(".op-bell-item");
    if (first) first.focus();
  }
  function close() {
    if (!state.open) return;
    state.open = false;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    btn.focus();
  }
  function outside(e) { if (!host.contains(e.target)) close(); }
  function onKey(e) { if (e.key === "Escape") { e.preventDefault(); close(); } }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(state.timer); state.timer = setTimeout(poll, 400 + Math.random() * 800); } });
  state.timer = setTimeout(poll, 300 + Math.random() * 900);     // random start offset so 60 tabs do not poll together
})();
