#!/usr/bin/env python3
"""
novel_downloader.plugins.base.searcher
--------------------------------------

Abstract base class providing common utilities for site-specific searchers.
"""

import abc
import asyncio
import logging
from typing import ClassVar
from urllib.parse import quote_plus, urljoin

import aiohttp

from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


class BaseSearcher(abc.ABC):
    site_name: ClassVar[str]
    priority: ClassVar[int] = 1000
    BASE_URL: ClassVar[str] = ""

    #: Fetch failures that mean "this site is unavailable right now" rather
    #: than "our code is broken": blocked/poisoned DNS, connection refused or
    #: reset, TLS/HTTP errors (403/404/5xx) and timeouts.  An aggregate search
    #: fans out over every site, so these happen on every run and must not
    #: flood the log with ERROR records; they are logged as WARNING instead.
    #: Anything else is an unexpected defect and keeps ERROR level.
    EXPECTED_FETCH_ERRORS: ClassVar[tuple[type[BaseException], ...]] = (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        TimeoutError,
        OSError,
    )

    #: Set by the search orchestrator when the caller asked for this site
    #: explicitly (e.g. ``--site``). Searchers whose site is slow or intrusive
    #: can use it to only run expensive fallbacks on an explicit request.
    explicitly_selected: bool = False

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        html = await self._fetch_html(keyword)
        if not html:
            return []
        return self._parse_html(html, limit)

    @property
    def nsfw(self) -> bool:
        return False

    @abc.abstractmethod
    async def _fetch_html(self, keyword: str) -> str:
        """
        Fetch raw HTML from search API or page

        :param keyword: The search term to query.
        :return: HTML text of the search results page, or an empty string on fail.
        """
        pass

    @abc.abstractmethod
    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        """
        Parse raw HTML from search API or page into list of SearchResult.

        :param html_str: Raw HTML string from search results page.
        :param limit: Maximum number of results to return, or None for all.
        :return: List of SearchResult dicts.
        """
        pass

    @staticmethod
    def _quote(q: str, encoding: str | None = None, errors: str | None = None) -> str:
        """URL-encode a query string safely."""
        return quote_plus(q, encoding=encoding, errors=errors)

    @staticmethod
    async def _response_to_str(
        resp: aiohttp.ClientResponse,
        encoding: str | None = None,
    ) -> str:
        """
        Read the full body of resp as text. First try the declared charset,
        then on UnicodeDecodeError fall back to a lenient utf-8 decode.
        """
        data: bytes = await resp.read()
        encodings = [
            encoding,
            resp.charset,
            "gb2312",
            "gb18030",
            "gbk",
            "utf-8",
        ]
        encodings_list: list[str] = [e for e in encodings if e]
        for enc in encodings_list:
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                # ``LookupError`` covers unknown/unsupported codec names; fall
                # through to the next candidate instead of failing the search.
                continue
        for enc in (encoding or "utf-8", "utf-8"):
            try:
                return data.decode(enc, errors="ignore")
            except LookupError:
                continue
        return data.decode("utf-8", errors="ignore")

    def _log_fetch_failure(self, keyword: str, url: str, exc: BaseException) -> None:
        """
        Log a failed site fetch at a level that matches its cause.

        Aggregate searches intentionally tolerate unreachable sites, so an
        expected transport failure (timeout, connection/DNS error, TLS or an
        HTTP error status raised by ``raise_for_status``) is logged as a
        warning: one search must not produce dozens of ERROR lines for sites
        that are merely blocked or down.  Any other exception is a real defect
        and keeps the historical ERROR level, now with a traceback.

        :param keyword: Search term being fetched.
        :param url: Endpoint that failed.
        :param exc: The exception raised by the request.
        """
        if isinstance(exc, self.EXPECTED_FETCH_ERRORS):
            logger.warning(
                "Failed to fetch HTML for keyword '%s' from '%s': %s",
                keyword,
                url,
                exc,
            )
            return
        logger.error(
            "Failed to fetch HTML for keyword '%s' from '%s'",
            keyword,
            url,
            exc_info=True,
        )

    @staticmethod
    def _first_str(xs: list[str], replaces: list[tuple[str, str]] | None = None) -> str:
        replaces = replaces or []
        value: str = xs[0].strip() if xs else ""
        for replace in replaces:
            old, new = replace
            value = value.replace(old, new)
        return value.strip()

    @staticmethod
    def _join_strs(xs: list[str], replaces: list[tuple[str, str]] | None = None) -> str:
        replaces = replaces or []
        value = "".join(s.strip() for s in xs if s and s.strip())
        for old, new in replaces:
            value = value.replace(old, new)
        return value.strip()

    @staticmethod
    def _build_url(base: str, params: dict[str, str]) -> str:
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}?{query_string}"

    @classmethod
    def _abs_url(cls, url: str) -> str:
        if url.startswith("//"):
            return "https:" + url
        return (
            url
            if url.startswith(("http://", "https://"))
            else urljoin(cls.BASE_URL, url)
        )
