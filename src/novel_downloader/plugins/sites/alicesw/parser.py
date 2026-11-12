#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.alicesw.parser
---------------------------------------------
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

#: ``/book/<book>/<hash>.html`` -> chapter id ``<book>-<hash>``.
_CHAPTER_HREF_RE = re.compile(r"/book/([^/]+)/([^/.]+)")


def _strip_spaced_label(value: str, label: str) -> str:
    """Remove ``标 签：`` style prefixes where the label is space-separated."""
    pattern = r"\s*".join(re.escape(char) for char in label)
    return re.sub(rf"^\s*{pattern}\s*[：:]\s*", "", value or "").strip()


@registrar.register_parser()
class AliceswParser(BaseParser):
    """
    Parser for 爱丽丝书屋 book-info pages.
    """

    site_name: str = "alicesw"
    BASE_URL = "https://www.alicesw.com"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if len(raw_pages) < 2:
            return None

        info_tree = parse_html_tree(raw_pages[0])
        catalog_tree = parse_html_tree(raw_pages[1])
        if info_tree is None or catalog_tree is None:
            return None

        # Metadata extraction.  The 2026 page uses div.novel_title rather
        # than <h1>, and keeps state/word count in div.novel_info.
        book_name = self._first_str(
            info_tree.xpath("//div[contains(@class,'novel_title')]/text()")
        ) or self._first_str(info_tree.xpath("//div[@id='detail-box']//h1/text()"))
        author = self._first_str(
            info_tree.xpath(
                "//div[@id='detail-box']//p/a[contains(@href,'author')]/text()"
            )
        )
        cover_rel = self._first_str(
            info_tree.xpath(
                "//div[@class='pic']//img[contains(@class,'fengmian2')]/@data-src"
            )
        ) or self._first_str(
            info_tree.xpath(
                "//div[@class='pic']//img[contains(@class,'fengmian2')]/@src"
            )
        )
        cover_url = resolve_url(info_tree, cover_rel, fallback_base=self.BASE_URL)

        tags = info_tree.xpath("//div[contains(@class,'tags_list')]/a/text()")
        if not tags:
            tags = info_tree.xpath("//p[contains(text(),'标签')]/a/text()")
        tags = [t.strip() for t in tags if t.strip()]

        serial_status = _strip_spaced_label(
            self._first_str(
                info_tree.xpath(
                    "//div[contains(@class,'novel_info')]"
                    "//p[contains(.,'状') and contains(.,'态')]/text()"
                )
            ),
            "状态",
        ) or self._first_str(
            info_tree.xpath("//div[@class='pic']/div[contains(text(),'状态')]/text()"),
            replaces=[("小说状态：", "")],
        )
        word_count = _strip_spaced_label(
            self._first_str(
                info_tree.xpath(
                    "//div[contains(@class,'novel_info')]"
                    "//p[contains(.,'字') and contains(.,'数')]/text()"
                )
            ),
            "字数",
        ) or self._first_str(
            info_tree.xpath("//div[@class='pic']/div[contains(text(),'字数')]/text()"),
            replaces=[("小说字数：", "")],
        )
        # The word-count line also carries the chapter count.
        word_count = word_count.split("·", 1)[0].strip()

        update_time = self._first_str(
            info_tree.xpath(
                "//div[@id='detail-box']//div[contains(text(),'更新时间')]/text()"
            ),
            replaces=[("更新时间：", "")],
        )

        summary = self._join_strs(
            info_tree.xpath("//div[contains(@class,'jianjie')]//p//text()")
        )
        if not summary:
            summary = self._first_str(
                info_tree.xpath('//meta[@property="og:description"]/@content')
            )

        chapters: list[ChapterInfoDict] = []
        for a in catalog_tree.xpath("//ul[@class='mulu_list']/li/a"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue

            match = _CHAPTER_HREF_RE.search(href)
            chapter_id = (
                f"{match.group(1)}-{match.group(2)}"
                if match
                else href.rsplit("/", 1)[-1].split(".", 1)[0]
            )
            chapters.append(
                {
                    "title": (a.text_content() or "").strip(),
                    "url": resolve_url(catalog_tree, href, fallback_base=self.BASE_URL)
                    .split("#", 1)[0]
                    .split("?", 1)[0],
                    "chapterId": chapter_id,
                }
            )
        chapters = dedupe_chapters(chapters)

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "word_count": word_count,
            "serial_status": serial_status,
            "tags": tags,
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

        title = self._first_str(tree.xpath("//h3[@class='j_chapterName']/text()"))
        paragraphs = [
            text.strip()
            for p in tree.xpath("//div[contains(@class,'read-content')]//p")
            if (text := self._norm_space(p.text_content()))
        ]

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
