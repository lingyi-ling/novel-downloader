#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ruochu.parser
--------------------------------------------
"""

import json
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
class RuochuParser(BaseParser):
    """
    Parser for 若初文学网 book pages.
    """

    site_name: str = "ruochu"
    BASE_URL = "https://www.ruochu.com"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 2 or not raw_pages[0].strip() or not raw_pages[1].strip():
            return None

        info_tree = parse_html_tree(raw_pages[0])
        catalog_tree = parse_html_tree(raw_pages[1])
        if info_tree is None or catalog_tree is None:
            return None

        # --- base info ---
        book_name = self._first_str(
            info_tree.xpath(
                '//div[contains(@class,"pattern-cover-detail")]//h1//span/text()'
            )
        )
        # The author link is in the cover breadcrumb ("作者：<a class=author>").
        # The previous selector matched the site-wide disclaimer paragraph,
        # which also contains the word "作者".
        author = self._first_str(
            info_tree.xpath(
                '//div[contains(@class,"cover-breadcrumb")]'
                '//a[contains(@class,"author")]/text()'
            )
        )
        if not author:
            author = self._first_str(
                info_tree.xpath('//a[contains(@class,"author")]/text()')
            )
        cover_src = self._first_str(
            info_tree.xpath('//div[@class="pic"]//img[@class="book-cover"]/@src')
        )
        cover_url = resolve_url(info_tree, cover_src, fallback_base=self.BASE_URL)
        word_count = self._first_str(
            info_tree.xpath('//span[contains(@class,"words")]/text()')
        )
        serial_status = (
            "连载中"
            if info_tree.xpath('//i[contains(@class,"is-serialize")]')
            else "完结"  # noqa: E501
        )
        update_time = self._first_str(info_tree.xpath('//span[@class="time"]/text()'))

        summary = self._join_strs(
            info_tree.xpath(
                '//div[contains(@class,"summary")]//pre[@class="note"]/text()'
            )
        )

        # --- Chapter volumes & listings ---
        chapters: list[ChapterInfoDict] = []
        for a in catalog_tree.xpath('//div[contains(@class,"chapter-list")]//ul/li/a'):
            url = (a.get("href") or "").strip()
            if not url or url.lower().startswith(("javascript:", "#")):
                continue
            title = a.text_content().strip()
            chap_id = chapter_id_from_href(url)
            if not chap_id:
                continue
            accessible = "isvip" not in (a.get("class") or "")
            chapters.append(
                {
                    "title": title,
                    "url": url,
                    "chapterId": chap_id,
                    "accessible": accessible,
                }
            )

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "serial_status": serial_status,
            "word_count": word_count,
            "update_time": update_time,
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
        if not raw_pages:
            return None

        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        text = raw_pages[0]
        if '"chapter"' not in text:
            return None
        if "(" in text and ")" in text:
            json_str = text.split("(", 1)[-1].rsplit(")", 1)[0].strip()
        else:
            return None

        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        chapter = data.get("chapter")
        if not isinstance(chapter, dict):
            return None
        html_content = chapter.get("htmlContent")
        if not html_content:
            return None

        title = chapter.get("title") or ""
        tree = parse_html_tree(str(html_content))
        if tree is None:
            return None

        paragraphs: list[str] = [
            text for p in tree.xpath("//p") if (text := p.text_content().strip())
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
