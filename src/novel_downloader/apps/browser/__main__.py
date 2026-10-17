#!/usr/bin/env python3
"""
novel_downloader.apps.browser.__main__
--------------------------------------

Entry point used by :func:`novel_downloader.apps.browser.main.search_via_browser`
(``python -m novel_downloader.apps.browser <site> <keyword>``).
"""

from novel_downloader.apps.browser.main import browser_search_main

if __name__ == "__main__":
    raise SystemExit(browser_search_main())
