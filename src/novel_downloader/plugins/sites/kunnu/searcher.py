#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.kunnu.searcher
---------------------------------------------

"""

import logging
from urllib.parse import quote, urlparse

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class KunnuSearcher(BaseSearcher):
    site_name = "kunnu"
    priority = 100
    BASE_URL = "https://www.kunnu.com/"
    #: Search runs on the live mirror: http://www.kunnu.com redirects to
    #: www.kunnu8.com, while https://www.kunnu.com sits behind a JS challenge.
    SEARCH_URL = "https://www.kunnu8.com/search/"

    async def _fetch_html(self, keyword: str) -> str:
        url = f"{self.SEARCH_URL}{quote(keyword)}"
        try:
            async with self.session.get(url) as resp:
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
        doc = html.fromstring(html_str)
        rows = doc.xpath(
            '//div[contains(@class,"search-list-cat")]'
            '/ul/li[contains(@class,"cat-search-item")]/a[@href]'
        )
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            href = self._first_str(row.xpath("./@href"))
            if not href:
                continue

            if limit is not None and idx >= limit:
                break

            path = urlparse(href).path
            book_id = path.strip("/").split("/")[-1]
            if not book_id:
                continue
            # Canonical detail page expected by KunnuFetcher.
            book_url = f"{self.BASE_URL}{book_id}/"

            title = self._first_str(row.xpath("./text()")) or book_id

            prio = self.priority + idx

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
