#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.yamibo.parser
--------------------------------------------

"""

from typing import Any
from urllib.parse import parse_qs, urlparse

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
class YamiboParser(BaseParser):
    """
    Parser for 百合会 book pages.
    """

    site_name: str = "yamibo"
    BASE_URL = "https://www.yamibo.com"

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
            tree.xpath('//h3[contains(@class,"col-md-12")]/text()')
        )
        author = self._first_str(
            tree.xpath('//h5[contains(@class,"text-warning")]/text()')
        )
        cover_url = resolve_url(
            tree,
            self._first_str(
                tree.xpath('//img[contains(@class,"img-responsive")]/@src')
            ),
            fallback_base=self.BASE_URL,
        )

        update_time = self._first_str(
            tree.xpath('//p[contains(text(),"更新时间：")]/text()'),
            replaces=[("更新时间：", "")],
        )
        serial_status = self._first_str(
            tree.xpath('//p[contains(text(),"作品状态：")]/text()'),
            replaces=[("作品状态：", "")],
        )
        book_type = self._first_str(
            tree.xpath('//p[contains(text(),"作品分类：")]/text()'),
            replaces=[("作品分类：", "")],
        )
        summary = self._first_str([tree.xpath('string(//div[@id="w0-collapse1"]/div)')])

        # volumes & chapters
        volumes: list[VolumeInfoDict] = []
        for volume_node in tree.xpath(
            '//div[contains(@class,"panel-info") and contains(@class,"panel-default")]'
        ):
            volume_name = (
                self._first_str(
                    volume_node.xpath(
                        './/div[contains(@class,"panel-heading")]//a/text()'
                    )
                )
                or "未命名卷"
            )
            chapters: list[ChapterInfoDict] = []
            for chap in volume_node.xpath(
                './/div[contains(@class,"panel-body")]//a[contains(@href,"view-chapter")]'
            ):
                title = self._first_str([chap.xpath("string()")])
                url = chap.get("href", "")
                chapter_id = self._chapter_id(url)
                if not chapter_id:
                    continue
                chapters.append({"title": title, "url": url, "chapterId": chapter_id})
            if chapters:
                volumes.append({"volume_name": volume_name, "chapters": chapters})

        # fallback: flat chapter list
        if not volumes:
            chapters = []
            for chap in tree.xpath(
                '//div[@class="panel-body"]//a[contains(@href,"view-chapter")]'
            ):
                title = self._first_str([chap.xpath("string()")])
                url = chap.get("href", "")
                chapter_id = self._chapter_id(url)
                if not chapter_id:
                    continue
                chapters.append({"title": title, "url": url, "chapterId": chapter_id})

            if not chapters:
                return None

            volumes = [{"volume_name": "单卷", "chapters": chapters}]

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "serial_status": serial_status,
            "tags": [book_type] if book_type else [],
            "summary": summary,
            "volumes": volumes,
            "extra": {},
        }

    @staticmethod
    def _chapter_id(url: str) -> str:
        """Extract ``id`` from ``view-chapter?id=...`` without query leftovers."""
        values = parse_qs(urlparse(url).query).get("id")
        if values:
            return values[0].strip()
        return chapter_id_from_href(url)

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

        paragraphs = [
            text
            for p in tree.xpath("//div[@id='w0-collapse1']//p//text()")
            if (text := self._norm_space(p))
        ]

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        title = self._first_str(
            [tree.xpath("string(//section[contains(@class,'col-md-9')]//h3)")]
        )

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }
