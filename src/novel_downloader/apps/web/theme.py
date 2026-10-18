#!/usr/bin/env python3
"""
novel_downloader.apps.web.theme
-------------------------------

The visual theme of the web UI.

The look is expressed as CSS custom properties plus a small set of Quasar
overrides, so every page inherits it without per-page ``ui.colors`` calls.
A light (warm paper) and a dark (warm charcoal) variant are provided; Quasar
switches between them through the ``body--light`` / ``body--dark`` classes.
"""

from __future__ import annotations

from nicegui import ui

_installed = False

# NOTE: every physical line must stay below the project's 88 column limit,
# because ruff checks line length inside string literals as well.
THEME_CSS = """
/* ---------------------------------------------------------------- tokens */
:root {
  --nd-bg: #F5F4EF;
  --nd-surface: #FFFFFF;
  --nd-surface-2: #FAF9F5;
  --nd-border: #E4E1D7;
  --nd-border-strong: #D8D4C6;
  --nd-text: #1F1E1B;
  --nd-text-muted: #6B675E;
  --nd-text-faint: #96918A;
  --nd-accent: #D97757;
  --nd-accent-strong: #C4663F;
  --nd-accent-soft: rgba(217, 119, 87, 0.10);
  --nd-positive: #3F7F5C;
  --nd-positive-soft: rgba(63, 127, 92, 0.12);
  --nd-negative: #B4472F;
  --nd-negative-soft: rgba(180, 71, 47, 0.12);
  --nd-warning: #A9762A;
  --nd-warning-soft: rgba(169, 118, 42, 0.14);
  --nd-info: #5A7C8C;
  --nd-info-soft: rgba(90, 124, 140, 0.14);
  --nd-font-sans: ui-sans-serif, -apple-system, "Segoe UI", Roboto,
    "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  --nd-font-serif: ui-serif, "Iowan Old Style", Georgia, "Songti SC",
    "Noto Serif SC", serif;
  --nd-font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --nd-radius-card: 14px;
  --nd-radius-control: 10px;
  --nd-radius-pill: 999px;
  --nd-elevation: 0 1px 2px rgba(31, 30, 27, 0.05);
  --nd-elevation-lg: 0 12px 32px rgba(31, 30, 27, 0.12);
}

body.body--dark {
  --nd-bg: #1B1A17;
  --nd-surface: #23221F;
  --nd-surface-2: #2A2925;
  --nd-border: #3A3833;
  --nd-border-strong: #4A4842;
  --nd-text: #EDEBE6;
  --nd-text-muted: #A8A399;
  --nd-text-faint: #7E7A72;
  --nd-accent: #E0895F;
  --nd-accent-strong: #F0A379;
  --nd-accent-soft: rgba(224, 137, 95, 0.16);
  --nd-positive: #6FAE86;
  --nd-positive-soft: rgba(111, 174, 134, 0.16);
  --nd-negative: #D07A63;
  --nd-negative-soft: rgba(208, 122, 99, 0.16);
  --nd-warning: #D3A24A;
  --nd-warning-soft: rgba(211, 162, 74, 0.16);
  --nd-info: #8FAEBE;
  --nd-info-soft: rgba(143, 174, 190, 0.16);
  --nd-elevation: 0 1px 2px rgba(0, 0, 0, 0.30);
  --nd-elevation-lg: 0 14px 36px rgba(0, 0, 0, 0.45);
}

/* ------------------------------------------------------ quasar brand vars */
/* NiceGUI writes the Quasar brand palette as an inline style on <body>,
   which outranks any :root declaration. Re-declaring the vars on elements
   restores this theme's palette and follows the dark variant. */
body * {
  --q-primary: var(--nd-accent);
  --q-secondary: var(--nd-text-muted);
  --q-accent: var(--nd-warning);
  --q-positive: var(--nd-positive);
  --q-negative: var(--nd-negative);
  --q-info: var(--nd-info);
  --q-warning: var(--nd-warning);
  --q-dark: #22211E;
  --q-dark-page: var(--nd-bg);
}

/* ------------------------------------------------------------------ base */
body {
  background: var(--nd-bg);
  color: var(--nd-text);
  font-family: var(--nd-font-sans);
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

body.body--light,
body.body--dark {
  background: var(--nd-bg);
  color: var(--nd-text);
}

/* The soft accent is too weak to read as a selection at 10-16% alpha, so
   the tint is rebuilt from the same accent token at a stronger mix; the
   plain token stays first as the fallback. Text keeps the theme colour,
   which measures ~12.4:1 on light and ~8.1:1 on dark. */
::selection {
  background: var(--nd-accent-soft);
  background: color-mix(in srgb, var(--nd-accent) 28%, transparent);
  color: var(--nd-text);
}

* {
  scrollbar-width: thin;
  scrollbar-color: var(--nd-border-strong) transparent;
}

body a {
  color: var(--nd-accent) !important;
  text-decoration: none;
}

body a:hover {
  color: var(--nd-accent-strong) !important;
}

/* --------------------------------------------------------------- surfaces */
body .nicegui-content {
  padding: 2rem 1.25rem 4rem;
  gap: 1.5rem;
}

body .q-card,
body.body--dark .q-card,
body .nicegui-card {
  background: var(--nd-surface);
  color: var(--nd-text);
  border: 1px solid var(--nd-border);
  border-radius: var(--nd-radius-card);
  box-shadow: var(--nd-elevation) !important;
  transition: border-color 0.15s ease, background-color 0.15s ease,
    box-shadow 0.15s ease;
}

body .q-card:hover,
body .nicegui-card:hover {
  border-color: var(--nd-border-strong);
}

body .nicegui-card {
  padding: 1.25rem;
  gap: 0.75rem;
}

body .q-separator,
body.body--dark .q-separator {
  background: var(--nd-border);
}

body .q-dialog__inner > .q-card {
  border-radius: 16px;
  box-shadow: var(--nd-elevation-lg) !important;
  padding: 1.5rem;
}

body .q-menu,
body.body--dark .q-menu {
  background: var(--nd-surface);
  color: var(--nd-text);
  border: 1px solid var(--nd-border);
  border-radius: var(--nd-radius-card);
  box-shadow: var(--nd-elevation-lg);
  padding: 8px;
}

body .q-item {
  border-radius: 8px;
  min-height: 34px;
  color: var(--nd-text);
}

body .q-item.q-manual-focusable--focused,
body .q-item:hover {
  background: var(--nd-accent-soft);
}

body .q-notification {
  border-radius: 12px;
  box-shadow: var(--nd-elevation-lg);
  font-family: var(--nd-font-sans);
}

/* --------------------------------------------------------------- controls */
body .q-btn {
  font-family: var(--nd-font-sans);
  text-transform: none;
  letter-spacing: 0.01em;
  font-weight: 500;
  border-radius: var(--nd-radius-control);
  min-height: 36px;
  padding: 0 14px;
  box-shadow: none !important;
  transition: background-color 0.15s ease, color 0.15s ease;
}

body .q-btn:focus-visible {
  outline: 2px solid var(--nd-accent) !important;
  outline-offset: 2px;
}

body .q-btn .q-icon {
  font-size: 1.1em;
}

body .q-btn-toggle {
  background: var(--nd-surface-2);
  border: 1px solid var(--nd-border-strong);
  border-radius: var(--nd-radius-control);
  padding: 2px;
  gap: 2px;
  overflow: hidden;
}

body .q-btn-toggle .q-btn {
  border-radius: 8px;
  min-height: 30px;
  padding: 0 12px;
  font-size: 13px;
}

body .q-btn-toggle .q-btn:not(.bg-primary) {
  background: transparent !important;
  color: var(--nd-text-muted) !important;
}

body .q-field--outlined .q-field__control {
  background: var(--nd-surface);
  border-radius: var(--nd-radius-control);
}

body .q-field--outlined .q-field__control:before {
  border: 1px solid var(--nd-border-strong);
  border-radius: var(--nd-radius-control);
}

body .q-field--outlined .q-field__control:hover:before {
  border-color: var(--nd-text-faint);
}

body .q-field--outlined.q-field--focused .q-field__control:after {
  border: 1px solid var(--nd-accent);
  border-radius: var(--nd-radius-control);
}

body .q-field--outlined:focus-within .q-field__control:before {
  outline: 2px solid var(--nd-accent);
  outline-offset: 2px;
}

body .q-field__label,
body .q-field__bottom {
  color: var(--nd-text-muted);
}

body .q-field__native,
body .q-field__input,
body .q-field__prefix,
body .q-field__suffix {
  color: var(--nd-text);
}

/* Chrome washes autofilled fields with its own blue/yellow fill, which
   clashes with both variants. A huge inset shadow repaints the field with
   the surface token, the text-fill keeps the theme ink, and the long
   transition keeps the UA fill from flashing in. */
body input:-webkit-autofill,
body input:-webkit-autofill:hover,
body input:-webkit-autofill:focus {
  -webkit-text-fill-color: var(--nd-text);
  caret-color: var(--nd-text);
  box-shadow: 0 0 0 100px var(--nd-surface) inset;
  -webkit-box-shadow: 0 0 0 100px var(--nd-surface) inset;
  transition: background-color 9999s ease-in-out 0s;
}

body .q-checkbox__label {
  color: var(--nd-text);
}

body .q-chip {
  border-radius: var(--nd-radius-pill);
  font-size: 12px;
}

/* ------------------------------------------------------------------- data */
body .q-linear-progress {
  height: 6px;
  border-radius: var(--nd-radius-pill);
  overflow: hidden;
}

body .q-pagination__content {
  gap: 2px;
}

body .q-pagination__content .q-btn {
  min-width: 34px;
  min-height: 34px;
  padding: 0;
  border-radius: 9px;
  font-size: 13px;
}

/* Quasar marks the current page with bg-primary (filled) while every
   other pager button stays flat text-primary; mute the flat ones so the
   active page reads as the accent-filled button. */
body .q-pagination .q-btn:not(.bg-primary) {
  background: transparent !important;
  color: var(--nd-text-muted) !important;
}

/* -------------------------------------------------------------- utilities */
body .nd-page {
  gap: 1.25rem;
}

body .nd-h1 {
  font-family: var(--nd-font-serif);
  font-size: 2rem;
  font-weight: 500;
  line-height: 1.15;
  letter-spacing: -0.01em;
  color: var(--nd-text);
}

body .nd-h2 {
  font-family: var(--nd-font-serif);
  font-size: 1.2rem;
  font-weight: 500;
  color: var(--nd-text);
}

body .nd-sub {
  color: var(--nd-text-muted);
  font-size: 0.925rem;
}

body .nd-muted {
  color: var(--nd-text-muted);
}

body .nd-toolbar {
  position: sticky;
  top: 56px;
  z-index: 5;
  padding: 1rem 1.15rem;
  gap: 0.85rem;
  background: var(--nd-surface);
}

body .nd-link {
  font-weight: 500;
  border-bottom: 1px solid transparent;
}

body .nd-link:hover {
  border-bottom-color: currentColor;
}

body .nd-btn-ghost {
  background: transparent !important;
  color: var(--nd-text) !important;
  border: 1px solid var(--nd-border-strong);
}

body .nd-btn-ghost:hover {
  background: var(--nd-surface-2) !important;
  border-color: var(--nd-accent);
  color: var(--nd-accent) !important;
}

body .nd-icon-btn {
  width: 34px;
  min-height: 34px;
  padding: 0;
  border: 1px solid var(--nd-border-strong);
  border-radius: 50%;
  color: var(--nd-text-muted) !important;
}

body .nd-icon-btn:hover {
  border-color: var(--nd-accent);
  color: var(--nd-accent) !important;
}

body .nd-brand {
  font-family: var(--nd-font-serif);
  font-size: 1.15rem;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--nd-text);
}

body .nd-cover {
  width: 72px;
  height: 96px;
  object-fit: cover;
  border-radius: 9px;
  border: 1px solid var(--nd-border);
  background: var(--nd-surface-2);
}

body .nd-cover-empty {
  width: 72px;
  height: 96px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 9px;
  border: 1px solid var(--nd-border);
  background: var(--nd-surface-2);
  color: var(--nd-text-faint);
}

body .nd-icon-tile {
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 11px;
  background: var(--nd-accent-soft);
  color: var(--nd-accent);
}

body .nd-icon-tile .q-icon {
  font-size: 22px;
}

body .nd-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.1rem 0.6rem;
  border-radius: var(--nd-radius-pill);
  background: var(--nd-surface-2);
  color: var(--nd-text-muted);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.7;
}

body .nd-pill--queued {
  background: var(--nd-warning-soft);
  color: var(--nd-warning);
}

body .nd-pill--running {
  background: var(--nd-accent-soft);
  color: var(--nd-accent);
}

body .nd-pill--processing,
body .nd-pill--exporting {
  background: var(--nd-info-soft);
  color: var(--nd-info);
}

body .nd-pill--completed {
  background: var(--nd-positive-soft);
  color: var(--nd-positive);
}

body .nd-pill--failed {
  background: var(--nd-negative-soft);
  color: var(--nd-negative);
}

body .nd-skeleton {
  background: linear-gradient(
    90deg,
    var(--nd-surface-2) 25%,
    var(--nd-border-strong) 37%,
    var(--nd-surface-2) 63%
  );
  background-size: 400% 100%;
  border: 1px solid var(--nd-border);
  border-radius: var(--nd-radius-card);
  animation: nd-shimmer 1.4s ease infinite;
}

body .nd-code {
  font-family: var(--nd-font-mono);
  font-size: 0.85rem;
}

/* ------------------------------------------------------------- app shell */
body .q-header,
body.body--dark .q-header {
  background: var(--nd-surface);
  color: var(--nd-text);
  border-bottom: 1px solid var(--nd-border);
}

body .nd-topbar {
  height: 56px;
  min-height: 56px;
  padding: 0 1rem;
}

body .q-drawer,
body.body--dark .q-drawer {
  background: var(--nd-surface) !important;
  color: var(--nd-text);
  border-right: 1px solid var(--nd-border);
}

body .q-drawer > .q-drawer__content {
  padding: 0;
}

body .nd-side {
  height: 100%;
  padding: 1.25rem 0.875rem 1.25rem 1rem;
}

body .nd-side-tagline {
  color: var(--nd-text-faint);
  font-size: 0.8rem;
}

body .nd-side-item {
  position: relative;
  width: 100%;
  justify-content: flex-start;
  gap: 0.65rem;
  min-height: 42px;
  padding: 0 14px;
  border-radius: var(--nd-radius-control);
  color: var(--nd-text-muted) !important;
  font-weight: 500;
}

body .nd-side-item:hover {
  background: var(--nd-surface-2) !important;
  color: var(--nd-text) !important;
}

body .nd-side-item--active,
body .nd-side-item--active:hover {
  background: var(--nd-accent-soft) !important;
  color: var(--nd-accent) !important;
}

body .nd-side-item--active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 3px;
  border-radius: var(--nd-radius-pill);
  background: var(--nd-accent);
}

/* -------------------------------------------------------------- page head */
body .nd-page-head {
  gap: 0.15rem;
  margin-bottom: 0.25rem;
}

/* ------------------------------------------------------------ empty state */
body .nd-empty {
  align-items: center;
  text-align: center;
  padding: 3.5rem 1rem;
  gap: 0.8rem;
  color: var(--nd-text-muted);
}

body .nd-icon-tile--lg {
  width: 64px;
  height: 64px;
  border-radius: 16px;
  flex: 0 0 auto;
}

body .nd-icon-tile--lg .q-icon {
  font-size: 30px;
}

/* ----------------------------------------------------------- task states */
body .nd-task {
  border-left: 3px solid var(--nd-border-strong);
}

body .nd-task--queued {
  border-left-color: var(--nd-warning);
}

body .nd-task--running {
  border-left-color: var(--nd-accent);
}

body .nd-task--processing,
body .nd-task--exporting {
  border-left-color: var(--nd-info);
}

body .nd-task--completed {
  border-left-color: var(--nd-positive);
}

body .nd-task--failed {
  border-left-color: var(--nd-negative);
}

body .nd-task--cancelled {
  border-left-color: var(--nd-text-faint);
}

/* ---------------------------------------------------------- parse banner */
body .nd-parse-banner {
  background: var(--nd-positive-soft);
  border-radius: var(--nd-radius-control);
  padding: 0.5rem 0.75rem;
}

/* --------------------------------------------------------- search results */
body .nd-hl {
  background: var(--nd-accent-soft);
  border-radius: 4px;
  padding: 0 2px;
}

@keyframes nd-shimmer {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0 50%;
  }
}

/* --------------------------------------------------- motion preferences */
/* Honour the OS request: state changes stay instantaneous and the
   shimmer stops instead of pulsing. */
@media (prefers-reduced-motion: reduce) {
  body .q-card,
  body .nicegui-card,
  body .q-btn {
    transition: none !important;
  }

  body .nd-skeleton {
    animation: none !important;
  }
}

/* ----------------------------------------------------------------- print */
/* Paper has a single palette, so the existing tokens are re-pinned to
   paper values before the rules below run. No new token is introduced and
   no colour literal leaves this block; every rule still uses var(--nd-*). */
@media print {
  body,
  body.body--dark {
    --nd-bg: #FFFFFF;
    --nd-surface: #FFFFFF;
    --nd-surface-2: #FFFFFF;
    --nd-text: #000000;
    --nd-text-muted: #333333;
    --nd-text-faint: #555555;
    --nd-border: #BFBFBF;
    --nd-border-strong: #8C8C8C;
    --nd-accent: #000000;
    --nd-accent-strong: #000000;
    --nd-accent-soft: transparent;
    --nd-positive-soft: transparent;
    --nd-negative-soft: transparent;
    --nd-warning-soft: transparent;
    --nd-info-soft: transparent;
    --nd-elevation: none;
    --nd-elevation-lg: none;
  }

  body {
    background: var(--nd-bg) !important;
    color: var(--nd-text) !important;
    -webkit-print-color-adjust: economy;
    print-color-adjust: economy;
  }

  /* Quasar reserves top padding for the header it no longer shows. */
  body .q-page-container {
    padding-top: 0 !important;
  }

  /* interactive chrome carries no meaning on paper */
  body .q-header,
  body .q-drawer,
  body .q-drawer__backdrop,
  body .q-menu,
  body .q-btn,
  body .q-notifications,
  body .nd-toolbar {
    display: none !important;
  }

  /* drop fills and shadows so ink lands on white, not tinted panels */
  body .q-card,
  body .nicegui-card,
  body .q-field--outlined .q-field__control {
    background: transparent !important;
    box-shadow: none !important;
  }
}
"""


def install() -> None:
    """Attach the theme stylesheet to every page (idempotent)."""
    global _installed
    if _installed:
        return
    ui.add_css(THEME_CSS, shared=True)
    _installed = True
