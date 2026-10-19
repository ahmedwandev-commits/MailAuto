"use strict";
/* Sidebar sub-items that deep-link to a tab (href like "/system#settings") should light up to match the tab
   that is actually open. The server can only mark the page path, so it marks one of them; here we refine it to
   the hash once the page is live, and keep it in sync as the user switches tabs. Strict-CSP safe (external file). */
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('.op-nav-link[href*="#"]'));
  if (!links.length) return;
  function here() { return location.pathname + (location.hash || ""); }
  function sameHref(a) { return a.getAttribute("href") === here(); }
  function onPath(a) { return a.getAttribute("href").split("#")[0] === location.pathname; }
  function sync() {
    if (!location.hash) return; // no hash -> keep whatever the server marked for this path
    links.forEach(function (a) {
      if (!onPath(a)) return;
      if (sameHref(a)) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }
  window.addEventListener("hashchange", sync);
  sync();
})();
