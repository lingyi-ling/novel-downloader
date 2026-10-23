#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.lnovel.searcher
----------------------------------------------
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class LnovelSearcher(BaseSearcher):
    """
    Searcher for 轻小说百科 (lnovel.org).

    The site's own search form (``q[name_cont]``) is ignored server-side,
    but the Rails "ransack" prefix predicate ``q[name_start]`` works, so the
    searcher matches titles by prefix. Result cards link to
    ``https://lnovel.org/books-{book_id}``, matching ``LnovelFetcher``.
    """

    site_name = "lnovel"
    priority = 100
    BASE_URL = "https://lnovel.org"
    SEARCH_URL = "https://lnovel.org/books"

    #: The site is intermittently unreachable; retry once before giving up.
    _ATTEMPTS = 2

    async def _fetch_html(self, keyword: str) -> str:
        url = self._build_url(
            self.SEARCH_URL, {"q%5Bname_start%5D": self._quote(keyword)}
        )
        headers = {**self.session.headers, "Referer": self.BASE_URL + "/books"}
        for _ in range(self._ATTEMPTS):
            try:
                async with self.session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    return await self._response_to_str(resp)
            except Exception:
                continue
        logger.error(
            "Failed to fetch search results for keyword '%s' from '%s'",
            keyword,
            url,
        )
        return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//main//a[starts-with(@href, "/books-")]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            href = (row.get("href") or "").strip()
            if not href:
                continue

            book_id = href.rsplit("-", 1)[-1]
            title = self._first_str([row.xpath("string()")])
            if not title:
                continue

            cover_url = self._abs_url(self._first_str(row.xpath(".//img/@src")))

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=self._abs_url(href),
                    cover_url=cover_url,
                    title=title,
                    author="-",
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=self.priority + idx,
                )
            )
        return results
