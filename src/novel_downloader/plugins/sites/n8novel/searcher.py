#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.n8novel.searcher
-----------------------------------------------

"""

import logging
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class N8novelSearcher(BaseSearcher):
    site_name = "n8novel"
    priority = 20
    BASE_URL = "https://www.8novel.com"
    SEARCH_URL = "https://www.8novel.com/search/"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"key": keyword}
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

        The live cards link with ``/novelbooks/6045``, but an absolute or
        protocol-relative copy (plus a tracking query/fragment) must yield the
        same id, because the fetcher rebuilds the URL from it.
        """
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        parts = urlsplit(url)
        if parts.scheme not in ("", "http", "https"):
            return ""
        path = parts.path or url
        return path.rstrip("/").rsplit("/", 1)[-1]

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []
        anchors = doc.xpath("//div[contains(@class,'picsize')]/a")
        results: list[SearchResult] = []

        for a in anchors:
            href = self._first_str(a.xpath("./@href"))
            if not href:
                continue

            book_id = self._url_to_id(href)
            if not book_id:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(href)

            cover_rel = self._first_str(a.xpath(".//img/@src"))
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            title = self._first_str(a.xpath("./@title"))

            word_count = self._first_str(a.xpath(".//eps//text()")) or "-"

            # Compute priority
            prio = self.priority + len(results)

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
                    word_count=word_count,
                    priority=prio,
                )
            )
        return results
