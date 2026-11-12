#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.novelpia.searcher
-------------------------------------------------

Search ノベルピア novels via the keyword search page
(https://novelpia.jp/search/keyword/date/1/<keyword>), which renders the
result list server-side.
"""

import logging
import re

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_BOOK_LINK_RE = re.compile(r"/novel/(\d+)")


@registrar.register_searcher()
class NovelpiaSearcher(BaseSearcher):
    site_name = "novelpia"
    priority = 100
    BASE_URL = "https://novelpia.jp"
    SEARCH_URL = "https://novelpia.jp/search/keyword/date/1/{query}"

    async def _fetch_html(self, keyword: str) -> str:
        url = self.SEARCH_URL.format(query=self._quote(keyword))
        try:
            async with self.session.get(
                url,
                headers={"Referer": self.BASE_URL + "/search"},
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, url, exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []

        # PC rows carry the "novelbox" class; the mobile duplicates do not.
        rows = doc.xpath('//div[contains(@class, "novelbox")]')
        results: list[SearchResult] = []

        for row in rows:
            onclick = self._first_str(
                row.xpath('.//b[contains(@onclick, "/novel/")]/@onclick')
            )
            match = _BOOK_LINK_RE.search(onclick) if onclick else None
            if match is None:
                href = self._first_str(
                    row.xpath('.//b/a[contains(@href, "/novel/")]/@href')
                )
                match = _BOOK_LINK_RE.search(href) if href else None
            if match is None:
                continue

            title = self._join_strs(
                row.xpath('.//b[contains(@onclick, "/novel/")]//text()')
            ) or self._join_strs(
                row.xpath('.//b/a[contains(@href, "/novel/")]//text()')
            )
            if not title:
                continue

            if limit is not None and len(results) >= limit:
                break

            book_id = match.group(1)
            book_url = f"{self.BASE_URL}/novel/{book_id}"

            # first <font> after the title is the author, the second is
            # the synopsis
            author = self._first_str(row.xpath('.//td[@valign="top"]/font[1]/text()'))

            cover_rel = self._first_str(row.xpath(".//td[1]//img/@src"))
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author or "-",
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results
