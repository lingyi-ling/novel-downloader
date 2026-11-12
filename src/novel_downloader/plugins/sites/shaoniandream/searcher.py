#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.shaoniandream.searcher
-----------------------------------------------------

Searcher for 少年梦 (www.shaoniandream.com).

The site's library page doubles as the search endpoint:
``GET https://www.shaoniandream.com/library?keywords=<keyword>``
returns a ``BookPicList`` of matching works, no login required.
"""

import logging
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class ShaoniandreamSearcher(BaseSearcher):
    site_name = "shaoniandream"
    priority = 100
    BASE_URL = "https://www.shaoniandream.com"
    SEARCH_URL = "https://www.shaoniandream.com/library"

    async def _fetch_html(self, keyword: str) -> str:
        url = self._build_url(self.SEARCH_URL, {"keywords": self._quote(keyword)})
        headers = {
            **self.session.headers,
            "Referer": "https://www.shaoniandream.com/",
        }
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []

        rows = doc.xpath('//div[@class="BookPicList"]//dl')
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(row.xpath('dd[@class="img"]/a/@href'))
            if not href or href.lower().startswith(("javascript:", "#")):
                continue

            # '.../book_detail/6686?from=list#top' -> '6686'
            path = urlsplit(href).path.rstrip("/")
            book_id = path.rsplit("/", 1)[-1]
            if not book_id:
                continue
            book_url = self._abs_url(path)

            title = self._first_str(row.xpath('dd[@class="title"]/a/text()'))
            if not title:
                continue

            if limit is not None and len(results) >= limit:
                break

            author = self._first_str(row.xpath('dd[@class="author"]/a[1]/text()'))

            latest_chapter = (
                self._first_str(
                    row.xpath('dd[@class="newChapter"]/a/text()'),
                    replaces=[("最新章节：", "")],
                )
                or "-"
            )

            cover_rel = self._first_str(
                row.xpath('dd[@class="img"]//img/@data-original')
            ) or self._first_str(row.xpath('dd[@class="img"]//img/@src'))
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter=latest_chapter,
                    update_date="-",
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results
