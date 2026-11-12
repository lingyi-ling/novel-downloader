#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.bixiange.parser
----------------------------------------------
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
class BixiangeParser(BaseParser):
    """
    Parser for 笔仙阁 book pages.
    """

    site_name: str = "bixiange"
    BASE_URL = "https://m.bixiange.top"

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

        # --- Basic Metadata ---
        book_name = self._first_str(tree.xpath('//div[@class="desc"]/h1/text()'))
        author = self._first_str(
            tree.xpath(
                '//div[@class="descTip"]//span[contains(text(), "作者")]/text()'
            ),  # noqa: E501
            replaces=[("作者：", "")],
        )
        cover_rel = self._first_str(tree.xpath('//div[@class="cover"]/img/@src'))
        cover_url = resolve_url(tree, cover_rel, fallback_base=self.BASE_URL)

        update_time = self._first_str(
            tree.xpath(
                '//div[@class="descTip"]//span[contains(text(), "时间")]/text()'
            ),  # noqa: E501
            replaces=[("时间：", "")],
        )

        genre = self._first_str(
            tree.xpath(
                '//div[@class="descTip"]//span[contains(text(), "分类")]/text()'
            ),  # noqa: E501
            replaces=[("分类：", "")],
        )
        tags = [genre] if genre else []

        summary = self._join_strs(tree.xpath('//div[@class="descInfo"]//text()'))

        # --- Volumes & Chapters ---
        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath('//div[contains(@class, "catalog")]//a'):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue

            chapter_id = chapter_id_from_href(href)
            if not chapter_id:
                continue
            title = (a.text_content() or "").strip()
            chapters.append(
                {
                    "title": title,
                    # The fetcher rebuilds the URL from book/chapter ids; keep
                    # the page link as-is except for query/fragment noise.
                    "url": href.split("#", 1)[0].split("?", 1)[0],
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
            tree.xpath('//div[contains(@class, "article")]//h1/text()')
        )

        paragraphs = [
            s for p in tree.xpath('//div[@id="mycontent"]//text()') if (s := p.strip())
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
