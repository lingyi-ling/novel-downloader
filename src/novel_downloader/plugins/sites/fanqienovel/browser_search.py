#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.browser_search
---------------------------------------------------------

Browser search description for 番茄小说网 (fanqienovel.com).

The site's search API only answers requests carrying an ``msToken`` plus an
``a_bogus`` signature that its own JavaScript computes, so an HTTP client gets
an empty body even with a valid login cookie. The search therefore has to be
performed by the site itself, in a real browser window (see
:mod:`novel_downloader.apps.browser`).
"""

from novel_downloader.apps.browser import BrowserSearchSpec

BROWSER_SEARCH = BrowserSearchSpec(
    site="fanqienovel",
    home_url="https://fanqienovel.com/",
    search_url="https://fanqienovel.com/search/{keyword}",
    response_marker="search_book/v1",
)
