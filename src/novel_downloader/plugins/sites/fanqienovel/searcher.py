#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.searcher
--------------------------------------------------
"""

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from novel_downloader.infra.config import get_config_value
from novel_downloader.infra.cookies import CookieStore, parse_cookies
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult
from novel_downloader.schemas.config import default_cache_dir

logger = logging.getLogger(__name__)

COOKIE_ENV_VAR = "FANQIENOVEL_COOKIE"


@registrar.register_searcher()
class FanqienovelSearcher(BaseSearcher):
    """
    Searcher for 番茄小说网 (fanqienovel.com).

    The site's search endpoint signs every request inside its own JavaScript
    (``msToken`` + ``a_bogus``); plain HTTP requests are answered with an empty
    body, with or without a login cookie. Searching therefore works in two
    steps, and only when the caller explicitly selected this site (an automatic
    "all sites" search skips it, so it stays fast and window-free):

      1. the site itself performs the search in a real browser window, and the
         response JSON is captured through the DevTools Protocol
      2. the captured entries are completed with their book detail page, which
         exposes the title and author in plain text (the search response writes
         them with a private-use font)
    """

    site_name = "fanqienovel"
    priority = 5
    BASE_URL = "https://fanqienovel.com"
    SEARCH_URL = "https://fanqienovel.com/api/author/search/search_book/v1"
    PAGE_SIZE = 10

    #: Seconds the browser window may take to answer.
    BROWSER_SEARCH_TIMEOUT = 60.0

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
                configured = get_config_value(
                    ["general", "cache_dir"], str(default_cache_dir())
                )
            except Exception:  # no config file on disk yet
                configured = str(default_cache_dir())
            cache_dir = Path(str(configured))
        store = CookieStore(cache_dir / "fanqienovel")
        store.load_all()
        return store.cache

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        """
        Search fanqienovel, driving a browser window when asked to explicitly.

        :param keyword: Search keyword.
        :param limit: Maximum number of results to return.
        :return: List of search results.
        """
        if not self.explicitly_selected:
            return await super().search(keyword, limit)

        results = await self._search_with_browser(keyword, limit)
        if results:
            return results

        logger.warning(
            "fanqienovel: browser-assisted search returned nothing "
            "(run 'novel-cli login fanqienovel' if no session is available)"
        )
        return await super().search(keyword, limit)

    async def _search_with_browser(
        self, keyword: str, limit: int | None
    ) -> list[SearchResult]:
        """Capture the search response from a browser window and enrich it."""
        from novel_downloader.apps.browser import search_via_browser

        try:
            body = await asyncio.to_thread(
                search_via_browser,
                self.site_name,
                keyword,
                timeout=self.BROWSER_SEARCH_TIMEOUT,
            )
        except Exception:
            logger.exception("fanqienovel: browser-assisted search failed")
            return []
        if not body:
            return []

        results = self._parse_html(body, limit)
        await self._fill_book_metadata(results)
        return results

    async def _fill_book_metadata(self, results: list[SearchResult]) -> None:
        """
        Replace the obfuscated titles and authors with the real ones.

        ``book_name``/``author`` are written with a private-use font in the
        search response, so they decode to junk. The book detail pages expose
        the same fields in plain text, and are publicly reachable, so they are
        fetched to fix the entries up.
        """
        if not results:
            return

        headers = dict(self.session.headers)
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        await asyncio.gather(
            *(self._fill_one(item, headers) for item in results),
            return_exceptions=True,
        )

    async def _fill_one(self, item: SearchResult, headers: dict[str, str]) -> None:
        """Complete a single search result with its book detail page."""
        page = await self._fetch_book_page(item["book_id"], headers)
        if not page:
            return

        title = page.get("bookName") or page.get("book_name")
        author = page.get("authorName") or page.get("author") or page.get("author_name")
        if isinstance(title, str) and title:
            item["title"] = title
        if isinstance(author, str) and author:
            item["author"] = author

    async def _fetch_book_page(
        self, book_id: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        """Fetch a book detail page and return its ``page`` state."""
        from novel_downloader.plugins.sites.fanqienovel.parser import (
            FanqienovelParser,
        )

        url = f"{self.BASE_URL}/page/{book_id}"
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                html_str = await self._response_to_str(resp)
        except Exception:
            logger.debug("fanqienovel: could not read book page %s", url, exc_info=True)
            return {}

        try:
            state = FanqienovelParser._extract_initial_state(html_str)
        except ValueError:
            logger.debug("fanqienovel: no book state found on %s", url, exc_info=True)
            return {}

        page = state.get("page")
        return page if isinstance(page, dict) else {}

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
