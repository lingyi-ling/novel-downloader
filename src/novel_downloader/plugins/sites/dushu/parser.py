#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.dushu.parser
-------------------------------------------
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
class DushuParser(BaseParser):
    """
    Parser for 读书 book pages.
    """

    site_name: str = "dushu"
    BASE_URL: str = "https://www.dushu.com"

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

        # --- Basic metadata ---
        book_name = self._first_str(tree.xpath("//div[@class='book-title']/h1/text()"))
        author = self._first_str(
            tree.xpath(
                "//div[@class='book-details']//table//tr[td[contains(., '作')]]/td[2]/text()"  # noqa: E501
            )
        )
        cover_rel = self._first_str(tree.xpath("//div[@class='book-pic']//img/@src"))
        cover_url = resolve_url(tree, cover_rel, fallback_base=self.BASE_URL)

        summary = self._join_strs(
            tree.xpath("//div[contains(@class,'txtsummary')]//text()"),
            replaces=[("\u3000", ""), ("\xa0", ""), ("　", "")],
        )

        # --- Volumes & chapters ---
        volumes: list[VolumeInfoDict] = []

        volume_nodes = tree.xpath(
            "//div[@class='book-summary']//div[@class='book-chapter']"
        )
        for idx, vol_node in enumerate(volume_nodes, start=1):
            vol_name = (vol_node.text_content() or "").strip() or f"未命名卷 {idx}"

            # Find the following table of chapters
            table = vol_node.xpath("following-sibling::table[1]")
            if not table:
                continue

            vol_chaps: list[ChapterInfoDict] = []
            for a in table[0].xpath(".//a"):
                href = (a.get("href") or "").strip()
                if not href or href.lower().startswith(("javascript:", "#")):
                    continue
                chap_id = chapter_id_from_href(href)
                if not chap_id:
                    continue
                title = (a.text_content() or "").strip()
                vol_chaps.append(
                    {
                        "title": title,
                        "url": resolve_url(tree, href, fallback_base=self.BASE_URL)
                        .split("#", 1)[0]
                        .split("?", 1)[0],
                        "chapterId": chap_id,
                    }
                )
            vol_chaps = dedupe_chapters(vol_chaps)

            if vol_chaps:
                volumes.append(
                    {
                        "volume_name": vol_name,
                        "chapters": vol_chaps,
                    }
                )

        if not volumes:
            return None

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

        tree = parse_html_tree(raw_pages[0])
        if tree is None:
            return None

        title = self._first_str(
            tree.xpath(
                "//p[contains(@class,'text-center') and contains(@class,'text-large')]/text()"  # noqa: E501
            )
        )

        paragraphs = [
            s
            for p in tree.xpath("//div[@class='content_txt']//text()")
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
