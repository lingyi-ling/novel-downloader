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
from typing import Any, ClassVar

from novel_downloader.plugins.base.fetcher import BaseFetcher

from .engine import render
from .model import PagesRule, Rule

logger = logging.getLogger(__name__)

__all__ = ["RuleFetcher"]

_MAX_EXTRA_PAGES = 50


class RuleFetcher(BaseFetcher):
    """Rule-driven fetcher. Concrete per-site subclasses set ``_rule``."""

    _rule: ClassVar[Rule]
    site_name: ClassVar[str] = "rule"
    DEFAULT_BASE_URL: ClassVar[str] = ""

    def _base(self) -> str:
        return (self._rule.base_url or self._base_url).rstrip("/")

    async def _get(self, url: str, **kwargs: Any) -> str:
        headers = dict(self._rule.headers)
        if headers:
            kwargs.setdefault("headers", headers)
        return await self.fetch(url, encoding=self._rule.encoding, **kwargs)

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        rule = self._rule
        if rule.book is None:
            raise NotImplementedError(f"{rule.site_key}: no 'book' section in rule")

        base = self._base()
        url = render(rule.book.url, base_url=base, book_id=book_id)
        pages = [await self._get(url, **kwargs)]

        if rule.book.pages is not None:
            pages.extend(
                await self._collect_pages(
                    rule.book.pages, pages, base, book_id=book_id, **kwargs
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
        pages = [await self._get(url, **kwargs)]

        if rule.chapter.pages is not None:
            pages.extend(
                await self._collect_pages(
                    rule.chapter.pages,
                    pages,
                    base,
                    book_id=book_id,
                    chapter_id=chapter_id,
                    **kwargs,
                )
            )
        return pages

    async def _collect_pages(
        self,
        pages_rule: PagesRule,
        pages: list[str],
        base: str,
        **fmt: Any,
    ) -> list[str]:
        """
        Walk the extra pages.

        Pagination continues while the URL of the *next* page is still
        referenced from the current page's HTML (mirrors the base
        ``should_continue_pagination`` heuristic).  This stops naturally on the
        last page, where the link is absent.
        """
        extra: list[str] = []
        origin = base
        page = pages_rule.start
        while len(extra) < _MAX_EXTRA_PAGES:
            next_url = render(pages_rule.url, base_url=base, page=page, **fmt)
            suffix = next_url[len(origin) :] if next_url.startswith(origin) else next_url
            basename = suffix.rsplit("/", 1)[-1]
            if suffix not in pages[-1] and basename not in pages[-1]:
                break
            try:
                html = await self._get(next_url)
            except Exception as e:
                logger.warning(
                    "Failed extra page (site=%s, url=%s): %s",
                    self.site_name,
                    next_url,
                    e,
                )
                break
            pages.append(html)
            extra.append(html)
            page += 1
        return extra
