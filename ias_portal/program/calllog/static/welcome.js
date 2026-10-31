"use strict";
/* The welcome after sign-in (templates/welcome.html): the message appears slowly (app.css, "welcome"), then the portal
   opens by itself after data-seconds - or at once on Continue, a click anywhere, or Enter / Space / Escape. With
   "reduce motion" on, nothing moves and it waits a little less. */
(function () {
  const root = document.getElementById("welcome");
  if (!root) return;
  const next = root.dataset.next || "/";
  const reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const seconds = Math.max(2, (Number(root.dataset.seconds) || 7) - (reduced ? 3 : 0));
  let gone = false;
  const go = () => {
    if (gone) return;
    gone = true;
    window.location.assign(next);
  };
  setTimeout(go, seconds * 1000);
  root.addEventListener("click", (e) => { if (!e.target.closest("a")) go(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " " || e.key === "Escape") { e.preventDefault(); go(); }
  });
})();
