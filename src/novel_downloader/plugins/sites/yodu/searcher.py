#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.yodu.searcher
--------------------------------------------

"""

from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult


@registrar.register_searcher()
class YoduSearcher(BaseSearcher):
    site_name = "yodu"
    priority = 15
    BASE_URL = "https://www.yodu.org/"
    SEARCH_URL = "https://www.yodu.org/sa"

    async def _fetch_html(self, keyword: str) -> str:
        payload = {
            "searchkey": keyword,
            "searchtype": "all",
        }
        try:
            async with self.session.post(self.SEARCH_URL, data=payload) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    @staticmethod
    def _url_to_id(url: str) -> str:
        """Derive the book id from a ``/book/<id>/...`` link."""
        value = url.strip()
        if value.startswith("//"):
            value = "https:" + value
        parts = urlsplit(value)
        if parts.scheme not in ("", "http", "https"):
            return ""
        path = parts.path or value
        marker = "/book/"
        if marker not in path:
            return ""
        return path.split(marker, 1)[1].split("/", 1)[0]

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []
        rows = doc.xpath('//ul[contains(@class,"ser-ret")]/li')
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(
                row.xpath('.//a[contains(@class,"g_thumb")]/@href')
            ) or self._first_str(row.xpath(".//h3//a/@href"))
            if not href:
                continue

            # '/book/17551/?for-search' -> "17551"
            book_id = self._url_to_id(href)
            if not book_id:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(href)

            cover_rel = self._first_str(
                row.xpath('.//a[contains(@class,"g_thumb")]//img/@_src')
            ) or self._first_str(
                row.xpath('.//a[contains(@class,"g_thumb")]//img/@src')
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            title = self._first_str(row.xpath(".//h3//a/@title")) or self._first_str(
                row.xpath(".//h3//a//text()")
            )
            author = self._first_str(row.xpath(".//em//span[2]/text()"))
            latest_chapter = self._first_str(
                row.xpath('.//p[contains(.,"最新章节")]//a/text()')
            )

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
