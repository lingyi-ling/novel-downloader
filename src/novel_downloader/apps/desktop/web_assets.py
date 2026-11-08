#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.web_assets
----------------------------------------

The single-file HTML front end rendered inside the pywebview window.

All styling and behaviour is inlined here so the window can be created from
the ``html=`` argument without touching the filesystem.  Visible strings are
English msgids marked with ``data-i18n``; :mod:`novel_downloader.infra.i18n`
translations are supplied at runtime by the bridge and applied by the boot
script, so the extraction pass keeps finding them here.
"""

from __future__ import annotations

__all__ = ["APP_HTML"]

APP_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>书音 BookTone</title>
<style>
/* ============================================================ tokens */
:root {
  --bg: #f8fafc;
  --side: #ffffff;
  --card: #ffffff;
  --card-2: #f8fafc;
  --hover: #f1f5f9;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --text: #0f172a;
  --text-2: #475569;
  --text-3: #64748b;
  --brand: #1e3a5f;
  --brand-soft: rgba(30, 58, 95, .07);
  --primary: #c2410c;
  --primary-hover: #9a3412;
  --on-primary: #ffffff;
  --accent: #c2410c;
  --accent-hover: #9a3412;
  --accent-soft: rgba(194, 65, 12, .10);
  --accent-soft-2: rgba(194, 65, 12, .20);
  --on-accent: #ffffff;
  --green: #059669; --green-bg: rgba(5, 150, 105, .10);
  --red: #dc2626;   --red-bg: rgba(220, 38, 38, .08);
  --blue: #2563eb;  --blue-bg: rgba(37, 99, 235, .08);
  --purple: #7c3aed;--purple-bg: rgba(124, 58, 237, .08);
  --yellow: #b45309;--yellow-bg: rgba(180, 83, 9, .09);
  --gray: #64748b;  --gray-bg: rgba(100, 116, 139, .10);
  --shadow-1: 0 1px 2px rgba(15, 23, 42, .05);
  --shadow-2: 0 8px 24px -12px rgba(15, 23, 42, .18);
  --shadow-3: 0 24px 60px -20px rgba(15, 23, 42, .28);
  --scroll: rgba(15, 23, 42, .16);
  --focus: rgba(194, 65, 12, .45);
  --content-max: 1080px;
  --ease: cubic-bezier(.2, .7, .3, 1);
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
}
:root[data-theme="dark"] {
  --bg: #0b1220;
  --side: #0f172a;
  --card: #111c2e;
  --card-2: #0f1a2b;
  --hover: #1a2942;
  --border: #22304a;
  --border-strong: #33456a;
  --text: #e8eef7;
  --text-2: #a9b7cc;
  --text-3: #8291a9;
  --brand: #9db8d6;
  --brand-soft: rgba(157, 184, 214, .10);
  --primary: #e8835f;
  --primary-hover: #f09a7a;
  --on-primary: #1a1208;
  --accent: #f0a184;
  --accent-hover: #f7c0a8;
  --accent-soft: rgba(232, 131, 95, .14);
  --accent-soft-2: rgba(232, 131, 95, .26);
  --on-accent: #1a1208;
  --green: #34d399; --green-bg: rgba(52, 211, 153, .12);
  --red: #f87171;   --red-bg: rgba(248, 113, 113, .12);
  --blue: #60a5fa;  --blue-bg: rgba(96, 165, 250, .12);
  --purple: #a78bfa;--purple-bg: rgba(167, 139, 250, .12);
  --yellow: #fbbf24;--yellow-bg: rgba(251, 191, 36, .12);
  --gray: #94a3b8;  --gray-bg: rgba(148, 163, 184, .12);
  --shadow-1: 0 1px 2px rgba(0, 0, 0, .4);
  --shadow-2: 0 8px 24px -12px rgba(0, 0, 0, .6);
  --shadow-3: 0 24px 60px -20px rgba(0, 0, 0, .7);
  --scroll: rgba(232, 238, 247, .16);
  --focus: rgba(240, 161, 132, .55);
}

/* ============================================================ base */
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: "Inter", "Segoe UI Variable Text", "Segoe UI", "PingFang SC",
    "Microsoft YaHei UI", system-ui, sans-serif;
  font-size: 14px; line-height: 1.55; color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  overflow: hidden; user-select: none;
}
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--scroll); border-radius: 8px; border: 3px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: var(--border-strong); background-clip: padding-box; }
::-webkit-scrollbar-corner { background: transparent; }
svg.icon { width: 17px; height: 17px; flex: none; }
button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; }
input, select, textarea { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: var(--radius-sm); }
[hidden] { display: none !important; }

/* ============================================================ shell */
.app { display: grid; grid-template-columns: 236px 1fr; height: 100vh; background: var(--bg); }

.sidebar {
  display: flex; flex-direction: column; gap: 2px;
  background: var(--side); border-right: 1px solid var(--border);
  padding: 18px 12px 12px; min-height: 0;
}
.logo { display: flex; align-items: center; gap: 10px; padding: 2px 8px 22px; }
.logo-mark {
  width: 32px; height: 32px; border-radius: 8px; flex: none;
  background: #1e3a5f;
  display: grid; place-items: center; color: #e9a184;
}
.logo-mark svg.icon { width: 17px; height: 17px; }
.logo-name { font-weight: 700; font-size: 14.5px; letter-spacing: -.01em; }
.logo-sub { font-size: 10.5px; color: var(--text-3); margin-top: 1px; letter-spacing: .02em; }

.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 10px;
  min-height: 36px; padding: 7px 10px; border-radius: var(--radius-md);
  color: var(--text-2); font-weight: 500; text-align: left; width: 100%;
  font-size: 13px;
  transition: background .15s ease, color .15s ease;
}
.nav-item svg.icon { width: 17px; height: 17px; opacity: .8; }
.nav-item:hover { background: var(--hover); color: var(--text); }
.nav-item.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nav-item.active svg.icon { opacity: 1; }

.nav-item kbd {
  margin-left: auto; font: 600 10px/1 ui-monospace, monospace;
  color: var(--text-3); background: transparent; border: 1px solid var(--border);
  border-radius: 4px; padding: 2px 4px; opacity: 0;
  transition: opacity .15s ease;
}
.nav-item:hover kbd, .nav-item:focus-visible kbd { opacity: .8; }
.nav-item.active kbd { color: var(--accent); border-color: var(--accent-soft-2); }

.count-chip {
  font-size: 10.5px; font-weight: 700; letter-spacing: .02em;
  background: var(--gray-bg); color: var(--text-2);
  border-radius: 4px; padding: 1px 6px; min-width: 18px; text-align: center;
}
.nav-item .count-chip { margin-left: auto; }
.count-chip.live { background: var(--accent-soft); color: var(--accent); }

.side-foot {
  margin-top: auto; display: flex; flex-direction: column; gap: 8px;
  border-top: 1px solid var(--border); padding-top: 10px;
}
.foot-row { display: flex; align-items: center; gap: 4px; }
.icon-btn {
  display: grid; place-items: center; width: 30px; height: 30px;
  border-radius: var(--radius-sm); color: var(--text-3);
  transition: background .15s ease, color .15s ease;
}
.icon-btn:hover { background: var(--hover); color: var(--text); }
.foot-meta { margin-left: auto; text-align: right; font-size: 10.5px; color: var(--text-3); line-height: 1.5; }

.update-bar {
  display: flex; flex-direction: column; gap: 8px;
  background: var(--accent-soft); border: 1px solid var(--accent-soft-2);
  border-radius: var(--radius-md); padding: 10px 11px;
}
.update-bar .u-text { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: var(--accent); }
.update-bar .u-text svg.icon { width: 14px; height: 14px; }
.update-bar .u-size { margin-left: auto; font-size: 11px; font-weight: 500; color: var(--text-2); white-space: nowrap; }
.update-bar .u-acts { display: flex; flex-wrap: wrap; gap: 6px; }
.update-bar .u-acts .btn { flex: 1 1 auto; height: 27px; padding: 0 9px; font-size: 11.5px; }

/* ============================================================ main */
.main { min-width: 0; display: flex; flex-direction: column; }
.page { display: none; flex-direction: column; min-height: 0; height: 100vh; animation: pageIn .18s var(--ease); }
.page.active { display: flex; }
@keyframes pageIn { from { opacity: 0; } to { opacity: 1; } }

.topbar { display: flex; align-items: center; gap: 10px; padding: 22px 26px 14px; flex: none; }
.topbar h1 { font-size: 17px; font-weight: 650; letter-spacing: -.01em; color: var(--brand); }
.topbar .sub { font-size: 12.5px; color: var(--text-2); margin-top: 2px; }
.topbar .spacer { flex: 1; }

.scroll { overflow-y: auto; padding: 2px 26px 26px; flex: 1; min-height: 0; }

/* One content column for every page. */
.topbar, .scroll {
  padding-left: max(26px, calc((100% - var(--content-max)) / 2));
  padding-right: max(26px, calc((100% - var(--content-max)) / 2));
}

/* ============================================================ controls */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  height: 34px; padding: 0 14px; border-radius: var(--radius-md);
  font-weight: 600; font-size: 13px; white-space: nowrap;
  border: 1px solid transparent;
  transition: background .15s ease, border-color .15s ease, color .15s ease, opacity .15s ease;
}
.btn:disabled { opacity: .45; cursor: default; }
.btn-primary { background: var(--primary); color: var(--on-primary); }
.btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
.btn-ghost { background: var(--card); border-color: var(--border); color: var(--text); }
.btn-ghost:hover:not(:disabled) { background: var(--hover); border-color: var(--border-strong); }
.btn-danger { background: transparent; border-color: var(--border); color: var(--red); }
.btn-danger:hover:not(:disabled) { background: var(--red-bg); border-color: var(--red); }
.btn-sm { height: 28px; padding: 0 10px; font-size: 12px; border-radius: var(--radius-sm); gap: 5px; }
.btn-sm svg.icon { width: 14px; height: 14px; }
.btn-lg { height: 44px; padding: 0 18px; font-size: 13.5px; border-radius: var(--radius-md); }

.field {
  display: flex; align-items: center; gap: 8px;
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-md);
  padding: 0 12px; height: 38px; color: var(--text-3);
  transition: border-color .15s ease, box-shadow .15s ease;
}
.field svg.icon { width: 16px; height: 16px; }
.field:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.field input { flex: 1; min-width: 0; border: none; outline: none; background: transparent; color: var(--text); user-select: text; }
.field input::placeholder { color: var(--text-3); }
.field-lg { height: 44px; border-radius: var(--radius-md); font-size: 14px; }

select.picker {
  appearance: none; -webkit-appearance: none;
  background: var(--card) url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat right 10px center;
  border: 1px solid var(--border); border-radius: var(--radius-md);
  height: 34px; padding: 0 30px 0 11px; color: var(--text); cursor: pointer;
  transition: border-color .15s ease;
}
select.picker:hover { border-color: var(--border-strong); }
select.picker:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px var(--accent-soft); }

.cb {
  appearance: none; -webkit-appearance: none;
  width: 15px; height: 15px; flex: none; cursor: pointer;
  border: 1px solid var(--border-strong); border-radius: 4px; background: var(--card);
  transition: background .12s ease, border-color .12s ease;
}
.cb:checked {
  background: var(--primary) url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20 6 9 17l-5-5'/%3E%3C/svg%3E") no-repeat center;
  border-color: var(--primary);
}

.switch {
  appearance: none; -webkit-appearance: none;
  width: 34px; height: 20px; flex: none; margin-left: auto;
  border-radius: 999px; background: var(--border-strong); cursor: pointer;
  position: relative; transition: background .16s ease;
}
.switch::after {
  content: ""; position: absolute; top: 2px; left: 2px;
  width: 16px; height: 16px; border-radius: 50%; background: #fff;
  transition: transform .16s var(--ease);
}
.switch:checked { background: var(--primary); }
.switch:checked::after { transform: translateX(14px); }

/* ============================================================ cards & rows */
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
}
.card.hover { transition: border-color .15s ease, box-shadow .15s ease; }
.card.hover:hover { border-color: var(--border-strong); box-shadow: var(--shadow-2); }

.section-head {
  display: flex; align-items: center; gap: 8px;
  margin: 20px 2px 8px; font-size: 11px; font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase; color: var(--text-3);
}
.section-head:first-child { margin-top: 6px; }

.row-card {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 14px; margin-bottom: 8px;
}
.row-main { flex: 1; min-width: 0; }
.row-title { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-sub { font-size: 12.5px; color: var(--text-2); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-meta { font-size: 11.5px; color: var(--text-3); margin-top: 3px; display: flex; gap: 10px; flex-wrap: wrap; }
.row-actions { display: flex; gap: 6px; flex: none; align-items: center; }

.site-tag {
  font-size: 10.5px; font-weight: 600; color: var(--text-2);
  background: transparent; border: 1px solid var(--border);
  padding: 1px 6px; border-radius: 4px;
}
.mono { font-variant-numeric: tabular-nums; }

.pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10.5px; font-weight: 700; letter-spacing: .03em;
  padding: 1px 7px; border-radius: 4px;
}
.pill::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.pill.queued    { color: var(--yellow); background: var(--yellow-bg); }
.pill.running   { color: var(--accent); background: var(--accent-soft); }
.pill.processing{ color: var(--blue);   background: var(--blue-bg); }
.pill.exporting { color: var(--purple); background: var(--purple-bg); }
.pill.completed { color: var(--green);  background: var(--green-bg); }
.pill.failed    { color: var(--red);    background: var(--red-bg); }
.pill.cancelled { color: var(--gray);   background: var(--gray-bg); }

.progress { height: 4px; border-radius: 2px; background: var(--hover); overflow: hidden; margin-top: 9px; }
.progress > i {
  display: block; height: 100%; width: 0; border-radius: 2px;
  background: var(--primary);
  transition: width .4s var(--ease);
}
.progress.indeterminate > i { width: 30% !important; animation: slide 1.4s ease-in-out infinite; }
@keyframes slide { 0% { transform: translateX(-110%); } 100% { transform: translateX(390%); } }

/* task rows carry their state on a left stripe */
.row-card.task { position: relative; padding-left: 17px; }
.row-card.task::before {
  content: ""; position: absolute; left: 0; top: 12px; bottom: 12px;
  width: 2px; border-radius: 0 2px 2px 0; background: var(--border-strong);
}
.row-card.task.st-running::before,
.row-card.task.st-processing::before,
.row-card.task.st-exporting::before { background: var(--accent); }
.row-card.task.st-queued::before { background: var(--yellow); }
.row-card.task.st-completed::before { background: var(--green); }
.row-card.task.st-failed::before { background: var(--red); }
.row-card.task.st-cancelled::before { background: var(--gray); }

/* ============================================================ search page */
.search-zone { margin: 2px 0 0; width: 100%; }
.search-row { display: flex; gap: 8px; }
.search-row .field { flex: 1; }
.filters { display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
.filters .field { height: 34px; border-radius: var(--radius-md); flex: 1; min-width: 240px; }
.filters .merge-toggle { margin-left: auto; }
.hint { font-size: 11.5px; color: var(--text-3); }
.spin {
  width: 14px; height: 14px; border-radius: 50%; flex: none;
  border: 2px solid var(--accent-soft-2); border-top-color: var(--accent);
  animation: rot .7s linear infinite; display: none;
}
.spin.on { display: inline-block; }
@keyframes rot { to { transform: rotate(360deg); } }

.result-card { padding: 12px 14px; margin-bottom: 8px; cursor: pointer; }
.result-card:hover { border-color: var(--border-strong); }
.result-card.sel { border-color: var(--brand); box-shadow: inset 0 0 0 1px var(--brand); }
.rc { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 12px; align-items: start; }
.rc-cover {
  position: relative; width: 44px; height: 60px; margin-top: 1px;
  border-radius: var(--radius-sm); overflow: hidden;
  display: grid; place-items: center;
  background: var(--card-2); border: 1px solid var(--border);
}
.rc-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.rc-cover span { font-weight: 600; font-size: 17px; color: var(--brand); opacity: .55; }
.rc-main { min-width: 0; }
.rc-main .row-title { font-size: 14px; }
.rc-side { display: flex; flex-direction: column; align-items: flex-end; gap: 7px; flex: none; }
.rc-side .src-caps { max-width: 260px; }
@media (max-width: 720px) {
  .rc { grid-template-columns: 38px minmax(0, 1fr); }
  .rc-cover { width: 38px; height: 52px; }
  .rc-side { grid-column: 1 / -1; flex-direction: row; align-items: center; justify-content: space-between; width: 100%; }
}

.split { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
.split > #results { margin-top: 14px; }
.split > #detail { margin-top: 14px; position: relative; }
.side-card { padding: 16px; }
/* Close affordance for the narrow-window drawer; hidden in the wide layout
   where the panel is a permanent sidebar and needs no dismissal. */
.detail-close {
  display: none; position: absolute; top: 8px; right: 8px;
  width: 28px; height: 28px; place-items: center;
  border-radius: var(--radius-sm); background: var(--card); color: var(--text-3);
}
.detail-close:hover { background: var(--hover); color: var(--text); }
#detail-backdrop { display: none; }
.detail-head { display: flex; gap: 12px; align-items: flex-start; }
.detail-cover { width: 80px; height: 106px; object-fit: cover; border-radius: var(--radius-sm);
  flex: none; background: var(--card-2); border: 1px solid var(--border); }
.detail-cover-blank {
  display: grid; place-items: center;
  background: var(--card-2);
  font-weight: 600; font-size: 26px; color: var(--brand); opacity: .6;
}
.detail-meta { display: flex; align-items: baseline; gap: 6px; margin-top: 7px; }
.detail-meta .k {
  font-size: 10px; letter-spacing: .07em; text-transform: uppercase;
  color: var(--text-3); font-weight: 700;
}
.detail-meta .v { font-size: 12.5px; color: var(--text-2); }
@media (max-width: 1180px) {
  .split { grid-template-columns: minmax(0, 1fr); }
  /* The wide layout's third column is gone, so the details panel becomes a
     bottom sheet over the results: same DOM node (and id), explicit close
     button, a backdrop to click away, and no horizontal overflow. */
  .split > #detail {
    display: block;
    position: fixed; left: 0; right: 0; bottom: 0; top: auto;
    z-index: 70; margin: 0;
    max-height: min(76vh, 620px);
    overflow-y: auto; overflow-x: hidden;
    border-radius: 14px 14px 0 0;
    border-bottom: none;
    box-shadow: var(--shadow-3);
    transform: translateY(100%);
    visibility: hidden;
    transition: transform .22s var(--ease), visibility .22s;
  }
  .split > #detail.open { transform: translateY(0); visibility: visible; }
  .split > #detail > .detail-close { display: grid; }
  .split > #detail .detail-head { padding-right: 28px; }
  #detail-backdrop.show {
    display: block; position: fixed; inset: 0; z-index: 65;
    background: rgba(15, 23, 42, .42);
  }
}
.best-chip {
  font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  color: var(--green); background: var(--green-bg); border-radius: 4px; padding: 1px 6px;
}
mark { background: var(--accent-soft-2); color: var(--text); border-radius: 2px; padding: 0 1px; }
.merge-toggle {
  display: flex; align-items: center; gap: 6px; flex: none;
  font-size: 12px; color: var(--text-2); cursor: pointer; user-select: none;
}
.merge-toggle:hover { color: var(--text); }
.src-caps { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; flex: none; }
.src-caps .site-tag { color: var(--text-3); }
.src-caps .site-tag.cap-best { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.src-caps .site-tag.cap-more { border-style: dashed; }

.empty { text-align: center; color: var(--text-3); padding: 64px 20px; }
.empty svg.icon { width: 34px; height: 34px; opacity: .35; margin-bottom: 10px; }
.empty .t { font-size: 13.5px; font-weight: 600; color: var(--text-2); }
.empty .s { font-size: 12px; margin-top: 3px; }

/* ============================================================ history */
.file-ico {
  width: 34px; height: 34px; border-radius: var(--radius-sm); flex: none;
  display: grid; place-items: center; font-size: 9px; font-weight: 800; letter-spacing: .04em;
  background: var(--card-2); border: 1px solid var(--border); color: var(--text-2);
}
.file-ico.epub { color: var(--purple); background: var(--purple-bg); border-color: transparent; }
.file-ico.txt  { color: var(--blue);   background: var(--blue-bg);   border-color: transparent; }
.file-ico.org  { color: var(--green);  background: var(--green-bg);  border-color: transparent; }

/* ============================================================ music */
.music-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
@media (max-width: 1080px) { .music-grid { grid-template-columns: minmax(0, 1fr); } }
.set-row {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 0; font-size: 12.5px; color: var(--text-2);
  border-bottom: 1px solid var(--border);
}
.set-row:last-of-type { border-bottom: none; }
.set-row .lbl { flex: 1; min-width: 0; }
.set-row select.picker { height: 30px; font-size: 12px; }
.track-row {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; border-bottom: 1px solid var(--border);
  transition: background .12s ease;
}
.track-row:hover { background: var(--hover); }
.track-row:last-child { border-bottom: none; }
.track-row .idx { width: 22px; text-align: right; font-size: 11px; color: var(--text-3); flex: none; }
.vip-tag {
  font-size: 9.5px; font-weight: 700; letter-spacing: .04em;
  color: var(--yellow); background: var(--yellow-bg);
  border-radius: 3px; padding: 1px 5px;
}
.src-tags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; max-width: 220px; flex: none; }
.src-tag {
  font-size: 9.5px; font-weight: 600; line-height: 1.5;
  color: var(--text-2); background: var(--card-2); border: 1px solid var(--border);
  border-radius: 3px; padding: 1px 5px; white-space: nowrap; max-width: 92px;
  overflow: hidden; text-overflow: ellipsis;
}
.health-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.health-dot.ok { background: var(--green); }
.health-dot.bad { background: var(--red); }
.health-dot.wait { background: var(--border-strong); }

/* Playlist export result: the written path stays on screen (and selectable),
   so copying it does not depend on a clipboard permission. */
.export-note {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0 2px 12px; padding: 10px 12px;
  background: var(--green-bg); border-radius: var(--radius-md);
  font-size: 12px; color: var(--text-2);
}
.export-note svg.icon { color: var(--green); flex: none; }
.export-note-main { flex: 1 1 240px; min-width: 0; }
.export-note-title { font-weight: 600; color: var(--text); }
.export-note-path {
  display: block; width: 100%; margin-top: 3px;
  border: none; outline: none; background: transparent;
  color: var(--text-2); font: 12px/1.45 ui-monospace, monospace;
  text-overflow: ellipsis; user-select: text;
}

/* ============================================================ modal */
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, .45);
  display: grid; place-items: center; z-index: 60;
  animation: fade .16s ease; backdrop-filter: blur(2px);
}
@keyframes fade { from { opacity: 0; } }
.modal {
  width: min(440px, calc(100vw - 56px));
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
  box-shadow: var(--shadow-3); padding: 20px;
  animation: pop .18s var(--ease);
}
@keyframes pop { from { opacity: 0; transform: translateY(6px); } }
.modal h3 { font-size: 15px; font-weight: 650; margin-bottom: 14px; }
.modal .field { margin-bottom: 8px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* ============================================================ toasts */
#toasts { position: fixed; top: 14px; right: 16px; z-index: 100; display: flex; flex-direction: column; gap: 8px; }
.toast {
  display: flex; align-items: stretch; gap: 10px;
  width: min(380px, calc(100vw - 32px));
  background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-md);
  box-shadow: var(--shadow-2); padding: 10px 10px 10px 0;
  font-size: 12.5px; animation: toastIn .18s var(--ease);
}
.toast.leaving { opacity: 0; transition: opacity .18s ease; }
@keyframes toastIn { from { opacity: 0; transform: translateY(-4px); } }
.toast .bar { width: 2px; border-radius: 2px; align-self: stretch; flex: none; margin-right: 2px; }
.toast.ok .bar { background: var(--green); } .toast.err .bar { background: var(--red); }
.toast.info .bar { background: var(--blue); } .toast.warn .bar { background: var(--yellow); }
.toast .msg { flex: 1; user-select: text; word-break: break-word; color: var(--text); }
.toast .x { color: var(--text-3); flex: none; padding: 0 2px; }
.toast .x:hover { color: var(--text); }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .01ms !important;
  }
}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute"><defs>
<symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></symbol>
<symbol id="i-hist" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9"/><path d="M3 4v5h5"/><path d="M12 8v4l3 2"/></symbol>
<symbol id="i-music" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></symbol>
<symbol id="i-heart" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7Z"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-ext" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></symbol>
<symbol id="i-folder" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9L9.2 3.9A2 2 0 0 0 7.5 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></symbol>
<symbol id="i-book" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
<symbol id="i-pulse" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 8L9 4l-3 8H2"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
<symbol id="i-desktop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/></symbol>
</defs></svg>

<div class="app">
  <aside class="sidebar">
    <div class="logo">
      <div class="logo-mark"><svg class="icon"><use href="#i-book"/></svg></div>
      <div><div class="logo-name">书音</div><div class="logo-sub" id="ver"></div></div>
    </div>
    <nav class="nav" id="nav">
      <button class="nav-item active" data-page="search"><svg class="icon"><use href="#i-search"/></svg><span data-i18n="Search">Search</span><kbd>1</kbd></button>
      <button class="nav-item" data-page="download"><svg class="icon"><use href="#i-down"/></svg><span data-i18n="Download">Download</span><kbd>2</kbd><span class="count-chip live" id="badge-live" hidden></span></button>
      <button class="nav-item" data-page="history"><svg class="icon"><use href="#i-hist"/></svg><span data-i18n="History">History</span><kbd>3</kbd></button>
      <button class="nav-item" data-page="music"><svg class="icon"><use href="#i-music"/></svg><span data-i18n="Music">Music</span><kbd>4</kbd></button>
      <button class="nav-item" data-page="follow"><svg class="icon"><use href="#i-heart"/></svg><span data-i18n="Follow">Follow</span><kbd>5</kbd></button>
    </nav>
    <div class="side-foot">
      <div class="update-bar" id="update-bar" hidden>
        <div class="u-text">
          <svg class="icon"><use href="#i-down"/></svg>
          <span id="update-text" data-i18n="Update available: v{ver}">Update available: v{ver}</span>
          <span class="u-size" id="update-size"></span>
        </div>
        <div class="u-acts">
          <button class="btn btn-primary btn-sm" id="update-install" data-i18n="Download and install">Download and install</button>
          <button class="btn btn-ghost btn-sm" id="update-notes" data-i18n="Release notes">Release notes</button>
          <button class="btn btn-ghost btn-sm" id="update-later" data-i18n="Later">Later</button>
        </div>
      </div>
      <div class="foot-row">
        <button class="icon-btn" id="shortcut-btn" data-i18n-title="Create desktop shortcut" title="Create desktop shortcut"><svg class="icon"><use href="#i-desktop"/></svg></button>
        <button class="icon-btn" id="theme-btn" data-i18n-title="Theme" title="Theme"><svg class="icon"><use href="#i-moon"/></svg></button>
        <div class="foot-meta"><span data-i18n="Ctrl+1-5 switch pages">Ctrl+1-5 switch pages</span><br>Ctrl+F / Ctrl+D</div>
      </div>
    </div>
  </aside>

  <main class="main">

    <!-- ============================================= SEARCH -->
    <section class="page active" id="page-search">
      <div class="topbar"><div><h1 data-i18n="Search">Search</h1><div class="sub" data-i18n="Search novels across all sites, ranked by quality">Search novels across all sites, ranked by quality</div></div></div>
      <div class="scroll">
        <div class="search-zone">
          <div class="search-row">
            <div class="field field-lg">
              <svg class="icon"><use href="#i-search"/></svg>
              <input id="q" data-i18n-ph="Enter book title or author…" placeholder="Enter book title or author…" autocomplete="off">
            </div>
            <button class="btn btn-primary btn-lg" id="btn-search"><span class="spin" id="search-spin"></span><svg class="icon"><use href="#i-search"/></svg><span data-i18n="Search">Search</span></button>
          </div>
          <div class="filters">
            <div class="field"><svg class="icon"><use href="#i-pulse"/></svg><input id="q-sites" data-i18n-ph="Site filter, comma separated (blank = all)" placeholder="Site filter, comma separated (blank = all)"></div>
            <label class="merge-toggle" for="q-group"><input type="checkbox" class="cb" id="q-group" checked><span data-i18n="Merge same book">Merge same book</span></label>
            <button class="btn btn-ghost btn-sm" id="btn-rank"><svg class="icon"><use href="#i-pulse"/></svg><span data-i18n="Rankings">Rankings</span></button>
            <button class="btn btn-ghost btn-sm" id="btn-cats"><svg class="icon"><use href="#i-book"/></svg><span data-i18n="Categories">Categories</span></button>
            <span class="hint" id="search-hint"></span>
          </div>
        </div>
        <div class="card" id="cat-panel" hidden style="margin-top:16px;padding:14px 16px">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
            <button class="btn btn-sm btn-primary" id="cat-male" data-i18n="Male">Male</button>
            <button class="btn btn-sm btn-ghost" id="cat-female" data-i18n="Female">Female</button>
            <span class="hint" id="cat-hint"></span>
          </div>
          <div id="cat-chips" class="filters" style="gap:8px;flex-wrap:wrap"></div>
        </div>
        <div class="split">
          <div id="results">
            <div class="empty">
              <svg class="icon"><use href="#i-search"/></svg>
              <div class="t" data-i18n="Search to see results">Search to see results</div>
              <div class="s" data-i18n="Type a title, or browse the rankings and categories">Type a title, or browse the rankings and categories</div>
            </div>
          </div>
          <aside class="card side-card" id="detail" hidden></aside>
        </div>
      </div>
    </section>

    <!-- ============================================= DOWNLOAD -->
    <section class="page" id="page-download">
      <div class="topbar">
        <div><h1 data-i18n="Download">Download</h1><div class="sub" data-i18n="Live queue and progress">Live queue and progress</div></div>
        <div class="spacer"></div>
        <button class="btn btn-ghost" id="btn-refresh-tasks"><svg class="icon"><use href="#i-refresh"/></svg><span data-i18n="Refresh">Refresh</span></button>
      </div>
      <div class="scroll" id="tasks-body"></div>
    </section>

    <!-- ============================================= HISTORY -->
    <section class="page" id="page-history">
      <div class="topbar">
        <div><h1 data-i18n="History">History</h1><div class="sub" id="hist-sub"></div></div>
        <div class="spacer"></div>
        <button class="btn btn-ghost" id="btn-open-dl"><svg class="icon"><use href="#i-folder"/></svg><span data-i18n="Open folder">Open folder</span></button>
        <button class="btn btn-ghost" id="btn-refresh-hist"><svg class="icon"><use href="#i-refresh"/></svg><span data-i18n="Refresh">Refresh</span></button>
      </div>
      <div class="scroll" id="hist-body"></div>
    </section>

    <!-- ============================================= MUSIC -->
    <section class="page" id="page-music">
      <div class="topbar">
        <div><h1 data-i18n="Music">Music</h1><div class="sub" data-i18n="Search and download across 25 music platforms">Search and download across 25 music platforms</div></div>
      </div>
      <div class="scroll">
        <div class="music-grid">
          <div>
            <div class="search-row">
              <select class="picker" id="m-platform" style="height:48px;border-radius:13px;max-width:230px"></select>
              <div class="field field-lg"><svg class="icon"><use href="#i-music"/></svg><input id="m-q" data-i18n-ph="Song / artist / album…" placeholder="Song / artist / album…" autocomplete="off"></div>
              <button class="btn btn-primary" id="btn-m-search" style="height:48px"><span class="spin" id="m-spin"></span><span data-i18n="Search">Search</span></button>
            </div>
            <div class="hint" id="m-note" style="margin-top:9px"></div>
            <div class="section-head"><span data-i18n="Results">Results</span><span class="count-chip" id="m-count">0</span>
              <div class="spacer" style="flex:1"></div>
              <button class="btn btn-sm btn-ghost" id="btn-m-export"><svg class="icon"><use href="#i-down"/></svg><span data-i18n="Export playlist (.m3u8)">Export playlist (.m3u8)</span></button>
              <button class="btn btn-sm btn-ghost" id="btn-m-selall"><span data-i18n="Select all">Select all</span></button>
              <button class="btn btn-sm btn-danger" id="btn-m-stop" hidden><svg class="icon"><use href="#i-stop"/></svg><span data-i18n="Stop">Stop</span></button>
              <button class="btn btn-sm btn-primary" id="btn-m-dl"><svg class="icon"><use href="#i-down"/></svg><span id="m-dl-label" data-i18n="Download selected">Download selected</span></button>
            </div>
            <div class="progress" id="m-progress" style="margin:0 2px 12px" hidden><i></i></div>
            <div class="export-note" id="m-export-note" hidden>
              <svg class="icon"><use href="#i-check"/></svg>
              <div class="export-note-main">
                <div class="export-note-title" id="m-export-title"></div>
                <input class="export-note-path" id="m-export-path" readonly>
              </div>
              <button class="btn btn-ghost btn-sm" id="m-export-copy" data-i18n="Copy">Copy</button>
              <button class="btn btn-ghost btn-sm" id="m-export-open"><svg class="icon"><use href="#i-folder"/></svg><span data-i18n="Open">Open</span></button>
            </div>
            <div id="m-tracks"></div>
          </div>
          <div class="card side-card">
            <div class="row-title" style="margin-bottom:8px" data-i18n="Quality & tags">Quality &amp; tags</div>
            <div class="set-row"><span class="lbl" data-i18n="Quality">Quality</span>
              <select class="picker" id="m-level" style="height:32px">
                <option value="hires">Hi-Res</option><option value="lossless">Lossless</option>
                <option value="exhigh">ExHigh</option><option value="higher">Higher</option>
                <option value="standard">Standard</option>
              </select></div>
            <label class="set-row"><span class="lbl" data-i18n="Embed tags">Embed tags</span><input type="checkbox" class="switch" id="m-tags" checked></label>
            <label class="set-row"><span class="lbl" data-i18n="Embed cover">Embed cover</span><input type="checkbox" class="switch" id="m-cover" checked></label>
            <label class="set-row"><span class="lbl" data-i18n="Save lyrics">Save lyrics</span><input type="checkbox" class="switch" id="m-lyric" checked></label>
            <label class="set-row"><span class="lbl" data-i18n="Split folders">Split folders</span><input type="checkbox" class="switch" id="m-split" checked></label>
            <label class="set-row"><span class="lbl" data-i18n="Skip existing">Skip existing</span><input type="checkbox" class="switch" id="m-skip" checked></label>
            <div class="section-head" style="margin-top:14px"><span data-i18n="Platform health">Platform health</span></div>
            <button class="btn btn-ghost btn-sm" id="btn-m-health" style="width:100%"><svg class="icon"><use href="#i-pulse"/></svg><span data-i18n="Check all platforms">Check all platforms</span></button>
            <div id="m-health" style="margin-top:8px"></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================= FOLLOW -->
    <section class="page" id="page-follow">
      <div class="topbar">
        <div><h1 data-i18n="Follow">Follow</h1><div class="sub" data-i18n="Track new chapters of books you care about">Track new chapters of books you care about</div></div>
        <div class="spacer"></div>
        <button class="btn btn-ghost" id="btn-f-add"><svg class="icon"><use href="#i-plus"/></svg><span data-i18n="Add book">Add book</span></button>
        <button class="btn btn-primary" id="btn-f-check"><span class="spin" id="f-spin"></span><svg class="icon"><use href="#i-refresh"/></svg><span data-i18n="Check updates">Check updates</span></button>
      </div>
      <div class="scroll" id="follow-body"></div>
    </section>

  </main>
</div>
<div id="detail-backdrop"></div>
<div id="toasts"></div>
<script>
"use strict";
/* ================================================================ i18n */
const I18N = Object.create(null);
function t(key) { return I18N[key] || key; }
function applyI18n(root) {
  document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.dataset.i18nTitle); });
}

/* ================================================================ helpers */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
function api(method, ...args) {
  const fn = window.pywebview && window.pywebview.api && window.pywebview.api[method];
  if (!fn) return Promise.reject(new Error("bridge not ready"));
  return fn.apply(window.pywebview.api, args);
}
async function call(method, ...args) {
  // Bridge errors are toasted here and the call resolves to ``undefined``:
  // DOM handlers await ``call`` without wrapping it, so rethrowing would only
  // surface as an unhandled promise rejection in the console.
  try { return await api(method, ...args); }
  catch (err) { toast((err && err.message) || String(err), "err"); }
  return undefined;
}

function toast(msg, kind = "info", ms = 3800, onclick = null) {
  const box = document.createElement("div");
  box.className = "toast " + kind;
  box.innerHTML = `<span class="bar"></span><span class="msg">${esc(msg)}</span><button class="x">✕</button>`;
  box.querySelector(".x").onclick = () => dismiss();
  if (onclick) {
    box.style.cursor = "pointer";
    box.addEventListener("click", e => {
      if (e.target.closest(".x")) return;
      onclick();
      dismiss();
    });
  }
  $("#toasts").appendChild(box);
  const timer = setTimeout(dismiss, ms);
  function dismiss() { clearTimeout(timer); box.classList.add("leaving"); setTimeout(() => box.remove(), 240); }
}

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function fmtDur(s) { if (!s || s <= 0) return ""; const m = Math.floor(s / 60), r = Math.round(s % 60); return m + ":" + String(r).padStart(2, "0"); }
function fmtEta(sec) { if (sec == null || sec <= 0) return ""; const m = Math.round(sec); if (m < 60) return "~" + m + "s"; return "~" + Math.floor(m / 60) + "m" + (m % 60) + "s"; }
function fmtTime(ts) { if (!ts) return ""; const d = new Date(ts * 1000); const p = x => String(x).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; }

function pill(status) { return `<span class="pill ${esc(status)}">${esc(t(status_label_key(status)))}</span>`; }
function status_label_key(status) { return { queued: "Queued", running: "Downloading", processing: "Processing", exporting: "Exporting", completed: "Completed", failed: "Failed", cancelled: "Cancelled" }[status] || status; }

function highlight(text, kw) {
  const raw = String(text ?? "");
  if (!kw) return esc(raw);
  try { return esc(raw).replace(new RegExp(esc(kw).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), m => `<mark>${m}</mark>`); }
  catch { return esc(raw); }
}

/* ================================================================ router */
let currentPage = "search";
const loaders = { search: null, download: refreshTasks, history: refreshHistory, music: initMusic, follow: refreshFollow };
function showPage(name) {
  currentPage = name;
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + name));
  const focusMap = { search: "#q", music: "#m-q" };
  if (focusMap[name]) setTimeout(() => $(focusMap[name]) && $(focusMap[name]).focus(), 60);
  if (loaders[name]) loaders[name]();
}
$("#nav").addEventListener("click", e => { const b = e.target.closest(".nav-item"); if (b) showPage(b.dataset.page); });

/* ================================================================ theme */
function setTheme(raw) {
  const mode = raw === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = mode;
  $("#theme-btn").innerHTML = `<svg class="icon"><use href="#i-${mode === "dark" ? "sun" : "moon"}"/></svg>`;
  store.set("theme", mode);
  api("save_prefs", { theme: mode }).catch(() => {});
}
$("#theme-btn").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

/* ================================================================ shortcuts */
$("#shortcut-btn").onclick = async () => {
  const r = await call("create_shortcut");
  if (r && r.message) toast(r.message, r.ok ? "ok" : "warn");
};

/* ================================================================ updates */
// Low-key bar in the sidebar: it only exists while a newer release is known.
// "Later" hides it for this session (localStorage is never touched).
const updates = { busy: false, dismissed: false, finished: false, state: null };
let updatePollTimer = null;

function applyUpdateState(st) {
  if (!st) return;
  updates.state = st;
  const bar = $("#update-bar");
  const show = !!st.available && !updates.dismissed;
  bar.hidden = !show;
  if (!show) return;
  // A release can be newer without shipping anything for this platform; say
  // so instead of showing an update with no way to act on it.
  const noAsset = !st.asset_name;
  $("#update-text").textContent = noAsset
    ? t("No package for this platform in v{ver}; open the release page").replace("{ver}", st.latest || "")
    : t("Update available: v{ver}").replace("{ver}", st.latest || "");
  $("#update-size").textContent = st.asset_size ? fmtSize(st.asset_size) : "";
  $("#update-notes").hidden = !(st.notes_url || st.url);
  $("#update-notes").classList.toggle("btn-primary", noAsset);
  $("#update-install").hidden = noAsset;
}

function setUpdateBusy(busy) {
  updates.busy = busy;
  for (const id of ["#update-install", "#update-notes", "#update-later"]) $(id).disabled = busy;
}

function pollUpdateState() {
  if (updatePollTimer) clearInterval(updatePollTimer);
  // The check runs on a background thread; poll until it lands. A bounded
  // number of attempts keeps an offline machine from poking the bridge forever.
  let left = 10;
  updatePollTimer = setInterval(async () => {
    let st = null;
    try { st = await api("update_state"); } catch { st = null; }
    applyUpdateState(st);
    if (!st || !st.pending || --left <= 0) {
      clearInterval(updatePollTimer);
      updatePollTimer = null;
    }
  }, 1200);
}

function bootUpdateCheck() {
  // Prefer the non-blocking bridge; the legacy blocking ``check_update`` call
  // stays for a backend that predates ``check_update_async``.
  const bridge = (window.pywebview && window.pywebview.api) || {};
  if (typeof bridge.check_update_async === "function") {
    api("check_update_async").then(r => { applyUpdateState(r); pollUpdateState(); }).catch(() => {});
  } else {
    api("check_update").then(r => {
      if (r && r.available) applyUpdateState(r);
    }).catch(() => {});
  }
}

$("#update-later").onclick = () => {
  updates.dismissed = true;
  $("#update-bar").hidden = true;
};

$("#update-notes").onclick = () => {
  const st = updates.state || {};
  const url = st.notes_url || st.url;
  if (url) api("open_url", url).catch(() => {});
};

$("#update-install").onclick = async () => {
  if (updates.busy) return;
  const btn = $("#update-install");
  // Disable synchronously (before the await): a double click must not start
  // two downloads or two installer launches.
  setUpdateBusy(true);
  // The backend downloads an announced update in the background, so by the
  // time the bar is clicked the installer is usually already on disk.
  const ready = !!(updates.state && updates.state.downloaded);
  if (!ready) btn.textContent = t("Downloading…");
  try {
    if (!ready) {
      const dl = await api("download_update");
      if (!dl || !dl.ok || !dl.path) {
        toast((dl && dl.error) || t("Update download failed"), "err", 9000);
        return;
      }
    }
    btn.textContent = t("Installing…");
    const res = await api("install_update");
    if (!res || !res.ok) {
      toast((res && res.error) || t("Could not start the installer"), "err", 12000);
      return;
    }
    // The backend closes the window right after launching the installer.
    updates.finished = true;
  } catch (e) {
    toast(String((e && e.message) || e), "err", 9000);
  } finally {
    if (!updates.finished) {
      setUpdateBusy(false);
      btn.textContent = t("Download and install");
    }
  }
};

/* ================================================================ search */
let searching = false;
let lastResults = [];
let lastKeyword = "";
let selIndex = -1;
/* Below 1180px the details panel is a bottom sheet instead of a third column
   (see the media query); these helpers keep its open/closed state, focus and
   the backdrop in sync without changing the wide layout. */
const narrowDetail = window.matchMedia("(max-width: 1180px)");
let detailReturnFocus = null;
function detailIsOpen() { const box = $("#detail"); return !!box && box.classList.contains("open"); }
function hideDetail() {
  const box = $("#detail");
  box.hidden = true;
  box.classList.remove("open");
  $("#detail-backdrop").classList.remove("show");
}
function openDetail() {
  const box = $("#detail");
  if (!box || box.hidden) return;
  if (!box.classList.contains("open")) {
    // A programmatic click may leave focus on <body>; fall back to the row
    // that was just selected so closing always has somewhere to return to.
    const active = document.activeElement;
    detailReturnFocus = (active && active !== document.body)
      ? active
      : ($("#results .result-card.sel") || $("#q"));
  }
  box.classList.add("open");
  $("#detail-backdrop").classList.add("show");
  const close = box.querySelector("[data-detail-close]");
  if (close) close.focus({ preventScroll: true });
}
function closeDetail() {
  const box = $("#detail");
  box.classList.remove("open");
  $("#detail-backdrop").classList.remove("show");
  // Put focus back on the card that opened the drawer (or the search field).
  const target = (detailReturnFocus && document.contains(detailReturnFocus))
    ? detailReturnFocus
    : ($("#results .result-card.sel") || $("#q"));
  detailReturnFocus = null;
  if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
}
narrowDetail.addEventListener("change", e => {
  if (!e.matches) { $("#detail").classList.remove("open"); $("#detail-backdrop").classList.remove("show"); }
});
$("#detail-backdrop").addEventListener("click", closeDetail);
function selectResult(i) {
  if (i < 0 || i >= lastResults.length) return;
  selIndex = i;
  document.querySelectorAll("#results .result-card").forEach((el, k) => el.classList.toggle("sel", k === i));
  renderDetail(lastResults[i], lastKeyword);
  if (narrowDetail.matches) openDetail();
}
function renderDetail(r, kw) {
  const box = $("#detail");
  if (!r) { hideDetail(); box.innerHTML = ""; return; }
  let cover = r.cover_url || "";
  if (cover.startsWith("//")) cover = "https:" + cover;
  const img = cover
    ? `<img class="detail-cover" src="${esc(cover)}" alt="" referrerpolicy="no-referrer">`
    : `<div class="detail-cover detail-cover-blank"><span>${esc((r.title || "?").trim().slice(0, 1) || "?")}</span></div>`;
  // The panel is narrow: the "Author:" prefix would push this line into a
  // ragged wrap, so the values speak for themselves here.
  const meta = [r.author, r.word_count, r.update_date].filter(Boolean).map(esc).join('<span style="opacity:.45"> · </span>');
  box.hidden = false;
  box.innerHTML = `
    <button type="button" class="detail-close" data-detail-close aria-label="${esc(t("Close"))}" title="${esc(t("Close"))}"><svg class="icon"><use href="#i-x"/></svg></button>
    <div class="detail-head">
      ${img}
      <div style="min-width:0">
        <div class="row-title" style="white-space:normal">${highlight(r.title, kw || "")}</div>
        <div class="row-sub" style="white-space:normal">${meta}</div>
        ${r.latest_chapter ? `<div class="row-meta">${esc(r.latest_chapter)}</div>` : ""}
        <div class="detail-meta"><span class="k">${esc(t("Source"))}</span><span class="v">${esc(r.site)}</span></div>
      </div>
    </div>
    <div class="row-actions" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn btn-primary btn-sm" data-add="${esc(r.site)}" data-bid="${esc(r.book_id)}" data-title="${esc(r.title)}"><svg class="icon"><use href="#i-down"/></svg>${esc(t("Download"))}</button>
      <button class="btn btn-ghost btn-sm" data-follow="${esc(r.site)}" data-bid="${esc(r.book_id)}" data-title="${esc(r.title)}"><svg class="icon"><use href="#i-heart"/></svg>${esc(t("Follow"))}</button>
    </div>
    <button class="btn btn-ghost btn-sm" style="margin-top:7px;width:100%" data-open-url="${esc(r.book_url || "")}"><svg class="icon"><use href="#i-ext"/></svg>${esc(t("Open in browser"))}</button>`;
}

async function doSearch() {
  const kw = $("#q").value.trim();
  if (!kw || searching) return;
  searching = true;
  $("#search-spin").classList.add("on");
  $("#btn-search").disabled = true;
  $("#search-hint").textContent = t("Searching…");
  $("#results").innerHTML = "";
  const started = performance.now();
  let rendered = -1;
  try {
    const job = await api("search_async", kw, $("#q-sites").value.trim(), $("#q-group").checked);
    if (!job || !job.search_id) throw new Error(t("Search failed"));
    for (;;) {
      // Poll while the sites answer: the fastest ones land in ~0.2s instead
      // of the page waiting for the slowest (bounded by the bridge budget).
      await new Promise(resolve => setTimeout(resolve, 250));
      const st = await api("search_progress", job.search_id);
      if (!st) break;
      // Bridge failures come back inside the payload, not as exceptions.
      if (st.error) throw new Error(st.error);
      const results = st.results || [];
      if (results.length !== rendered) {
        rendered = results.length;
        lastResults = results; lastKeyword = kw; selIndex = -1;
        hideDetail();
        renderResults(results, kw);
      }
      const secs = ((performance.now() - started) / 1000).toFixed(1);
      $("#search-hint").textContent = t("{n} results in {s}s").replace("{n}", results.length).replace("{s}", secs);
      if (st.done) break;
    }
  } catch (e) {
    $("#results").innerHTML = `<div class="empty"><svg class="icon"><use href="#i-search"/></svg><div class="t">${esc(t("Search failed"))}</div><div class="s">${esc(String(e && e.message || e))}</div></div>`;
    $("#search-hint").textContent = "";
  } finally {
    searching = false;
    $("#search-spin").classList.remove("on");
    $("#btn-search").disabled = false;
  }
}
function renderResults(results, kw) {
  const box = $("#results");
  if (!results.length) {
    box.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-search"/></svg><div class="t">${esc(t("No results"))}</div><div class="s">${esc(t("Try another keyword or clear the site filter"))}</div><button class="btn btn-ghost btn-sm" id="search-in-music" style="margin-top:12px"><svg class="icon"><use href="#i-music"/></svg>${esc(t("Search in Music"))}</button></div>`;
    return;
  }
  box.innerHTML = results.map((r, i) => {
    const sub = [r.author, r.word_count, r.update_date].filter(Boolean).map(esc).join('<span style="opacity:.4"> · </span>');
    return `<div class="card hover result-card" data-sel="${i}" tabindex="0" role="group" aria-label="${esc(r.title)}">
      <div class="rc">
        ${coverBox(r)}
        <div class="rc-main">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="row-title" style="white-space:normal">${highlight(r.title, kw)}</span>
            ${r.best ? `<span class="best-chip">${esc(t("Best match"))}</span>` : ""}
          </div>
          <div class="row-sub">${sub}</div>
          ${r.latest_chapter ? `<div class="row-meta">${esc(r.latest_chapter)}</div>` : ""}
        </div>
        <div class="rc-side">
          ${sourceCaps(r)}
          <button class="btn btn-primary btn-sm" data-add="${esc(r.site)}" data-bid="${esc(r.book_id)}" data-title="${esc(r.title)}"><svg class="icon"><use href="#i-down"/></svg>${esc(t("Download"))}</button>
        </div>
      </div>
    </div>`;
  }).join("");
}
/**
 * Cover thumbnail with a typographic fallback.
 *
 * The letter stays in the DOM behind the image: a broken or blocked cover
 * simply reveals it instead of leaving an empty framed box (no onerror
 * bookkeeping, and the image is decorative because the title is right there).
 */
function coverBox(r) {
  const letter = esc((r.title || "?").trim().slice(0, 1) || "?");
  let src = r.cover_url || "";
  if (src.startsWith("//")) src = "https:" + src;
  const img = src ? `<img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : "";
  return `<div class="rc-cover">${img}<span>${letter}</span></div>`;
}
function sourceCaps(r) {
  // Merged rows ("Merge same book" on) carry ``sources``: every site holding
  // the book, best first. The first capsule (the best source, the row's own
  // site) is highlighted; alternates render as quiet outline pills.
  const sources = Array.isArray(r.sources) ? r.sources.filter(Boolean) : null;
  if (!sources || sources.length < 2) return `<span class="site-tag">${esc(r.site)}</span>`;
  const shown = sources.slice(0, 6);
  const rest = sources.length - shown.length;
  return `<div class="src-caps">` +
    shown.map((s, i) => `<span class="site-tag${i === 0 ? " cap-best" : ""}">${esc(s)}</span>`).join("") +
    (rest > 0 ? `<span class="site-tag cap-more">+${rest}</span>` : "") +
    `</div>`;
}
$("#results").addEventListener("keydown", e => {
  const card = e.target.closest("[data-sel]");
  if (!card || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault();
  selectResult(+card.dataset.sel);
});
$("#results").addEventListener("click", async e => {
  if (e.target.closest("#search-in-music")) {
    const kw = $("#q").value.trim();
    $("#m-q").value = kw;
    showPage("music");
    // Wait for the music page to load its settings/platforms before querying.
    Promise.resolve(typeof initMusic === "function" ? initMusic() : null)
      .then(() => { if (kw) doMusicSearch(); })
      .catch(() => {});
    return;
  }
  const card = e.target.closest("[data-sel]");
  if (card && !e.target.closest("[data-add]")) selectResult(+card.dataset.sel);
  const add = e.target.closest("[data-add]");
  const fol = e.target.closest("[data-follow]");
  const url = e.target.closest("[data-open-url]");
  if (add) {
    const r = await call("add_task", add.dataset.site, add.dataset.bid, add.dataset.title);
    if (r && r.ok) { toast(t("Queued: {title}").replace("{title}", add.dataset.title), "ok"); showPage("download"); }
    else if (r && r.error) toast(r.error, "err");
  } else if (fol) {
    const r = await call("add_watch", fol.dataset.site, fol.dataset.bid, fol.dataset.title);
    if (r) toast(r.message || (r.ok ? t("Added to follow list") : t("Already followed")), r.ok ? "ok" : "info");
  } else if (url && url.dataset.openUrl) {
    api("open_url", url.dataset.openUrl).catch(() => {});
  }
});
$("#detail").addEventListener("click", async e => {
  if (e.target.closest("[data-detail-close]")) { closeDetail(); return; }
  const add = e.target.closest("[data-add]");
  const fol = e.target.closest("[data-follow]");
  const url = e.target.closest("[data-open-url]");
  if (add) {
    const r = await call("add_task", add.dataset.site, add.dataset.bid, add.dataset.title);
    if (r && r.ok) { toast(t("Queued: {title}").replace("{title}", add.dataset.title), "ok"); showPage("download"); }
    else if (r && r.error) toast(r.error, "err");
  } else if (fol) {
    const r = await call("add_watch", fol.dataset.site, fol.dataset.bid, fol.dataset.title);
    if (r) toast(r.message || (r.ok ? t("Added to follow list") : t("Already followed")), r.ok ? "ok" : "info");
  } else if (url && url.dataset.openUrl) {
    api("open_url", url.dataset.openUrl).catch(() => {});
  }
});
$("#btn-rank").onclick = async () => {
  const btn = $("#btn-rank");
  btn.disabled = true;
  $("#search-hint").textContent = t("Searching…");
  try {
    const data = await call("rank_list", 10);
    if (data && data.error) throw new Error(data.error);
    const results = (data && data.results) || [];
    lastResults = results; lastKeyword = ""; selIndex = -1;
    hideDetail();
    renderResults(results, "");
    $("#search-hint").textContent = t("{n} results in {s}s").replace("{n}", results.length).replace("{s}", "0.0");
  } catch (e) {
    $("#results").innerHTML = `<div class="empty"><svg class="icon"><use href="#i-search"/></svg><div class="t">${esc(t("Search failed"))}</div><div class="s">${esc(String(e && e.message || e))}</div></div>`;
    $("#search-hint").textContent = "";
  } finally {
    btn.disabled = false;
  }
};
let catGroups = {};
let catGender = "male";
function renderCatChips() {
  const items = catGroups[catGender] || [];
  $("#cat-chips").innerHTML = items.map(c =>
    `<button class="btn btn-sm btn-ghost" data-cat="${esc(c.id)}" data-gender="${catGender === "male" ? "1" : "2"}">${esc(c.name)}</button>`
  ).join("");
}
$("#btn-cats").onclick = async () => {
  const panel = $("#cat-panel");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.hidden = false;
  if (Object.keys(catGroups).length) { renderCatChips(); return; }
  $("#cat-hint").textContent = t("Searching…");
  try {
    const data = await call("category_list");
    catGroups = (data && data.groups) || {};
    $("#cat-hint").textContent = "";
    renderCatChips();
  } catch (e) {
    $("#cat-hint").textContent = String(e && e.message || e);
  }
};
function switchCat(gender) {
  catGender = gender;
  $("#cat-male").className = "btn btn-sm " + (gender === "male" ? "btn-primary" : "btn-ghost");
  $("#cat-female").className = "btn btn-sm " + (gender === "female" ? "btn-primary" : "btn-ghost");
  renderCatChips();
}
$("#cat-male").onclick = () => switchCat("male");
$("#cat-female").onclick = () => switchCat("female");
$("#cat-chips").addEventListener("click", async e => {
  const chip = e.target.closest("[data-cat]");
  if (!chip) return;
  $("#cat-hint").textContent = t("Searching…");
  const started = performance.now();
  try {
    const data = await call("category_books", chip.dataset.cat, chip.dataset.gender, 20);
    const results = (data && data.results) || [];
    lastResults = results; lastKeyword = ""; selIndex = -1;
    hideDetail();
    renderResults(results, "");
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    $("#cat-hint").textContent = t("{n} results in {s}s").replace("{n}", results.length).replace("{s}", secs);
  } catch (e) {
    $("#cat-hint").textContent = String(e && e.message || e);
  }
});
$("#btn-search").onclick = doSearch;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) doSearch(); });
// "Merge same book" defaults to on: without it one title lists once per site.
// An explicit earlier choice (stored as JSON true/false) still wins.
$("#q-group").checked = store.get("merge_same_book", true) !== false;
$("#q-group").addEventListener("change", () => store.set("merge_same_book", $("#q-group").checked));

/* ================================================================ download */
let taskTimer = null;
const knownStatus = new Map();
function renderTasks(snap) {
  const groups = [
    ["live", t("Active"), (snap.running || []).concat(snap.processing || [], snap.exporting || [])],
    ["queued", t("Queued"), snap.queued || []],
    ["done", t("Completed"), snap.completed || []],
  ];
  const liveCount = groups[0][2].length + groups[1][2].length;
  const badge = $("#badge-live");
  badge.hidden = liveCount === 0;
  badge.textContent = liveCount;
  if (currentPage !== "download") return;
  const body = $("#tasks-body");
  if (liveCount === 0 && (snap.completed || []).length === 0) {
    body.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-down"/></svg><div class="t">${esc(t("Nothing in the queue"))}</div><div class="s">${esc(t("Search for a book and press Download"))}</div></div>`;
    return;
  }
  body.innerHTML = groups.map(([key, label, tasks]) => (!tasks.length && key === "queued") ? "" : `
    <div class="section-head">${esc(label)}<span class="count-chip ${key === "live" ? "live" : ""}">${tasks.length}</span></div>
    ${tasks.map(taskCard).join("") || `<div class="hint" style="padding:2px 2px 8px">${esc(t("No tasks yet"))}</div>`}
  `).join("");
}
function taskCard(k) {
  const pct = Math.round((k.progress || 0) * 100);
  const active = ["running", "processing", "exporting"].includes(k.status);
  const indet = active && !(k.chapters_total > 0);
  const stage = k.export_stage ? ` · ${esc(t("Exporting {fmt}").replace("{fmt}", k.export_stage.toUpperCase()))}` : "";
  const speed = k.eta && active ? ` · ${esc(fmtEta(k.eta))}` : "";
  const err = k.error ? `<div class="row-sub" style="color:var(--red)">${esc(k.error)}</div>` : "";
  const retry = k.retry_count > 0 ? ` <span class="hint">(↻${k.retry_count})</span>` : "";
  const exports = (k.exports || []).map(p =>
    `<button class="btn btn-ghost btn-sm" data-open-file="${esc(p)}"><svg class="icon"><use href="#i-folder"/></svg>${esc(p.split(/[\\/]/).pop())}</button>`).join("");
  const created = k.created_at ? `<span>${esc(fmtTime(k.created_at))}</span>` : "";
  return `<div class="card hover row-card task st-${esc(k.status)}">
    <div class="row-main">
      <div style="display:flex;align-items:center;gap:9px"><span class="row-title">${esc(k.title)}</span>${pill(k.status)}${retry}</div>
      <div class="row-sub"><span class="site-tag">${esc(k.site)}</span> <span class="mono">${k.chapters_done || 0}/${k.chapters_total || 0}${stage}${speed}</span></div>
      ${active || indet ? `<div class="progress ${indet ? "indeterminate" : ""}"><i style="width:${pct}%"></i></div>` : ""}
      ${err}
    </div>
    <div class="row-actions">${exports}
      ${k.status === "queued" || active ? `<button class="btn btn-danger btn-sm" data-cancel="${esc(k.task_id)}">${esc(t("Cancel"))}</button>` : ""}
      ${k.retryable ? `<button class="btn btn-ghost btn-sm" data-retry="${esc(k.task_id)}"><svg class="icon"><use href="#i-refresh"/></svg>${esc(t("Retry"))}</button>` : ""}
      ${created ? `<span class="hint mono">${created}</span>` : ""}
    </div>
  </div>`;
}
async function refreshTasks() {
  try {
    const snap = await api("get_tasks");
    for (const bucket of ["running", "processing", "exporting", "queued", "completed"]) {
      for (const k of snap[bucket] || []) {
        const prev = knownStatus.get(k.task_id);
        if (prev && prev !== k.status && ["completed", "failed", "cancelled"].includes(k.status)) {
          const kind = k.status === "completed" ? "ok" : k.status === "failed" ? "err" : "info";
          toast(`${t(status_label_key(k.status))}: ${k.title}`, kind);
          if (k.status === "completed") api("play_chime").catch(() => {});
        }
        knownStatus.set(k.task_id, k.status);
      }
    }
    renderTasks(snap);
  } catch {}
}
$("#tasks-body").addEventListener("click", async e => {
  const c = e.target.closest("[data-cancel]");
  const r = e.target.closest("[data-retry]");
  const f = e.target.closest("[data-open-file]");
  if (c) { await call("cancel_task", c.dataset.cancel); refreshTasks(); }
  else if (r) { await call("retry_task", r.dataset.retry); refreshTasks(); }
  else if (f) { api("open_path", f.dataset.openFile).catch(() => {}); }
});
$("#btn-refresh-tasks").onclick = refreshTasks;

/* ================================================================ history */
async function refreshHistory() {
  const body = $("#hist-body");
  try {
    const items = await api("get_history");
    $("#hist-sub").textContent = t("{n} files in {dir}").replace("{n}", items.length).replace("{dir}", await api("get_data_dir") || "");
    if (!items.length) {
      body.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-hist"/></svg><div class="t">${esc(t("No exported books yet"))}</div><div class="s">${esc(t("Files land here when a download finishes"))}</div></div>`;
      return;
    }
    body.innerHTML = items.map(it => `
      <div class="card hover row-card">
        <div class="file-ico ${esc(it.ext)}">${esc((it.ext || "?").toUpperCase())}</div>
        <div class="row-main">
          <div class="row-title">${esc(it.name)}</div>
          <div class="row-meta"><span class="mono">${esc(it.size_text)}</span><span>${esc(it.mtime_text)}</span></div>
        </div>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm" data-open-file="${esc(it.path)}"><svg class="icon"><use href="#i-play"/></svg>${esc(t("Open"))}</button>
        </div>
      </div>`).join("");
  } catch (e) {
    body.innerHTML = `<div class="empty"><div class="t">${esc(String(e && e.message || e))}</div></div>`;
  }
}
$("#hist-body").addEventListener("click", e => {
  const f = e.target.closest("[data-open-file]");
  if (f) api("open_path", f.dataset.openFile).catch(() => {});
});
$("#btn-refresh-hist").onclick = refreshHistory;
$("#btn-open-dl").onclick = () => api("open_downloads_dir").catch(() => {});

/* ================================================================ music */
const music = { tracks: [], selected: new Set(), downloading: false, note: "", batch: [], searchId: "", labels: {} };
let musicInited = false;
async function initMusic() {
  if (musicInited) { renderHealth(); return; }
  musicInited = true;
  try {
    const st = await call("get_music_settings");
    $("#m-level").value = st.level || "exhigh";
    $("#m-tags").checked = !!st.embed_tags; $("#m-cover").checked = !!st.embed_cover;
    $("#m-lyric").checked = !!st.save_lyric; $("#m-split").checked = !!st.split_folder;
    $("#m-skip").checked = !!st.skip_existing;
  } catch {}
  try {
    const plats = await call("get_music_platforms");
    const sel = $("#m-platform");
    music.labels = Object.fromEntries((plats || []).map(p => [p.key, p.label]));
    // "All platforms" comes first and is the default: aggregate search needs no
    // per-platform choice from the user.
    sel.innerHTML = `<option value="all">${esc(t("All platforms"))}</option>` +
      (plats || []).map(p => `<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("");
    sel.value = "all";
    sel.onchange = updateMusicNote;
    updateMusicNote();
  } catch (e) {
    $("#m-note").textContent = String(e && e.message || e);
  }
  for (const [id, key] of [["#m-level", "level"], ["#m-tags", "embed_tags"], ["#m-cover", "embed_cover"], ["#m-lyric", "save_lyric"], ["#m-split", "split_folder"], ["#m-skip", "skip_existing"]]) {
    $(id).addEventListener("change", () => saveMusicSettings());
  }
  pollMusic();
}
function saveMusicSettings() {
  api("save_music_settings", {
    level: $("#m-level").value, embed_tags: $("#m-tags").checked, embed_cover: $("#m-cover").checked,
    save_lyric: $("#m-lyric").checked, split_folder: $("#m-split").checked, skip_existing: $("#m-skip").checked,
  }).catch(() => {});
}
async function updateMusicNote() {
  try { const note = await call("music_note", $("#m-platform").value); $("#m-note").textContent = note || ""; }
  catch { $("#m-note").textContent = ""; }
}
async function doMusicSearch() {
  const kw = $("#m-q").value.trim();
  if (!kw) return;
  $("#m-spin").classList.add("on");
  $("#btn-m-search").disabled = true;
  try {
    const data = await call("music_search", $("#m-platform").value, kw, 30);
    if (!data) return;
    if (data.error) {
      toast(data.error, "err");
      music.searchId = "";
      music.tracks = [];
      renderTracks();
      $("#m-count").textContent = "0";
      return;
    }
    music.tracks = data.tracks || [];
    // The bridge keeps the original provider objects; the id lets the download
    // call get them back instead of the JSON copies.
    music.searchId = data.search_id || "";
    music.note = data.note || "";
    music.selected = new Set(music.tracks.map((_, i) => i).filter(i => !music.tracks[i].vip));
    renderTracks();
    $("#m-count").textContent = music.tracks.length;
    if (data.note) toast(data.note, "info");
  } finally {
    $("#m-spin").classList.remove("on");
    $("#btn-m-search").disabled = false;
  }
}
function platformShortLabel(key) {
  const label = String((music.labels || {})[key] || "");
  const short = label.includes(" · ") ? label.split(" · ").pop() : label;
  return short || key;
}
function sourceChips(tr) {
  const listed = Array.isArray(tr && tr.sources) ? tr.sources.filter(s => String(s || "").trim()) : [];
  const fallback = String((tr && tr.provider) || "").trim();
  const sources = listed.length ? listed : (fallback ? [fallback] : []);
  if (!sources.length) return "";
  const shown = sources.slice(0, 3).map(s => `<span class="src-tag" title="${esc(s)}">${esc(platformShortLabel(s))}</span>`).join("");
  const more = sources.length > 3 ? `<span class="src-tag">+${sources.length - 3}</span>` : "";
  return `<span class="src-tags" title="${esc(sources.join(", "))}">${shown}${more}</span>`;
}
function renderTracks() {
  const box = $("#m-tracks");
  if (!music.tracks.length) {
    box.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-music"/></svg><div class="t">${esc(t("Search a song to begin"))}</div></div>`;
    return;
  }
  box.innerHTML = music.tracks.map((tr, i) => `
    <div class="card row-card track-row" style="padding:9px 14px">
      <input type="checkbox" class="cb" data-i="${i}" ${music.selected.has(i) ? "checked" : ""}>
      <span class="idx mono">${i + 1}</span>
      <div class="row-main">
        <div style="display:flex;align-items:center;gap:8px"><span class="row-title">${esc(tr.name)}</span>${tr.vip ? `<span class="vip-tag">VIP</span>` : ""}</div>
        <div class="row-sub">${esc(tr.artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
      </div>
      ${sourceChips(tr)}
      ${tr.ext ? `<span class="hint mono">${esc(tr.ext.toUpperCase())}</span>` : ""}
      <span class="hint mono">${esc(fmtDur(tr.duration_s))}</span>
    </div>`).join("");
}
$("#m-tracks").addEventListener("change", e => {
  const cb = e.target.closest(".cb");
  if (!cb) return;
  const i = +cb.dataset.i;
  if (cb.checked) music.selected.add(i); else music.selected.delete(i);
});
$("#btn-m-selall").onclick = () => {
  if (music.selected.size === music.tracks.length) music.selected.clear();
  else music.tracks.forEach((_, i) => music.selected.add(i));
  renderTracks();
};
$("#btn-m-search").onclick = doMusicSearch;
$("#m-q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) doMusicSearch(); });

$("#btn-m-dl").onclick = async () => {
  if (music.downloading || !music.selected.size) return;
  // Disable synchronously (before the await): a double click would otherwise
  // race past the guard and ask the bridge to start a second batch.
  const btn = $("#btn-m-dl");
  btn.disabled = true;
  let started = false;
  try {
    const indexes = [...music.selected].sort((a, b) => a - b);
    music.batch = indexes;
    const tracks = indexes.map(i => music.tracks[i]);
    const r = await call("music_download", $("#m-platform").value, tracks, null, music.searchId || null);
    if (r && r.ok !== false) {
      started = true;
      music.downloading = true;
      $("#btn-m-stop").hidden = false;
      $("#m-progress").hidden = false;
      $("#m-progress").classList.add("indeterminate");
      pollMusic();
    } else if (r && r.error) toast(r.error, "warn");
  } finally {
    if (!started) btn.disabled = false;
  }
};
$("#btn-m-stop").onclick = () => api("music_stop").catch(() => {});

/* Playlist export: only tracks that already carry a local file path can be
   written (the engine does not download anything here). The written path is
   shown in a selectable, read-only field so it can be copied even when the
   clipboard API is unavailable. */
function showExportNote(r) {
  const skipped = Number(r.skipped || 0);
  $("#m-export-title").textContent = t("Playlist saved") +
    (skipped > 0 ? " · " + t("{n} track(s) without a local file were skipped").replace("{n}", skipped) : "");
  const input = $("#m-export-path");
  input.value = r.path || "";
  input.title = r.path || "";
  $("#m-export-note").hidden = false;
}
$("#btn-m-export").onclick = async () => {
  if (!music.tracks.length) {
    toast(t("Search songs first; there is nothing to export"), "warn", 5000);
    return;
  }
  const ready = music.tracks.filter(tr => String((tr && tr.path) || "").trim());
  if (!ready.length) {
    toast(t("No track has a local file path yet — download the tracks first"), "warn", 6000);
    return;
  }
  const btn = $("#btn-m-export");
  btn.disabled = true;
  try {
    const r = await call("music_export", $("#m-platform").value, ready);
    if (r && r.ok) {
      showExportNote(r);
      toast(t("Playlist saved to {path}").replace("{path}", r.path), "ok", 9000, () => api("open_path", r.path).catch(() => {}));
    } else if (r && r.error) {
      toast(r.error, "err", 7000);
    }
  } finally {
    btn.disabled = false;
  }
};
$("#m-export-copy").onclick = async () => {
  const input = $("#m-export-path");
  const path = input.value;
  if (!path) return;
  let copied = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(path);
      copied = true;
    }
  } catch {}
  if (!copied) {
    // Fallback: select the field and use the legacy copy command.
    input.focus();
    input.select();
    try { copied = document.execCommand("copy"); } catch {}
  }
  toast(copied ? t("Copied") : t("Could not copy; the path is selected"), copied ? "ok" : "warn", 2600);
};
$("#m-export-open").onclick = () => {
  const path = $("#m-export-path").value;
  if (path) api("open_path", path).catch(() => {});
};

let musicPoll = null;
function pollMusic() {
  if (musicPoll) return;
  musicPoll = setInterval(async () => {
    if (currentPage !== "music") return;
    try {
      const st = await api("get_music_status");
      // Paths land on the downloader's Track objects; mirror them back onto the
      // search rows so the export button can see them afterwards.
      if (st && Array.isArray(st.paths)) {
        st.paths.forEach((p, i) => {
          const target = music.batch[i];
          if (p && target != null && music.tracks[target]) music.tracks[target].path = p;
        });
      }
      if (!st || !st.active) {
        if (music.downloading) {
          music.downloading = false;
          $("#btn-m-stop").hidden = true;
          $("#btn-m-dl").disabled = false;
          $("#m-progress").classList.remove("indeterminate");
          $("#m-progress").querySelector("i").style.width = "0%";
          setTimeout(() => { $("#m-progress").hidden = true; }, 600);
          if (st && st.summary) toast(st.summary, st.failed ? "warn" : "ok", 6000);
        }
        return;
      }
      const bar = $("#m-progress");
      bar.hidden = false;
      bar.classList.remove("indeterminate");
      bar.querySelector("i").style.width = Math.round((st.fraction || 0) * 100) + "%";
      $("#m-dl-label").textContent = t("Downloading {done}/{total}").replace("{done}", st.done).replace("{total}", st.total);
    } catch {}
  }, 500);
}

$("#btn-m-health").onclick = async () => {
  const btn = $("#btn-m-health");
  btn.disabled = true;
  try { await call("music_health_start"); } finally { btn.disabled = false; }
  renderHealth();
};
function renderHealth() {
  const box = $("#m-health");
  api("get_music_health").then(rows => {
    if (!rows || !rows.length) { box.innerHTML = ""; return; }
    box.innerHTML = rows.map(r => `
      <div style="display:flex;align-items:center;gap:9px;padding:4px 0;font-size:12.5px">
        <span class="health-dot ${r.done ? (r.ok ? "ok" : "bad") : "wait"}"></span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.error || "")}">${esc(r.label)}</span>
        <span class="hint mono">${r.done ? esc(r.elapsed_text) : "…"}</span>
      </div>`).join("");
  }).catch(() => {});
}
let healthPoll = null;
healthPoll = setInterval(() => { if (currentPage === "music" && $("#m-health").childElementCount) renderHealth(); }, 1500);

/* ================================================================ follow */
async function refreshFollow() {
  const body = $("#follow-body");
  try {
    const items = await api("get_watchlist");
    if (!items.length) {
      body.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-heart"/></svg><div class="t">${esc(t("No followed books"))}</div><div class="s">${esc(t("Follow a search result or add a book manually"))}</div></div>`;
      return;
    }
    body.innerHTML = items.map(w => `
      <div class="card hover row-card">
        <div class="row-main">
          <div style="display:flex;align-items:center;gap:9px"><span class="row-title">${esc(w.title || w.site + "/" + w.book_id)}</span><span class="site-tag">${esc(w.site)}</span></div>
          <div class="row-sub">${esc(w.last_chapter || t("No chapter recorded yet"))}</div>
          <div class="row-meta"><span>${esc(w.updated_text || "")}</span><span class="mono">${esc(w.book_id)}</span></div>
        </div>
        <div class="row-actions">
          <button class="btn btn-primary btn-sm" data-fdl="${esc(w.site)}" data-bid="${esc(w.book_id)}" data-title="${esc(w.title)}"><svg class="icon"><use href="#i-down"/></svg>${esc(t("Download"))}</button>
          <button class="btn btn-danger btn-sm" data-unfollow="${esc(w.site)}" data-bid="${esc(w.book_id)}" title="${esc(t("Remove"))}" aria-label="${esc(t("Remove"))}"><svg class="icon"><use href="#i-trash"/></svg></button>
        </div>
      </div>`).join("");
  } catch (e) {
    body.innerHTML = `<div class="empty"><div class="t">${esc(String(e && e.message || e))}</div></div>`;
  }
}
$("#follow-body").addEventListener("click", async e => {
  const d = e.target.closest("[data-fdl]");
  const u = e.target.closest("[data-unfollow]");
  if (d) {
    const r = await call("add_task", d.dataset.fdl, d.dataset.bid, d.dataset.title);
    if (r && r.ok) { toast(t("Queued: {title}").replace("{title}", d.dataset.title), "ok"); showPage("download"); }
    else if (r && r.error) toast(r.error, "err");
  } else if (u) {
    await call("remove_watch", u.dataset.unfollow, u.dataset.bid);
    refreshFollow();
  }
});
$("#btn-f-add").onclick = () => openModal([
  { id: "f-site", ph: "Site (e.g. n23qb)", val: "" },
  { id: "f-bid", ph: "Book ID", val: "" },
  { id: "f-title", ph: "Title (optional)", val: "" },
], async vals => {
  const r = await call("add_watch", vals["f-site"], vals["f-bid"], vals["f-title"]);
  if (r) toast(r.message || (r.ok ? t("Added to follow list") : t("Already followed")), r.ok ? "ok" : "info");
  refreshFollow();
});
$("#btn-f-check").onclick = async () => {
  $("#f-spin").classList.add("on");
  $("#btn-f-check").disabled = true;
  try {
    const r = await call("check_updates");
    if (r) {
      toast(t("Checked {n} books, {u} updated, {f} failed").replace("{n}", r.checked).replace("{u}", r.updated).replace("{f}", r.failed),
        r.failed ? "warn" : r.updated ? "ok" : "info", 6000);
    }
    refreshFollow();
  } finally {
    $("#f-spin").classList.remove("on");
    $("#btn-f-check").disabled = false;
  }
};

/* ================================================================ modal */
function openModal(fields, onOk) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `<div class="modal">
    <h3>${esc(t("Add book"))}</h3>
    ${fields.map(f => `<div class="field"><input id="${f.id}" placeholder="${esc(t(f.ph))}" autocomplete="off"></div>`).join("")}
    <div class="modal-actions">
      <button class="btn btn-ghost" data-no>${esc(t("Cancel"))}</button>
      <button class="btn btn-primary" data-ok>${esc(t("OK"))}</button>
    </div></div>`;
  document.body.appendChild(mask);
  mask.addEventListener("mousedown", e => { if (e.target === mask) mask.remove(); });
  const close = () => mask.remove();
  mask.querySelector("[data-no]").onclick = close;
  mask.querySelector("[data-ok]").onclick = async () => {
    const vals = {};
    for (const f of fields) vals[f.id] = mask.querySelector("#" + f.id).value.trim();
    if (!vals[fields[0].id] || !vals[fields[1].id]) { toast(t("Site and book ID are required"), "warn"); return; }
    close();
    await onOk(vals);
  };
  mask.querySelector(".field input").focus();
  mask.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
}

/* ================================================================ shortcuts */
function typingTarget(el) {
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable === true;
}
document.addEventListener("keydown", e => {
  // Escape dismisses the narrow-window detail drawer (and returns focus);
  // it is checked first so typing inside the drawer cannot swallow it.
  if (e.key === "Escape" && !e.isComposing && detailIsOpen()) {
    e.preventDefault();
    closeDetail();
    return;
  }
  // Never hijack keys while the user is typing or composing text (IME):
  // Ctrl+F / Ctrl+D would otherwise steal keystrokes from the search fields.
  if (e.isComposing) return;
  if (typingTarget(e.target) || typingTarget(document.activeElement)) return;
  if (e.ctrlKey && !e.shiftKey && !e.altKey) {
    const pages = ["search", "download", "history", "music", "follow"];
    if (e.key >= "1" && e.key <= "5") { e.preventDefault(); showPage(pages[+e.key - 1]); }
    else if (e.key.toLowerCase() === "f") { e.preventDefault(); showPage("search"); }
    else if (e.key.toLowerCase() === "d") {
      e.preventDefault();
      const btn = $("#btn-m-dl");
      if (currentPage === "music" && !btn.disabled) btn.click();
    }
  }
});

/* ================================================================ boot */
function boot() {
  api("get_initial_state").then(state => {
    Object.assign(I18N, state.i18n || {});
    applyI18n();
    $("#ver").textContent = "v" + (state.version || "");
    setTheme(state.theme || store.get("theme", "light"));
    if (state.is_frozen && !state.shortcut_exists) {
      setTimeout(() => toast(t("Click here to create a desktop shortcut"), "info", 6000), 1200);
    }
    // Update checks start after the page is ready and never block it: the
    // bar appears on its own once the background check reports a release.
    bootUpdateCheck();
    renderTracks();
    initMusic().catch(() => {});
    refreshTasks();
    refreshFollow();
  }).catch(err => {
    toast("Bridge failed: " + (err && err.message || err), "err", 12000);
  });
}
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);

taskTimer = setInterval(refreshTasks, 1200);
window.addEventListener("beforeunload", () => { clearInterval(taskTimer); clearInterval(healthPoll); if (musicPoll) clearInterval(musicPoll); if (updatePollTimer) clearInterval(updatePollTimer); });
</script>
</body>
</html>
"""
