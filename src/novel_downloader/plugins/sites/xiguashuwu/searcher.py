#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.xiguashuwu.searcher
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
class XiguashuwuSearcher(BaseSearcher):
    site_name = "xiguashuwu"
    priority = 500
    BASE_URL = "https://www.xiguashuwu.net"
    SEARCH_URL = "https://www.xiguashuwu.net/search/{query}"

    #: The site's WAF interstitial ("安全验证") is a trivial math captcha whose
    #: verify() handler simply sets this constant cookie and reloads, so sending
    #: it up front skips the challenge entirely.
    WAF_COOKIE = {"nginx_waf_passed": "1"}

    @property
    def nsfw(self) -> bool:
        return True

    async def _fetch_html(self, keyword: str) -> str:
        url = self.SEARCH_URL.format(query=self._quote(keyword))
        headers = {
            **self.session.headers,
            "Referer": "https://www.xiguashuwu.net/search/",
        }
        try:
            async with self.session.get(
                url, headers=headers, cookies=self.WAF_COOKIE
            ) as resp:
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
        rows = doc.xpath('//div[@class="SHsectionThree-middle"]/p')
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(
                row.xpath(".//a[starts-with(@href,'/book/')][1]/@href")
            )
            if not href:
                href = self._first_str(row.xpath(".//a[1]/@href"))
            if not href:
                continue

            # '/book/184974/iszip/0/' -> "184974"
            book_id = self._url_to_id(href)
            if not book_id:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(href)

            title = (
                self._first_str(
                    row.xpath(".//a[starts-with(@href,'/book/')][1]//text()")
                )
                or self._first_str(row.xpath(".//a[1]//text()"))
                or "-"
            )

            author = (
                self._first_str(
                    row.xpath(".//a[starts-with(@href,'/writer/')][1]//text()")
                )
                or self._first_str(row.xpath(".//a[2]//text()"))
                or "-"
            )

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="-",
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results
