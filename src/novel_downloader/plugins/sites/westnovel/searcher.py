#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.westnovel.searcher
-------------------------------------------------

Searcher for 西方奇幻小说网 main channel (www.westnovel.com).

The site has no server-side search endpoint: the old EmpireCMS backend
(``/e/search/index.php``) is gone, and the nav entry "搜索小说" points to
``/all.html`` -- a static page listing every book of the site (the page
itself suggests pressing CTRL+F). This searcher fetches that listing once
and filters the titles client-side.

``book_id`` follows the existing fetcher/parser convention
(GenericFetcher ``BOOK_ID_REPLACEMENTS = [("-", "/")]``): the URL path with
"/" replaced by "-", e.g. "/wow/bljq/" -> "wow-bljq", "/qldyx/" -> "qldyx".
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class WestnovelSearcher(BaseSearcher):
    site_name = "westnovel"
    priority = 100
    BASE_URL = "https://www.westnovel.com"
    SEARCH_URL = "https://www.westnovel.com/all.html"

    #: keyword captured in _fetch_html for the client-side title filter
    _query: str = ""

    async def _fetch_html(self, keyword: str) -> str:
        self._query = keyword.strip()
        headers = {
            **self.session.headers,
            "Referer": "https://www.westnovel.com/",
        }
        try:
            async with self.session.get(self.SEARCH_URL, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'",
                keyword,
                self.SEARCH_URL,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        if not self._query:
            return []
        query = self._query.lower()

        doc = html.fromstring(html_str)
        rows = doc.xpath('//ul[@class="chapter-grid"]//li/a[@href]')
        results: list[SearchResult] = []

        for anchor in rows:
            href = self._first_str(anchor.xpath("./@href"))
            title = self._first_str(anchor.xpath(".//text()")) or self._first_str(
                anchor.xpath("./@title")
            )
            if not href or not title:
                continue
            if query not in title.lower():
                continue

            # '/wow/bljq/' -> 'wow-bljq', '/qldyx/' -> 'qldyx'
            book_id = href.strip("/").replace("/", "-")
            if not book_id:
                continue
            book_url = self._abs_url(href)

            if limit is not None and len(results) >= limit:
                break

            prio = self.priority + len(results)
            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="",
                    title=title,
                    author="-",
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=prio,
                )
            )
        return results
