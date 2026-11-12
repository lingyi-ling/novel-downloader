#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.zhenhunxiaoshuo.parser
-----------------------------------------------------

"""

from typing import Any

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    parse_html_tree,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


@registrar.register_parser()
class ZhenhunxiaoshuoParser(BaseParser):
    """
    Parser for 镇魂小说网 book pages.
    """

    site_name: str = "zhenhunxiaoshuo"

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
            tree.xpath("//h1[contains(@class,'focusbox-title')]/text()")
        )
        summary = self._join_strs(
            tree.xpath(
                "//div[contains(@class,'focusbox-text')]//p[contains(@class,'text')]//text()"
            ),
            replaces=[("\u3000", " ")],
        )

        chapters: list[ChapterInfoDict] = [
            {
                "title": (a.text or "").strip(),
                "url": href,
                "chapterId": chapter_id_from_href(href),
            }
            for a in tree.xpath("//div[contains(@class,'excerpts')]//article//a[@href]")
            if (href := (a.get("href") or "").strip())
            and not href.lower().startswith(("javascript:", "#"))
        ]

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": "",
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
            tree.xpath(
                "//header[contains(@class,'article-header')]//h1[contains(@class,'article-title')]/text()"
            )
        )

        paragraphs = [
            text
            for p in tree.xpath("//article[contains(@class,'article-content')]//p")
            if (text := self._norm_space("".join(p.itertext())))
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
