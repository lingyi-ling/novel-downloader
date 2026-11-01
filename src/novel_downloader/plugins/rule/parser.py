#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.parser
------------------------------------

Rule-driven parser: turns the raw pages returned by
:class:`~.fetcher.RuleFetcher` into the library's ``BookInfoDict`` /
``ChapterDict`` structures, using only the selectors declared in the rule.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)

from .engine import abs_url, extract, extract_multi, parse_page, select
from .model import Rule, RuleError

logger = logging.getLogger(__name__)

__all__ = ["RuleParser"]


class RuleParser(BaseParser):
    """Rule-driven parser. Concrete per-site subclasses set ``_rule``."""

    _rule: ClassVar[Rule]
    site_name: str = "rule"

    # ------------------------------------------------------------------ #
    # book info
    # ------------------------------------------------------------------ #
    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        rule = self._rule
        book_rule = rule.book
        if not raw_pages or book_rule is None:
            return None

        base = (rule.base_url or "").rstrip("/")
        fmt = rule.book_format
        tree = parse_page(raw_pages[0], fmt)

        def field_value(name: str) -> str:
            fld = book_rule.fields.get(name)
            return extract(tree, fld, fmt) if fld else ""

        tags_field = book_rule.fields.get("tags")
        tags = extract_multi(tree, tags_field, fmt) if tags_field else []

        chapters = self._parse_chapters(raw_pages, base)
        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [
            {"volume_name": book_rule.chapters.volume_name, "chapters": chapters}
        ]

        info: BookInfoDict = {
            "book_name": field_value("book_name"),
            "author": field_value("author"),
            "cover_url": abs_url(base, field_value("cover_url")),
            "update_time": field_value("update_time"),
            "summary": field_value("summary"),
            "extra": {"site": rule.site_key},
            "volumes": volumes,
            "tags": tags,
        }
        for optional in ("word_count", "serial_status", "summary_brief"):
            value = field_value(optional)
            if value:
                info[optional] = value
        return info

    def _parse_chapters(self, raw_pages: list[str], base: str) -> list[ChapterInfoDict]:
        rule = self._rule
        book_rule = rule.book
        if book_rule is None:
            return []

        chapters_rule = book_rule.chapters
        id_rx = re.compile(chapters_rule.id_regex) if chapters_rule.id_regex else None
        fmt = rule.book_format

        out: list[ChapterInfoDict] = []
        seen: set[str] = set()

        for raw in raw_pages:
            tree = parse_page(raw, fmt)
            for node in select(tree, chapters_rule.item, fmt):
                href = extract(node, chapters_rule.url, fmt)
                if not href:
                    continue
                book_url = abs_url(base, href)
                if book_url in seen:
                    continue
                seen.add(book_url)

                title = extract(node, chapters_rule.title, fmt)
                cid = self._chapter_id(href, id_rx, chapters_rule.id_group)
                out.append({"title": title, "url": book_url, "chapterId": cid})
        return out

    @staticmethod
    def _chapter_id(href: str, id_rx: re.Pattern[str] | None, group: int) -> str:
        if id_rx is not None:
            m = id_rx.search(href)
            if m:
                # ``group`` is validated >= 0 at parse time; a negative index
                # would raise a raw ``IndexError`` here, so stay defensive.
                return m.group(group) if 0 < group <= len(m.groups()) else m.group(0)
        stem = urlsplit(href).path.rstrip("/").split("/")[-1]
        return stem.rsplit(".", 1)[0] if "." in stem else stem

    # ------------------------------------------------------------------ #
    # chapter content
    # ------------------------------------------------------------------ #
    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        rule = self._rule
        chapter_rule = rule.chapter
        if not raw_pages or chapter_rule is None:
            return None

        title = ""
        bodies: list[str] = []
        fmt = rule.chapter_format

        for raw in raw_pages:
            try:
                tree = parse_page(raw, fmt)
            except RuleError:
                # Blank / degenerate responses (interstitials, empty extra
                # pages) simply contribute nothing instead of aborting.
                continue
            if chapter_rule.title is not None and not title:
                title = extract(tree, chapter_rule.title, fmt)
            body = extract(tree, chapter_rule.content, fmt)
            if body:
                bodies.append(body)

        content = "\n".join(bodies)
        for old, new in chapter_rule.replace:
            content = content.replace(old, new)
        content = self._drop_lines(content, chapter_rule.drop).strip()

        if not content:
            return None

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": rule.site_key},
        }

    @staticmethod
    def _drop_lines(content: str, patterns: list[str]) -> str:
        if not patterns:
            return content
        rx = re.compile("|".join(patterns))
        return "\n".join(line for line in content.splitlines() if not rx.search(line))
