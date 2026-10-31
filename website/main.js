/* Novel Downloader website interactions. */
"use strict";

/* ------------------------------------------------ theme (matches site + persists) */
const root = document.documentElement;
const stored = (() => {
  try { return localStorage.getItem("noveldl-theme"); } catch { return null; }
})();
if (stored === "dark" || stored === "light") {
  root.dataset.theme = stored;
} else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
  root.dataset.theme = "dark";
}
document.getElementById("theme-btn").addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("noveldl-theme", next); } catch { /* private mode */ }
});

/* ------------------------------------------------ tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const key = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t === tab),
    );
    document.querySelectorAll(".tab-panel").forEach((p) =>
      p.classList.toggle("active", p.dataset.panel === key),
    );
  });
});

/* ------------------------------------------------ scroll reveal */
const revealables = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15 },
  );
  revealables.forEach((el) => io.observe(el));
} else {
  revealables.forEach((el) => el.classList.add("in"));
}

/* ------------------------------------------------ hero count-up */
const counters = document.querySelectorAll("[data-count]");
const animate = (el) => {
  const target = Number(el.dataset.count) || 0;
  const started = performance.now();
  const duration = 900;
  const tick = (now) => {
    const t = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = String(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
};
if ("IntersectionObserver" in window) {
  const cio = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          animate(entry.target);
          cio.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.6 },
  );
  counters.forEach((el) => cio.observe(el));
} else {
  counters.forEach((el) => {
    el.textContent = el.dataset.count;
  });
}
