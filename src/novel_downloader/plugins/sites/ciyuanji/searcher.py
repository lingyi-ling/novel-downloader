#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ciyuanji.searcher
------------------------------------------------
"""

import json
import logging
from typing import Any

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.common.parser._utils import parse_html_tree
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class CiyuanjiSearcher(BaseSearcher):
    site_name = "ciyuanji"
    priority = 5
    BASE_URL = "https://www.ciyuanji.com/"
    BOOK_INFO_URL = "https://www.ciyuanji.com/b_d_{book_id}.html"
    SEARCH_URL = "https://www.ciyuanji.com/search/{query}_0_0_0_0_0_1.html"

    async def _fetch_html(self, keyword: str) -> str:
        url = self.SEARCH_URL.format(query=self._quote(keyword))
        try:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, url, exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        data = self._find_next_data(html_str)
        rows = self._extract_search_list(data)
        results: list[SearchResult] = []

        for row in rows:
            if limit is not None and len(results) >= limit:
                break
            if not isinstance(row, dict):
                continue

            book_id = str(row.get("bookId") or "")
            if not book_id:
                continue
            title = str(row.get("bookName") or "").strip()
            author = str(row.get("authorName") or "").strip()
            cover_url = str(row.get("imgUrl") or row.get("blurryImgUrl") or "")
            if cover_url:
                cover_url = self._abs_url(cover_url)
            word_count = str(row.get("wordCount") or "")
            update_date = str(row.get("latestUpdateTime") or "")
            latest_chapter = str(row.get("latestChapterName") or "")
            book_url = self.BOOK_INFO_URL.format(book_id=book_id)

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
                    word_count=word_count,
                    priority=self.priority + len(results),
                )
            )
        return results

    @staticmethod
    def _find_next_data(html_str: str) -> dict[str, Any]:
        """
        Extract SSR JSON from <script id="__NEXT_DATA__">.
        """
        tree = parse_html_tree(html_str)
        if tree is None:
            return {}
        script = tree.xpath('//script[@id="__NEXT_DATA__"]/text()')
        if not script:
            return {}
        try:
            data = json.loads(script[0].strip())
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_search_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        props = data.get("props", {})
        page_props = props.get("pageProps", {})
        search_list = page_props.get("list", {})
        return search_list if isinstance(search_list, list) else []
