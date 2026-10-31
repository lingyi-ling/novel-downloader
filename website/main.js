/* Novel Downloader website interactions. */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ------------------------------------------------ theme (persisted) */
const root = document.documentElement;
const themeColorMetas = $$('meta[name="theme-color"]');
function syncThemeColor() {
  const bg = root.dataset.theme === "dark" ? "#171614" : "#f6f5f1";
  // once the user picks a theme manually, drop the media hints so the
  // browser chrome follows the page instead of the OS scheme
  themeColorMetas.forEach((m) => {
    m.removeAttribute("media");
    m.setAttribute("content", bg);
  });
}
const storedTheme = (() => {
  try { return localStorage.getItem("noveldl-theme"); } catch { return null; }
})();
if (storedTheme === "dark" || storedTheme === "light") {
  root.dataset.theme = storedTheme;
  syncThemeColor();
} else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
  root.dataset.theme = "dark";
}
$("#theme-btn").addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("noveldl-theme", next); } catch { /* private mode */ }
  syncThemeColor();
});

/* ------------------------------------------------ i18n (zh source of truth, data-en swap) */
const langBtn = $("#lang-btn");
const META_DESC = {
  zh: "异步、模块化、可扩展的小说下载工具链：84 个下载站点、59 个搜索源、25 个音乐平台，CLI / REST API / Windows 桌面 / Android 四端同源。",
  en: "Async, modular, extensible novel-downloading toolkit: 84 download sites, 59 search sources and 25 music platforms — CLI, REST API, Windows desktop and Android share one service layer.",
};
function currentLang() {
  try { return localStorage.getItem("noveldl-lang") || "zh"; } catch { return "zh"; }
}
function applyLang(lang) {
  const isEn = lang === "en";
  document.documentElement.lang = isEn ? "en" : "zh-CN";
  if (langBtn) langBtn.textContent = isEn ? "中文" : "EN";
  $$("[data-en]").forEach((el) => {
    if (el.dataset.zh === undefined) el.dataset.zh = el.innerHTML;
    el.innerHTML = isEn ? el.dataset.en : el.dataset.zh;
  });
  document.title = isEn
    ? "Novel Downloader — aggregate search · async downloads · four clients"
    : "Novel Downloader — 多站点聚合 · 异步下载 · 四端同源";
  const metaDesc = document.querySelector('meta[name="description"]');
  if (metaDesc) metaDesc.setAttribute("content", META_DESC[isEn ? "en" : "zh"]);
  // keep control labels in the active language for screen readers
  const labels = [
    ["lang-btn", "切换语言", "Switch language"],
    ["theme-btn", "切换主题", "Toggle theme"],
    ["burger-btn", "打开菜单", "Open menu"],
    ["to-top", "回到顶部", "Back to top"],
    ["start-tabs", "快速开始方式", "Quick start options"],
  ];
  labels.forEach(([id, zh, en]) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-label", isEn ? en : zh);
  });
}
// hreflang deep link: ?lang=zh / ?lang=en wins once and is persisted
(() => {
  let q = null;
  try { q = new URLSearchParams(window.location.search).get("lang"); } catch { q = null; }
  if (q === "en" || q === "zh") {
    try { localStorage.setItem("noveldl-lang", q); } catch { /* private mode */ }
  }
})();
if (currentLang() === "en") applyLang("en");
if (langBtn) {
  langBtn.addEventListener("click", () => {
    const next = currentLang() === "en" ? "zh" : "en";
    try { localStorage.setItem("noveldl-lang", next); } catch { /* private mode */ }
    applyLang(next);
  });
}

/* ------------------------------------------------ tabs (ARIA + keyboard + session memory) */
const tabs = $$(".tab");
function activateTab(key, opts) {
  const target = tabs.find((t) => t.dataset.tab === key);
  if (!target) return;
  tabs.forEach((t) => {
    const on = t === target;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
  });
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === key));
  try { sessionStorage.setItem("noveldl-tab", key); } catch { /* private mode */ }
  if (opts && opts.focus) target.focus();
}
tabs.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  tab.addEventListener("keydown", (e) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    let i = tabs.indexOf(tab);
    if (e.key === "ArrowRight") i = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") i = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") i = 0;
    else i = tabs.length - 1;
    activateTab(tabs[i].dataset.tab, { focus: true });
  });
});
// restore the tab chosen earlier in this session (survives reload & lang switch)
(() => {
  let saved = null;
  try { saved = sessionStorage.getItem("noveldl-tab"); } catch { saved = null; }
  const valid = saved && tabs.some((t) => t.dataset.tab === saved);
  activateTab(valid ? saved : "cli");
})();

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
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !mobileMenu.hidden) {
      mobileMenu.classList.remove("open");
      mobileMenu.hidden = true;
      burger.setAttribute("aria-expanded", "false");
      burger.focus();
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
const GH_CACHE_TTL = 30 * 60 * 1000; /* 30 min — survives reloads, not whole sessions */
function ghCacheGet(key) {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const box = JSON.parse(raw);
    if (!box || typeof box.t !== "number" || Date.now() - box.t > GH_CACHE_TTL) {
      sessionStorage.removeItem(key);
      return null;
    }
    return box.v;
  } catch { return null; }
}
function ghCacheSet(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify({ t: Date.now(), v: value })); } catch { /* private mode */ }
}
async function ghJson(path, cacheKey) {
  const cached = ghCacheGet(cacheKey);
  if (cached) return cached;
  const resp = await fetch("https://api.github.com" + path, {
    headers: { Accept: "application/vnd.github+json" },
  });
  if (!resp.ok) throw new Error(String(resp.status));
  const data = await resp.json();
  ghCacheSet(cacheKey, data);
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
        starsEl.setAttribute("aria-label", d.stargazers_count + " GitHub stars");
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
      // keep the JSON-LD softwareVersion in sync too
      const ld = document.getElementById("ld-software");
      if (ld) {
        try {
          const data = JSON.parse(ld.textContent);
          data.softwareVersion = ver;
          ld.textContent = JSON.stringify(data);
        } catch { /* leave the static value */ }
      }
    })
    .catch(() => {});
}
