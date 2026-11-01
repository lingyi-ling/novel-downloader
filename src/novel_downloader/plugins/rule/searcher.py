#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.searcher
--------------------------------------

Rule-driven searcher. Given a ``search`` section in the rule it performs the
request (GET or POST) and maps each result node to a
:class:`~novel_downloader.schemas.SearchResult`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar
from urllib.parse import quote_plus, urlsplit

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.schemas import SearchResult

from .engine import abs_url, extract, parse_page, render, select
from .model import Rule

logger = logging.getLogger(__name__)

__all__ = ["RuleSearcher"]


class RuleSearcher(BaseSearcher):
    """Rule-driven searcher. Concrete per-site subclasses set ``_rule``."""

    _rule: ClassVar[Rule]
    site_name: ClassVar[str] = "rule"
    priority: ClassVar[int] = 200
    BASE_URL: ClassVar[str] = ""

    #: URLs of the most recent ``_fetch_html`` call.  They let
    #: ``single_result_fallback`` tell "the site redirected the search straight
    #: to a book page" apart from "the selector matched nothing".
    _request_url: str = ""
    _final_url: str = ""

    async def _text_of(self, resp: Any) -> str:
        """Decode ``resp`` and remember the URL the request *ended* at."""
        self._final_url = str(resp.url)
        return await self._response_to_str(resp, self._rule.encoding)

    async def _fetch_html(self, keyword: str) -> str:
        rule = self._rule
        search = rule.search
        if search is None:
            return ""

        base = (rule.base_url or self.BASE_URL).rstrip("/")
        url = render(
            search.url,
            base_url=base,
            keyword=quote_plus(keyword),
            raw_keyword=keyword,
            page=1,
        )
        headers = dict(search.headers) if search.headers else None
        self._request_url = url
        self._final_url = ""

        try:
            if search.json_body is not None:
                payload = self._render_body(search.json_body, keyword)
                async with self.session.post(
                    url, json=payload, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    return await self._text_of(resp)

            if search.method == "POST":
                data = {
                    k: render(
                        v, keyword=quote_plus(keyword), raw_keyword=keyword, page=1
                    )
                    for k, v in search.data.items()
                }
                async with self.session.post(url, data=data, headers=headers) as resp:
                    resp.raise_for_status()
                    return await self._text_of(resp)

            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._text_of(resp)
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'", keyword, url
            )
            return ""

    @staticmethod
    def _render_body(payload: dict[str, Any], keyword: str) -> dict[str, Any]:
        """Expand ``{keyword}`` / ``{page}`` templates inside a JSON body."""

        def walk(value: Any) -> Any:
            if isinstance(value, str):
                return render(
                    value, keyword=quote_plus(keyword), raw_keyword=keyword, page=1
                )
            if isinstance(value, dict):
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            return value

        return {k: walk(v) for k, v in payload.items()}

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        rule = self._rule
        search = rule.search
        if search is None or not html_str:
            return []

        base = (rule.base_url or self.BASE_URL).rstrip("/")
        fmt = rule.search_format
        tree = parse_page(html_str, fmt)
        items = select(tree, search.item, fmt) if search.item else [tree]

        id_rx = re.compile(search.book_id_regex) if search.book_id_regex else None
        results: list[SearchResult] = []

        def field(name: str, node: Any) -> str:
            fld = search.fields.get(name)
            return extract(node, fld, fmt) if fld else ""

        for idx, item in enumerate(items):
            if limit is not None and idx >= limit:
                break

            book_url = abs_url(base, field("url", item))
            if not book_url:
                continue

            results.append(
                SearchResult(
                    site=rule.site_key,
                    book_id=self._book_id(book_url, id_rx, search.book_id_group),
                    book_url=book_url,
                    cover_url=abs_url(base, field("cover", item)),
                    title=field("title", item),
                    author=field("author", item),
                    latest_chapter=field("latest", item),
                    update_date=field("update", item),
                    word_count=field("word", item) or "-",
                    priority=search.priority + idx,
                )
            )
        if not results and (limit is None or limit > 0):
            results = self._single_result_fallback(tree, fmt, base)
        return results

    def _single_result_fallback(
        self, tree: Any, fmt: str, base: str
    ) -> list[SearchResult]:
        """
        Synthesize one result when a search straight-redirects to a book page.

        Some sites ignore the query and 302 to a detail page when the title is
        an exact match, so the item selector matches nothing.  With
        ``single_result_fallback`` enabled we accept the response as a single
        hit when there is evidence it *is* a detail page:

        * the final response URL differs from the requested URL, or
        * the ``book`` section's ``book_name`` field (or the search ``title``
          field) yields a non-empty value.

        Book fields are preferred for metadata, falling back to the search
        fields of the same meaning.  The URL is the final response URL.
        """
        rule = self._rule
        search = rule.search
        if search is None or not search.single_result_fallback:
            return []

        final_url = self._final_url
        request_url = self._request_url
        book_url = final_url or request_url
        if not book_url:
            return []

        book_fields = rule.book.fields if rule.book is not None else {}

        def value(book_name: str, search_name: str) -> str:
            fld = book_fields.get(book_name)
            if fld is not None:
                found = extract(tree, fld, fmt)
                if found:
                    return found
            alt = search.fields.get(search_name)
            return extract(tree, alt, fmt) if alt is not None else ""

        title = value("book_name", "title")
        redirected = bool(final_url and request_url and final_url != request_url)
        if not redirected and not title:
            return []

        id_rx = re.compile(search.book_id_regex) if search.book_id_regex else None
        return [
            SearchResult(
                site=rule.site_key,
                book_id=self._book_id(book_url, id_rx, search.book_id_group),
                book_url=book_url,
                cover_url=abs_url(base, value("cover_url", "cover")),
                title=title,
                author=value("author", "author"),
                latest_chapter="",
                update_date=value("update_time", "update"),
                word_count=value("word_count", "word") or "-",
                priority=search.priority,
            )
        ]

    @staticmethod
    def _book_id(book_url: str, id_rx: re.Pattern[str] | None, group: int) -> str:
        if id_rx is not None:
            m = id_rx.search(book_url)
            if m:
                # ``group`` is validated >= 0 at parse time; a negative index
                # would raise a raw ``IndexError`` here, so stay defensive.
                return m.group(group) if 0 < group <= len(m.groups()) else m.group(0)
        path = urlsplit(book_url).path.rstrip("/")
        return path.split("/")[-1] if path else book_url
