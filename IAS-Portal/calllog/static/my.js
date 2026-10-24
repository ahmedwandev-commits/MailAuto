"use strict";
/* My work (/my): everything that belongs to the signed-in employee under their own user, plus the shared "Team status"
   strip (docs/HUB-DESIGN.md sections 3, 3.1, 3.2, brief docs/handoff/briefs/page-my.md).

   Reads GET /api/ops/my every 30 s through OPS.loader (paused while the tab is hidden). An administrator or manager can
   open somebody's page read-only with /my?as=<id> (the shell adds data-view-as and a banner); the page then sends ?as and
   offers no write. An employee only ever sees their own rows and their team(s)' e-mails, never an "Unclassified" one.
   Everything is built with CL.h / textContent, Arabic carries dir="auto", there is no innerHTML or inline style. */
(function () {
  const { h, $, api, toast, bidi, fmtNum } = CL;
  const SELF = document.currentScript ? document.currentScript.src : "";
  const page = $("#my-page");
  const VIEW_AS = page.dataset.viewAs || "";
  const READ_ONLY = !!VIEW_AS;
  const TICK_MS = 15000;

  const CALL_STATUSES = [["resolved", "Resolved"], ["transferred", "Transferred"], ["ticket_inquiry", "Ticket inquiry"]];

  const state = { data: null, serverTime: "" };
  const el = {};
  let strip = null;
  let skew = 0;

  // ------------------------------------------------------------------ helpers
  const serverNow = () => Date.now() + skew;
  function noteServerTime(iso) {
    const t = OPS.parseNaive(iso);
    if (!Number.isNaN(t)) { skew = t - Date.now(); state.serverTime = iso; }
  }
  function liveMinutes(iso, fallback) {
    const t = OPS.parseNaive(iso);
    if (Number.isNaN(t)) return fallback == null ? null : fallback;
    return Math.max(0, Math.floor((serverNow() - t) / 60000));
  }

  function ensureStyles() {
    if (document.querySelector('link[href*="ops-my.css"]')) return Promise.resolve();
    const href = SELF ? new URL("ops-my.css", SELF).href : "/static/ops-my.css";
    return new Promise((done) => {
      const link = h("link", { rel: "stylesheet", href });
      link.addEventListener("load", done);
      link.addEventListener("error", done);
      setTimeout(done, 2000);
      document.head.append(link);
    });
  }

  // ------------------------------------------------------------------ my calls
  function callsCard(c) {
    const today = c.today, yest = c.yesterday, week = c.last7;
    const shared = (state.data.shared && state.data.shared.calls) || {};
    const shareTotal = shared.total || 0, where = shared.scope === "teams" ? "on the team" : "in all teams";
    const hours = c.per_hour_today || [];
    const active = hours.filter((b) => b.total > 0);
    const lo = active.length ? Math.max(0, active[0].key - 1) : 8, hi = active.length ? active[active.length - 1].key : 9;
    const series = hours.filter((b) => b.key >= lo && b.key <= hi).map((b) => b.total);
    const dot = (key, label) => h("div", { class: "op-fact" }, h("dt", {}, h("span", { class: "status-chip" }, h("i", { class: `dot s-${key}` }), label)), h("dd", {}, fmtNum(today[key] || 0)));
    return OPS.card("My calls", { hint: shareTotal ? `${fmtNum(today.total)} of ${fmtNum(shareTotal)} ${where} today` : "" },
      h("div", { class: "my-tiles" },
        OPS.kpi({ label: "Today", value: fmtNum(today.total), sub: "calls logged", icon: "phone", hero: true }),
        OPS.kpi({ label: "Yesterday", value: fmtNum(yest.total), sub: "whole day" }),
        OPS.kpi({ label: "Last 7 days", value: fmtNum(week.total), sub: "including today" })),
      h("div", { class: "op-viz-row" },                  // today's calls by result, next to the per-hour line
        h("dl", { class: "op-facts op-facts-row" }, CALL_STATUSES.map(([key, label]) => dot(key, label))),
        series.length > 1 ? h("div", { class: "op-viz" }, OPS.sparkline(series, { width: 260, height: 44, label: "My calls per hour today" }),
          h("span", { class: "hint" }, `per hour, ${String(lo).padStart(2, "0")}:00 – now`)) : null));
  }

  // ------------------------------------------------------------------ e-mail tables
  function mailRow(item, mine) {
    const when = item.first_reply_time || item.received_time;
    return h("tr", {},
      h("td", {}, OPS.statusChip(item.sla_status || item.status)),
      h("td", { dir: "auto" }, bidi(item.subject || "(no subject)")),
      mine ? h("td", { dir: "auto" }, bidi(item.customer_name || "—")) : null,
      h("td", { dir: "auto" }, bidi(item.team || "—")),
      h("td", { class: "nowrap" }, item.waiting || (item.response_minutes != null ? OPS.fmtDuration(item.response_minutes) : OPS.fmtWhen(when, state.serverTime))));
  }
  function mailTable(items, mine, caption) {
    return h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("caption", { class: "sr-only" }, caption),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "State"), h("th", { scope: "col" }, "Subject"),
        mine ? h("th", { scope: "col" }, "Customer") : null, h("th", { scope: "col" }, "Team"), h("th", { scope: "col" }, mine ? "Waiting / reply" : "Waiting"))),
      h("tbody", {}, items.map((i) => mailRow(i, mine)))));
  }

  // ------------------------------------------------------------------ Feature B: open / download / done (my mails)
  const myTbl = {};        // persistent OPS.table instances so the pager keeps its page across the 30 s polls

  function openMail(item) {
    OPS.mailViewer(item.message_id, { onChange: () => loader.refresh() });
  }
  async function doneItem(item) {
    const note = h("textarea", { rows: "2", maxlength: "300", dir: "auto", placeholder: "Optional note for the manager" });
    await OPS.modal({
      title: "Mark as done", submitLabel: "Mark done",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(item.subject || "(no subject)")),
        h("label", { class: "my-field" }, h("span", {}, "Note (optional)"), note)),
      onSubmit: async () => {
        await api("POST", "/api/ops/email/done", { message_id: item.message_id, note: note.value.trim() });
        toast("Marked as done.");
        await loader.refresh();
      },
    });
  }
  async function reopenItem(item) {
    const ok = await CL.confirmDialog({ title: "Reopen the e-mail",
      message: `Reopen "${item.subject || "(no subject)"}"?`, confirmLabel: "Reopen", danger: false });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/email/done?message_id=${encodeURIComponent(item.message_id)}`);
      toast("Reopened.");
      await loader.refresh();
    } catch (e) { toast(e.message, "error"); }
  }

  async function changeTeamItem(item) {
    const all = (state.data && state.data.all_teams) || [];
    const select = h("select", { "aria-label": "Team" }, all.map((t) => h("option", { value: t, selected: t === item.team }, t)));
    await OPS.modal({
      title: "Move to another team", submitLabel: "Move",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, `Move "${item.subject || "(no subject)"}" to the team that should handle it.`),
        h("label", { class: "my-field" }, h("span", {}, "Team"), select)),
      onSubmit: async () => {
        if (!select.value || select.value === item.team) throw new Error("Pick a different team.");
        await api("POST", "/api/ops/email/team", { message_id: item.message_id, team: select.value });
        toast(`Moved to ${select.value}.`);
        await loader.refresh();
      },
    });
  }

  // the assignee's own actions: "no reply needed" (with a reason) and handing the mail to a teammate
  async function noActionItem(item) {
    const reasons = (state.data && state.data.no_action_reasons) || [];
    const select = h("select", { "aria-label": "Reason", required: true }, h("option", { value: "" }, "Choose a reason…"),
      reasons.map((r) => h("option", { value: r }, r)));
    const note = h("textarea", { rows: "2", maxlength: "300", dir: "auto", placeholder: "Note (needed for “Other”)" });
    await OPS.modal({
      title: "No reply needed", submitLabel: "Mark no reply needed",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(item.subject || "(no subject)")),
        h("label", { class: "my-field" }, h("span", {}, "Why"), select),
        h("label", { class: "my-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        if (!select.value) throw new Error("Choose why no reply is needed.");
        if (select.value === "Other" && note.value.trim().length < 3) throw new Error("Say in a few words why no reply is needed.");
        await api("POST", "/api/ops/my/email/no-action", { message_id: item.message_id, reason: select.value, note: note.value.trim() });
        toast("Marked as needing no reply.");
        await loader.refresh();
      },
    });
  }
  async function undoNoActionItem(item) {
    const ok = await CL.confirmDialog({ title: "Undo “no reply needed”",
      message: `Show "${item.subject || "(no subject)"}" as waiting for a reply again?`, confirmLabel: "Undo", danger: false });
    if (!ok) return;
    try {
      await api("DELETE", `/api/ops/my/email/no-action?message_id=${encodeURIComponent(item.message_id)}`);
      toast("It is waiting for a reply again.");
      await loader.refresh();
    } catch (e) { toast(e.message, "error"); }
  }
  async function reassignItem(item) {
    let mates;
    try { mates = (await api("GET", `/api/ops/my/email/teammates?message_id=${encodeURIComponent(item.message_id)}`)).teammates || []; }
    catch (e) { toast(e.message, "error"); return; }
    if (!mates.length) { toast("No active teammate to hand it to.", "error"); return; }
    const select = h("select", { "aria-label": "Teammate", required: true }, h("option", { value: "" }, "Choose a teammate…"),
      mates.map((m) => h("option", { value: m.user_id }, `${m.name}${(m.teams || []).length ? ` (${m.teams.join(", ")})` : ""}`)));
    const note = h("textarea", { rows: "2", maxlength: "300", dir: "auto", placeholder: "Optional note for your teammate" });
    await OPS.modal({
      title: "Hand to a teammate", submitLabel: "Hand over",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(item.subject || "(no subject)")),
        h("label", { class: "my-field" }, h("span", {}, "Teammate"), select),
        h("label", { class: "my-field" }, h("span", {}, "Note"), note)),
      onSubmit: async () => {
        if (!select.value) throw new Error("Choose a teammate.");
        const res = await api("POST", "/api/ops/my/email/reassign", { message_id: item.message_id, to_user_id: select.value, note: note.value.trim() });
        toast(`Handed to ${res.to}.`);
        await loader.refresh();
      },
    });
  }

  function assignedActions(r) {
    const btns = [h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); openMail(r); } }, OPS.icon("mail"), "Open")];
    if (!READ_ONLY) {
      const can = r.can || {};
      btns.push(r.assignment_done
        ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reopenItem(r); } }, "Reopen")
        : h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); doneItem(r); } }, OPS.icon("check"), "Done"));
      if (can.no_action) btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); noActionItem(r); } }, "No action"));
      if (can.undo_no_action) btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); undoNoActionItem(r); } }, "Undo no action"));
      if (can.reassign) btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); reassignItem(r); } }, OPS.icon("user"), "Reassign"));
      if ((state.data && state.data.all_teams || []).length)
        btns.push(h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: (e) => { e.stopPropagation(); changeTeamItem(r); } }, "Team"));
    }
    return h("div", { class: "op-row-actions" }, btns);
  }
  function stateCell(r) {
    return h("div", { class: "em-assignee" }, OPS.statusChip(r.sla_status || r.status), r.assignment_done ? OPS.chip("Done", "ok", "check") : null,
      r.no_action_reason ? h("span", { class: "hint", dir: "auto", title: r.no_action_reason }, (r.no_action_reason.split(" - ")[1] || "").slice(0, 40)) : null);
  }
  const assignedColumns = () => [
    { key: "state", label: "State", render: stateCell, text: (r) => (r.sla_status || r.status) + (r.assignment_done ? " done" : "") },
    { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
    { key: "customer_name", label: "Customer", render: (r) => bidi(r.customer_name || "—"), text: (r) => r.customer_name },
    { key: "team", label: "Team", render: (r) => bidi(r.team || "—"), text: (r) => r.team },
    { key: "waiting", label: "Waiting", render: (r) => r.waiting || OPS.fmtWhen(r.received_time, state.serverTime), text: (r) => r.waiting },
    { key: "assigned_by", label: "Assigned by", render: (r) => (r.assigned_by ? bidi(r.assigned_by) : h("span", { class: "hint" }, "—")), text: (r) => r.assigned_by },
    { key: "actions", label: "", render: assignedActions },
  ];

  function emailCard(e) {
    if (!e.configured) {
      return OPS.card("My e-mails", {}, OPS.emptyState({ title: "No e-mail address on your account", icon: "mail",
        text: "Ask your administrator to add your e-mail address to your account so your replies and assignments show here." }));
    }
    if (!e.available) return OPS.card("My e-mails", {}, OPS.emptyState({ title: "No e-mail scan yet", icon: "mail", text: "Your e-mails will appear after the first scan." }));
    const a = e.assigned, r = e.replied;
    const parts = [h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Assigned to me", value: fmtNum(a.pending), sub: `${fmtNum(a.done)} done`, tone: a.pending ? "warn" : null, icon: "mail" }),
      OPS.kpi({ label: "Replied today", value: fmtNum(r.count), sub: r.average_first_reply_minutes != null ? `avg ${OPS.fmtDuration(r.average_first_reply_minutes)}` : "", icon: "check" }),
      OPS.kpi({ label: "On time / late", value: `${fmtNum(r.sla_met)} / ${fmtNum(r.sla_breached)}`, tone: r.sla_breached ? "danger" : "ok", icon: "shield" }))];
    if (a.items.length) {
      if (!myTbl.assigned) myTbl.assigned = OPS.table({ search: false, copy: false, pageSize: 10, pageSizes: [10, 25, 50],
        caption: "E-mails assigned to me", rowKey: (r2) => r2.message_id, onRowClick: openMail, columns: assignedColumns(), rows: a.items });
      else myTbl.assigned.setRows(a.items);
      parts.push(h("div", {}, h("h3", { class: "my-sub" }, "Assigned to me — open first"), myTbl.assigned.el));
    }
    if (r.items.length) parts.push(h("div", {}, h("h3", { class: "my-sub" }, "Replied today"), mailTable(r.items, true, "E-mails I replied to today")));
    if (!a.items.length && !r.items.length) parts.push(OPS.emptyState({ title: "Nothing assigned to you yet", icon: "check", text: "E-mails assigned to you, and the ones you reply to, show here." }));
    return OPS.card("My e-mails", { hint: "open a mail to read it, mark it done or “no reply needed”, or hand it to a teammate" }, parts);
  }

  function teamCard(e) {
    if (e.team_emails_hint === "no_team") {
      return OPS.card("My team's e-mails", {}, OPS.emptyState({ title: "You are not in a team yet", icon: "info",
        text: "Ask your administrator to add you to a team to see your team's e-mails here." }));
    }
    const s = e.team_summary || {};
    const items = e.team_emails || [];
    const summary = h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Pending", value: fmtNum(s.pending || 0), tone: s.pending ? "warn" : null, icon: "clock" }),
      OPS.kpi({ label: "Replied", value: fmtNum(s.replied || 0), icon: "check" }),
      OPS.kpi({ label: "Past SLA", value: fmtNum(s.sla_breached || 0), tone: s.sla_breached ? "danger" : null, icon: "alert" }),
      OPS.kpi({ label: "Reply rate", value: OPS.fmtPct(s.reply_rate), icon: "activity" }));
    let list;
    if (items.length) {
      if (!myTbl.team) myTbl.team = OPS.table({ search: false, copy: false, pageSize: 10, pageSizes: [10, 25, 50], caption: "My team's e-mails",
        columns: [
          { key: "state", label: "State", render: (r) => OPS.statusChip(r.sla_status || r.status), text: (r) => r.sla_status || r.status },
          { key: "subject", label: "Subject", render: (r) => bidi(r.subject || "(no subject)"), text: (r) => r.subject },
          { key: "team", label: "Team", render: (r) => bidi(r.team || "—"), text: (r) => r.team },
          { key: "waiting", label: "Waiting", render: (r) => r.current_waiting_time || OPS.fmtWhen(r.received_time, state.serverTime), text: (r) => r.current_waiting_time },
        ], rows: items });
      else myTbl.team.setRows(items);
      list = myTbl.team.el;
    } else {
      list = OPS.emptyState({ title: "Nothing pending for your team", icon: "check", text: "Your team's open e-mails show here." });
    }
    return OPS.card("My team's e-mails", { hint: (s.teams || []).join(", ") }, summary, list,
      s.more ? h("p", { class: "hint" }, `and ${fmtNum(s.more)} more not shown`) : null);
  }

  // ------------------------------------------------------------------ my quality reviews
  async function appeal(review) {
    const note = h("textarea", { rows: "3", maxlength: "500", required: true, dir: "auto", "aria-label": "Why do you disagree?" });
    await OPS.modal({
      title: "Appeal this rating", submitLabel: "Send appeal",
      body: h("div", {}, h("p", { class: "muted", dir: "auto" }, bidi(review.subject || "")),
        h("label", { class: "my-field" }, h("span", {}, "Why do you disagree?"), note)),
      onSubmit: async () => {
        if (!note.value.trim()) throw new Error("Please explain why you disagree.");
        await api("POST", "/api/ops/email/quality/appeal", { message_id: review.message_id, appeal: note.value.trim() });
        toast("Appeal sent.");
        await loader.refresh();
      },
    });
  }

  function qualityCard(q) {
    if (!q.configured || !q.reviews.length) {
      return OPS.card("My quality reviews", {}, OPS.emptyState({ title: "No reviews yet", icon: "activity",
        text: "When a manager reviews one of your replies, it shows here with its rating." }));
    }
    const tone = (r) => (r === "Needs Improvement" ? "warn" : r === "Excellent" ? "ok" : "info");
    const rows = q.reviews.map((rev) => h("tr", {},
      h("td", {}, rev.rating ? OPS.chip(rev.rating, tone(rev.rating)) : h("span", { class: "hint" }, "—"),
        rev.rating_withdrawn ? h("div", { class: "hint" }, "withdrawn") : null),
      h("td", { dir: "auto" }, bidi(rev.subject || "(no subject)"),
        rev.note ? h("div", { class: "hint", dir: "auto" }, bidi(rev.note)) : null),
      h("td", { dir: "auto" }, bidi(rev.reasons || "")),
      h("td", { dir: "auto" }, rev.reviewed_by ? bidi(rev.reviewed_by) : h("span", { class: "hint" }, "—"),
        rev.reviewed_at ? h("div", { class: "hint" }, OPS.fmtWhen(rev.reviewed_at, null)) : null),
      h("td", {}, rev.appeal_decision ? OPS.chip(`Appeal: ${rev.appeal_decision}`, "info")
        : rev.appeal_open ? OPS.chip("Appeal pending", "info")
          : (rev.can_appeal && !READ_ONLY ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => appeal(rev) }, "Appeal") : h("span", { class: "hint" }, "—")))));
    return OPS.card("My quality reviews", { hint: `${fmtNum(q.counts.reviewed || 0)} reviewed` },
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("caption", { class: "sr-only" }, "My quality reviews"),
        h("thead", {}, h("tr", {}, ["Rating", "Subject", "Reasons", "Rated by", ""].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, rows))));
  }

  // ------------------------------------------------------------------ my security requests
  function slaCard(s) {
    if (!s.configured) {
      return OPS.card("My tickets", {}, OPS.emptyState({ title: "No security name on your account", icon: "shield",
        text: "Ask your administrator to set your security name (as it appears in the portal) so your requests show here." }));
    }
    if (!s.available) return OPS.card("My tickets", {}, OPS.emptyState({ title: "No ticketing scan yet", icon: "shield", text: "Your requests will appear after the first scan." }));
    const t = s.totals;
    const tiles = h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Open", value: fmtNum(t.open), icon: "shield", hero: true }),
      OPS.kpi({ label: "Reminder", value: fmtNum(t.reminder), tone: t.reminder ? "warn" : null, icon: "clock" }),
      OPS.kpi({ label: "Breached", value: fmtNum(t.breached), tone: t.breached ? "danger" : null, icon: "alert" }));
    if (!s.requests.length) return OPS.card("My tickets", {}, tiles, OPS.emptyState({ title: "Nothing waiting for you", icon: "check", text: "No ticket is open under your name." }));
    const rows = s.requests.map((r) => {
      const fields = r.fields || {};
      return h("tr", { "data-sent": r.sent_at || "" },
        h("td", {}, OPS.statusChip(r.status)),
        h("td", { class: "nowrap my-age", "data-age": "" }, OPS.fmtDuration(liveMinutes(r.sent_at, r.age_minutes))),
        ...["اسم الموظف", "النظام", "نوع الطلب"].map((k) => h("td", { dir: "auto" }, bidi(fields[k] || "—"))));
    });
    return OPS.card("My tickets", {}, tiles,
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("caption", { class: "sr-only" }, "My tickets"),
        h("thead", {}, h("tr", {}, ["State", "Age", "Employee", "System", "Request type"].map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, rows))));
  }

  // ------------------------------------------------------------------ my SLA (the portal's status history, by my number)
  function slaHistoryCard(s) {
    if (!s || !s.available) return null;
    const t = s.today || {}, N = s.now || {}, sla = s.sla_minutes || 180;
    const fmtMin = (m) => (m == null ? "—" : OPS.fmtDuration(m));
    const tiles = h("div", { class: "my-tiles" },
      OPS.kpi({ label: "Executed today", value: fmtNum(t.executed || 0), sub: "«تم التنفيذ»", tone: t.executed ? "ok" : null, icon: "check", hero: true }),
      OPS.kpi({ label: "Not executed", value: fmtNum(t.not_executed || 0), sub: "«لم يتم التنفيذ»", icon: "alert" }),
      OPS.kpi({ label: "My median time", value: fmtMin(t.median_minutes), sub: "assignment → execution, today", icon: "clock" }),
      OPS.kpi({ label: "Open with me", value: fmtNum(N.open || 0), sub: N.breached ? `${fmtNum(N.breached)} past ${OPS.fmtDuration(sla)}` : "right now", tone: N.breached ? "danger" : null, icon: "shield" }),
      t.confirmed_by_me ? OPS.kpi({ label: "Confirmed by me", value: fmtNum(t.confirmed_by_me), sub: "«تم التعزيز» today", icon: "check" }) : null);
    const days = (s.days || []).slice().reverse().map((d) => h("tr", {},
      h("td", { class: "nowrap" }, new Date(`${d.date}T12:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })),
      h("td", { class: "num" }, fmtNum(d.executed || 0)), h("td", { class: "num" }, fmtNum(d.not_executed || 0)),
      h("td", { class: "num" }, fmtMin(d.median_minutes))));
    const open = (s.open_now || []).map((o) => h("tr", {},
      h("td", {}, o.id || "—"), h("td", { dir: "auto" }, bidi(o.system || "—")), h("td", { dir: "auto" }, bidi(o.requester || "—")),
      h("td", { class: "nowrap" }, o.minutes == null ? h("span", { class: "hint" }, "not read yet") : o.breached ? OPS.chip(fmtMin(o.minutes), "danger") : fmtMin(o.minutes)),
      h("td", {}, o.on_hold ? OPS.chip("on hold", "warn") : "")));
    const table = (caption, heads, body) => h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("caption", { class: "sr-only" }, caption), h("thead", {}, h("tr", {}, heads.map((c) => h("th", { scope: "col" }, c)))), h("tbody", {}, body)));
    return OPS.card("My SLA", { hint: `from the portal's status history${(s.numbers || []).length ? ` · employee no. ${s.numbers.join(", ")}` : ""}` }, tiles,
      open.length ? table("Requests open with me", ["Request", "System", "Requester", "Waiting", ""], open) : null,
      table("My last 7 days", ["Day", "Executed", "Not executed", "Median time"], days));
  }

  function tickAges() {
    for (const cell of page.querySelectorAll(".my-age[data-age]")) {
      const tr = cell.closest("tr");
      if (tr && tr.dataset.sent) cell.textContent = OPS.fmtDuration(liveMinutes(tr.dataset.sent, null));
    }
  }

  // ------------------------------------------------------------------ render / loop
  function render(d) {
    state.data = d;
    noteServerTime(d.server_time);
    if (strip) strip.update(d.shared);
    const name = (d.user && d.user.name) || "there";
    el.greeting.textContent = READ_ONLY ? `${name}'s work` : `Hello, ${name}`;
    el.date.textContent = d.date ? new Date(`${d.date}T12:00:00`).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "";
    const col = d.shared && d.shared.collector;
    el.freshness.textContent = col && col.last_update
      ? `${col.online ? "Live · data from" : "Data from"} ${OPS.fmtClock(col.last_update)}`
      : "";
    el.freshness.hidden = !(col && col.last_update);
    el.body.replaceChildren(...[callsCard(d.calls), emailCard(d.email), teamCard(d.email), qualityCard(d.quality), slaCard(d.sla),
      slaHistoryCard(d.sla_history)].filter(Boolean));
    if (state.openMailId) { const id = state.openMailId; state.openMailId = ""; OPS.mailViewer(id, { onChange: () => loader.refresh() }); }
  }
  function showError(e) {
    if (!state.data) toast(e.network ? "Cannot reach the server - retrying." : e.message, "error");
  }

  const loader = OPS.loader({ url: () => `/api/ops/my${VIEW_AS ? `?as=${encodeURIComponent(VIEW_AS)}` : ""}`,
    interval: 30000, since: false, onData: render, onError: showError });

  function build() {
    const banner = page.querySelector(".op-viewing");             // server-rendered read-only banner (name + Back) - keep it
    el.greeting = h("h1", {}, READ_ONLY ? "Work" : "My work");
    el.date = h("p", { class: "hint" });
    el.freshness = h("p", { class: "hint my-fresh", role: "status", hidden: true });
    el.stripHost = h("section", { id: "myStrip", class: "op-strip-host" });
    el.body = h("div", { class: "my-stack" }, h("p", { class: "empty" }, "Loading your work…"));
    const logCall = READ_ONLY ? null : h("a", { class: "btn btn-primary", href: "/dashboard" }, OPS.icon("phone"), "Log a call");
    const titlebar = h("div", { class: "op-titlebar" }, h("div", {}, el.greeting, el.date, el.freshness), h("span", { class: "grow" }), logCall);
    page.replaceChildren(...(banner ? [banner, titlebar] : [titlebar]), el.stripHost, el.body);
    strip = OPS.sharedStrip(el.stripHost, { poll: false, links: false });
  }

  async function start() {
    await ensureStyles();
    state.openMailId = new URLSearchParams(location.search).get("mail") || "";   // notification deep link: /my?mail=<id>
    build();
    loader.start();
    setInterval(() => { if (!document.hidden) tickAges(); }, TICK_MS);
  }
  start();
})();
