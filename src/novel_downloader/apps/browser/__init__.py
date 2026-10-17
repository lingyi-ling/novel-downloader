#!/usr/bin/env python3
"""
novel_downloader.apps.browser
-----------------------------

Browser-assisted search helpers (see
:mod:`novel_downloader.apps.browser.main`).
"""

from novel_downloader.apps.browser.main import (
    BROWSER_SEARCH_TIMEOUT,
    BrowserSearchSpec,
    browser_search_main,
    capture_search_json,
    resolve_spec,
    run_browser_search,
    search_via_browser,
)

__all__ = [
    "BROWSER_SEARCH_TIMEOUT",
    "BrowserSearchSpec",
    "browser_search_main",
    "capture_search_json",
    "resolve_spec",
    "run_browser_search",
    "search_via_browser",
]
