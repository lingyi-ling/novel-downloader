#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.syosetu18.searcher
-------------------------------------------------

Search R18 (Nocturne/Moonlight/Midnight) 小説家になろう novels through
the official なろうR18小説API (https://api.syosetu.com/novel18api/api/).
The HTML search on novel18.syosetu.com rejects non-browser clients, but
the JSON API exposes the same ``word`` search. Results are ordered by
overall rating.
"""

import json
import logging

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class Syosetu18Searcher(BaseSearcher):
    site_name = "syosetu18"
    priority = 100
    BASE_URL = "https://novel18.syosetu.com"
    SEARCH_URL = "https://api.syosetu.com/novel18api/api/"

    #: output fields: title / ncode / writer / general_lastup /
    #: general_all_no / end / length
    OF_FIELDS = "t-n-w-gl-ga-e-l"
    LIMIT = 20

    @property
    def nsfw(self) -> bool:
        return True

    async def _fetch_html(self, keyword: str) -> str:
        params = {
            "out": "json",
            "lim": str(self.LIMIT),
            "order": "hyoka",
            "of": self.OF_FIELDS,
            "word": self._quote(keyword),
        }
        url = self._build_url(self.SEARCH_URL, params)
        try:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error(
                "Failed to fetch results for keyword '%s' from '%s'",
                keyword,
                self.SEARCH_URL,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        try:
            data = json.loads(html_str)
        except json.JSONDecodeError:
            return []

        # payload layout: [{"allcount": N}, {work}, {work}, ...]
        if not isinstance(data, list) or len(data) < 2:
            return []

        results: list[SearchResult] = []
        for idx, raw in enumerate(data[1:]):
            if not isinstance(raw, dict):
                continue
            if limit is not None and idx >= limit:
                break

            ncode = str(raw.get("ncode") or "").strip()
            if not ncode:
                continue

            book_id = ncode.lower()
            status = "完結済" if raw.get("end") == 1 else "連載中"
            episodes = str(raw.get("general_all_no") or "").strip()
            latest_chapter = f"{status}（全{episodes}話）" if episodes else status

            update_date = str(raw.get("general_lastup") or "").strip()
            word_count = str(raw.get("length") or "").strip()

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=f"{self.BASE_URL}/{book_id}/",
                    cover_url="",
                    title=str(raw.get("title") or "").strip(),
                    author=str(raw.get("writer") or "").strip() or "-",
                    latest_chapter=latest_chapter,
                    update_date=update_date or "-",
                    word_count=word_count or "-",
                    priority=self.priority + idx,
                )
            )
        return results
