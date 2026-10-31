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
<title>Novel Downloader</title>
<style>
/* ============================================================ tokens */
:root {
  --bg: #f6f5f1;
  --bg-grad: radial-gradient(1200px 700px at 85% -10%, #fdf0e9 0%, transparent 55%),
             radial-gradient(900px 600px at -10% 110%, #eef0e8 0%, transparent 50%);
  --side: #efede6;
  --card: #ffffff;
  --card-2: #fbfaf7;
  --hover: #f3f1ea;
  --border: #e5e2d8;
  --border-strong: #d6d2c4;
  --text: #201f1b;
  --text-2: #6d6960;
  --text-3: #a09a8c;
  --accent: #d97757;
  --accent-hover: #c4653f;
  --accent-soft: rgba(217, 119, 87, .10);
  --accent-soft-2: rgba(217, 119, 87, .18);
  --on-accent: #ffffff;
  --green: #3e8e63; --green-bg: rgba(62, 142, 99, .12);
  --red: #cc4444;   --red-bg: rgba(204, 68, 68, .10);
  --blue: #4a7fb5;  --blue-bg: rgba(74, 127, 181, .12);
  --purple: #8a63b5;--purple-bg: rgba(138, 99, 181, .12);
  --yellow: #b58520;--yellow-bg: rgba(181, 133, 32, .13);
  --gray: #8b867a;  --gray-bg: rgba(139, 134, 122, .14);
  --shadow-1: 0 1px 2px rgba(32, 31, 27, .05);
  --shadow-2: 0 6px 24px -8px rgba(32, 31, 27, .14), 0 2px 6px rgba(32,31,27,.05);
  --shadow-3: 0 16px 48px -12px rgba(32, 31, 27, .22);
  --scroll: rgba(32, 31, 27, .18);
  --focus: rgba(217, 119, 87, .45);
}
:root[data-theme="dark"] {
  --bg: #171614;
  --bg-grad: radial-gradient(1200px 700px at 85% -10%, #2a201a 0%, transparent 55%),
             radial-gradient(900px 600px at -10% 110%, #1c2018 0%, transparent 50%);
  --side: #1e1d1a;
  --card: #232220;
  --card-2: #292825;
  --hover: #2c2b27;
  --border: #34322c;
  --border-strong: #45423a;
  --text: #ece9e2;
  --text-2: #a39e93;
  --text-3: #726d62;
  --accent: #e08a63;
  --accent-hover: #eb9d78;
  --accent-soft: rgba(224, 138, 99, .13);
  --accent-soft-2: rgba(224, 138, 99, .22);
  --on-accent: #1c1208;
  --green: #5fae85; --green-bg: rgba(95, 174, 133, .14);
  --red: #e06565;   --red-bg: rgba(224, 101, 101, .13);
  --blue: #6f9fce;  --blue-bg: rgba(111, 159, 206, .14);
  --purple: #a983cf;--purple-bg: rgba(169, 131, 207, .14);
  --yellow: #d4a440;--yellow-bg: rgba(212, 164, 64, .14);
  --gray: #8f8a7e;  --gray-bg: rgba(143, 138, 126, .16);
  --shadow-1: 0 1px 2px rgba(0, 0, 0, .3);
  --shadow-2: 0 6px 24px -8px rgba(0, 0, 0, .5), 0 2px 6px rgba(0,0,0,.25);
  --shadow-3: 0 16px 48px -12px rgba(0, 0, 0, .6);
  --scroll: rgba(236, 233, 226, .16);
  --focus: rgba(224, 138, 99, .5);
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
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-thumb { background: var(--scroll); border-radius: 8px; border: 2px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: var(--border-strong); background-clip: padding-box; }
::-webkit-scrollbar-corner { background: transparent; }
svg.icon { width: 18px; height: 18px; flex: none; }
button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; }
input, select, textarea { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; border-radius: 8px; }
[hidden] { display: none !important; }

/* ============================================================ shell */
.app { display: grid; grid-template-columns: 234px 1fr; height: 100vh; background: var(--bg-grad); }

.sidebar {
  display: flex; flex-direction: column; gap: 4px;
  background: var(--side); border-right: 1px solid var(--border);
  padding: 18px 14px 14px; min-height: 0;
}
.logo { display: flex; align-items: center; gap: 11px; padding: 4px 10px 18px; }
.logo-mark {
  width: 34px; height: 34px; border-radius: 10px; flex: none;
  background: linear-gradient(135deg, var(--accent) 0%, #b95a38 100%);
  display: grid; place-items: center; color: #fff;
  box-shadow: 0 4px 12px -2px var(--accent-soft-2), inset 0 1px 0 rgba(255,255,255,.25);
}
.logo-name { font-weight: 700; font-size: 15px; letter-spacing: -.01em; }
.logo-sub { font-size: 11px; color: var(--text-3); margin-top: -2px; }

.nav { display: flex; flex-direction: column; gap: 3px; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 11px;
  padding: 9px 12px; border-radius: 10px; color: var(--text-2);
  font-weight: 500; text-align: left; width: 100%;
  transition: background .16s ease, color .16s ease, transform .1s ease;
}
.nav-item:hover { background: var(--hover); color: var(--text); }
.nav-item:active { transform: scale(.985); }
.nav-item.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nav-item.active::before {
  content: ""; position: absolute; left: -14px; top: 22%; bottom: 22%;
  width: 3px; border-radius: 0 3px 3px 0; background: var(--accent);
}
.nav-item kbd {
  margin-left: auto; font: 600 10px/1 inherit; color: var(--text-3);
  background: var(--card); border: 1px solid var(--border); border-bottom-width: 2px;
  border-radius: 5px; padding: 3px 5px; opacity: .75;
}
.nav-item.active kbd { color: var(--accent); border-color: var(--accent-soft-2); }

.side-foot { margin-top: auto; display: flex; flex-direction: column; gap: 8px; }
.foot-row { display: flex; align-items: center; gap: 6px; }
.icon-btn {
  display: grid; place-items: center; width: 34px; height: 34px;
  border-radius: 9px; color: var(--text-2);
  transition: background .15s ease, color .15s ease;
}
.icon-btn:hover { background: var(--hover); color: var(--text); }
.foot-meta { margin-left: auto; text-align: right; font-size: 11px; color: var(--text-3); line-height: 1.35; }

/* ============================================================ main */
.main { min-width: 0; display: flex; flex-direction: column; }
.page { display: none; flex-direction: column; min-height: 0; height: 100vh; animation: pageIn .28s cubic-bezier(.2,.7,.3,1); }
.page.active { display: flex; }
@keyframes pageIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

.topbar {
  display: flex; align-items: center; gap: 14px;
  padding: 20px 28px 14px; flex: none;
}
.topbar h1 { font-size: 19px; font-weight: 700; letter-spacing: -.02em; }
.topbar .sub { font-size: 12.5px; color: var(--text-2); margin-top: 1px; }
.topbar .spacer { flex: 1; }

.scroll { overflow-y: auto; padding: 4px 28px 28px; flex: 1; min-height: 0; }

/* ============================================================ controls */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  height: 36px; padding: 0 15px; border-radius: 10px;
  font-weight: 600; font-size: 13.5px; white-space: nowrap;
  border: 1px solid transparent;
  transition: background .15s ease, border-color .15s ease, color .15s ease,
              transform .12s ease, box-shadow .15s ease, opacity .15s ease;
}
.btn:active { transform: scale(.97); }
.btn:disabled { opacity: .5; cursor: default; transform: none; }
.btn-primary { background: var(--accent); color: var(--on-accent); box-shadow: var(--shadow-1), inset 0 1px 0 rgba(255,255,255,.18); }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); box-shadow: var(--shadow-2); }
.btn-ghost { background: var(--card); border-color: var(--border); color: var(--text); }
.btn-ghost:hover:not(:disabled) { background: var(--hover); border-color: var(--border-strong); }
.btn-danger { background: var(--red-bg); color: var(--red); }
.btn-danger:hover:not(:disabled) { background: var(--red); color: #fff; }
.btn-sm { height: 30px; padding: 0 11px; font-size: 12.5px; border-radius: 8px; gap: 6px; }
.btn-sm svg.icon { width: 15px; height: 15px; }

.field {
  display: flex; align-items: center; gap: 9px;
  background: var(--card); border: 1px solid var(--border); border-radius: 11px;
  padding: 0 13px; height: 42px; color: var(--text-3);
  transition: border-color .15s ease, box-shadow .15s ease;
}
.field:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.field input { flex: 1; min-width: 0; border: none; outline: none; background: transparent; color: var(--text); user-select: text; }
.field input::placeholder { color: var(--text-3); }
.field-lg { height: 48px; border-radius: 13px; font-size: 14.5px; }
select.picker {
  appearance: none; -webkit-appearance: none;
  background: var(--card) url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat right 11px center;
  border: 1px solid var(--border); border-radius: 10px;
  height: 36px; padding: 0 32px 0 12px; color: var(--text); cursor: pointer;
  transition: border-color .15s ease;
}
select.picker:hover { border-color: var(--border-strong); }
select.picker:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px var(--accent-soft); }

/* ============================================================ cards & rows */
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 13px;
  box-shadow: var(--shadow-1);
  transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease;
}
.card.hover:hover { border-color: var(--border-strong); box-shadow: var(--shadow-2); transform: translateY(-1px); }

.section-head {
  display: flex; align-items: center; gap: 9px;
  margin: 22px 2px 10px; font-size: 12px; font-weight: 700;
  letter-spacing: .07em; text-transform: uppercase; color: var(--text-3);
}
.section-head:first-child { margin-top: 6px; }
.count-chip {
  font-size: 11px; font-weight: 700; letter-spacing: 0; text-transform: none;
  background: var(--gray-bg); color: var(--text-2);
  border-radius: 99px; padding: 1px 8px;
}
.count-chip.live { background: var(--accent-soft); color: var(--accent); }

.row-card {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 16px; margin-bottom: 9px;
}
.row-main { flex: 1; min-width: 0; }
.row-title { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-sub { font-size: 12.5px; color: var(--text-2); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-meta { font-size: 12px; color: var(--text-3); margin-top: 3px; display: flex; gap: 12px; flex-wrap: wrap; }
.row-actions { display: flex; gap: 7px; flex: none; align-items: center; }

.site-tag {
  font-size: 11px; font-weight: 600; color: var(--accent);
  background: var(--accent-soft); padding: 2px 8px; border-radius: 6px;
}
.mono { font-variant-numeric: tabular-nums; }

.pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11.5px; font-weight: 600; padding: 3px 10px; border-radius: 99px;
}
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.queued    { color: var(--yellow); background: var(--yellow-bg); }
.pill.running   { color: var(--accent); background: var(--accent-soft); }
.pill.processing{ color: var(--blue);   background: var(--blue-bg); }
.pill.exporting { color: var(--purple); background: var(--purple-bg); }
.pill.completed { color: var(--green);  background: var(--green-bg); }
.pill.failed    { color: var(--red);    background: var(--red-bg); }
.pill.cancelled { color: var(--gray);   background: var(--gray-bg); }

/* progress */
.progress { height: 6px; border-radius: 99px; background: var(--hover); overflow: hidden; margin-top: 9px; }
.progress > i {
  display: block; height: 100%; width: 0; border-radius: 99px;
  background: linear-gradient(90deg, var(--accent), #e39a76);
  transition: width .45s cubic-bezier(.25,.8,.35,1);
}
.progress.indeterminate > i { width: 34% !important; animation: slide 1.2s ease-in-out infinite; }
@keyframes slide { 0% { transform: translateX(-110%); } 100% { transform: translateX(380%); } }

/* ============================================================ search page */
.search-zone { max-width: 760px; margin: 2px auto 0; width: 100%; }
.search-row { display: flex; gap: 10px; }
.search-row .field { flex: 1; }
.filters { display: flex; gap: 10px; margin-top: 10px; align-items: center; }
.filters .field { height: 36px; border-radius: 10px; flex: 1; }
.hint { font-size: 12px; color: var(--text-3); }
.spin {
  width: 16px; height: 16px; border-radius: 50%; flex: none;
  border: 2px solid var(--accent-soft-2); border-top-color: var(--accent);
  animation: rot .7s linear infinite; display: none;
}
.spin.on { display: inline-block; }
@keyframes rot { to { transform: rotate(360deg); } }

.result-card { padding: 15px 17px; margin-bottom: 10px; max-width: 860px; }
.best-chip {
  font-size: 10.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  color: var(--green); background: var(--green-bg); border-radius: 6px; padding: 2px 7px;
}
mark { background: var(--accent-soft-2); color: var(--accent); border-radius: 3px; padding: 0 2px; }

.empty { text-align: center; color: var(--text-3); padding: 60px 20px; }
.empty svg.icon { width: 44px; height: 44px; opacity: .4; margin-bottom: 12px; }
.empty .t { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty .s { font-size: 12.5px; margin-top: 4px; }

/* ============================================================ history */
.file-ico {
  width: 38px; height: 38px; border-radius: 10px; flex: none;
  display: grid; place-items: center; font-size: 10px; font-weight: 800; letter-spacing: .03em;
}
.file-ico.txt  { color: var(--blue);   background: var(--blue-bg); }
.file-ico.epub { color: var(--purple);background: var(--purple-bg); }

/* ============================================================ music */
.track-row { padding: 9px 14px; margin-bottom: 6px; gap: 12px; }
.track-row .idx { width: 26px; text-align: right; color: var(--text-3); font-size: 12px; flex: none; font-variant-numeric: tabular-nums; }
.track-row.playing .row-title { color: var(--accent); }
.vip-tag { font-size: 10px; font-weight: 800; color: var(--yellow); background: var(--yellow-bg); border-radius: 5px; padding: 1px 5px; }
.cb {
  appearance: none; -webkit-appearance: none; width: 17px; height: 17px; flex: none;
  border: 1.6px solid var(--border-strong); border-radius: 5.5px; cursor: pointer; position: relative;
  transition: background .14s ease, border-color .14s ease;
}
.cb:hover { border-color: var(--accent); }
.cb:checked { background: var(--accent); border-color: var(--accent); }
.cb:checked::after {
  content: ""; position: absolute; left: 4.6px; top: 1.6px; width: 4.4px; height: 8.5px;
  border: solid var(--on-accent); border-width: 0 2px 2px 0; transform: rotate(42deg);
}
.music-grid { display: grid; grid-template-columns: 1fr 300px; gap: 18px; align-items: start; }
.side-card { padding: 16px; position: sticky; top: 0; }
.set-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 0; font-size: 13px; }
.set-row + .set-row { border-top: 1px dashed var(--border); }
.set-row .lbl { color: var(--text-2); }
.switch { appearance: none; -webkit-appearance: none; width: 36px; height: 21px; border-radius: 99px; background: var(--border-strong); position: relative; cursor: pointer; transition: background .18s ease; flex: none; }
.switch::after { content: ""; position: absolute; top: 2.5px; left: 2.5px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .18s cubic-bezier(.3,.9,.4,1.2); box-shadow: 0 1px 3px rgba(0,0,0,.25); }
.switch:checked { background: var(--accent); }
.switch:checked::after { transform: translateX(15px); }

.health-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.health-dot.ok { background: var(--green); box-shadow: 0 0 0 3px var(--green-bg); }
.health-dot.bad { background: var(--red); box-shadow: 0 0 0 3px var(--red-bg); }
.health-dot.wait { background: var(--gray); opacity: .5; }

/* ============================================================ follow */
.modal-mask {
  position: fixed; inset: 0; background: rgba(20, 18, 14, .42);
  display: grid; place-items: center; z-index: 60;
  animation: fade .18s ease; backdrop-filter: blur(2px);
}
@keyframes fade { from { opacity: 0; } }
.modal {
  width: min(460px, calc(100vw - 60px));
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  box-shadow: var(--shadow-3); padding: 22px;
  animation: pop .22s cubic-bezier(.2,.9,.3,1.15);
}
@keyframes pop { from { opacity: 0; transform: scale(.94) translateY(8px); } }
.modal h3 { font-size: 15.5px; font-weight: 700; margin-bottom: 14px; }
.modal .field { margin-bottom: 10px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 16px; }

/* ============================================================ toasts */
#toasts { position: fixed; top: 16px; right: 18px; z-index: 100; display: flex; flex-direction: column; gap: 9px; }
.toast {
  display: flex; align-items: flex-start; gap: 10px;
  min-width: 250px; max-width: 380px;
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  box-shadow: var(--shadow-3); padding: 12px 14px; font-size: 13px;
  animation: toastIn .3s cubic-bezier(.2,.9,.3,1.1);
}
.toast.leaving { animation: toastOut .25s ease forwards; }
@keyframes toastIn { from { opacity: 0; transform: translateX(30px); } }
@keyframes toastOut { to { opacity: 0; transform: translateX(30px); } }
.toast .bar { width: 3.5px; border-radius: 99px; align-self: stretch; flex: none; }
.toast.ok .bar { background: var(--green); } .toast.err .bar { background: var(--red); }
.toast.info .bar { background: var(--blue); } .toast.warn .bar { background: var(--yellow); }
.toast .msg { flex: 1; user-select: text; word-break: break-word; }
.toast .x { color: var(--text-3); flex: none; padding: 0 2px; }
.toast .x:hover { color: var(--text); }
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
      <div><div class="logo-name">Novel Downloader</div><div class="logo-sub" id="ver"></div></div>
    </div>
    <nav class="nav" id="nav">
      <button class="nav-item active" data-page="search"><svg class="icon"><use href="#i-search"/></svg><span data-i18n="Search">Search</span><kbd>1</kbd></button>
      <button class="nav-item" data-page="download"><svg class="icon"><use href="#i-down"/></svg><span data-i18n="Download">Download</span><kbd>2</kbd><span class="count-chip live" id="badge-live" hidden></span></button>
      <button class="nav-item" data-page="history"><svg class="icon"><use href="#i-hist"/></svg><span data-i18n="History">History</span><kbd>3</kbd></button>
      <button class="nav-item" data-page="music"><svg class="icon"><use href="#i-music"/></svg><span data-i18n="Music">Music</span><kbd>4</kbd></button>
      <button class="nav-item" data-page="follow"><svg class="icon"><use href="#i-heart"/></svg><span data-i18n="Follow">Follow</span><kbd>5</kbd></button>
    </nav>
    <div class="side-foot">
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
            <button class="btn btn-primary field-lg" id="btn-search" style="height:48px"><span class="spin" id="search-spin"></span><svg class="icon"><use href="#i-search"/></svg><span data-i18n="Search">Search</span></button>
          </div>
          <div class="filters">
            <div class="field"><svg class="icon"><use href="#i-pulse"/></svg><input id="q-sites" data-i18n-ph="Site filter, comma separated (blank = all)" placeholder="Site filter, comma separated (blank = all)"></div>
            <span class="hint" id="search-hint"></span>
          </div>
        </div>
        <div id="results" style="margin-top:18px"></div>
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
              <button class="btn btn-sm btn-ghost" id="btn-m-selall"><span data-i18n="Select all">Select all</span></button>
              <button class="btn btn-sm btn-danger" id="btn-m-stop" hidden><svg class="icon"><use href="#i-stop"/></svg><span data-i18n="Stop">Stop</span></button>
              <button class="btn btn-sm btn-primary" id="btn-m-dl"><svg class="icon"><use href="#i-down"/></svg><span id="m-dl-label" data-i18n="Download selected">Download selected</span></button>
            </div>
            <div class="progress" id="m-progress" style="margin:0 2px 12px" hidden><i></i></div>
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

/* ================================================================ search */
let searching = false;
async function doSearch() {
  const kw = $("#q").value.trim();
  if (!kw || searching) return;
  searching = true;
  $("#search-spin").classList.add("on");
  $("#btn-search").disabled = true;
  $("#search-hint").textContent = t("Searching…");
  $("#results").innerHTML = "";
  try {
    const data = await api("search", kw, $("#q-sites").value.trim());
    // The bridge reports aggregate search failures inside the payload (site
    // timeouts, engine errors), not as exceptions: surface them instead of
    // letting them render as "No results".
    if (data && data.error) throw new Error(data.error);
    const results = (data && data.results) || [];
    renderResults(results, kw);
    $("#search-hint").textContent = t("{n} results in {s}s").replace("{n}", results.length).replace("{s}", ((data && data.elapsed) || 0).toFixed(1));
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
    box.innerHTML = `<div class="empty"><svg class="icon"><use href="#i-search"/></svg><div class="t">${esc(t("No results"))}</div><div class="s">${esc(t("Try another keyword or clear the site filter"))}</div></div>`;
    return;
  }
  box.innerHTML = results.map(r => {
    const sub = [r.author && t("Author") + ": " + r.author, r.word_count, r.update_date].filter(Boolean).map(esc).join('<span style="opacity:.4"> · </span>');
    return `<div class="card hover result-card">
      <div style="display:flex;gap:12px;align-items:flex-start">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px">
            <span class="row-title" style="white-space:normal">${highlight(r.title, kw)}</span>
            ${r.best ? `<span class="best-chip">${esc(t("Best match"))}</span>` : ""}
          </div>
          <div class="row-sub">${sub}</div>
          ${r.latest_chapter ? `<div class="row-meta">${esc(r.latest_chapter)}</div>` : ""}
        </div>
        <span class="site-tag">${esc(r.site)}</span>
      </div>
      <div class="row-actions" style="margin-top:11px">
        <button class="btn btn-primary btn-sm" data-add="${esc(r.site)}" data-bid="${esc(r.book_id)}" data-title="${esc(r.title)}"><svg class="icon"><use href="#i-down"/></svg>${esc(t("Download"))}</button>
        <button class="btn btn-ghost btn-sm" data-follow="${esc(r.site)}" data-bid="${esc(r.book_id)}" data-title="${esc(r.title)}"><svg class="icon"><use href="#i-heart"/></svg>${esc(t("Follow"))}</button>
        <button class="btn btn-ghost btn-sm" data-open-url="${esc(r.book_url || "")}"><svg class="icon"><use href="#i-ext"/></svg>${esc(t("Open in browser"))}</button>
      </div>
    </div>`;
  }).join("");
}
$("#results").addEventListener("click", async e => {
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
$("#btn-search").onclick = doSearch;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) doSearch(); });

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
  return `<div class="card hover row-card">
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
const music = { tracks: [], selected: new Set(), downloading: false, note: "" };
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
    sel.innerHTML = (plats || []).map(p => `<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("");
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
      music.tracks = [];
      renderTracks();
      $("#m-count").textContent = "0";
      return;
    }
    music.tracks = data.tracks || [];
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
    const tracks = [...music.selected].sort((a, b) => a - b).map(i => music.tracks[i]);
    const r = await call("music_download", $("#m-platform").value, tracks);
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

let musicPoll = null;
function pollMusic() {
  if (musicPoll) return;
  musicPoll = setInterval(async () => {
    if (currentPage !== "music") return;
    try {
      const st = await api("get_music_status");
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
          <button class="btn btn-danger btn-sm" data-unfollow="${esc(w.site)}" data-bid="${esc(w.book_id)}"><svg class="icon"><use href="#i-trash"/></svg></button>
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
    api("check_update").then(r => {
      if (r && r.available && r.url) {
        const msg = r.message || t("New version {ver} is out — click to download").replace("{ver}", r.latest || "");
        toast(msg, "info", 8000, () => api("open_url", r.url).catch(() => {}));
      }
    }).catch(() => {});
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
window.addEventListener("beforeunload", () => { clearInterval(taskTimer); clearInterval(healthPoll); if (musicPoll) clearInterval(musicPoll); });
</script>
</body>
</html>
"""
