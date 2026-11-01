#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.fetcher
-------------------------------------

A single HTTP fetcher that is fully driven by a :class:`~.model.Rule`.

It implements the :class:`~novel_downloader.plugins.protocols.FetcherProtocol`
surface (``fetch_book_info`` / ``fetch_chapter_content``) by expanding the
URL templates declared in the rule and, when configured, walking the extra
pagination pages (``book.pages`` / ``chapter.pages``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from novel_downloader.plugins.base.fetcher import BaseFetcher

from .engine import (
    body_fingerprint,
    matches_stop_when,
    render,
    should_stop_pagination,
)
from .model import PagesRule, Rule

logger = logging.getLogger(__name__)

__all__ = ["RuleFetcher"]

_MAX_EXTRA_PAGES = 50

#: Control characters never valid in a rendered header value (CRLF injection).
_HEADER_CTRL_RE = re.compile(r"[\r\n\x00]")


class RuleFetcher(BaseFetcher):
    """Rule-driven fetcher. Concrete per-site subclasses set ``_rule``."""

    _rule: ClassVar[Rule]
    site_name: str = "rule"
    DEFAULT_BASE_URL: str = ""

    def _base(self) -> str:
        return (self._rule.base_url or self._base_url).rstrip("/")

    async def _get(
        self, url: str, extra_headers: dict[str, str] | None = None, **kwargs: Any
    ) -> str:
        headers = {**self._rule.headers, **(extra_headers or {})}
        if headers:
            kwargs.setdefault("headers", headers)
        return await self.fetch(url, encoding=self._rule.encoding, **kwargs)

    def _render_headers(
        self, headers: dict[str, str], base: str, **fmt: Any
    ) -> dict[str, str]:
        """
        Render ``{base_url}`` / ``{book_id}`` ... placeholders inside headers.

        Rendered values come partly from page data (``book_id``), so any CR/LF
        or NUL they carry is stripped: passing them through would make the HTTP
        client raise a raw ``ValueError`` on header injection.
        """
        out: dict[str, str] = {}
        for key, value in headers.items():
            rendered = render(value, base_url=base, **fmt)
            clean = _HEADER_CTRL_RE.sub("", rendered)
            if clean != rendered:
                logger.warning(
                    "Stripped control characters from rendered header %r", key
                )
            out[key] = clean
        return out

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        rule = self._rule
        if rule.book is None:
            raise NotImplementedError(f"{rule.site_key}: no 'book' section in rule")

        base = self._base()
        url = render(rule.book.url, base_url=base, book_id=book_id)
        headers = self._render_headers(rule.book.headers, base, book_id=book_id)
        pages = [await self._get(url, headers, **kwargs)]

        if rule.book.pages is not None:
            pages.extend(
                await self._collect_pages(
                    rule.book.pages,
                    pages[-1],
                    base,
                    headers,
                    first_url=url,
                    book_id=book_id,
                    **kwargs,
                )
            )
        return pages

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        rule = self._rule
        if rule.chapter is None:
            raise NotImplementedError(f"{rule.site_key}: no 'chapter' section in rule")

        base = self._base()
        url = render(
            rule.chapter.url, base_url=base, book_id=book_id, chapter_id=chapter_id
        )
        headers = self._render_headers(
            rule.chapter.headers, base, book_id=book_id, chapter_id=chapter_id
        )
        pages = [await self._get(url, headers, **kwargs)]

        if rule.chapter.pages is not None:
            pages.extend(
                await self._collect_pages(
                    rule.chapter.pages,
                    pages[-1],
                    base,
                    headers,
                    first_url=url,
                    book_id=book_id,
                    chapter_id=chapter_id,
                    **kwargs,
                )
            )
        return pages

    async def _collect_pages(
        self,
        pages_rule: PagesRule,
        current: str,
        base: str,
        extra_headers: dict[str, str] | None,
        first_url: str = "",
        **fmt: Any,
    ) -> list[str]:
        """
        Walk the extra pages and return only the *newly* fetched ones.

        When ``max_pages`` is set the pages ``start .. max_pages`` are fetched
        unconditionally, which is what JSON endpoints (that never reference the
        next page in their payload) need.  Otherwise pagination continues while
        the URL of the *next* page is still referenced from the current page's
        HTML (mirrors the base ``should_continue_pagination`` heuristic) and
        stops naturally on the last page, where the link is absent.

        A rendered URL that was already collected (a pages template without
        ``{page}``, or one that points back at the entry URL) is never fetched
        again -- re-collecting it would duplicate the parser's output.

        :param current: payload of the page fetched just before this call.
        :param first_url: URL of that first page, used to seed the seen-set.
        :return: the additional pages, in fetch order (never includes ``current``).
        """
        extra: list[str] = []
        origin = base
        page = pages_rule.start
        last = pages_rule.max_pages
        seen: set[str] = {first_url} if first_url else set()
        seen_bodies: set[str] = {body_fingerprint(current)}

        # ``stop_when`` on the entry page means "this is the last one": do not
        # even ask for a follow-up.
        if matches_stop_when(pages_rule, current):
            return []

        while len(extra) < _MAX_EXTRA_PAGES and (last is None or page <= last):
            next_url = render(pages_rule.url, base_url=base, page=page, **fmt)
            if next_url in seen:
                break  # the template rendered an already-collected page
            if last is None:
                suffix = (
                    next_url[len(origin) :] if next_url.startswith(origin) else next_url
                )
                basename = suffix.rsplit("/", 1)[-1]
                if suffix not in current and basename not in current:
                    break
            try:
                current = await self._get(next_url, extra_headers)
            except Exception as e:
                logger.warning(
                    "Failed extra page (site=%s, url=%s): %s",
                    self.site_name,
                    next_url,
                    e,
                )
                break
            extra.append(current)
            if should_stop_pagination(pages_rule, current, next_url, seen, seen_bodies):
                break
            seen.add(next_url)
            page += 1
        return extra
