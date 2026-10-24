#!/usr/bin/env python3
"""
novel_downloader.apps.browser
-----------------------------

Browser-assisted search helpers (see
:mod:`novel_downloader.apps.browser.main`).
"""

from novel_downloader.apps.browser.main import (
    BROWSER_SEARCH_TIMEOUT,
    BrowserSearchResult,
    BrowserSearchSpec,
    browser_search_main,
    capture_search_json,
    is_browser_available,
    resolve_spec,
    run_browser_search,
    run_browser_search_detailed,
    search_via_browser,
    search_via_browser_result,
)
from novel_downloader.apps.browser.settings import (
    DEFAULT_BROWSER_ASSISTED_SITES,
    browser_assisted_sites,
    is_browser_assisted_site,
    parse_browser_assisted_sites,
)

__all__ = [
    "BROWSER_SEARCH_TIMEOUT",
    "DEFAULT_BROWSER_ASSISTED_SITES",
    "BrowserSearchResult",
    "BrowserSearchSpec",
    "browser_assisted_sites",
    "browser_search_main",
    "capture_search_json",
    "is_browser_assisted_site",
    "is_browser_available",
    "parse_browser_assisted_sites",
    "resolve_spec",
    "run_browser_search",
    "run_browser_search_detailed",
    "search_via_browser",
    "search_via_browser_result",
]
