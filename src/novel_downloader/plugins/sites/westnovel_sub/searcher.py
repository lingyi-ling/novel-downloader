#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.westnovel_sub.searcher
-----------------------------------------------------

Searcher for the 西方奇幻小说网轻小说频道 (www.westnovel.com/q/),
the channel handled by the ``westnovel_sub`` fetcher/parser.

The channel's search form posts to a channel-local EmpireCMS backend::

    POST https://www.westnovel.com/q/e/search/index.php
    show=title,writer & tempid=1 & keyboard=<keyword> & tbname=sinfo

That backend no longer answers: the endpoint returns an empty ``200`` body
(verified from both plain HTTP and a real browser), so no result cards are
ever produced. The channel pages themselves (``/q/list/`` and its
``index_<n>.html`` pages) are static EmpireCMS listings, so the search falls
back to fetching those listing pages and filtering the titles client-side --
the same technique the main-channel ``westnovel`` searcher uses on
``/all.html``. Page fetches stop as soon as ``limit`` matches are found.

``book_id`` follows the fetcher/resolver convention for this site:
``q-list-<numeric>``, i.e. ``/q/list/8156.html`` -> ``q-list-8156``.
"""

import asyncio
import time

import aiohttp
from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult


@registrar.register_searcher()
class WestnovelSubSearcher(BaseSearcher):
    site_name = "westnovel_sub"
    priority = 100
    BASE_URL = "https://www.westnovel.com"
    SEARCH_URL = "https://www.westnovel.com/q/e/search/index.php"
    #: First listing page (page 1); pages 2..N use ``LIST_PAGE_URL``.
    LIST_URL = "https://www.westnovel.com/q/list/"
    LIST_PAGE_URL = "https://www.westnovel.com/q/list/index_{page}.html"
    #: Total number of channel listing pages (``index_150.html`` is the last).
    MAX_LIST_PAGES = 150
    #: How many listing pages to fetch concurrently during the fallback.
    _LIST_CONCURRENCY = 8
    #: Soft wall-clock budget (seconds) for one search.  The orchestrator
    #: cancels a site at its per-search deadline (5s by default, often 8s);
    #: scanning all ``MAX_LIST_PAGES`` pages can take longer than that, so the
    #: fallback stops early and returns the matches found so far instead of
    #: always being cancelled with nothing to show.  Never exceeds the
    #: session's own timeout minus a safety margin.
    SCAN_BUDGET_SECONDS = 4.0
    #: The channel search backend answers with an empty body; cap the probe so
    #: a hung response cannot consume the whole search budget.
    _PROBE_TIMEOUT_SECONDS = 2.0

    #: keyword captured in ``_fetch_html`` for the client-side title filter
    _query: str = ""

    async def _fetch_html(self, keyword: str) -> str:
        self._query = keyword.strip()
        headers = {
            **self.session.headers,
            "Origin": "https://www.westnovel.com",
            "Referer": "https://www.westnovel.com/q/list/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # aiohttp form-encodes with UTF-8 percent-encoding, matching the
        # UTF-8 channel pages.
        form = {
            "show": "title,writer",
            "tempid": "1",
            "keyboard": keyword,
            "tbname": "sinfo",
        }
        try:
            async with self.session.post(
                self.SEARCH_URL,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._PROBE_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        """
        Search the 轻小说频道.

        The channel search backend is dead, so this first tries the POST
        endpoint (cheap, capped by ``_PROBE_TIMEOUT_SECONDS``) and, if it
        yields nothing, scans the static listing pages client-side.  The scan
        honours a soft deadline so the search returns partial results instead
        of overrunning the caller's per-site budget.

        :param keyword: Search keyword.
        :param limit: Maximum number of results to return.
        :return: List of search results.
        """
        deadline = time.monotonic() + self._budget()
        html_str = await self._fetch_html(keyword)
        if html_str:
            results = self._parse_html(html_str, limit)
            if results:
                return results
        return await self._search_listing(keyword, limit, deadline)

    def _budget(self) -> float:
        """Return this search's wall-clock budget, bounded by the session."""
        try:
            total = self.session.timeout.total
        except AttributeError:
            total = None
        if total is None:
            return self.SCAN_BUDGET_SECONDS
        return max(0.5, min(self.SCAN_BUDGET_SECONDS, total - 0.75))

    async def _search_listing(
        self, keyword: str, limit: int | None, deadline: float | None = None
    ) -> list[SearchResult]:
        """
        Filter the static channel listing by ``keyword`` (client-side).

        Stops as soon as ``limit`` matches are collected or the deadline (see
        :meth:`_budget`) passes; the latter can yield partial results, which
        is preferable to a guaranteed cancellation.
        """
        if not keyword.strip():
            return []

        if deadline is None:
            deadline = time.monotonic() + self._budget()

        cap = limit if limit and limit > 0 else None
        pages = list(range(1, self.MAX_LIST_PAGES + 1))
        results: list[SearchResult] = []
        seen: set[str] = set()
        sem = asyncio.Semaphore(self._LIST_CONCURRENCY)

        async def fetch(page: int) -> str:
            url = self.LIST_URL if page == 1 else self.LIST_PAGE_URL.format(page=page)
            async with sem:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ""
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=remaining)
                    ) as resp:
                        resp.raise_for_status()
                        return await self._response_to_str(resp)
                except Exception:
                    return ""

        for start in range(0, len(pages), self._LIST_CONCURRENCY):
            if time.monotonic() >= deadline:
                break
            group = pages[start : start + self._LIST_CONCURRENCY]
            pages_html = await asyncio.gather(*(fetch(p) for p in group))
            for page_html in pages_html:
                if not page_html:
                    continue
                for item in self._parse_html(page_html):
                    book_id = item["book_id"]
                    if book_id in seen:
                        continue
                    seen.add(book_id)
                    results.append(item)
                    if cap is not None and len(results) >= cap:
                        return results
        return results

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[@id="sitebox"]//dl')
        results: list[SearchResult] = []
        query = self._query.lower()

        for idx, row in enumerate(rows):
            href = self._first_str(row.xpath("./dd/h3/a[1]/@href"))
            if not href:
                continue

            title = self._first_str(row.xpath("./dd/h3/a[1]//text()"))
            # The listing is unfiltered, so drop titles that do not match.
            if query and query not in title.lower():
                continue

            if limit is not None and len(results) >= limit:
                break

            # '/q/list/8156.html' -> 'q-list-8156'
            parts = href.strip("/").split("/")
            if len(parts) != 3 or parts[-1].split(".", 1)[0] == "":
                continue
            book_id = "-".join([parts[0], parts[1], parts[-1].split(".", 1)[0]])
            book_url = self._abs_url(href)

            cover_url = self._abs_url(self._first_str(row.xpath("./dt//img/@src")))

            prio = self.priority + idx
            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author="-",
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=prio,
                )
            )
        return results
