#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.qimao.searcher
---------------------------------------------

"""

import json
import logging

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class QimaoSearcher(BaseSearcher):
    site_name = "qimao"
    priority = 5
    BASE_URL = "https://www.qimao.com"
    SEARCH_URL = "https://www.qimao.com/qimaoapi/api/search/result"
    PAGE_SIZE = 30

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "keyword": keyword,
            "count": "0",
            "page": "1",
            "page_size": str(self.PAGE_SIZE),
        }
        headers = {
            **self.session.headers,
            "Referer": f"{self.BASE_URL}/",
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
            data = json.loads(html_str)
        except (TypeError, ValueError):
            return []

        items = (data.get("data") or {}).get("search_list") or []
        results: list[SearchResult] = []

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            if limit is not None and idx >= limit:
                break

            book_id = str(item.get("book_id") or "")
            if not book_id:
                continue

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=str(
                        item.get("read_url") or f"{self.BASE_URL}/shuku/{book_id}/"
                    ),
                    cover_url=str(item.get("image_link") or ""),
                    title=str(item.get("title") or ""),
                    author=str(item.get("author") or ""),
                    latest_chapter=str(item.get("latest_chapter_title") or "-"),
                    update_date=str(item.get("update_time") or "-"),
                    word_count=str(item.get("words_num") or "-"),
                    priority=self.priority + idx,
                )
            )

        return results
