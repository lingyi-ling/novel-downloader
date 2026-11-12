#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.akatsuki_novels.searcher
--------------------------------------------------------

Search 暁 novels via the novel search form at
https://www.akatsuki-novels.com/novels/ (GET to ``/novels/index``).

The site was redesigned in 2026: the form is a plain ``GET`` with a single
``multi_keyword`` field (the old code POSTed ``data[Novel][multi_keyword]`` to
``/novels/index/`` and was answered with ``405 Method Not Allowed``), and the
result cards are now ``<li>`` elements linking to ``/novels/view/<id>`` instead
of the old ``div.topicsBox`` / ``novel_id~`` markup.  Both shapes are parsed so
cached pages and the current site keep working.
"""

import logging
import re

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_BOOK_ID_RE = re.compile(r"(?:novel_id~|/novels/view/)(\d+)")


@registrar.register_searcher()
class AkatsukiNovelsSearcher(BaseSearcher):
    site_name = "akatsuki_novels"
    priority = 100
    BASE_URL = "https://www.akatsuki-novels.com"
    SEARCH_URL = "https://www.akatsuki-novels.com/novels/index"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"multi_keyword": keyword}
        try:
            async with self.session.get(
                self.SEARCH_URL,
                params=params,
                headers={"Referer": self.BASE_URL + "/novels"},
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    @staticmethod
    def _book_id(href: str) -> str:
        match = _BOOK_ID_RE.search(href or "")
        return match.group(1) if match else ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []

        rows = doc.xpath('//div[contains(@class, "topicsBox")]')
        if not rows:
            # 2026 layout: one <li> per result inside the results <ul>.
            rows = doc.xpath(
                '//ul[contains(@class, "grid-cols-2")]'
                '/li[.//a[contains(@href, "/novels/view/")]]'
            )

        results: list[SearchResult] = []
        for row in rows:
            if limit is not None and len(results) >= limit:
                break

            # Current layout uses /novels/view/<id>; cached pages still use
            # /stories/index/novel_id~<id>.
            href = self._first_str(
                row.xpath('.//a[contains(@href, "/novels/view/")]/@href')
            ) or self._first_str(
                row.xpath(
                    './strong[contains(@class, "novelTitle")]'
                    '/a[contains(@href, "novel_id~")]/@href'
                )
            )
            if not href:
                continue

            book_id = self._book_id(href)
            if not book_id:
                continue
            book_url = self._abs_url(href)

            if row.xpath('.//a[contains(@href, "/novels/view/")]'):
                title = self._join_strs(
                    row.xpath('.//a[contains(@href, "/novels/view/")]//text()')
                )
                author = self._first_str(
                    row.xpath('.//a[contains(@href, "/users/view/")]/text()')
                )
                status = self._first_str(
                    row.xpath('.//span[contains(text(), "話")]/text()')
                )
                update_date = self._first_str(
                    row.xpath('.//span[contains(text(), "最終更新")]/text()'),
                    replaces=[("最終更新", "")],
                )
                word_count = self._first_str(
                    row.xpath('.//span[contains(text(), "文字")]/text()'),
                    replaces=[("文字", "")],
                )
            else:
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
                        './strong[contains(@class, "novelTitle")]'
                        "/text()[normalize-space()]"
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
                    priority=self.priority + len(results),
                )
            )
        return results
