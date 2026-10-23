#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.faloo.searcher
---------------------------------------------
"""

import logging
import re

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_WORD_COUNT_RE = re.compile(r"字数[：:]\s*([^|｜]+)")
_DATE_RE = re.compile(r"日期[：:]\s*(\S+)")


@registrar.register_searcher()
class FalooSearcher(BaseSearcher):
    """
    Searcher for 飞卢小说网 (b.faloo.com).

    Uses the original-library book search the site's own search box jumps to:
    ``https://b.faloo.com/l_0_1.html?t=1&k=<gbk-encoded keyword>``.
    Pages are GBK encoded, so the keyword is percent-encoded with GBK.
    """

    site_name = "faloo"
    priority = 100
    BASE_URL = "https://b.faloo.com"
    SEARCH_URL = "https://b.faloo.com/l_0_1.html"

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "t": "1",
            "k": self._quote(keyword, encoding="gbk"),
        }
        full_url = self._build_url(self.SEARCH_URL, params)
        headers = {
            **self.session.headers,
            "Referer": "https://b.faloo.com/",
        }
        try:
            async with self.session.get(full_url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'",
                keyword,
                self.SEARCH_URL,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        if not html_str:
            return []

        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[@class="TwoBox02_02"]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            href = self._first_str(row.xpath('.//div[@class="TwoBox02_08"]/h1/a/@href'))
            if not href:
                continue

            # '//b.faloo.com/1440069.html' -> "1440069"
            book_id = href.rsplit("/", 1)[-1].removesuffix(".html")
            book_url = self._abs_url(href)

            title = self._first_str(
                row.xpath('.//div[@class="TwoBox02_08"]/h1/a/text()')
            )
            author = self._first_str(
                row.xpath('.//div[@class="TwoBox02_09"]//a/text()')
            )

            stats = " ".join(
                t.strip()
                for t in row.xpath(
                    './div[@class="TwoBox02_04"]/div[@class="TwoBox02_05"][2]//text()'
                )
                if t and t.strip()
            )
            wc_match = _WORD_COUNT_RE.search(stats)
            date_match = _DATE_RE.search(stats)
            word_count = wc_match.group(1).strip() if wc_match else "-"
            update_date = date_match.group(1).strip() if date_match else "-"

            latest_chapter = self._first_str(
                row.xpath(
                    './/span[starts-with(normalize-space(.),"最新章节")]/a/text()'
                )
            )

            cover_rel = self._first_str(
                row.xpath('./div[@class="TwoBox02_03"]//img/@src')
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            prio = self.priority + idx

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter=latest_chapter or "-",
                    update_date=update_date,
                    word_count=word_count,
                    priority=prio,
                )
            )
        return results
