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

        try:
            if search.method == "POST":
                data = {
                    k: render(
                        v, keyword=quote_plus(keyword), raw_keyword=keyword, page=1
                    )
                    for k, v in search.data.items()
                }
                async with self.session.post(url, data=data, headers=headers) as resp:
                    resp.raise_for_status()
                    return await self._response_to_str(resp, rule.encoding)

            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp, rule.encoding)
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'", keyword, url
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        rule = self._rule
        search = rule.search
        if search is None or not html_str:
            return []

        base = (rule.base_url or self.BASE_URL).rstrip("/")
        tree = parse_page(html_str)
        items = select(tree, search.item) if search.item else [tree]

        id_rx = re.compile(search.book_id_regex) if search.book_id_regex else None
        results: list[SearchResult] = []

        def field(name: str, node: Any) -> str:
            fld = search.fields.get(name)
            return extract(node, fld) if fld else ""

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
        return results

    @staticmethod
    def _book_id(book_url: str, id_rx: re.Pattern[str] | None, group: int) -> str:
        if id_rx is not None:
            m = id_rx.search(book_url)
            if m:
                return m.group(group) if group <= len(m.groups()) else m.group(0)
        path = urlsplit(book_url).path.rstrip("/")
        return path.split("/")[-1] if path else book_url
