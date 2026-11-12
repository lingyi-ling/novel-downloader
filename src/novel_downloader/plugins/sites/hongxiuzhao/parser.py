#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.hongxiuzhao.parser
-------------------------------------------------
"""

import json
from typing import Any

from novel_downloader.infra.paths import HONGXIUZHAO_MAP_PATH
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
class HongxiuzhaoParser(BaseParser):
    """
    Parser for 红袖招 book pages.
    """

    site_name: str = "hongxiuzhao"
    BASE_URL = "https://hongxiuzhao.net"

    ADS = {
        "为防失联",
        "hongxiuzhao",
        "本站不支持",
        "如果喜欢本站",
        "收藏永久网址",
    }

    _FONT_MAP: dict[str, str] = {}

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
            tree.xpath('//div[contains(@class, "m-bookdetail")]//h1/text()')
        )
        author = self._first_str(tree.xpath('//p[contains(@class, "author")]/a/text()'))
        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath('//a[contains(@class, "cover")]//img/@src')),
            fallback_base=self.BASE_URL,
        )

        summary = self._join_strs(
            tree.xpath('//p[contains(@class, "summery")]//text()')
        )

        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath('//section[contains(@class,"yd-chapter")]//ul//a'):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            chapter_id = chapter_id_from_href(href)
            title = (a.text_content() or "").strip()
            chapters.append(
                {
                    "title": title,
                    "url": self.BASE_URL + href if href.startswith("/") else href,
                    "chapterId": chapter_id,
                }
            )

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
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

        title = ""
        paragraphs: list[str] = []

        for curr_html in raw_pages:
            tree = parse_html_tree(curr_html)
            if tree is None:
                continue

            if not title:
                title = self._first_str(
                    tree.xpath('//div[@class="article-content"]//h1/text()')
                )

            for p in tree.xpath('//div[@class="article-content"]//p'):
                text = (p.text_content() or "").strip()
                if not text or self._is_ad_line(text):
                    continue
                text = self._map_fonts(text)
                if text:
                    paragraphs.append(text)

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }

    @classmethod
    def _map_fonts(cls, text: str) -> str:
        """
        Apply font mapping to the input text.
        """
        if not cls._FONT_MAP:
            cls._FONT_MAP = json.loads(HONGXIUZHAO_MAP_PATH.read_text(encoding="utf-8"))

        return "".join(cls._FONT_MAP.get(c, c) for c in text)
