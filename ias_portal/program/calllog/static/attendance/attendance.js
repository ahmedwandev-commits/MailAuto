/* Attendance & Workforce - progressive enhancement. Every page works without this file (forms post normally);
   the script only adds comfort. Same-origin script, no inline handlers (CSP). */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  document.addEventListener("click", function (ev) {
    var cf = ev.target.closest("[data-confirm]");
    if (cf && !window.confirm(cf.getAttribute("data-confirm"))) ev.preventDefault();
    if (ev.target.closest("[data-print]")) window.print();
    if (ev.target.closest("[data-back]")) window.history.back();
    var row = ev.target.closest("tr[data-href]");
    if (row && !ev.target.closest("a,button,input,select,summary,label")) window.location = row.getAttribute("data-href");
  });

  document.addEventListener("change", function (ev) {
    var el = ev.target.closest("[data-autosubmit]");
    if (el && el.form) el.form.submit();
  });

  // ---- live filter + sortable tables
  $$("input[data-table-filter]").forEach(function (inp) {
    var table = document.getElementById(inp.getAttribute("data-table-filter"));
    if (!table) return;
    var counter = document.getElementById(inp.getAttribute("data-count") || "");
    inp.addEventListener("input", function () {
      var q = inp.value.toLowerCase(), shown = 0;
      $$("tbody tr", table).forEach(function (tr) {
        var hide = q && tr.textContent.toLowerCase().indexOf(q) < 0;
        tr.hidden = !!hide;
        if (!hide) shown++;
      });
      if (counter) counter.textContent = shown;
    });
  });
  $$("table.tbl th.sortable").forEach(function (th) {
    th.addEventListener("click", function () {
      var table = th.closest("table"), idx = th.cellIndex, dir = th.getAttribute("data-dir") === "asc" ? "desc" : "asc";
      $$("th.sortable", table).forEach(function (x) { x.removeAttribute("data-dir"); });
      th.setAttribute("data-dir", dir);
      var body = table.tBodies[0], rows = $$("tr", body);
      function val(tr) { var c = tr.cells[idx]; return (c.getAttribute("data-sort") || c.textContent).trim(); }
      rows.sort(function (a, b) {
        var x = val(a), y = val(b), nx = parseFloat(x), ny = parseFloat(y);
        var r = (!isNaN(nx) && !isNaN(ny) && /^[-+]?[\d.]/.test(x) && /^[-+]?[\d.]/.test(y)) ? nx - ny : x.localeCompare(y, undefined, { numeric: true });
        return dir === "asc" ? r : -r;
      });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });

  // ---- dialogs (data-dialog="id" on a button; data-* of the button are copied to [data-fill] fields)
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-dialog]");
    if (b) {
      var dlg = document.getElementById(b.getAttribute("data-dialog"));
      if (!dlg || !dlg.showModal) return;
      $$("[data-fill]", dlg).forEach(function (el) {
        var v = b.getAttribute("data-" + el.getAttribute("data-fill")) || "";
        if (el.tagName === "INPUT") el.value = v; else el.textContent = v;
      });
      $$("[data-fill-select]", dlg).forEach(function (el) {
        var v = b.getAttribute("data-" + el.getAttribute("data-fill-select"));
        if (v) el.value = v;
      });
      var first = $$("[data-tabbtn]", dlg)[0];
      if (first) selectTab(dlg, b.getAttribute("data-tab") || first.getAttribute("data-tabbtn"));
      dlg.showModal();
    }
    if (ev.target.closest("[data-close]")) { var d = ev.target.closest("dialog"); if (d) d.close(); }
    if (ev.target.closest("[data-copy-times]")) {          // "Mark present": the planned shift's start and end
      var ei = $("#cdExpIn"), eo = $("#cdExpOut");
      if (ei && ei.value) $("#cdIn").value = ei.value.slice(0, 5);
      if (eo && eo.value) $("#cdOut").value = eo.value.slice(0, 5);
    }
    var tb = ev.target.closest("[data-tabbtn]");
    if (tb) selectTab(tb.closest("dialog"), tb.getAttribute("data-tabbtn"));
  });
  function selectTab(dlg, name) {
    $$("[data-tabbtn]", dlg).forEach(function (x) { x.setAttribute("aria-selected", x.getAttribute("data-tabbtn") === name ? "true" : "false"); });
    $$("[data-tabpane]", dlg).forEach(function (x) { x.hidden = x.getAttribute("data-tabpane") !== name; });
  }
  $$("dialog.dlg").forEach(function (d) { d.addEventListener("click", function (e) { if (e.target === d) d.close(); }); });

  // ---- weekly planner: colour + change counter
  var plan = $("#weekplan");
  if (plan) {
    var counter = $("#changeCount"), save = $("#saveWeek"), reason = $("#weekReason");
    function recount() {
      var n = 0;
      $$("select.cellsel", plan).forEach(function (s) {
        s.setAttribute("data-code", s.value);
        var changed = s.value !== s.getAttribute("data-orig");
        s.closest("td").classList.toggle("changed", changed);
        if (changed) n++;
      });
      if (counter) counter.textContent = n;
      if (save) save.disabled = n === 0;
      if (reason) reason.required = n > 0;
      var bar = $("#saveBar"); if (bar) bar.hidden = n === 0;
    }
    plan.addEventListener("change", recount);
    recount();
  }

  // ---- name-matching progress on the schedule preview
  var matchTable = $("#matchTable");
  if (matchTable) {
    var skipBox = $("#skipUnresolved"), btn = $("#confirmImport"), left = $("#unresolvedCount"), bar = $("#matchBar");
    var selects = $$("select.match", matchTable);
    function recheck() {
      var open = 0;
      selects.forEach(function (s) {
        var ok = s.value !== "" || s.getAttribute("data-auto") === "1";
        s.closest("tr").classList.toggle("unresolved", !ok);
        if (!ok) open++;
      });
      if (left) left.textContent = open;
      var done = selects.length - open;
      if (bar) bar.setAttribute("width", selects.length ? Math.round(100 * done / selects.length) : 100);
      if (btn && btn.getAttribute("data-blocked") !== "1") btn.disabled = open > 0 && !(skipBox && skipBox.checked);
    }
    matchTable.addEventListener("change", recheck);
    if (skipBox) skipBox.addEventListener("change", recheck);
    recheck();
  }

  // ---- 'exceptions only' filter on the profile day table
  $$('[data-exceptions-only]').forEach(function (cb) {
    cb.addEventListener('change', function () {
      $$('#daytable tbody tr').forEach(function (tr) { tr.hidden = cb.checked && tr.getAttribute('data-ex') === '0'; });
    });
  });

  // ---- upload drop zone
  $$(".dropzone").forEach(function (z) {
    var input = $("input[type=file]", z), label = $(".picked", z);
    ["dragenter", "dragover"].forEach(function (e) { z.addEventListener(e, function (ev) { ev.preventDefault(); z.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (e) { z.addEventListener(e, function () { z.classList.remove("over"); }); });
    z.addEventListener("drop", function (ev) { ev.preventDefault(); if (ev.dataTransfer.files.length && input) { input.files = ev.dataTransfer.files; show(); } });
    if (input) input.addEventListener("change", show);
    function show() { if (label && input.files.length) label.textContent = input.files[0].name + " (" + Math.round(input.files[0].size / 1024) + " KB)"; }
  });
})();
