#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.n71ge.parser
-------------------------------------------

"""

from typing import Any

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    parse_html_tree,
    resolve_url,
    strip_page_marker,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


@registrar.register_parser()
class N71geParser(BaseParser):
    """
    Parser for 新吾爱文学 book pages.
    """

    site_name: str = "n71ge"
    BASE_URL = "https://www.71ge.com"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if not raw_pages:
            return None
        tree = parse_html_tree(raw_pages[0])
        if tree is None:
            return None

        book_name = (
            self._first_str(tree.xpath("//meta[@name='og:novel:book_name']/@content"))
            or self._first_str(tree.xpath("//meta[@property='og:title']/@content"))
            or self._first_str(tree.xpath("//div[@class='introduce']//h1/text()"))
        )

        author = (
            self._first_str(tree.xpath("//meta[@name='og:novel:author']/@content"))
            or self._first_str(tree.xpath("//meta[@name='author']/@content"))
            or self._first_str(
                tree.xpath(
                    "//div[@class='introduce']//p[@class='bq']//span[contains(., '作者')]/a/text()"  # noqa: E501
                )
            )
        )

        cover_url = self._first_str(
            tree.xpath("//meta[@property='og:image']/@content")
        ) or self._first_str(tree.xpath("//div[contains(@class,'pic')]//img/@src"))
        cover_url = resolve_url(tree, cover_url, fallback_base=self.BASE_URL)

        serial_status = self._first_str(
            tree.xpath("//meta[@property='og:novel:status']/@content")
        ) or self._first_str(
            tree.xpath("//div[@class='introduce']//span[contains(., '状态')]/text()"),
            replaces=[("状态：", "")],
        )

        update_time = self._first_str(
            tree.xpath("//meta[@name='og:novel:update_time']/@content")
        ) or self._first_str(
            tree.xpath("//div[@class='introduce']//span[contains(., '更新')]/text()"),
            replaces=[("更新：", "")],
        )

        category = self._first_str(
            tree.xpath("//meta[@property='og:novel:category']/@content")
        )
        tags = [category] if category else []

        summary = self._first_str(
            tree.xpath("//meta[@property='og:description']/@content")
        ) or self._norm_space(
            tree.xpath("string(//div[@class='introduce']//p[@class='jj'])")
        )

        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath("//div[contains(@class,'ml_list')]//ul/li/a"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            title = (a.text or "").strip()
            if not title:
                continue
            chapters.append(
                {
                    "title": title,
                    "url": resolve_url(tree, href, fallback_base=self.BASE_URL),
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
            "serial_status": serial_status,
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
        if not raw_pages:
            return None

        raw_pages = self._usable_pages(raw_pages)

        title_text: str = ""
        paragraphs: list[str] = []
        for curr_html in raw_pages:
            tree = parse_html_tree(curr_html)
            if tree is None:
                continue

            if not title_text:
                title_text = strip_page_marker(
                    self._first_str(
                        tree.xpath(
                            "//div[@id='nr_content']//div[@class='nr_title']/h3/text()"
                        )
                    )
                )

            for p in tree.xpath(
                "//div[@id='nr_content']//div[@class='novelcontent']//p"
            ):
                for elem in p.iter():
                    if txt := elem.text and elem.text.strip():
                        paragraphs.append(txt)
                    if tail := elem.tail and elem.tail.strip():
                        paragraphs.append(tail)

            if not paragraphs:
                continue
            if paragraphs[-1].startswith("本章未完") or paragraphs[-1].startswith(
                "本章已完"
            ):  # noqa: E501
                paragraphs.pop()

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title_text,
            "content": content,
            "extra": {"site": self.site_name},
        }
