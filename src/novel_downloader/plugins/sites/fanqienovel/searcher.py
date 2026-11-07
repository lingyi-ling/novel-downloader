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


class _AiohttpResponse:
    """Minimal adapter so ``register.mint_device`` can drive aiohttp."""

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text


class _AiohttpTransport:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def post(
        self, url: str, *, headers: dict[str, str], data: bytes
    ) -> _AiohttpResponse:
        async with self._session.post(url, headers=headers, data=data) as resp:
            return _AiohttpResponse(resp.status, await resp.text())


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
    RANK_URL = "https://fanqienovel.com/rank"
    PAGE_SIZE = 10

    #: Seconds the browser window may take to answer.
    BROWSER_SEARCH_TIMEOUT = 60.0

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._cookies = self._load_cookies()
        self._device: Any = None
        self._device_checked = False

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

        results = await self._search_via_app(keyword, limit)
        if results:
            return results

        results = await self._search_with_browser(keyword, limit)
        if results:
            return results

        logger.warning(
            "fanqienovel: browser-assisted search returned nothing "
            "(run 'novel-cli login fanqienovel' if no session is available)"
        )
        return await super().search(keyword, limit)

    def _app_device(self) -> Any:
        """Device identity for the app API, looked up once."""
        if self._device is None and not self._device_checked:
            from novel_downloader.plugins.sites.fanqienovel.appapi import (
                discover_device,
            )

            self._device = discover_device(self._cache_dir())
            self._device_checked = True
        return self._device

    def _cache_dir(self) -> Path:
        override = os.environ.get("NOVEL_CACHE_DIR", "").strip()
        if override:
            return Path(override) / self.site_name
        try:
            configured = get_config_value(
                ["general", "cache_dir"], str(default_cache_dir())
            )
        except Exception:  # no config file on disk yet
            configured = str(default_cache_dir())
        return Path(str(configured)) / self.site_name

    async def _ensure_device(self) -> Any:
        """Return a device identity, registering one if needed."""
        device = self._app_device()
        if device is not None:
            return device
        if os.environ.get("NOVELDL_FANQIE_AUTO_REGISTER", "1") == "0":
            return None
        from novel_downloader.plugins.sites.fanqienovel.register import mint_device

        try:
            device = await mint_device(
                _AiohttpTransport(self.session), self._cache_dir()
            )
        except Exception:
            logger.debug("fanqienovel: search-time registration failed", exc_info=True)
            return None
        if device is not None:
            self._device = device
        return device

    async def rank(self, limit: int = 10) -> list[SearchResult]:
        """Return the site's ranking page as search results.

        The page is server-rendered, so it is a plain HTML fetch; it carries the
        book ids and metadata, while the titles/authors are written with the
        usual private-use font and are repaired from the book pages.
        """
        from novel_downloader.plugins.sites.fanqienovel.parser import (
            FanqienovelParser,
        )

        headers = dict(self.session.headers)
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        try:
            async with self.session.get(self.RANK_URL, headers=headers) as resp:
                resp.raise_for_status()
                html_str = await self._response_to_str(resp)
            state = FanqienovelParser._extract_initial_state(html_str)
        except Exception:
            logger.debug("fanqienovel: rank page unavailable", exc_info=True)
            return []

        rank = state.get("rank")
        books = rank.get("book_list") if isinstance(rank, dict) else None
        if not isinstance(books, list):
            return []

        results: list[SearchResult] = []
        for entry in books:
            if len(results) >= max(1, limit):
                break
            item = self._rank_result(entry)
            if item is not None:
                results.append(item)
        await self._fill_book_metadata(results)
        return results

    def _rank_result(self, entry: Any) -> SearchResult | None:
        if not isinstance(entry, dict):
            return None
        book_id = entry.get("bookId")
        if not book_id:
            return None
        cover = entry.get("thumbUri")
        if isinstance(cover, str) and cover.startswith("//"):
            cover = "https:" + cover
        words = entry.get("wordNumber")
        return SearchResult(
            site=self.site_name,
            book_id=str(book_id),
            book_url=f"{self.BASE_URL}/page/{book_id}",
            cover_url=cover if isinstance(cover, str) else "",
            title=str(entry.get("bookName") or ""),
            author=str(entry.get("author") or ""),
            latest_chapter=str(entry.get("lastChapterTitle") or ""),
            update_date="",
            word_count=str(words) if isinstance(words, int) else "",
            priority=self.priority,
        )

    async def _search_via_app(
        self, keyword: str, limit: int | None
    ) -> list[SearchResult]:
        """Search through the app API: no login, no browser window."""
        device = await self._ensure_device()
        if device is None:
            return []

        from novel_downloader.plugins.sites.fanqienovel.appapi import sign_query
        from novel_downloader.plugins.sites.fanqienovel.argus import x_argus
        from novel_downloader.plugins.sites.fanqienovel.ladon import x_ladon
        from novel_downloader.plugins.sites.fanqienovel.register import (
            CLIENT_USER_AGENT,
        )

        wanted = limit if limit is not None else self.PAGE_SIZE * 3
        results: list[SearchResult] = []
        offset = 0
        while len(results) < wanted:
            query = f"aid=1967&q={quote(keyword)}&offset={offset}"
            url = (
                "https://novel.snssdk.com/api/novel/channel/homepage/search/search/v1/"
                f"?{query}"
            )
            timestamp = int(datetime.datetime.now().timestamp())
            gorgon, khronos = sign_query(query, khronos=timestamp)
            headers = {
                "X-Gorgon": gorgon,
                "X-Khronos": str(khronos),
                "x-argus": x_argus(query, timestamp, device_id=device.device_id),
                "x-ladon": x_ladon(timestamp),
                "User-Agent": CLIENT_USER_AGENT,
                "Accept-Encoding": "identity",
            }
            payload = await self._get_json(url, headers)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                break
            items = data.get("ret_data")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if len(results) >= wanted:
                    break
                result = self._app_result(item)
                if result is not None:
                    results.append(result)
            if not data.get("has_more"):
                break
            offset = int(data.get("offset") or (offset + len(items)))
            if offset <= 0:
                break
        return results

    async def _get_json(self, url: str, headers: dict[str, str]) -> Any:
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json(content_type=None)
        except Exception:
            logger.debug("fanqienovel: app search request failed", exc_info=True)
            return {}

    def _app_result(self, item: Any) -> SearchResult | None:
        if not isinstance(item, dict):
            return None
        book_id = item.get("book_id")
        title = item.get("title")
        if not book_id or not isinstance(title, str) or not title:
            return None
        author = item.get("author")
        cover = item.get("thumb_url")
        return SearchResult(
            site=self.site_name,
            book_id=str(book_id),
            book_url=f"{self.BASE_URL}/page/{book_id}",
            cover_url=cover if isinstance(cover, str) else "",
            title=title,
            author=author if isinstance(author, str) else "",
            latest_chapter="",
            update_date="",
            word_count="",
            priority=self.priority,
        )

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
