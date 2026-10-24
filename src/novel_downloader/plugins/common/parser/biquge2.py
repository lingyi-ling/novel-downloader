#!/usr/bin/env python3
"""
novel_downloader.plugins.common.parser.biquge2
----------------------------------------------
"""

from typing import Any

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    dedupe_chapters,
    resolve_url,
    strip_label,
    strip_page_marker,
)
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


class Biquge2Parser(BaseParser):
    """
    Base Parser for biquge-related book pages.
    """

    BASE_URL = "https://www.biquge.com"
    ADS = {
        r"biquge\.com",
    }

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])

        # --- Metadata ---
        book_name = self._first_str(tree.xpath('//div[@id="info"]/h1/text()'))
        author = strip_label(
            self._first_str(tree.xpath('//div[@id="info"]/p[1]/text()')),
            "作者",
        )
        serial_status = strip_label(
            self._first_str(tree.xpath('//div[@id="info"]/p[2]/text()')),
            "状态",
        )
        update_time = strip_label(
            self._first_str(tree.xpath('//div[@id="info"]/p[3]/text()')),
            "最后更新",
        )

        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath('//div[@id="sidebar"]//img/@src')),
            fallback_base=self.BASE_URL,
        )

        summary = self._join_strs(tree.xpath('//div[@id="intro"]/p//text()'))

        # --- Volumes & Chapters ---
        chapters = self._chapters_from_tree(tree)
        if not chapters:
            # Alternate template: no "正文" section label, take every <dd>.
            chapters = self._chapters_from_dds(tree)
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
            "volumes": volumes,
            "extra": {},
        }

    def _make_chapter(self, tree: Any, anchor: Any) -> ChapterInfoDict | None:
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "#")):
            return None
        title = (anchor.text_content() or "").strip()
        if not title:
            title = (anchor.get("title") or "").strip()
        return {
            "title": title,
            "url": resolve_url(tree, href, fallback_base=self.BASE_URL),
            "chapterId": chapter_id_from_href(href),
        }

    def _chapters_from_tree(self, tree: Any) -> list[ChapterInfoDict]:
        """Collect the chapters of every section whose heading mentions 正文."""
        chapters: list[ChapterInfoDict] = []
        for dt in tree.xpath('//div[contains(@class,"listmain")]/dl/dt'):
            if "正文" not in (dt.text_content() or ""):
                continue
            sibling = dt.getnext()
            while sibling is not None and sibling.tag == "dd":
                anchors = sibling.xpath(".//a[@href]")
                if anchors:
                    chapter = self._make_chapter(tree, anchors[0])
                    if chapter is not None:
                        chapters.append(chapter)
                sibling = sibling.getnext()
        return chapters

    def _chapters_from_dds(self, tree: Any) -> list[ChapterInfoDict]:
        """Fallback used when no section is explicitly labelled 正文."""
        chapters: list[ChapterInfoDict] = []
        for dd in tree.xpath('//div[contains(@class,"listmain")]//dl/dd'):
            anchors = dd.xpath(".//a[@href]")
            if anchors:
                chapter = self._make_chapter(tree, anchors[0])
                if chapter is not None:
                    chapters.append(chapter)
        return chapters

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])

        title = self._first_str(tree.xpath('//div[@class="content"]/h1//text()'))
        if not title:
            title = self._first_str(
                tree.xpath('//div[contains(@class,"content")]/h1//text()')
            )
        title = strip_page_marker(title)

        # collect all visible text inside #content, skipping scripts/styles
        raw_texts = tree.xpath(
            '//div[@id="content"]//text()[not(ancestor::script) and not(ancestor::style)]'  # noqa: E501
        )

        paragraphs: list[str] = []
        for ln in raw_texts:
            # normalize spaces: \xa0 (nbsp) and \u3000 (ideographic space)
            s = ln.replace("\xa0", "").replace("\u3000", "").strip()
            if not s or self._is_ad_line(s):
                continue
            paragraphs.append(s)

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
