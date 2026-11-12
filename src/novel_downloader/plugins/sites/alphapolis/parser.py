#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.alphapolis.parser
------------------------------------------------
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
class AlphapolisParser(BaseParser):
    """
    Parser for アルファポリス book pages.
    """

    site_name: str = "alphapolis"
    BASE_URL: str = "https://www.alphapolis.co.jp"

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

        main = tree.xpath(
            '//div[@class="cover novels section"]/div[@class="content-main"]'
        )
        info = tree.xpath('//div[@class="content-info gray-menu section"]')

        book_name = self._first_str(
            tree.xpath("//h1[contains(@class,'title')]/text()")
        ) or (
            self._first_str(main[0].xpath('.//h1[@class="title"]/text()'))
            if main
            else ""
        )
        author = self._first_str(
            tree.xpath("//div[contains(@class,'author')]//a[1]/text()")
        ) or (
            self._first_str(main[0].xpath('.//div[@class="author"]//a[1]/text()'))
            if main
            else ""
        )
        cover_rel = (
            self._first_str(info[0].xpath('.//div[@class="cover"]//img/@src'))
            if info
            else ""
        )
        if not cover_rel:
            cover_rel = self._first_str(
                tree.xpath('//meta[@property="og:image"]/@content')
            )
        cover_url = resolve_url(tree, cover_rel, fallback_base=self.BASE_URL)
        update_time = (
            self._first_str(
                info[0].xpath(
                    './/table[contains(@class,"detail")]//tr[th[text()="更新日時"]]/td/text()'
                )
            )
            if info
            else ""
        )

        tags = [
            s
            for t in (
                main[0].xpath(
                    './/div[@class="content-tags"]//span[@class="tag"]/a/text()'
                )
                if main
                else []
            )
            if t and (s := t.strip())
        ]

        summary = (
            self._join_strs(main[0].xpath('.//div[@class="abstract"]/text()'))
            if main
            else ""
        )
        if not summary:
            summary = self._first_str(
                tree.xpath('//meta[@name="description"]/@content')
            )

        # --- Volumes & Chapters ---
        volumes: list[VolumeInfoDict] = []
        vol_idx: int = 1
        vol_name: str | None = None
        vol_chaps: list[ChapterInfoDict] = []

        def flush_volume() -> None:
            nonlocal vol_idx, vol_name, vol_chaps
            if not vol_chaps:
                return

            volumes.append(
                {
                    "volume_name": vol_name or f"未命名卷 {vol_idx}",
                    "chapters": vol_chaps,
                }
            )

            vol_name = None
            vol_chaps = []
            vol_idx += 1

        for elem in tree.xpath('//div[@class="episodes"]/*'):
            tag = (elem.tag or "").lower()
            if tag == "h3":
                flush_volume()
                vol_name = elem.text_content().strip()
                continue

            if tag == "div" and "episode" in (elem.get("class") or ""):
                anchors = elem.xpath(".//a")
                if not anchors:
                    # A separator/placeholder div carries no chapter link.
                    continue
                href = (anchors[0].get("href") or "").strip()
                if not href or href.lower().startswith(("javascript:", "#")):
                    continue
                chapter_id = chapter_id_from_href(href)
                if not chapter_id:
                    continue
                title = anchors[0].xpath('string(.//span[@class="title"])').strip()
                vol_chaps.append(
                    {
                        "title": title,
                        "url": resolve_url(tree, href, fallback_base=self.BASE_URL)
                        .split("#", 1)[0]
                        .split("?", 1)[0],
                        "chapterId": chapter_id,
                    }
                )

        flush_volume()

        volumes = [
            {**volume, "chapters": dedupe_chapters(volume["chapters"])}
            for volume in volumes
        ]
        volumes = [volume for volume in volumes if volume["chapters"]]
        if not volumes:
            return None

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
            tree.xpath("//h2[contains(@class,'episode-title')]/text()")
        )

        paragraphs = [
            s for p in tree.xpath('//div[@id="novelBody"]//text()') if (s := p.strip())
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
