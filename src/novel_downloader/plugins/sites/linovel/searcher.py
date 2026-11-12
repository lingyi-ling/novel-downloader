#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.linovel.searcher
-----------------------------------------------
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import chapter_id_from_href
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class LinovelSearcher(BaseSearcher):
    site_name = "linovel"
    priority = 30
    BASE_URL = "https://www.linovel.net/"
    SEARCH_URL = "https://www.linovel.net/search/"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"kw": keyword}
        try:
            async with self.session.get(self.SEARCH_URL, params=params) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        if not html_str.strip():
            return []
        doc = html.fromstring(html_str)
        rows = doc.xpath('//a[contains(@class, "search-book") and @href]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            href = row.get("href", "").strip()
            if not href or "book/" not in href:
                continue

            if limit is not None and idx >= limit:
                break

            book_url = self._abs_url(href)
            # Ignore query/fragment decorations when deriving the id.
            book_id = chapter_id_from_href(href)

            cover_rel = self._first_str(
                row.xpath('.//div[contains(@class,"book-cover")]//img/@src')
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""
            title = self._first_str(
                row.xpath('.//div[contains(@class,"book-name")]/text()')
            )
            author_extra = self._first_str(
                row.xpath('.//div[contains(@class,"book-extra")]/text()')
            )

            if "丨" in author_extra:
                author, update_date = (x.strip() for x in author_extra.split("丨", 1))
            else:
                author = author_extra.strip()
                update_date = "-"

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date=update_date,
                    word_count="-",
                    priority=self.priority + idx,
                )
            )
        return results
