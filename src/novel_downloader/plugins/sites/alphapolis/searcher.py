#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.alphapolis.searcher
--------------------------------------------------

Search アルファポリス novels via the global search page
(https://www.alphapolis.co.jp/search?query=...&tab=novel&category=novel).
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class AlphapolisSearcher(BaseSearcher):
    site_name = "alphapolis"
    priority = 100
    BASE_URL = "https://www.alphapolis.co.jp"
    SEARCH_URL = "https://www.alphapolis.co.jp/search"

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "query": keyword,
            "tab": "novel",
            "category": "novel",
        }
        try:
            async with self.session.get(
                self.SEARCH_URL,
                params=params,
                headers={"Referer": self.BASE_URL + "/"},
            ) as resp:
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
            '//div[contains(@class, "content-block") and contains(@class, "novels")]'
        )
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            href = self._first_str(
                row.xpath('.//h2[contains(@class, "title")]/a[1]/@href')
            )
            if not href:
                continue

            if limit is not None and idx >= limit:
                break

            # href format: /novel/182309940/328987318
            # book_id keeps the dash form ("182309940-328987318") expected
            # by the fetcher's BOOK_ID_REPLACEMENTS ("-" -> "/").
            parts = href.strip("/").split("/")
            book_id = "-".join(parts[1:]) if len(parts) >= 3 else parts[-1]
            book_url = self._abs_url(href)

            title = self._first_str(
                row.xpath('.//h2[contains(@class, "title")]/a[1]//text()')
            )
            author = self._first_str(
                row.xpath('.//div[contains(@class, "author")]/a[1]/text()')
            )

            status = self._first_str(
                row.xpath('.//span[contains(@class, "content-status")]/text()')
            )

            word_count = self._first_str(
                row.xpath('.//span[contains(@class, "wordcount")]/text()'),
                replaces=[("文字数", "")],
            )

            update_date = self._first_str(
                row.xpath('.//span[contains(@class, "updated")]/text()'),
                replaces=[("最終更新日", "")],
            )

            cover_rel = self._first_str(
                row.xpath('.//img[contains(@src, "cdn-image")]/@src')
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author or "-",
                    latest_chapter=status or "-",
                    update_date=update_date or "-",
                    word_count=word_count or "-",
                    priority=self.priority + idx,
                )
            )
        return results
