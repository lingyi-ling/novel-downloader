#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.wenku8.parser
--------------------------------------------
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
    MediaResource,
    VolumeInfoDict,
)


@registrar.register_parser()
class Wenku8Parser(BaseParser):
    """
    Parser for 轻小说文库 book-info pages.
    """

    site_name: str = "wenku8"
    BASE_URL = "https://www.wenku8.net"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if len(raw_pages) < 2:
            return None

        info_tree = parse_html_tree(raw_pages[0])
        catalog_tree = parse_html_tree(raw_pages[1])
        if info_tree is None or catalog_tree is None:
            return None

        # --- Metadata ---
        book_name = self._first_str(info_tree.xpath("//table//b/text()"))
        author = self._first_str(
            info_tree.xpath('//td[contains(text(),"小说作者")]/text()'),
            replaces=[("小说作者：", "")],
        )
        cover_url = resolve_url(
            info_tree,
            self._first_str(info_tree.xpath('//img[contains(@src,"/image/")]/@src')),
            fallback_base=self.BASE_URL,
        )

        serial_status = self._first_str(
            info_tree.xpath('//td[contains(text(),"文章状态")]/text()'),
            replaces=[("文章状态：", "")],
        )
        word_count = self._first_str(
            info_tree.xpath('//td[contains(text(),"全文长度")]/text()'),
            replaces=[("全文长度：", "")],
        )
        update_time = self._first_str(
            info_tree.xpath('//td[contains(text(),"最后更新")]/text()'),
            replaces=[("最后更新：", "")],
        )

        tags_text = self._first_str(
            info_tree.xpath('//span[contains(text(),"Tags")]/b/text()')
        )
        if tags_text:
            tags_text = tags_text.replace("作品Tags：", "").replace("　", " ")
            tags: list[str] = [t.strip() for t in tags_text.split() if t.strip()]
        else:
            tags = []

        summary = self._join_strs(
            info_tree.xpath(
                '//span[contains(text(),"内容简介")]/following-sibling::span[1]//text()'
            )
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
            vol_idx += 1
            vol_name = None
            vol_chaps = []

        # Parse catalog table
        for elem in catalog_tree.xpath('//table[@class="css"]//tr'):
            td_v = elem.xpath('./td[@class="vcss"]/text()')
            if td_v:
                # new volume
                flush_volume()
                vol_name = td_v[0].strip()
                continue

            for a in elem.xpath('.//td[@class="ccss"]/a'):
                href = a.get("href", "").strip()
                if not href or href.lower().startswith(("javascript:", "#")):
                    continue
                title = a.text_content().strip()
                chapter_id = chapter_id_from_href(href)
                vol_chaps.append(
                    {
                        "title": title,
                        "url": href,
                        "chapterId": chapter_id,
                    }
                )

        flush_volume()

        if not volumes:
            return None

        return {
            "book_name": book_name,
            "author": author,
            "cover_url": cover_url,
            "update_time": update_time,
            "word_count": word_count,
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
        title = self._first_str(tree.xpath('//div[@id="title"]/text()'))

        paragraphs: list[str] = []
        resources: list[MediaResource] = []
        curr_paragraph_idx = 0

        # Iterate through direct children of content div
        for elem in tree.xpath('//div[@id="content"]/*'):
            tag = (elem.tag or "").lower()

            # Skip site footers or ads
            if tag == "ul" and elem.get("id") == "contentdp":
                # include tail (might have text after ul)
                if tail := (elem.tail or "").strip():
                    paragraphs.append(tail)
                    curr_paragraph_idx += 1
                continue

            elif tag == "div" and "divimage" in (elem.get("class") or ""):
                # Collect all image links
                urls = elem.xpath(".//a/@href") or elem.xpath(".//img/@src")
                for src in urls:
                    src = src.strip()
                    if not src:
                        continue
                    # Images are served from the chapter's origin; a bare
                    # ``../image/x.jpg`` would otherwise stay relative and be
                    # unusable for the downloader.
                    src = resolve_url(None, src, fallback_base=self.BASE_URL)
                    resources.append(
                        {
                            "type": "image",
                            "paragraph_index": curr_paragraph_idx,
                            "url": src,
                        }
                    )
                if tail := (elem.tail or "").strip():
                    paragraphs.append(tail)
                    curr_paragraph_idx += 1
                continue

            elif tag == "br":
                if tail := (elem.tail or "").strip():
                    paragraphs.append(tail)
                    curr_paragraph_idx += 1
                continue

            else:
                if text := elem.text_content().strip():
                    paragraphs.append(text)
                    curr_paragraph_idx += 1
                if tail := (elem.tail or "").strip():
                    paragraphs.append(tail)
                    curr_paragraph_idx += 1

        if not (paragraphs or resources):
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {
                "site": self.site_name,
                "resources": resources,
            },
        }
