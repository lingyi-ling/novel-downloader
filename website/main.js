/* Novel Downloader website interactions. */
"use strict";

/* Reveal styles are scoped to .js so a blocked or missing main.js leaves the
   content visible instead of permanently transparent. */
document.documentElement.classList.add("js");

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ------------------------------------------------ theme (persisted) */
const root = document.documentElement;
const themeColorMetas = $$('meta[name="theme-color"]');
/* Resolved once: syncThemeBtn() is called on load and on every toggle. */
const themeBtn = $("#theme-btn");
if (themeBtn) {
  themeBtn.addEventListener("click", () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("noveldl-theme", next); } catch { /* private mode */ }
    syncThemeColor();
    syncThemeBtn();
  });
}
const reduceMotion = window.matchMedia
  ? window.matchMedia("(prefers-reduced-motion: reduce)")
  : { matches: false };
function syncThemeBtn() {
  if (themeBtn) themeBtn.setAttribute("aria-pressed", String(root.dataset.theme === "dark"));
}
function syncThemeColor() {
  const bg = root.dataset.theme === "dark" ? "#0b1220" : "#f7f9fc";
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
syncThemeBtn();

/* ------------------------------------------------ i18n (zh source of truth, data-en swap) */
const langBtn = $("#lang-btn");
/* Collected on the first language switch, then reused for every toggle. */
let translated = null;
const PAGE_TITLE = {
  zh: "书音 BookTone — 小说与音乐 · 多站点聚合 · 四端同源",
  en: "BookTone — aggregate search · async downloads · four clients",
};
const META_DESC = {
  zh: "异步、模块化、可扩展的小说下载工具链：84 个下载站点、59 个搜索源、28 个音乐平台，CLI / REST API / Windows 桌面 / Android 四端同源。",
  en: "Async, modular, extensible novel-downloading toolkit: 84 download sites, 59 search sources and 28 music platforms — CLI, REST API, Windows desktop and Android share one service layer.",
};
function currentLang() {
  return localStorageValue("noveldl-lang") === "en" ? "en" : "zh";
}
function localStorageValue(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function persistLang(lang) {
  try { localStorage.setItem("noveldl-lang", lang); } catch { /* private mode */ }
}
// An explicit toggle must win over the ?lang= deep link on the next reload.
function dropLangParam() {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("lang")) return;
    url.searchParams.delete("lang");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  } catch { /* file:// or blocked history API */ }
}
function applyLang(lang) {
  const isEn = lang === "en";
  document.documentElement.lang = isEn ? "en" : "zh-CN";
  if (langBtn) langBtn.textContent = isEn ? "中文" : "EN";
  if (!translated) translated = $$("[data-en]");
  translated.forEach((el) => {
    if (el.dataset.zh === undefined) el.dataset.zh = el.innerHTML;
    el.innerHTML = isEn ? el.dataset.en : el.dataset.zh;
  });
  document.title = isEn ? PAGE_TITLE.en : PAGE_TITLE.zh;
  const metaDesc = document.querySelector('meta[name="description"]');
  if (metaDesc) metaDesc.setAttribute("content", META_DESC[isEn ? "en" : "zh"]);
  // keep control labels in the active language for screen readers
  const labels = [
    ["lang-btn", "切换语言", "Switch language"],
    ["theme-btn", "切换主题", "Toggle theme"],
    ["burger-btn", "打开菜单", "Open menu"],
    ["to-top", "回到顶部", "Back to top"],
    ["appwin", "书音 BookTone 桌面客户端预览", "BookTone desktop app preview"],
    ["start-tabs", "快速开始方式", "Quick start options"],
    ["nav-links", "主导航", "Main navigation"],
    ["mobile-menu", "主导航", "Main navigation"],
    ["foot-links", "页脚链接", "Footer links"],
  ];
  labels.forEach(([id, zh, en]) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-label", isEn ? en : zh);
  });
  // the menu button label describes the action, which depends on state too
  const burgerEl = document.getElementById("burger-btn");
  if (burgerEl) {
    const open = burgerEl.getAttribute("aria-expanded") === "true";
    burgerEl.setAttribute(
      "aria-label",
      isEn ? (open ? "Close menu" : "Open menu") : (open ? "关闭菜单" : "打开菜单"),
    );
  }
  const stars = document.getElementById("gh-stars");
  if (stars && stars.dataset.count) {
    stars.setAttribute("aria-label", starsLabel(stars.dataset.count));
  }
}
// hreflang deep link: ?lang=zh / ?lang=en wins once and is persisted
(() => {
  let q = null;
  try { q = new URLSearchParams(window.location.search).get("lang"); } catch { q = null; }
  if (q === "en" || q === "zh") persistLang(q);
})();
if (currentLang() === "en") applyLang("en");
if (langBtn) {
  langBtn.addEventListener("click", () => {
    const next = currentLang() === "en" ? "zh" : "en";
    persistLang(next);
    dropLangParam();
    applyLang(next);
  });
}

/* ------------------------------------------------ tabs (ARIA + keyboard + session memory) */
const tabs = $$(".tab");
const tabPanels = $$(".tab-panel");
const tabBar = $("#start-tabs");
function activateTab(key, opts) {
  const target = tabs.find((t) => t.dataset.tab === key);
  if (!target) return;
  tabs.forEach((t) => {
    const on = t === target;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
  });
  tabPanels.forEach((p) => p.classList.toggle("active", p.dataset.panel === key));
  try { sessionStorage.setItem("noveldl-tab", key); } catch { /* private mode */ }
  if (opts && opts.focus) target.focus();
}
/* One delegated pair of listeners instead of two per button. */
if (tabBar) {
  tabBar.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (tab && tabBar.contains(tab)) activateTab(tab.dataset.tab);
  });
  tabBar.addEventListener("keydown", (e) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
    const tab = e.target.closest(".tab");
    if (!tab) return;
    e.preventDefault();
    let i = tabs.indexOf(tab);
    if (e.key === "ArrowRight") i = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") i = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") i = 0;
    else i = tabs.length - 1;
    activateTab(tabs[i].dataset.tab, { focus: true });
  });
}
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
  if (reduceMotion.matches) {
    el.textContent = String(target);
    return;
  }
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
const spyLinks = $$(".nav-links a[href^='#'], .mobile-menu a[href^='#']");
const spyTargets = [
  ...new Set(
    spyLinks
      .map((a) => a.getAttribute("href"))
      .filter((href) => href && href.length > 1),
  ),
]
  .map((href) => document.querySelector(href))
  .filter(Boolean);
if ("IntersectionObserver" in window && spyTargets.length) {
  const sio = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const active = "#" + entry.target.id;
        spyLinks.forEach((a) => {
          const on = a.getAttribute("href") === active;
          a.classList.toggle("active", on);
          if (on) a.setAttribute("aria-current", "true");
          else a.removeAttribute("aria-current");
        });
      });
    },
    { rootMargin: "-40% 0px -55% 0px" },
  );
  spyTargets.forEach((sec) => sio.observe(sec));
}

/* ------------------------------------------------ mobile menu */
const burger = $("#burger-btn");
const mobileMenu = $("#mobile-menu");
function setMenu(open, opts) {
  if (!burger || !mobileMenu) return;
  mobileMenu.classList.toggle("open", open);
  mobileMenu.hidden = !open;
  burger.setAttribute("aria-expanded", String(open));
  const isEn = document.documentElement.lang === "en";
  burger.setAttribute(
    "aria-label",
    isEn ? (open ? "Close menu" : "Open menu") : (open ? "关闭菜单" : "打开菜单"),
  );
  if (open === false && opts && opts.focus) burger.focus();
}
if (burger && mobileMenu) {
  burger.addEventListener("click", () => setMenu(mobileMenu.hidden));
  mobileMenu.addEventListener("click", (e) => {
    if (e.target.closest("a")) setMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !mobileMenu.hidden) setMenu(false, { focus: true });
  });
}

/* ------------------------------------------------ back to top */
const toTop = $("#to-top");
if (toTop) {
  let topTicking = false;
  let topShown = null;
  const syncToTop = () => {
    topTicking = false;
    const show = window.scrollY > 520;
    // a scroll frame that does not cross the threshold writes nothing
    if (show === topShown) return;
    topShown = show;
    // keep the button out of the tab order while it is invisible
    toTop.hidden = !show;
    toTop.classList.toggle("show", show);
  };
  window.addEventListener(
    "scroll",
    () => {
      if (topTicking) return;
      topTicking = true;
      requestAnimationFrame(syncToTop);
    },
    { passive: true },
  );
  syncToTop();
  toTop.addEventListener("click", () =>
    window.scrollTo({ top: 0, behavior: reduceMotion.matches ? "auto" : "smooth" }),
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
function starsLabel(n) {
  return n + (currentLang() === "en" ? " GitHub stars" : " 个 GitHub 星标");
}
/* Decorative GitHub chips are not needed for first paint: start their
   requests once the browser is idle so they never compete with the CSS/HTML
   that the visitor is actually waiting for. */
function whenIdle(fn) {
  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(fn, { timeout: 3000 });
  } else {
    setTimeout(fn, 1200);
  }
}
const starsEl = $("#gh-stars");
if (starsEl) {
  whenIdle(() => {
    ghJson("/repos/lingyi-ling/novel-downloader", "gh-repo")
      .then((d) => {
        const n = document.getElementById("gh-stars-n");
        if (typeof d.stargazers_count === "number" && n) {
          n.textContent = formatStars(d.stargazers_count);
          starsEl.hidden = false;
          starsEl.dataset.count = String(d.stargazers_count);
          starsEl.setAttribute("aria-label", starsLabel(d.stargazers_count));
        }
      })
      .catch(() => {});
  });
}
const releaseEl = $("#gh-release");
if (releaseEl) {
  // The updater manifest is a tiny, cacheable document published with every
  // release; the GitHub API is only the fallback (it is rate limited and its
  // response is much larger).
  const MANIFEST_URL =
    "https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json";

  async function latestVersion() {
    try {
      const resp = await fetch(MANIFEST_URL, { cache: "no-cache" });
      if (resp.ok) {
        const data = await resp.json();
        if (data && typeof data.version === "string" && data.version) {
          return data.version;
        }
      }
    } catch {
      /* offline or blocked: fall through to the API */
    }
    const d = await ghJson(
      "/repos/lingyi-ling/novel-downloader/releases/latest",
      "gh-release",
    );
    return String(d.tag_name || "").replace(/^v/, "");
  }

  whenIdle(() => {
    latestVersion()
      .then((ver) => {
        if (!ver) return;
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
          } catch {
            /* leave the static value */
          }
        }
      })
      .catch(() => {});
  });
}
