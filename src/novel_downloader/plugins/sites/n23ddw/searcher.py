#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.n23ddw.searcher
----------------------------------------------

"""

import logging
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class N23ddwSearcher(BaseSearcher):
    site_name = "n23ddw"
    priority = 30
    BASE_URL = "https://www.23ddw.net/"
    SEARCH_URL = "https://www.23ddw.net/searchss/"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"searchkey": keyword}
        try:
            async with self.session.get(self.SEARCH_URL, params=params) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    @staticmethod
    def _url_to_id(url: str) -> str:
        """
        Derive the book id from a search-result link.

        The fetcher expects ``<parent>-<book>`` and turns ``-`` back into
        ``/``.  Only the URL path may be used: absolute links, query strings
        and fragments must not become part of the id.
        """
        value = url.strip()
        if value.startswith("//"):
            value = "https:" + value
        parts = urlsplit(value)
        if parts.scheme not in ("", "http", "https"):
            return ""
        path = parts.path or value
        path = path.strip("/")
        prefix, sep, rest = path.partition("/du/")
        if sep:
            path = rest
        elif path.startswith("du/"):
            path = path[len("du/") :]
        if not path:
            return ""
        return path.replace("/", "-")

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []
        rows = doc.xpath('//div[@id="hotcontent"]//div[@class="item"]')
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(row.xpath(".//dl/dt/a/@href"))
            if not href:
                continue

            # "/du/291/291325/" -> "291-291325"
            book_id = self._url_to_id(href)
            if not book_id:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(href)

            title = self._first_str(row.xpath(".//dl/dt/a/text()"))
            author = self._first_str(
                row.xpath('.//div[contains(@class,"btm")]//a[1]/text()')
            )
            cover_url = self._first_str(
                row.xpath(
                    './/div[contains(@class,"image")]//img[1]/@data-original | '
                    './/div[contains(@class,"image")]//img[1]/@src'
                )
            )
            cover_url = self._abs_url(cover_url) if cover_url else ""

            word_count = (
                self._first_str(
                    row.xpath(
                        './/div[contains(@class,"btm")]//em[contains(@class,"orange")]/text()'
                    )
                )
                or "-"
            )
            update_date = (
                self._first_str(
                    row.xpath(
                        './/div[contains(@class,"btm")]//em[contains(@class,"blue")]/text()'
                    )
                )
                or "-"
            )

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
                    word_count=word_count,
                    priority=self.priority + len(results),
                )
            )
        return results
