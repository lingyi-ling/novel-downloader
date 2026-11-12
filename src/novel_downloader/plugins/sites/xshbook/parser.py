#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.xshbook.parser
---------------------------------------------

"""

import re
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
class XshbookParser(BaseParser):
    """Parser for 小说虎 book pages."""

    site_name: str = "xshbook"
    BASE_URL = "https://www.xshbook.com"

    ADS = {
        r"谨记我们的网址",
        r"温馨提示",
        r"^提示.{0,50}$",  # "提示" 开头且内容较短
        r"^分享.{0,40}$",  # "分享" 开头且内容较短
    }
    # Inline ads like “本文搜：xxx.com 免费阅读”
    _INLINE_AD_PATTERN = re.compile(
        r"(本文搜|搜索[:：]?)\s*.*?\s*(免费阅读|本文免费阅读)"
    )  # noqa: E501

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

        book_name = self._first_str(tree.xpath("//div[@id='info']/h1/text()"))

        author = self._first_str(
            tree.xpath("//div[@id='info']/p[1]/text()"),
            replaces=[("\xa0", ""), ("作者:", "")],
        )

        update_time = self._first_str(
            tree.xpath("//meta[@property='og:novel:update_time']/@content")
        )

        summary = "\n".join(
            line
            for p in tree.xpath("//div[@id='intro']//p")
            if (line := self._first_str(p.xpath("string()").splitlines()))
        ).strip()
        summary = summary.split("本站提示", 1)[0].strip()

        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath("//div[@id='fmimg']//img/@src")),
            fallback_base=self.BASE_URL,
        )

        book_type = self._first_str(tree.xpath("//div[@class='con_top']/a[2]/text()"))
        tags: list[str] = [book_type] if book_type else []

        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath("//div[@id='list']//dd/a"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            title = self._norm_space(a.text_content())
            # /95071/95071941/389027455.html -> "389027455"
            chapter_id = chapter_id_from_href(href)
            chapters.append({"title": title, "url": href, "chapterId": chapter_id})

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
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
        tree = parse_html_tree(raw_pages[0])
        if tree is None:
            return None

        title = self._first_str(tree.xpath("//div[@class='bookname']/h1/text()"))
        if not title:
            title = self._first_str(
                tree.xpath("//div[@class='con_top']/text()[last()]")
            )

        def clean(raw: str) -> str:
            return self._INLINE_AD_PATTERN.sub("", self._norm_space(raw)).strip()

        paragraphs = [
            text
            for p in tree.xpath("//div[@id='content']//p")
            if (text := clean(p.text_content() or "")) and not self._is_ad_line(text)
        ]

        if not paragraphs:
            # Some pages keep the body as bare text separated by ``<br>``.
            paragraphs = [
                text
                for node in tree.xpath(
                    "//div[@id='content']//text()[not(ancestor::script) "
                    "and not(ancestor::style) and not(ancestor::p)]"
                )
                if (text := clean(node)) and not self._is_ad_line(text)
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
