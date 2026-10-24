#!/usr/bin/env python3
"""
novel_downloader.plugins.common.parser.biquge3
----------------------------------------------
"""

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


class Biquge3Parser(BaseParser):
    """
    Base Parser for biquge-related book pages.
    """

    ADS = {
        # Inline "read next page" prompt kept inside the content block.
        r"^本章未完[，,].*下一页.*$",
        r"^第\(\d+/\d+\)页$",
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

        book_name = self._first_str(
            tree.xpath('//meta[@property="og:novel:book_name"]/@content')
        )
        author = self._first_str(
            tree.xpath('//meta[@property="og:novel:author"]/@content')
        )
        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath('//meta[@property="og:image"]/@content')),
        )
        update_time = self._first_str(
            tree.xpath('//meta[@property="og:novel:update_time"]/@content')
        )
        serial_status = self._first_str(
            tree.xpath('//meta[@property="og:novel:status"]/@content')
        )
        summary = self._first_str(
            tree.xpath('//meta[@property="og:description"]/@content')
        )
        category = self._first_str(
            tree.xpath('//meta[@property="og:novel:category"]/@content')
        )
        tags = [category] if category else []

        chapters = self._chapters_from_tree(tree)
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

    def _chapter_from_anchor(self, anchor: Any) -> ChapterInfoDict | None:
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "#")):
            return None
        title = "".join(anchor.itertext()).strip()
        if not title:
            title = (anchor.get("title") or "").strip()
        return {
            "title": title,
            "url": href,
            "chapterId": chapter_id_from_href(href),
        }

    def _chapters_from_tree(self, tree: Any) -> list[ChapterInfoDict]:
        # Main template: <dt>目录章节</dt> followed by <a><dd>title</dd></a>.
        anchors = tree.xpath('//div[@id="list"]/dl/dt[2]/following-sibling::a[@href]')
        if not anchors:
            # Variant: anchors wrapped in <dd> elements.
            anchors = tree.xpath(
                '//div[@id="list"]/dl/dt[2]/following-sibling::dd//a[@href]'
            )
        if not anchors:
            # Last resort (single-section catalogue or different dt count).
            anchors = tree.xpath('//div[@id="list"]//a[@href][@rel="chapter"]')
        if not anchors:
            anchors = tree.xpath('//div[@id="list"]//dd//a[@href]')

        chapters: list[ChapterInfoDict] = []
        for anchor in anchors:
            chapter = self._chapter_from_anchor(anchor)
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

        title = ""
        paragraphs: list[str] = []

        for curr_html in raw_pages:
            tree = html.fromstring(curr_html)

            if not title:
                title = self._first_str(
                    tree.xpath('//h1[contains(@class,"bookname")]//text()')
                )
                if not title:
                    title = self._first_str(
                        tree.xpath('//div[contains(@class,"bookname")]/h1//text()')
                    )
                title = strip_page_marker(title)

            page_paragraphs: list[str] = []
            for p in tree.xpath('//div[@id="content"]//div[@id="booktxt"]/p'):
                text = self._join_strs(
                    p.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]")
                )
                if text and not self._is_ad_line(text):
                    page_paragraphs.append(text)

            if not page_paragraphs:
                # Variant without <p> wrappers: read the raw text nodes and
                # split them on the template's <br> line breaks.
                for node in tree.xpath(
                    '//div[@id="content"]//text()[not(ancestor::script) and '
                    "not(ancestor::style)]"
                ):
                    for line in node.splitlines():
                        text = line.replace("\xa0", " ").strip()
                        if text and not self._is_ad_line(text):
                            page_paragraphs.append(text)

            paragraphs.extend(page_paragraphs)

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
