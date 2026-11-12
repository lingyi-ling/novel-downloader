#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.alicesw.searcher
-----------------------------------------------
"""

import logging
import re
from urllib.parse import urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_WORD_COUNT_RE = re.compile(r"字数[：:]\s*([^\s\u3000]+)")


@registrar.register_searcher()
class AliceswSearcher(BaseSearcher):
    site_name = "alicesw"
    priority = 500
    BASE_URL = "https://www.alicesw.com/"
    SEARCH_URL = "https://www.alicesw.com/search.html"

    @property
    def nsfw(self) -> bool:
        return True

    async def _fetch_html(self, keyword: str) -> str:
        params = {"q": keyword, "f": "_all"}
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
        doc = parse_html_tree(html_str)
        if doc is None:
            return []
        rows = doc.xpath("//div[contains(@class, 'list-group-item')]")
        results: list[SearchResult] = []

        for row in rows:
            if limit is not None and len(results) >= limit:
                break

            href = self._first_str(row.xpath(".//h5/a/@href"))
            if not href:
                continue

            book_id = urlsplit(href).path.rsplit("/", 1)[-1].split(".", 1)[0]
            if not book_id:
                continue
            book_url = self._abs_url(href)
            title = self._join_strs(row.xpath(".//h5/a//text()"))
            author = self._first_str(
                row.xpath(".//p[contains(@class,'text-muted')]/a/text()")
            )
            update_date = self._first_str(
                row.xpath(
                    ".//p[contains(@class,'timedesc')]/text()[contains(., '更新时间')]"
                )
            )
            update_date = update_date.split("更新时间：", 1)[-1]
            meta_text = self._join_strs(
                row.xpath(
                    ".//p[contains(@class,'text-muted')][contains(., '字数')]/text()"
                )
            )
            wc_match = _WORD_COUNT_RE.search(meta_text)
            word_count = wc_match.group(1) if wc_match else "-"

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
                    word_count=word_count,
                    priority=self.priority + len(results),
                )
            )
        return results
