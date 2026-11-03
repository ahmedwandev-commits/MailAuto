/* Scoring rules page: ready-made sets, row editors instead of typed syntax, and the live "Try it" preview.
   The text boxes (late_tiers, late_steps, categories ...) stay the source of truth for the server: the row editors read them when the page
   opens and write them back on every change, so the form posts exactly what it always did. No inline script or style (strict CSP);
   everything is built with createElement / textContent. */
(function () {
  "use strict";
  var form = document.getElementById("scoringForm");
  if (!form) return;
  form.classList.add("js");

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function input(cls, value, placeholder, label) {
    var i = el("input", cls);
    i.value = value == null ? "" : value;
    if (placeholder) i.placeholder = placeholder;
    if (label) i.setAttribute("aria-label", label);
    i.autocomplete = "off";
    return i;
  }
  function raw(name) { return form.elements[name]; }
  function fire(node) { node.dispatchEvent(new Event("input", { bubbles: true })); }

  // ------------------------------------------------------------------ row editors
  var KINDS = {
    tiers: {
      cols: [["from", "from minute", "num"], ["to", "to minute (empty = and more)", "num"], ["value", "costs %", "dec"]],
      parse: function (text) {
        return split(text).map(function (p) {
          var m = /^\s*(\d+)\s*-\s*(\d*)\s*=\s*([\d.]+)\s*$/.exec(p);
          return m ? [m[1], m[2], m[3]] : null;
        }).filter(Boolean);
      },
      write: function (rows) {
        return rows.filter(function (r) { return r[0] !== "" && r[2] !== ""; })
          .map(function (r) { return r[0] + "-" + r[1] + "=" + r[2]; }).join("; ");
      },
      blank: ["", "", ""], add: "Add a row", lead: ["from", "to", "costs"]
    },
    steps: {
      cols: [["from", "from time no.", "num"], ["to", "to time no. (empty = and more)", "num"], ["extra", "extra %", "dec"], ["factor", "or multiply ×", "dec"]],
      parse: function (text) {
        return split(text).map(function (p) {
          var m = /^\s*(\d+)\s*(?:-\s*(\d*))?\s*=\s*(?:x\s*([\d.]+))?\s*(?:\+\s*([\d.]+))?\s*$/i.exec(p);
          if (!m) return null;
          return [m[1], m[2] === undefined ? m[1] : m[2], m[4] || "", m[3] || ""];
        }).filter(Boolean);
      },
      write: function (rows) {
        return rows.filter(function (r) { return r[0] !== "" && (r[2] !== "" || r[3] !== ""); }).map(function (r) {
          return r[0] + "-" + r[1] + "=" + (r[3] !== "" ? "x" + r[3] : "") + (r[2] !== "" ? "+" + r[2] : "");
        }).join("; ");
      },
      blank: ["", "", "", ""], add: "Add a step", lead: ["from the", "to the", "costs", "or ×"]
    },
    categories: {
      cols: [["name", "category name", "txt"], ["min", "from score", "dec"]],
      parse: function (text) {
        return split(text).map(function (p) {
          var i = p.lastIndexOf("=");
          return i > 0 ? [p.slice(0, i).trim(), p.slice(i + 1).trim()] : null;
        }).filter(Boolean);
      },
      write: function (rows) {
        return rows.filter(function (r) { return r[0] !== "" && r[1] !== ""; }).map(function (r) { return r[0] + "=" + r[1]; }).join("; ");
      },
      blank: ["", ""], add: "Add a category", lead: ["name", "from score"]
    }
  };
  function split(text) { return String(text || "").replace(/\n/g, ";").split(";").map(function (s) { return s.trim(); }).filter(Boolean); }

  function Editor(box) {
    var kind = KINDS[box.getAttribute("data-kind")];
    var target = raw(box.getAttribute("data-target"));
    var list = el("div", "rows-list");
    var addBtn = el("button", "btn btn-ghost btn-sm", "+ " + kind.add);
    addBtn.type = "button";
    box.textContent = "";
    box.appendChild(list);
    box.appendChild(addBtn);
    var self = this;

    function save() {
      var rows = Array.prototype.map.call(list.children, function (r) {
        return Array.prototype.map.call(r.querySelectorAll("input"), function (i) { return i.value.trim(); });
      });
      target.value = kind.write(rows);
      fire(target);
    }
    function addRow(values) {
      var r = el("div", "rows-row");
      kind.cols.forEach(function (c, i) {
        var f = input("rows-in " + c[2], values[i], c[1], c[1]);
        f.addEventListener("input", save);
        r.appendChild(f);
      });
      var rm = el("button", "btn btn-ghost btn-sm rows-rm", "×");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove this row");
      rm.addEventListener("click", function () { r.remove(); save(); });
      r.appendChild(rm);
      list.appendChild(r);
    }
    self.rebuild = function () {
      list.textContent = "";
      var rows = kind.parse(target.value);
      rows.forEach(addRow);
      if (!rows.length) addRow(kind.blank);
    };
    addBtn.addEventListener("click", function () { addRow(kind.blank); });
    target.addEventListener("change", self.rebuild);       // somebody typed in the text box
    self.rebuild();
  }
  var editors = Array.prototype.map.call(form.querySelectorAll(".rows[data-kind]"), function (b) { return new Editor(b); });

  // ------------------------------------------------------------------ the text boxes are hidden unless asked for
  var showRaw = document.getElementById("scShowRaw");
  if (showRaw) showRaw.addEventListener("change", function () { form.classList.toggle("show-raw", showRaw.checked); });

  // ------------------------------------------------------------------ ready-made sets
  var note = document.getElementById("scPresetNote");
  Array.prototype.forEach.call(form.querySelectorAll(".sc-preset"), function (b) {
    b.addEventListener("click", function () {
      var values = {};
      try { values = JSON.parse(b.getAttribute("data-preset")); } catch (e) { return; }
      Object.keys(values).forEach(function (name) {
        var f = raw(name);
        if (f) f.value = values[name];
      });
      editors.forEach(function (e) { e.rebuild(); });
      Array.prototype.forEach.call(form.querySelectorAll(".sc-repeat"), function (d) {
        var open = Array.prototype.some.call(d.querySelectorAll("input"), function (i) { return i.name.indexOf("_steps") > 0 && i.value; })
          || Array.prototype.some.call(d.querySelectorAll("input"), function (i) { return i.name.indexOf("_consec_days") > 0 && i.value; });
        d.open = open;
      });
      Array.prototype.forEach.call(form.querySelectorAll(".sc-preset"), function (x) { x.classList.toggle("on", x === b); });
      if (note) note.textContent = "The " + b.getAttribute("data-name") + " set is in the boxes. Nothing is saved until you press “Save as a new version”.";
      schedule();
    });
  });

  // ------------------------------------------------------------------ the live preview (the real scoring code answers)
  var out = document.getElementById("scPreview");
  var timer = null, seq = 0;
  function schedule() { clearTimeout(timer); timer = setTimeout(run, 350); }
  function g(n) { return Math.round(n * 100) / 100; }
  function show(res) {
    out.textContent = "";
    if (res.error) { out.appendChild(el("p", "form-error small", res.error)); return; }
    var head = el("div", "sc-score");
    head.appendChild(el("div", "sc-big", String(g(res.score))));
    head.appendChild(el("div", "sc-of", "of " + g(res.start)));
    if (res.category) head.appendChild(el("span", "chip " + (res.tone || "t-na"), res.category));
    out.appendChild(head);
    var t = el("table", "tbl sc-table");
    var hr = el("tr");
    ["Problem", "×", "Points", "Repeats", "Lost"].forEach(function (h) { hr.appendChild(el("th", h === "Problem" ? "" : "num", h)); });
    var th = el("thead"); th.appendChild(hr); t.appendChild(th);
    var tb = el("tbody");
    res.deductions.forEach(function (d) {
      if (!d.active && !d.count) return;
      var r = el("tr");
      r.appendChild(el("td", "", d.label.replace(/ \(.*\)$/, "")));
      r.appendChild(el("td", "num", String(d.count)));
      r.appendChild(el("td", "num", String(g(d.base))));
      r.appendChild(el("td", "num", d.repeat ? "+" + g(d.repeat) : "–"));
      r.appendChild(el("td", "num", d.total ? "−" + g(d.total) : "0"));
      tb.appendChild(r);
    });
    t.appendChild(tb);
    out.appendChild(t);
    (res.caps || []).forEach(function (c) { out.appendChild(el("p", "small muted", "Limit applied - " + c)); });
    if (!res.category) out.appendChild(el("p", "small muted", "No category: add categories below to see one."));
  }
  function run() {
    var mine = ++seq;
    fetch(form.getAttribute("data-preview-url"), { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (res) { if (mine === seq) show(res); })
      .catch(function () { if (mine === seq) show({ error: "The preview could not be computed - check the boxes." }); });
  }
  form.addEventListener("input", schedule);
  form.addEventListener("change", schedule);
  run();
})();
