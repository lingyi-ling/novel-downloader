#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.qqbook.searcher
----------------------------------------------
"""

import json
import logging
from typing import Any

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class QqbookSearcher(BaseSearcher):
    """
    Searcher for QQ 阅读 (book.qq.com).

    Uses the same JSON API as the official web SPA (``/so/{keyword}`` page):
    ``https://book.qq.com/api/booksearch/query``. No login or signature is
    required for keyword queries.
    """

    site_name = "qqbook"
    priority = 100
    BASE_URL = "https://book.qq.com"
    SEARCH_URL = "https://book.qq.com/api/booksearch/query"

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "keyWord": keyword,
            "pageNo": "1",
            "pageSize": "20",
        }
        headers = {
            **self.session.headers,
            "Referer": "https://book.qq.com/book-search",
        }
        try:
            async with self.session.get(
                self.SEARCH_URL, params=params, headers=headers
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
        try:
            data: Any = json.loads(html_str)
        except json.JSONDecodeError:
            return []

        if not isinstance(data, dict) or data.get("code") != 0:
            return []

        payload = data.get("data") or {}
        book_list = payload.get("bookList") or []
        if not isinstance(book_list, list):
            return []

        results: list[SearchResult] = []
        for idx, item in enumerate(book_list):
            if limit is not None and idx >= limit:
                break
            if not isinstance(item, dict):
                continue

            bid = str(item.get("bid") or "").strip()
            if not bid:
                continue

            total_words = item.get("totalWords")
            word_count = (
                f"{int(total_words) / 10000:.1f}万字"
                if isinstance(total_words, (int, float)) and total_words > 0
                else "-"
            )

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=bid,
                    book_url=f"https://book.qq.com/book-detail/{bid}",
                    cover_url="",
                    title=str(item.get("title") or "").strip(),
                    author=str(item.get("author") or "").strip(),
                    latest_chapter="-",
                    update_date="-",
                    word_count=word_count,
                    priority=self.priority + idx,
                )
            )
        return results
