#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ciweimao.searcher
------------------------------------------------
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class CiweimaoSearcher(BaseSearcher):
    """
    Searcher for 刺猬猫 (www.ciweimao.com).

    Uses the web book list the header search form submits to:
    ``https://www.ciweimao.com/get-search-book-list/0-0-0-0-0-0/全部/
    <keyword>/1``. No login is required for keyword queries.
    """

    site_name = "ciweimao"
    priority = 100
    BASE_URL = "https://www.ciweimao.com"
    SEARCH_URL = "https://www.ciweimao.com/get-search-book-list/0-0-0-0-0-0"

    async def _fetch_html(self, keyword: str) -> str:
        full_url = f"{self.SEARCH_URL}/{self._quote('全部')}/{self._quote(keyword)}/1"
        headers = {
            **self.session.headers,
            "Referer": "https://www.ciweimao.com/",
        }
        try:
            async with self.session.get(full_url, headers=headers) as resp:
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
        if not html_str:
            return []

        doc = html.fromstring(html_str)
        rows = doc.xpath("//li[@data-book-id]")
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            href = self._first_str(row.xpath('./a[@class="cover"]/@href'))
            if not href:
                continue

            # 'https://www.ciweimao.com/book/100177159' -> "100177159"
            book_id = href.rstrip("/").rsplit("/", 1)[-1]
            book_url = self._abs_url(href)

            title = self._first_str(
                row.xpath('./div[@class="cnt"]/p[@class="tit"]/a[1]/@title')
            ) or self._first_str(
                row.xpath('./div[@class="cnt"]/p[@class="tit"]/a[1]//text()')
            )

            author = self._first_str(row.xpath('./div[@class="cnt"]/p[2]/a[1]//text()'))

            update_line = self._join_strs(
                row.xpath(
                    './div[@class="cnt"]/p[starts-with('
                    'normalize-space(.),"最近更新")]/text()'
                ),
                replaces=[("最近更新：", ""), ("最近更新:", "")],
            )
            update_date, _, latest = update_line.partition("/")
            update_date = update_date.strip()
            latest_chapter = latest.strip()

            cover_rel = self._first_str(row.xpath('./a[@class="cover"]/img/@src'))
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            prio = self.priority + idx

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter=latest_chapter or "-",
                    update_date=update_date or "-",
                    word_count="-",
                    priority=prio,
                )
            )
        return results
