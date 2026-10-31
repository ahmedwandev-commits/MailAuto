/* Workforce › Team schedule: drag a person onto Morning / Evening (Night / Day off: the administrative affairs only) of a
   working day, or click a person and then a shift (phones, keyboard). A person has one place a day. Save sends the month to
   the server (which checks everything again). No inline script (strict CSP): the data comes from <script type=application/json>. */
(function () {
  "use strict";
  const root = document.getElementById("tp");
  const raw = document.getElementById("tpData");
  if (!root || !raw) return;
  const data = JSON.parse(raw.textContent || "{}");
  const EDIT = root.dataset.edit === "1";
  const AFFAIRS = root.dataset.affairs === "1";
  // D = Normal day 08:00-16:00; CD = comp day: the manager, any number of people (owner). Leaves come from the Leave page.
  const CODES = ["M", "D", "E", "N", "OFF", "CD"];
  const MINE = AFFAIRS ? CODES : ["M", "D", "E", "CD"];
  const WORK = ["M", "D", "E", "N"];                  // what the day counter beside a name counts
  const names = {};
  const managers = new Set();                // a manager: only the morning shift is paid the shift allowance (owner)
  (data.employees || []).forEach((e) => { names[e.id] = e.name; if (e.manager) managers.add(e.id); });
  const leaves = data.leaves || {};
  const state = document.getElementById("tpState");
  let cells = JSON.parse(JSON.stringify(data.cells || {}));
  let picked = null;
  let dirty = false;

  const short = (n) => String(n || "").split(/\s+/).slice(0, 3).join(" ");
  const day = (d) => {
    cells[d] = cells[d] || {};
    CODES.forEach((c) => { cells[d][c] = cells[d][c] || []; });
    return cells[d];
  };
  const say = (text) => { if (state) state.textContent = text; };
  const editable = (code) => EDIT && MINE.includes(code);
  const onLeave = (id, d) => leaves[`${id}|${d}`];

  function place(id, d, code) {
    if (!editable(code) || data.holidays[d]) return false;
    if (onLeave(id, d)) { say(`${short(names[id])} is on leave on ${d}.`); return false; }
    const cell = day(d);
    for (const c of CODES) {
      if (cell[c].includes(id) && !editable(c)) { say(`${short(names[id])} already has a ${c === "N" ? "night shift" : "day off"} on ${d}.`); return false; }
    }
    CODES.forEach((c) => { cell[c] = (cell[c] || []).filter((x) => x !== id); });
    cell[code].push(id);
    dirty = true;
    return true;
  }

  function take(id, d, code) {
    if (!editable(code)) return;
    day(d)[code] = day(d)[code].filter((x) => x !== id);
    dirty = true;
  }

  function chip(id, d, code) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tp-put";
    b.dataset.id = id;
    const who = document.createElement("span");
    who.setAttribute("translate", "no");                   // a name is never translated; the tag beside it is
    who.textContent = short(names[id] || id);
    b.append(who);
    if (managers.has(id) && (code === "E" || code === "N" || code === "D")) {
      b.classList.add("tp-noallow");
      const tag = document.createElement("span");
      tag.textContent = " · no allowance";
      b.append(tag);
    }
    if (editable(code)) {
      b.draggable = true;
      b.title = "Click to take off";
      b.addEventListener("click", (ev) => { ev.stopPropagation(); take(id, d, code); render(); });
      b.addEventListener("dragstart", (ev) => { ev.dataTransfer.setData("text/plain", JSON.stringify({ id, d, code })); });
    } else {
      b.disabled = true;
    }
    return b;
  }

  function render() {
    const count = {};
    document.querySelectorAll(".tp-day").forEach((sec) => {
      const d = sec.dataset.date;
      const cell = cells[d] || {};
      sec.querySelectorAll(".tp-slot").forEach((slot) => {
        const code = slot.dataset.code;
        const box = slot.querySelector(".tp-in");
        box.replaceChildren(...(cell[code] || []).filter((id) => names[id]).map((id) => chip(id, d, code)));
      });
      WORK.forEach((c) => (cell[c] || []).forEach((id) => { count[id] = (count[id] || 0) + 1; }));
      let note = sec.querySelector(".tp-leave");
      const away = Object.keys(leaves).filter((k) => k.endsWith(`|${d}`)).map((k) => short(names[k.split("|")[0]])).filter(Boolean);
      if (away.length) {
        if (!note) { note = document.createElement("p"); note.className = "tp-leave hint small"; sec.appendChild(note); }
        note.textContent = `On leave: ${away.join(", ")}`;
      } else if (note) note.remove();
    });
    document.querySelectorAll(".tp-count").forEach((el) => { el.textContent = count[el.dataset.count] || 0; });
    if (dirty) say("Not saved yet");
  }

  // ---------------------------------------------------------------- drag and drop, click to place
  document.querySelectorAll("#tpList .tp-chip").forEach((el) => {
    const id = Number(el.dataset.id);
    el.addEventListener("dragstart", (ev) => { ev.dataTransfer.setData("text/plain", JSON.stringify({ id })); });
    el.addEventListener("click", () => {
      if (!EDIT) return;
      picked = picked === id ? null : id;
      document.querySelectorAll("#tpList .tp-chip").forEach((c) => c.classList.toggle("sel", Number(c.dataset.id) === picked));
      say(picked ? `${short(names[id])}: now click a shift` : "");
    });
  });
  document.querySelectorAll(".tp-slot").forEach((slot) => {
    const code = slot.dataset.code;
    const d = slot.closest(".tp-day").dataset.date;
    if (!editable(code)) return;
    slot.tabIndex = 0;
    slot.addEventListener("dragover", (ev) => { ev.preventDefault(); slot.classList.add("over"); });
    slot.addEventListener("dragleave", () => slot.classList.remove("over"));
    slot.addEventListener("drop", (ev) => {
      ev.preventDefault();
      slot.classList.remove("over");
      let info;
      try { info = JSON.parse(ev.dataTransfer.getData("text/plain")); } catch (e) { return; }
      if (!info || !names[info.id]) return;
      if (info.d && info.d !== d && info.code) take(info.id, info.d, info.code);   // moved from another day
      place(info.id, d, code);
      render();
    });
    const drop = () => { if (picked != null && place(picked, d, code)) render(); };
    slot.addEventListener("click", drop);
    slot.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); drop(); } });
  });

  const find = document.getElementById("tpFind");
  if (find) find.addEventListener("input", () => {
    const q = find.value.trim().toLowerCase();
    document.querySelectorAll("#tpList li").forEach((li) => { li.hidden = q && !li.textContent.toLowerCase().includes(q); });
  });

  // ---------------------------------------------------------------- tools
  const days = Array.from(document.querySelectorAll(".tp-day")).map((s) => s.dataset.date);
  const weekday = (d) => new Date(`${d}T12:00:00`).getDay();
  const copy = document.getElementById("tpCopy");
  if (copy) copy.addEventListener("click", () => {
    const first = {};
    for (const d of days) { const w = weekday(d); if (w in first) break; first[w] = d; }
    days.forEach((d) => {
      const src = first[weekday(d)];
      if (!src || src === d || data.holidays[d]) return;
      MINE.forEach((c) => { day(d)[c] = []; });
      MINE.forEach((c) => (day(src)[c] || []).forEach((id) => place(id, d, c)));
    });
    dirty = true;
    render();
  });
  // one day -> the following days of the same week (the week starts on Sunday)
  const weekOf = (d) => { const t = new Date(`${d}T12:00:00`); t.setDate(t.getDate() - t.getDay()); return t.toISOString().slice(0, 10); };
  document.querySelectorAll(".tp-copyday").forEach((btn) => btn.addEventListener("click", () => {
    const src = btn.dataset.date;
    const later = days.filter((d) => d > src && weekOf(d) === weekOf(src) && !data.holidays[d]);
    if (!later.length) { say("This is the last working day of the week."); return; }
    later.forEach((d) => {
      MINE.forEach((c) => { day(d)[c] = []; });
      MINE.forEach((c) => (day(src)[c] || []).forEach((id) => place(id, d, c)));
    });
    dirty = true;
    render();
    say(`Copied to ${later.length} day(s) · not saved yet`);
  }));

  const clear = document.getElementById("tpClear");
  if (clear) clear.addEventListener("click", () => {
    if (!window.confirm("Take everybody off every day of this month?")) return;
    days.forEach((d) => MINE.forEach((c) => { day(d)[c] = []; }));
    dirty = true;
    render();
  });

  async function save() {
    const token = document.querySelector('meta[name="csrf-token"]');
    say("Saving…");
    const r = await fetch(root.dataset.save, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token ? token.content : "" },
      body: JSON.stringify({ month: root.dataset.month, team: root.dataset.team, cells }),
    });
    const ans = await r.json().catch(() => ({}));
    if (!r.ok) { say(ans.error || "Not saved"); return false; }
    dirty = false;
    say(`Saved · ${ans.days} day(s)` + (ans.problems && ans.problems.length ? ` · not placed: ${ans.problems.join("; ")}` : ""));
    return true;
  }
  const saveBtn = document.getElementById("tpSave");
  if (saveBtn) saveBtn.addEventListener("click", () => { save(); });
  const submit = document.getElementById("tpSubmitForm");
  if (submit) submit.addEventListener("submit", async (ev) => {
    if (!dirty) return;
    ev.preventDefault();                       // save the last changes first, then send
    if (await save()) submit.submit();
  });
  window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });

  render();
  dirty = false;
  say("");
})();
