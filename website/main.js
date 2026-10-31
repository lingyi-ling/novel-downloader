/* Novel Downloader website interactions. */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ------------------------------------------------ theme (persisted) */
const root = document.documentElement;
const storedTheme = (() => {
  try { return localStorage.getItem("noveldl-theme"); } catch { return null; }
})();
if (storedTheme === "dark" || storedTheme === "light") {
  root.dataset.theme = storedTheme;
} else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
  root.dataset.theme = "dark";
}
$("#theme-btn").addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("noveldl-theme", next); } catch { /* private mode */ }
});

/* ------------------------------------------------ i18n (zh source of truth, data-en swap) */
const langBtn = $("#lang-btn");
function currentLang() {
  try { return localStorage.getItem("noveldl-lang") || "zh"; } catch { return "zh"; }
}
function applyLang(lang) {
  document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
  if (langBtn) langBtn.textContent = lang === "en" ? "中文" : "EN";
  $$("[data-en]").forEach((el) => {
    if (el.dataset.zh === undefined) el.dataset.zh = el.innerHTML;
    el.innerHTML = lang === "en" ? el.dataset.en : el.dataset.zh;
  });
  document.title = lang === "en"
    ? "Novel Downloader — aggregate search · async downloads · four clients"
    : "Novel Downloader — 多站点聚合 · 异步下载 · 四端同源";
}
if (currentLang() === "en") applyLang("en");
if (langBtn) {
  langBtn.addEventListener("click", () => {
    const next = currentLang() === "en" ? "zh" : "en";
    try { localStorage.setItem("noveldl-lang", next); } catch { /* private mode */ }
    applyLang(next);
  });
}

/* ------------------------------------------------ tabs */
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const key = tab.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === key));
  });
});

/* ------------------------------------------------ scroll reveal */
const revealables = $$(".reveal");
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
const counters = $$("[data-count]");
const animateCount = (el) => {
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
          animateCount(entry.target);
          cio.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.6 },
  );
  counters.forEach((el) => cio.observe(el));
} else {
  counters.forEach((el) => { el.textContent = el.dataset.count; });
}

/* ------------------------------------------------ scrollspy */
const spyLinks = $$(".nav-links a[href^='#']");
const spyTargets = spyLinks
  .map((a) => document.querySelector(a.getAttribute("href")))
  .filter(Boolean);
if ("IntersectionObserver" in window && spyTargets.length) {
  const sio = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        spyLinks.forEach((a) =>
          a.classList.toggle("active", a.getAttribute("href") === "#" + entry.target.id),
        );
      });
    },
    { rootMargin: "-40% 0px -55% 0px" },
  );
  spyTargets.forEach((sec) => sio.observe(sec));
}

/* ------------------------------------------------ mobile menu */
const burger = $("#burger-btn");
const mobileMenu = $("#mobile-menu");
if (burger && mobileMenu) {
  burger.addEventListener("click", () => {
    const open = mobileMenu.classList.toggle("open");
    mobileMenu.hidden = !open;
    burger.setAttribute("aria-expanded", String(open));
  });
  mobileMenu.addEventListener("click", (e) => {
    if (e.target.closest("a")) {
      mobileMenu.classList.remove("open");
      mobileMenu.hidden = true;
      burger.setAttribute("aria-expanded", "false");
    }
  });
}

/* ------------------------------------------------ back to top */
const toTop = $("#to-top");
if (toTop) {
  window.addEventListener(
    "scroll",
    () => {
      toTop.hidden = false;
      toTop.classList.toggle("show", window.scrollY > 520);
    },
    { passive: true },
  );
  toTop.addEventListener("click", () =>
    window.scrollTo({ top: 0, behavior: "smooth" }),
  );
}

/* ------------------------------------------------ github chips (graceful) */
function ghCacheGet(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
function ghCacheSet(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* private mode */ }
}
async function ghJson(path, cacheKey) {
  const cached = ghCacheGet(cacheKey);
  if (cached) return JSON.parse(cached);
  const resp = await fetch("https://api.github.com" + path, {
    headers: { Accept: "application/vnd.github+json" },
  });
  if (!resp.ok) throw new Error(String(resp.status));
  const data = await resp.json();
  ghCacheSet(cacheKey, JSON.stringify(data));
  return data;
}
function formatStars(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}
const starsEl = $("#gh-stars");
if (starsEl) {
  ghJson("/repos/lingyi-ling/novel-downloader", "gh-repo")
    .then((d) => {
      const n = document.getElementById("gh-stars-n");
      if (typeof d.stargazers_count === "number" && n) {
        n.textContent = formatStars(d.stargazers_count);
        starsEl.hidden = false;
      }
    })
    .catch(() => {});
}
const releaseEl = $("#gh-release");
if (releaseEl) {
  ghJson("/repos/lingyi-ling/novel-downloader/releases/latest", "gh-release")
    .then((d) => {
      if (!d.tag_name) return;
      const ver = String(d.tag_name).replace(/^v/, "");
      const n = document.getElementById("gh-release-n");
      if (n) {
        n.textContent = ver;
        releaseEl.hidden = false;
      }
      // keep the showcase mock's version pill in sync with the real release
      const pill = document.getElementById("appwin-ver");
      if (pill) pill.textContent = "v" + ver;
    })
    .catch(() => {});
}
