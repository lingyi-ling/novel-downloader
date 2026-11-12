#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.mangg_com.parser
-----------------------------------------------

"""

from typing import Any

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
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


@registrar.register_parser()
class ManggComParser(BaseParser):
    """
    Parser for 追书网 book pages.
    """

    site_name: str = "mangg_com"
    BASE_URL = "https://www.mangg.com"
    ADS: set[str] = {
        "记住追书网网",
        r"mangg\.com",
    }

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

        # --- Metadata ---
        book_name = self._first_str(tree.xpath('//div[@id="info"]/h1/text()'))
        author = self._first_str(
            tree.xpath('//div[@id="info"]/p[1]/text()'),
            replaces=[(chr(0xA0), ""), ("作者：", "")],
        )
        serial_status = self._first_str(
            tree.xpath('//div[@id="info"]/p[2]/text()'),
            replaces=[(chr(0xA0), ""), ("状态：", ""), (",", "")],
        )
        update_time = self._first_str(
            tree.xpath('//div[@id="info"]/p[3]/text()'),
            replaces=[("最后更新：", "")],
        )

        cover_src = self._first_str(tree.xpath('//div[@id="sidebar"]//img/@src'))
        cover_url = resolve_url(tree, cover_src, fallback_base=self.BASE_URL)

        summary = self._join_strs(
            tree.xpath('//div[@id="intro"]/text() | //div[@id="intro"]//p/text()'),
            replaces=[(chr(0xA0), "")],
        )

        # --- Volumes & Chapters ---
        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath('//div[@id="list"]//dd/a'):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            chapters.append(
                {
                    "title": (a.text_content() or "").strip(),
                    "url": href,
                    "chapterId": chapter_id_from_href(href),
                }
            )

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

        title = self._first_str(tree.xpath('//div[@class="bookname"]/h1/text()'))

        paragraphs: list[str] = []
        for ln in tree.xpath(
            '//div[@id="content"]//text()[not(ancestor::script) and not(ancestor::style)]'  # noqa: E501
        ):
            if not ln or not ln.strip() or self._is_ad_line(ln):
                continue
            paragraphs.append(ln.strip())

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
