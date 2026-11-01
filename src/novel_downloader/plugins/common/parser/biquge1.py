#!/usr/bin/env python3
"""
novel_downloader.plugins.common.parser.biquge1
----------------------------------------------
"""

import re
from typing import Any

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    dedupe_chapters,
    resolve_url,
    strip_page_marker,
)
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


class Biquge1Parser(BaseParser):
    """
    Base Parser for biquge-related book pages.
    """

    ADS = {r"^第\(\d+/\d+\)页$"}
    BASE_URL: str = ""
    _NESTED_HTML_RE = re.compile(r"<!DOCTYPE.*?>", re.I | re.S)
    _HEAD_RE = re.compile(r"<head>.*?</head>", re.I | re.S)
    _CHAPTER_SUFFIX_RE = re.compile(r"\s*[-—－]\s*《[^》]*》\s*$")
    _BOOK_PREFIX_RE = re.compile(r"^\s*《[^》]*》\s*")

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])

        book_name = self._first_str(
            tree.xpath('//meta[@property="og:novel:book_name"]/@content')
        )
        author = self._first_str(
            tree.xpath('//meta[@property="og:novel:author"]/@content')
        )
        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath('//meta[@property="og:image"]/@content')),
            fallback_base=self.BASE_URL,
        )
        update_time = self._first_str(
            tree.xpath('//meta[@property="og:novel:update_time"]/@content')
        )
        serial_status = self._first_str(
            tree.xpath('//meta[@property="og:novel:status"]/@content')
        )
        category = self._first_str(
            tree.xpath('//meta[@property="og:novel:category"]/@content')
        )
        tags = [category] if category else []

        summary = self._first_str(
            tree.xpath('//meta[@property="og:description"]/@content'),
            replaces=[("\u00a0", ""), (" ", ""), ("<br>", "\n"), ("<br/>", "\n")],
        )
        if not summary:
            summary = self._join_strs(
                tree.xpath('//div[@id="intro_pc"]//text()'),
                replaces=[("\u00a0", " ")],
            )

        # --- Volumes & Chapters ---
        chapters: list[ChapterInfoDict] = []
        for curr_html in raw_pages:
            t = html.fromstring(curr_html)
            chapters.extend(self._chapters_from_tree(t))
        chapters = dedupe_chapters(chapters)

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "serial_status": serial_status,
            "summary": summary,
            "tags": tags,
            "volumes": volumes,
            "extra": {},
        }

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        title: str = ""
        paragraphs: list[str] = []

        # A document shell (e.g. ``<html></html>``) is non-blank as fetched
        # but empty once the wrappers are stripped, and ``fromstring("")``
        # raises ParserError. Re-apply the blank-page guard to the stripped
        # payloads so shell pages are skipped like any other empty page.
        stripped_pages = self._usable_pages(
            [self._strip_nested_html(page) for page in raw_pages]
        )
        for curr_html in stripped_pages:
            tree = html.fromstring(curr_html)
            if not title:
                title = self._clean_title(self._first_str(tree.xpath("//h1//text()")))

            for article in tree.xpath("//article"):
                texts = [
                    t.strip()
                    for t in article.xpath(
                        ".//text()[not(ancestor::script) and not(ancestor::style)]"
                    )
                    if t.strip()
                ]
                texts = [t for t in texts if not self._is_ad_line(t)]
                if texts:
                    paragraphs.append("\n".join(texts))

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }

    def _chapters_from_tree(self, tree: Any) -> list[ChapterInfoDict]:
        """Extract chapter links from one catalogue page."""
        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath('//div[contains(@class,"book_list2")]//a[@href]'):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            title = (a.text_content() or "").strip()
            if not title:
                title = (a.get("title") or "").strip()
            chapters.append(
                {
                    "title": title,
                    "url": href,
                    "chapterId": chapter_id_from_href(href),
                }
            )
        return chapters

    @classmethod
    def _clean_title(cls, raw_title: str) -> str:
        """Drop the book-name suffix/prefix that this template appends to <h1>."""
        title = cls._BOOK_PREFIX_RE.sub("", raw_title.strip())
        title = cls._CHAPTER_SUFFIX_RE.sub("", title)
        return strip_page_marker(title).strip(" —-")

    @classmethod
    def _strip_nested_html(cls, s: str) -> str:
        s = cls._NESTED_HTML_RE.sub("", s)
        s = cls._HEAD_RE.sub("", s)
        for tag in ("html", "body"):
            s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
        return s
