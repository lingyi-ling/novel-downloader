#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.mangg_net.searcher
-------------------------------------------------

"""

from urllib.parse import urlsplit

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult


@registrar.register_searcher()
class ManggNetSearcher(BaseSearcher):
    site_name = "mangg_net"
    priority = 30
    BASE_URL = "https://www.mangg.net/"
    SEARCH_URL = "https://www.mangg.net/search.php"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"q": keyword}
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
        doc = html.fromstring(html_str)
        rows = doc.xpath(
            '//div[contains(@class,"col-12") and contains(@class,"col-md-6")]/dl'
        )
        results: list[SearchResult] = []

        for row in rows:
            if limit is not None and len(results) >= limit:
                break

            href = self._first_str(row.xpath("./dt/a/@href")) or self._first_str(
                row.xpath("./dd/h3/a/@href")
            )
            if not href:
                continue

            book_id = self._url_to_id(href)
            book_url = self._abs_url(href)

            cover_src = self._first_str(row.xpath("./dt/a/img/@src"))
            cover_url = self._abs_url(cover_src) if cover_src else ""

            title = self._first_str(row.xpath("./dd/h3/a/text()"))

            author = self._first_str(
                row.xpath(
                    './/dd[contains(@class,"book_other")][contains(normalize-space(.),"作者")]/span/text()'
                )
            )
            latest_chapter = self._first_str(
                row.xpath(
                    './/dd[contains(@class,"book_other")][contains(normalize-space(.),"最新章节")]//a/text()'
                )
            )
            update_date = self._first_str(
                row.xpath(
                    './/dd[contains(@class,"book_other")][contains(normalize-space(.),"更新时间")]/text()'
                ),
                replaces=[("更新时间：", "")],
            )

            # Compute priority
            prio = self.priority + len(results)

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter=latest_chapter,
                    update_date=update_date,
                    word_count="-",
                    priority=prio,
                )
            )
        return results

    @staticmethod
    def _url_path(url: str) -> str:
        """
        Return the path of a search-result link.

        The live pages link with relative paths (``/id84814/``), but a
        protocol-relative or absolute URL must yield the same book id.
        Query strings and fragments are dropped because the fetchers build
        their own URLs from the id.
        """
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return urlsplit(url).path or url

    @staticmethod
    def _url_to_id(url: str) -> str:
        # "/id84814/" -> "id84814"
        return ManggNetSearcher._url_path(url).strip("/")
