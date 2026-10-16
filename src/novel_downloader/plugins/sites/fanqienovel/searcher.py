#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.searcher
--------------------------------------------------
"""

import datetime
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import aiohttp

from novel_downloader.infra.config import get_config_value
from novel_downloader.infra.cookies import CookieStore, parse_cookies
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

COOKIE_ENV_VAR = "FANQIENOVEL_COOKIE"


@registrar.register_searcher()
class FanqienovelSearcher(BaseSearcher):
    """
    Searcher for 番茄小说网 (fanqienovel.com).

    The site's search endpoint only answers requests carrying a logged-in
    session, so a login cookie is required. It is resolved, in order, from:

      1. the ``FANQIENOVEL_COOKIE`` environment variable
      2. the persisted cookie store under ``<cache_dir>/fanqienovel``

    Without a cookie the searcher logs a warning and returns no results, so
    aggregate searches keep working.
    """

    site_name = "fanqienovel"
    priority = 5
    BASE_URL = "https://fanqienovel.com"
    SEARCH_URL = "https://fanqienovel.com/api/author/search/search_book/v1"
    PAGE_SIZE = 10

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._cookies = self._load_cookies()

    @staticmethod
    def _load_cookies() -> dict[str, str]:
        raw = os.environ.get(COOKIE_ENV_VAR, "").strip()
        if raw:
            return parse_cookies(raw)
        override = os.environ.get("NOVEL_CACHE_DIR", "").strip()
        if override:
            cache_dir = Path(override)
        else:
            try:
                configured = get_config_value(["general", "cache_dir"], "./novel_cache")
            except Exception:  # no config file on disk yet
                configured = "./novel_cache"
            cache_dir = Path(str(configured))
        store = CookieStore(cache_dir / "fanqienovel")
        store.load_all()
        return store.cache

    async def _fetch_html(self, keyword: str) -> str:
        if not self._cookies:
            logger.warning(
                "fanqienovel search skipped: no login cookie found (set %s to enable)",
                COOKIE_ENV_VAR,
            )
            return ""

        params = {
            "filter": "127,127,127,127",
            "page_count": str(self.PAGE_SIZE),
            "page_index": "0",
            "query_type": "0",
            "query_word": keyword,
        }
        headers = {
            **self.session.headers,
            "Referer": f"{self.BASE_URL}/search/{quote(keyword)}",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
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
            payload = json.loads(html_str)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []

        data = payload.get("data")
        items = data.get("search_book_data_list") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        results: list[SearchResult] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if limit is not None and idx >= limit:
                break

            book_id = str(item.get("book_id") or "")
            if not book_id:
                continue

            cover = str(item.get("thumb_url") or "").replace("http://", "https://")
            if cover.startswith("//"):
                cover = "https:" + cover

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=f"{self.BASE_URL}/page/{book_id}",
                    cover_url=cover,
                    title=str(item.get("book_name") or ""),
                    author=str(item.get("author") or ""),
                    latest_chapter=str(item.get("last_chapter_title") or "-"),
                    update_date=self._format_time(item.get("last_chapter_time")),
                    word_count=str(item.get("word_count") or "-"),
                    priority=self.priority + idx,
                )
            )

        return results

    @staticmethod
    def _format_time(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        if text.isdigit():
            ts = int(text)
            if ts > 10**12:  # milliseconds
                ts //= 1000
            try:
                dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
            except (OverflowError, OSError, ValueError):
                return text
            return dt.strftime("%Y-%m-%d")
        return text
