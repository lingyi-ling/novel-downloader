#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.kadokado.searcher
------------------------------------------------
"""

import json
import logging

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class KadokadoSearcher(BaseSearcher):
    site_name = "kadokado"
    priority = 30
    BASE_URL = "https://www.kadokado.com.tw"
    SEARCH_URL = "https://api.kadokado.com.tw/v3/search"

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "order": "Relevance",
            "typeFilter": "All",
            "statusFilter": "All",
            "rRatedFilter": "All",
            "paidContentFilter": "All",
            "wordCountFilter": "All",
            "keyword": keyword,
            "current": "1",
            "limit": "96",
        }
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
        try:
            data = json.loads(html_str)
        except (TypeError, json.JSONDecodeError):
            logger.warning(f"{self.site_name}: invalid JSON in search response")
            return []

        if not isinstance(data, dict):
            return []
        rows = data.get("data")
        if not isinstance(rows, list) or not rows:
            return []

        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break
            if not isinstance(row, dict):
                continue

            book_id = str(row.get("id") or "")
            if not book_id:
                continue

            title = (row.get("displayName") or "").strip()
            author = (row.get("ownerDisplayName") or "").strip()
            cover_urls = row.get("coverUrls") or []
            cover_rel = (
                cover_urls[0] if isinstance(cover_urls, list) and cover_urls else ""
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""
            word_count = str(row.get("wordCount") or "")
            book_url = f"{self.BASE_URL}/book/{book_id}"

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date="-",
                    word_count=word_count,
                    priority=self.priority + idx,
                )
            )
        return results
