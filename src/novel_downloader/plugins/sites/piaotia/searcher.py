#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.piaotia.searcher
-----------------------------------------------

"""

import logging
import re
from typing import Any

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import (
    parse_html_tree,
    resolve_url,
    strip_label,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_BOOKINFO_RE = re.compile(r"/bookinfo/(\d+)/(\d+)\.html?")
_CHAPTER_DIR_RE = re.compile(r"/html/(\d+)/(\d+)(?:/|$)")
_COVER_PATH_RE = re.compile(r"/files/article/image/(\d+)/(\d+)/")
_LATEST_CHAPTER_RE = re.compile(r"[第0-9零一二三四五六七八九十百千万]")


@registrar.register_searcher()
class PiaotiaSearcher(BaseSearcher):
    site_name = "piaotia"
    priority = 30
    BASE_URL = "https://www.piaotia.com"
    SEARCH_URL = "https://www.piaotia.com/modules/article/search.php"

    async def _fetch_html(self, keyword: str) -> str:
        # data = {
        #     "searchtype": "articlename",
        #     # "searchtype": "author",
        #     # "searchtype": "keywords",
        #     "searchkey": self._quote(keyword, encoding="gbk", errors='replace'),
        #     "Submit": self._quote(" 搜 索 ", encoding="gbk", errors='replace'),
        # }
        searchtype = "articlename"
        searchkey = self._quote(keyword, encoding="gbk", errors="replace")
        submit = self._quote(" 搜 索 ", encoding="gbk", errors="replace")
        body = f"searchtype={searchtype}&searchkey={searchkey}&Submit={submit}"
        headers = {
            **self.session.headers,
            "Origin": "https://www.piaotia.com",
            "Referer": "https://www.piaotia.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self.session.post(
                self.SEARCH_URL, data=body, headers=headers
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp, encoding="gbk")
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = parse_html_tree(html_str)
        if doc is None:
            return []

        # An exact title match answers with a 302 to the book-info page instead
        # of a result table.  Its chapter/review tables also have class "grid",
        # so the old row selector turned book reviews into "books".
        if doc.xpath('//td[contains(text(),"全文长度")]'):
            hit = self._parse_book_page(doc)
            if hit is None or (limit is not None and limit <= 0):
                return []
            return [hit]

        rows = doc.xpath(
            '//table[@class="grid"]//tr[td[1]/a[1][contains(@href,"bookinfo/")]]'
        )
        results: list[SearchResult] = []

        for row in rows:
            href = self._first_str(row.xpath("./td[1]/a[1]/@href"))
            match = _BOOKINFO_RE.search(href)
            if match is None:
                continue
            title = self._first_str(row.xpath("./td[1]/a[1]//text()"))
            if not title:
                continue

            if limit is not None and len(results) >= limit:
                break

            category, bid = match.group(1), match.group(2)
            book_id = f"{category}-{bid}"
            book_url = f"{self.BASE_URL}/bookinfo/{category}/{bid}.html"

            latest_chapter = self._first_str(row.xpath("./td[2]/a[1]//text()")) or "-"
            author = self._first_str(row.xpath("./td[3]//text()")) or "-"
            word_count = self._first_str(row.xpath("./td[4]//text()")) or "-"
            update_date = self._first_str(row.xpath("./td[5]//text()")) or "-"

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="",
                    title=title,
                    author=author,
                    latest_chapter=latest_chapter,
                    update_date=update_date,
                    word_count=word_count,
                    priority=self.priority + len(results),
                )
            )
        return results

    def _parse_book_page(self, doc: Any) -> SearchResult | None:
        """Build the single result for an exact-match book-info page."""
        tree = doc
        title = self._first_str(tree.xpath("//h1/text()"))
        if not title:
            return None

        ids: tuple[str, str] | None = None
        for href in tree.xpath('//a[contains(@href,"/html/")]/@href'):
            match = _CHAPTER_DIR_RE.search(href)
            if match:
                ids = (match.group(1), match.group(2))
                break
        if ids is None:
            cover = self._first_str(tree.xpath('//td[@width="80%"]//img/@src'))
            match = _COVER_PATH_RE.search(cover)
            if match:
                ids = (match.group(1), match.group(2))
        if ids is None:
            return None

        category, bid = ids
        author = strip_label(
            self._first_str(
                tree.xpath(
                    '//td[contains(text(),"作") and contains(text(),"者")]/text()'
                )
            ),
            "作者",
        )
        word_count = strip_label(
            self._first_str(tree.xpath('//td[contains(text(),"全文长度")]/text()')),
            "全文长度",
        )
        update_date = strip_label(
            self._first_str(tree.xpath('//td[contains(text(),"最后更新")]/text()')),
            "最后更新",
        )
        cover_url = self._first_str(tree.xpath('//td[@width="80%"]//img/@src'))
        cover_url = resolve_url(tree, cover_url, fallback_base=self.BASE_URL)

        latest_chapter = "-"
        for href, text in zip(
            tree.xpath(f'//a[contains(@href,"/html/{category}/{bid}/")]/@href'),
            tree.xpath(f'//a[contains(@href,"/html/{category}/{bid}/")]/text()'),
            strict=False,
        ):
            cleaned = text.strip()
            if (
                cleaned
                and "index.html" not in href
                and _LATEST_CHAPTER_RE.search(cleaned)
            ):
                latest_chapter = cleaned
                break

        return SearchResult(
            site=self.site_name,
            book_id=f"{category}-{bid}",
            book_url=f"{self.BASE_URL}/bookinfo/{category}/{bid}.html",
            cover_url=cover_url,
            title=title,
            author=author or "-",
            latest_chapter=latest_chapter,
            update_date=update_date or "-",
            word_count=word_count or "-",
            priority=self.priority,
        )
