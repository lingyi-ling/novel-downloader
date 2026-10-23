#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.akatsuki_novels.searcher
--------------------------------------------------------

Search 暁 novels via the novel search form at
https://www.akatsuki-novels.com/novels/ (POST to ``/novels/index/``).
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class AkatsukiNovelsSearcher(BaseSearcher):
    site_name = "akatsuki_novels"
    priority = 100
    BASE_URL = "https://www.akatsuki-novels.com"
    SEARCH_URL = "https://www.akatsuki-novels.com/novels/index/"

    async def _fetch_html(self, keyword: str) -> str:
        form = {
            "_method": "POST",
            "data[Novel][multi_keyword]": keyword,
        }
        try:
            async with self.session.post(
                self.SEARCH_URL,
                data=form,
                headers={"Referer": self.BASE_URL + "/novels"},
            ) as resp:
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
        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[contains(@class, "topicsBox")]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            href = self._first_str(
                row.xpath(
                    './strong[contains(@class, "novelTitle")]'
                    '/a[contains(@href, "novel_id~")]/@href'
                )
            )
            if not href:
                continue

            if limit is not None and idx >= limit:
                break

            # href format: /stories/index/novel_id~18228
            book_id = href.split("novel_id~", 1)[-1].strip("/")
            book_url = self._abs_url(href)

            title = self._join_strs(
                row.xpath(
                    './strong[contains(@class, "novelTitle")]'
                    '/a[contains(@href, "novel_id~")]//text()'
                )
            )
            author = self._first_str(
                row.xpath(
                    './strong[contains(@class, "novelTitle")]'
                    '/a[contains(@href, "/users/view/")]/text()'
                )
            )

            # e.g. "連載中（全 64話）"
            status = self._first_str(
                row.xpath(
                    './strong[contains(@class, "novelTitle")]/text()[normalize-space()]'
                )
            )

            update_date = self._join_strs(
                row.xpath('.//span[contains(text(), "最終掲載日時")]/text()'),
                replaces=[
                    ("最終掲載日時：", ""),
                    ("\xa0", ""),
                    ("\u3000", " "),
                ],
            )

            # e.g. "文字数： 315,731 / 1話当たりの文字数： 4,933"
            word_count_raw = self._join_strs(
                row.xpath('.//span[starts-with(text(), "文字数")]/text()'),
                replaces=[("文字数：", ""), ("\xa0", "")],
            )
            word_count = word_count_raw.split("/", 1)[0].strip()

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="",
                    title=title,
                    author=author or "-",
                    latest_chapter=status or "-",
                    update_date=update_date or "-",
                    word_count=word_count or "-",
                    priority=self.priority + idx,
                )
            )
        return results
