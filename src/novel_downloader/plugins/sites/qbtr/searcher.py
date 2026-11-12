#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.qbtr.searcher
--------------------------------------------

"""

import logging
import re
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_BOOK_PATH_RE = re.compile(r"^/(?P<category>[^/]+)/(?P<book_id>\d+)\.html?$")


@registrar.register_searcher()
class QbtrSearcher(BaseSearcher):
    site_name = "qbtr"
    priority = 30
    BASE_URL = "https://www.qbtr.cc"
    SEARCH_URL = "https://www.qbtr.cc/e/search/index.php"

    async def _fetch_html(self, keyword: str) -> str:
        keyboard = self._quote(keyword, encoding="gbk", errors="replace")
        show = "title"
        classid = "0"
        body = f"keyboard={keyboard}&show={show}&classid={classid}"
        headers = {
            **self.session.headers,
            "Origin": "https://www.qbtr.cc",
            "Referer": "https://www.qbtr.cc/",
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
            if href.lower().startswith(("javascript:", "#")):
                continue

            # ``/tongren/8850.html`` -> ``tongren-8850``.  Use the URL path so
            # an absolute or query-carrying href cannot leak into the id.
            path = urlsplit(href).path
            match = _BOOK_PATH_RE.match(path)
            if match is None:
                continue
            book_id = f"{match.group('category')}-{match.group('book_id')}"

            title = self._first_str(row.xpath(".//h3/a[1]//text()"))
            if not title:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_url = self._abs_url(path)

            author = (
                self._first_str(
                    row.xpath(".//div[contains(@class,'booknews')]/text()"),
                    replaces=[("作者：", "")],
                )
                or "-"
            )

            update_date = (
                self._first_str(
                    row.xpath(
                        ".//div[contains(@class,'booknews')]"
                        "/label[contains(@class,'date')]/text()"
                    )
                )
                or "-"
            )

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="",
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date=update_date,
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results
