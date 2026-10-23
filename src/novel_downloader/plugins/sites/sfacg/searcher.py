#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.sfacg.searcher
---------------------------------------------
"""

import json
import logging
from typing import Any

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class SfacgSearcher(BaseSearcher):
    """
    Searcher for SF轻小说 (m.sfacg.com).

    Uses the mobile site's JSON search API
    ``/API/HTML5.ashx?op=search&keyword=...`` and returns books whose
    ``book_url`` matches the ``https://m.sfacg.com/b/{book_id}/`` info page
    used by ``SfacgFetcher``.
    """

    site_name = "sfacg"
    priority = 100
    BASE_URL = "https://m.sfacg.com/"
    SEARCH_URL = "https://m.sfacg.com/API/HTML5.ashx"

    _BOOK_URL = "https://m.sfacg.com/b/{book_id}/"
    _COVER_BASE = "https://rs.sfacg.com/web/novel/images/NovelCover/Big/"

    async def _fetch_html(self, keyword: str) -> str:
        url = self._build_url(
            self.SEARCH_URL,
            {"op": "search", "keyword": self._quote(keyword)},
        )
        headers = {
            **self.session.headers,
            "Referer": "https://m.sfacg.com/search.html",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error(
                "Failed to fetch search results for keyword '%s' from '%s'",
                keyword,
                url,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        try:
            data: dict[str, Any] = json.loads(html_str)
        except json.JSONDecodeError:
            return []

        novels = data.get("Novels")
        if not isinstance(novels, list):
            return []

        results: list[SearchResult] = []
        for idx, item in enumerate(novels):
            if limit is not None and idx >= limit:
                break
            if not isinstance(item, dict):
                continue

            book_id = str(item.get("NovelID") or "").strip()
            title = str(item.get("NovelName") or "").strip()
            if not book_id or not title:
                continue

            author = str(item.get("AuthorName") or "").strip()
            cover_path = str(item.get("NovelCover") or "").strip()
            cover_url = self._COVER_BASE + cover_path if cover_path else ""

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=self._BOOK_URL.format(book_id=book_id),
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=self.priority + idx,
                )
            )
        return results
