#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.czbooks.parser
---------------------------------------------
"""

from typing import Any

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
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


@registrar.register_parser()
class CzbooksParser(BaseParser):
    """
    Parser for 小说狂人 book pages.
    """

    site_name: str = "czbooks"
    BASE_URL: str = "https://czbooks.net"

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

        # 2026 layout keeps the title in <h1 class="title">, not span.title.
        book_name = self._first_str(
            tree.xpath(
                "//div[contains(@class,'info')]//h1[contains(@class,'title')]/text()"
            )
        ) or self._first_str(
            tree.xpath("//div[contains(@class,'info')]//span[@class='title']/text()")
        )
        author = self._first_str(
            tree.xpath(
                "//div[contains(@class,'info')]//span[@class='author']//a/text()"
            )
        )
        cover_rel = self._first_str(
            tree.xpath("//div[contains(@class,'thumbnail')]//img/@src")
        )
        cover_url = resolve_url(tree, cover_rel, fallback_base=self.BASE_URL)

        update_time = self._first_str(
            tree.xpath(
                "//div[contains(@class,'state')]//tr[normalize-space(td[1])='更新時間']/td[2]/text()"
            )
        )
        serial_status = self._first_str(
            tree.xpath(
                "//div[contains(@class,'state')]//tr[normalize-space(td[1])='連載狀態']/td[2]/text()"
            )
        )

        genre = self._first_str(tree.xpath("//a[@id='novel-category']/text()"))
        tags = [genre] if genre else []

        summary = self._join_strs(
            tree.xpath("//div[contains(@class,'description')]//text()")
        )

        # Chapters from the book_list
        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath("//ul[@id='chapter-list']//a"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            if href.startswith("//"):
                href = "https:" + href

            chapter_id = chapter_id_from_href(href)
            if not chapter_id:
                continue
            title = (a.text_content() or "").strip()
            chapters.append(
                {
                    "title": title,
                    "url": resolve_url(tree, href, fallback_base=self.BASE_URL)
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

        title = self._first_str(
            tree.xpath("//h1[contains(@class,'name')]/text()")
        ) or self._first_str(tree.xpath("//div[contains(@class,'name')]/text()"))

        paragraphs = [  # from div class="content"
            s for p in tree.xpath("//div[@class='content']//text()") if (s := p.strip())
        ] or [
            s
            for p in tree.xpath("//div[contains(@class,'content')]//text()")
            if (s := p.strip())
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
