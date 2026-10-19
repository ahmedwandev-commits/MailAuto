"use strict";
/* A subtle animated "network" background: moving nodes joined by lines, drawn on one fixed canvas behind every page
   (the login and the whole app). Strict-CSP friendly (external file, no inline script/style). It respects
   prefers-reduced-motion (draws a single still frame), pauses while the tab is hidden, and recolours with the theme. */
(function () {
  if (window.__networkBg) return;
  window.__networkBg = true;

  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const canvas = document.createElement("canvas");
  canvas.className = "network-bg";
  canvas.setAttribute("aria-hidden", "true");
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1, nodes = [], raf = 0, rgb = [27, 158, 107];

  function brandRgb() {
    const v = (getComputedStyle(document.documentElement).getPropertyValue("--brand") || "").trim().replace("#", "");
    const hex = v.length === 3 ? v.split("").map((c) => c + c).join("") : v;
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return rgb;
    const n = parseInt(hex, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.width = Math.floor(window.innerWidth * dpr);
    H = canvas.height = Math.floor(window.innerHeight * dpr);
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    const count = Math.max(22, Math.min(70, Math.round((window.innerWidth * window.innerHeight) / 24000)));
    nodes = Array.from({ length: count }, () => ({
      x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.22 * dpr, vy: (Math.random() - 0.5) * 0.22 * dpr,
    }));
    rgb = brandRgb();
  }

  function render() {
    ctx.clearRect(0, 0, W, H);
    const link = 155 * dpr, r = rgb[0], g = rgb[1], b = rgb[2];
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const o = nodes[j], dx = a.x - o.x, dy = a.y - o.y, d = Math.hypot(dx, dy);
        if (d < link) {
          ctx.strokeStyle = "rgba(" + r + "," + g + "," + b + "," + (0.24 * (1 - d / link)).toFixed(3) + ")";
          ctx.lineWidth = dpr;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(o.x, o.y); ctx.stroke();
        }
      }
      ctx.beginPath();
      ctx.arc(a.x, a.y, 1.7 * dpr, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(" + r + "," + g + "," + b + ",0.65)";
      ctx.fill();
    }
  }

  function step() {
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      if (n.x <= 0 || n.x >= W) n.vx *= -1;
      if (n.y <= 0 || n.y >= H) n.vy *= -1;
    }
    render();
    raf = window.requestAnimationFrame(step);
  }

  function start() { if (!raf) raf = window.requestAnimationFrame(step); }
  function stop() { if (raf) { window.cancelAnimationFrame(raf); raf = 0; } }

  function init() {
    document.body.appendChild(canvas);
    resize();
    render();
    if (reduce) return;                                   // one still frame, no motion
    start();
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(resize, 150); });
    document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
    new MutationObserver(() => { rgb = brandRgb(); }).observe(
      document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
