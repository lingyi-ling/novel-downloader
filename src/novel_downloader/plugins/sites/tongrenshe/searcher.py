#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.tongrenshe.searcher
--------------------------------------------------
"""

import logging
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class TongrensheSearcher(BaseSearcher):
    site_name = "tongrenshe"
    priority = 30
    SEARCH_URL = "https://tongrenshe.cc/e/search/index.php"
    BASE_URL = "https://tongrenshe.cc"

    async def _fetch_html(self, keyword: str) -> str:
        keyboard = self._quote(keyword, encoding="gbk", errors="replace")
        show = "title"
        classid = "0"
        body = f"keyboard={keyboard}&show={show}&classid={classid}"
        headers = {
            **self.session.headers,
            "Origin": "https://tongrenshe.cc",
            "Referer": "https://tongrenshe.cc/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self.session.post(
                self.SEARCH_URL, data=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    @staticmethod
    def _url_to_id(url: str) -> str:
        """Derive the numeric book id from a ``/tongren/<id>.html`` link."""
        value = url.strip()
        if value.startswith("//"):
            value = "https:" + value
        parts = urlsplit(value)
        if parts.scheme not in ("", "http", "https"):
            return ""
        path = parts.path or value
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        return leaf.split(".", 1)[0]

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []
        rows = doc.xpath('//div[@class="books m-cols"]/div[@class="bk"]')
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(row.xpath(".//h3/a[1]/@href"))
            if not href:
                continue

            # '/tongren/9302.html' -> "9302"
            book_id = self._url_to_id(href)
            if not book_id:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(href)

            cover_rel = self._first_str(
                row.xpath("./div[@class='pic']/a[1]/img[1]/@src")
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            title = self._first_str(
                row.xpath("./div[@class='bk_right']/h3/a[1]//text()")
            )

            author = (
                self._first_str(
                    row.xpath("./div[@class='bk_right']/div[@class='booknews']/text()"),
                    replaces=[("作者：", "")],
                )
                or "-"
            )

            update_date = self._first_str(
                row.xpath(
                    "./div[@class='bk_right']/div[@class='booknews']/label[@class='date']/text()"
                )
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
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results
