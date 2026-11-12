#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.akatsuki_novels.parser
-----------------------------------------------------

The 2026 redesign kept the old ``/stories/index/novel_id~<id>`` book pages and
``/stories/view/<chapter>/novel_id~<id>`` chapter pages, but replaced the
markup: the title/author live in a page ``<header>`` instead of
``h3.font-bb``, the chapter list is an ``<ol>`` of ``/stories/view/`` links
instead of ``table.list``, and chapter prose sits in
``div.reading-body`` instead of ``div.body-novel``.  Both templates are
supported so previously cached pages still parse.
"""

import re
from typing import Any

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    dedupe_chapters,
    parse_html_tree,
    resolve_url,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)

_CHAPTER_ID_RE = re.compile(r"/stories/view/(\d+)")


@registrar.register_parser()
class AkatsukiNovelsParser(BaseParser):
    """
    Parser for 暁 book pages.
    """

    site_name: str = "akatsuki_novels"
    BASE_URL = "https://www.akatsuki-novels.com"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        tree = parse_html_tree(raw_pages[0])
        if tree is None:
            return None

        book_name = self._first_str(
            tree.xpath('//article/header//h1[contains(@class, "font-bold")]/text()')
        ) or self._first_str(tree.xpath('//h3[@class="font-bb"]/a/text()'))
        author = self._first_str(
            tree.xpath('//article/header//a[contains(@href, "/users/view/")]/text()')
        ) or self._first_str(
            tree.xpath('//h3[@class="font-bb"][contains(., "作者")]/a/text()')
        )
        summary = self._first_str(
            tree.xpath(
                '//article/header//p[contains(@class, "whitespace-pre-line")]/text()'
            )
        ) or self._join_strs(
            tree.xpath(
                '//div[contains(normalize-space(@class), "body-x1 body-normal body-w640")]/div/div[1]//text()'  # noqa: E501
            )
        )

        # Current layout: <ol> of /stories/view/<chapter>/novel_id~<book>.
        anchors = tree.xpath(
            '//ol//a[contains(@href, "/stories/view/")]'
        ) or tree.xpath('//table[@class="list"]//tbody//tr/td[1]/a')

        chapters: list[ChapterInfoDict] = []
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            match = _CHAPTER_ID_RE.search(href)
            if not match:
                continue
            chapters.append(
                {
                    "title": (a.text_content() or "").strip(),
                    "url": resolve_url(tree, href, fallback_base=self.BASE_URL)
                    .split("#", 1)[0]
                    .split("?", 1)[0],
                    "chapterId": match.group(1),
                }
            )
        chapters = dedupe_chapters(chapters)

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": "",
            "update_time": "",
            "summary": summary,
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

        tree = parse_html_tree(raw_pages[0])
        if tree is None:
            return None

        title = self._first_str(
            tree.xpath('//article//h1[contains(@class, "font-bold")]/text()')
        ) or self._first_str(tree.xpath("//h2/text()"))

        body = tree.xpath('//div[contains(@class, "reading-body")]')
        if body:
            content = (body[0].text_content() or "").strip()
        else:
            paragraphs = [
                s
                for p in tree.xpath('//div[@class="body-novel"]//text()')
                if (s := p.strip())
            ]
            content = "\n".join(paragraphs)

        if not content:
            return None

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
