/* Arabic for every page: the pages are written in English; when the person chose Arabic (base.html loads this file and
   the dictionary i18n-ar.js only then) every piece of interface text is replaced by its Arabic from the dictionary -
   the text of the page, placeholders, tooltips, aria-labels, button values, the page title and alert / confirm / prompt
   messages - also everything the page's scripts draw later (a MutationObserver). Data (names, e-mails, subjects,
   numbers, the portal's Arabic statuses) is not in the dictionary, so it stays as it is.
   Matching: the text is trimmed and its spaces collapsed; then (1) the exact phrase, (2) the phrase without a trailing
   ":" "…" "..." "." "?" "!" (put back afterwards), (3) the patterns (a whole-text regex; captured parts are translated
   too when they are phrases). Elements with translate="no" / class notranslate are left alone.
   No inline script or style (strict CSP); text is set with textContent / nodeValue only. */
(function () {
  "use strict";
  var DATA = window.IAS_AR || { phrases: {}, patterns: [] };
  var PHRASES = DATA.phrases || {};
  var root = document.documentElement;
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, PRE: 1, CODE: 1, NOSCRIPT: 1 };
  var ATTRS = ["placeholder", "title", "aria-label", "alt"];
  var TAIL = /(\s*(?:\.\.\.|…|:|\.|\?|!|▲|▼))$/;
  var LETTER = /[A-Za-z]/;
  var cache = new Map();
  var made = new Set();             // our own Arabic output: never translated again (a pattern must not feed on its result)
  var mine = new WeakMap();         // node -> the value we last wrote into it (its own mutation is then ignored)

  function has(key) { return Object.prototype.hasOwnProperty.call(PHRASES, key); }

  /* a word a pattern cannot match without (outside groups, classes and optional letters): a quick indexOf filter */
  function needle(src) {
    var CUT = "\u0000";             // what was not literal text: a word never runs across it
    var s = src.replace(/\\./g, CUT).replace(/\[[^\]]*\]/g, CUT);
    var prev;
    do { prev = s; s = s.replace(/\([^()]*\)[?*+]?/g, CUT); } while (s !== prev);
    if (s.indexOf("|") > -1) return "";
    s = s.replace(/.[?*]/g, CUT).replace(/.\{\d*,?\d*\}/g, CUT);
    var best = "";
    (s.match(/[A-Za-z][A-Za-z' -]*[A-Za-z]/g) || []).forEach(function (w) { if (w.length > best.length) best = w; });
    return best;
  }
  var PATTERNS = [];
  (DATA.patterns || []).forEach(function (p) {
    try { PATTERNS.push({ re: new RegExp("^(?:" + p[0] + ")$"), to: p[1], needle: needle(p[0]) }); }
    catch (e) { /* a bad entry must not stop the others */ }
  });

  function phrase(s) {
    if (has(s)) return PHRASES[s];
    var m = TAIL.exec(s);
    if (m) {
      var head = s.slice(0, s.length - m[1].length);
      if (has(head)) return PHRASES[head] + m[1].replace("?", "؟");
    }
    return null;
  }
  function viaPatterns(s) {
    for (var i = 0; i < PATTERNS.length; i++) {
      var p = PATTERNS[i];
      if (p.needle && s.indexOf(p.needle) < 0) continue;
      var m = p.re.exec(s);
      if (!m) continue;
      return p.to.replace(/\$(\d)/g, function (_, n) {
        var part = m[+n];
        if (part == null || part === s) return part == null ? "" : part;
        var t = tr(part);                // a piece is a phrase or itself a pattern ("3 h 40 min")
        return t == null ? part : keepSpaces(part, t);
      });
    }
    return null;
  }
  /* the Arabic of one piece of text, or null when it has none */
  function tr(text) {
    if (!text || !LETTER.test(text)) return null;
    var s = text.replace(/\s+/g, " ").trim();
    if (made.has(s)) return null;
    if (cache.has(s)) return cache.get(s);
    var out = phrase(s);
    if (out == null) out = viaPatterns(s);
    if (cache.size > 20000) { cache.clear(); made.clear(); }
    cache.set(s, out);
    if (out != null) made.add(out.replace(/\s+/g, " ").trim());
    return out;
  }
  function keepSpaces(original, arabic) {
    var lead = /^\s*/.exec(original)[0], trail = /\s*$/.exec(original)[0];
    return lead + arabic + trail;
  }

  function skipped(el) {
    for (var e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (SKIP[e.tagName] || e.getAttribute("translate") === "no" || (e.classList && e.classList.contains("notranslate"))) return true;
      if (e.isContentEditable) return true;
    }
    return false;
  }
  function textNode(node) {
    var parent = node.parentElement;
    if (!parent || mine.get(node) === node.nodeValue || skipped(parent)) return;
    var t = tr(node.nodeValue);
    if (t != null) {
      var v = keepSpaces(node.nodeValue, t);
      mine.set(node, v);
      if (node.nodeValue !== v) node.nodeValue = v;
    }
  }
  function attrs(el) {
    if (skipped(el)) return;
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v) { var t = tr(v); if (t != null && t !== v) el.setAttribute(ATTRS[i], t); }
    }
    if (el.tagName === "INPUT" && (el.type === "submit" || el.type === "button") && el.value) {
      var tv = tr(el.value); if (tv != null && tv !== el.value) el.value = tv;
    }
  }
  function walk(node) {
    if (node.nodeType === 3) { textNode(node); return; }
    if (node.nodeType !== 1 && node.nodeType !== 11) return;
    if (node.nodeType === 1) { if (skipped(node)) return; attrs(node); }
    var els = node.querySelectorAll ? node.querySelectorAll("[placeholder],[title],[aria-label],[alt],input[type=submit],input[type=button]") : [];
    for (var i = 0; i < els.length; i++) attrs(els[i]);
    var w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, null);
    var list = [], n;
    while ((n = w.nextNode())) list.push(n);
    for (var j = 0; j < list.length; j++) textNode(list[j]);
  }
  function title() {
    var t = tr(document.title);
    if (t != null) { if (t !== document.title) document.title = t; }
    else {                                     // "Page · IAS Portal": each part on its own
      var parts = document.title.split(" · ");
      var joined = parts.map(function (p) { var x = tr(p); return x == null ? p : x; }).join(" · ");
      if (parts.length > 1 && joined !== document.title) document.title = joined;
    }
  }

  ["alert", "confirm", "prompt"].forEach(function (name) {
    var original = window[name];
    if (typeof original !== "function") return;
    window[name] = function (message) {
      var args = Array.prototype.slice.call(arguments);
      if (typeof message === "string") {
        args[0] = message.split("\n").map(function (line) { var t = tr(line); return t == null ? line : keepSpaces(line, t); }).join("\n");
      }
      return original.apply(window, args);
    };
  });

  /* a safety fuse: a page script that keeps rewriting what we translate (or a bad entry) must never freeze the page -
     past 4,000 changes in one second the translator rests for two seconds */
  var burst = 0, burstAt = 0, resting = false;
  var observer = new MutationObserver(function (records) {
    if (resting) return;
    var now = Date.now();
    if (now - burstAt > 1000) { burst = 0; burstAt = now; }
    burst += records.length;
    if (burst > 4000) {
      resting = true;
      setTimeout(function () { resting = false; burst = 0; walk(document.body); }, 2000);
      return;
    }
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (r.type === "childList") {
        for (var j = 0; j < r.addedNodes.length; j++) walk(r.addedNodes[j]);
        if (r.target.nodeName === "TITLE") title();
      } else if (r.type === "characterData") {
        textNode(r.target);
      } else if (r.type === "attributes" && r.target.nodeType === 1) {
        attrs(r.target);
      }
    }
  });

  function start() {
    walk(document.body);
    title();
    observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true,
                             attributeFilter: ATTRS.concat(["value"]) });
    root.classList.remove("i18n-pending");
  }
  window.IAS_I18N = { tr: tr, translate: walk };
  setTimeout(function () { root.classList.remove("i18n-pending"); }, 2500);   // never leave a page hidden
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
