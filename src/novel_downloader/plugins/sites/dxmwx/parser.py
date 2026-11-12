#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.dxmwx.parser
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
class DxmwxParser(BaseParser):
    """
    Parser for 大熊猫文学网 book pages.
    """

    site_name: str = "dxmwx"
    BASE_URL: str = "https://www.dxmwx.org"
    ADS = {"大熊猫文学", "点这里听书"}

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

        book_name = self._first_str(
            info_tree.xpath("//span[contains(@style,'font-size: 24px')]/text()")
        )
        author = self._first_str(
            info_tree.xpath(
                # The author row is the only 28px-high div with a link.  A
                # single-CJK-char ``contains(., '著')`` is unreliable in
                # libxml2 (``contains(., '作者')`` works), so match on style.
                "//div[contains(@style,'height: 28px')]//a/text()"
            )
        )
        tags = [
            t.strip()
            for t in info_tree.xpath("//span[@class='typebut']//a/text()")
            if t.strip()
        ]
        cover_rel = self._first_str(info_tree.xpath("//img[@class='imgwidth']/@src"))
        cover_url = resolve_url(info_tree, cover_rel, fallback_base=self.BASE_URL)

        update_time = self._join_strs(
            info_tree.xpath(
                "//span[starts-with(normalize-space(.), '更新时间：')]/text()"
            ),  # noqa: E501
            replaces=[("更新时间：", "")],
        )
        nodes = info_tree.xpath("(//div[contains(@style,'border-bottom')])[1]/div")
        summary = ""
        if nodes:
            node = nodes[0]
            lines: list[str] = []

            if node.text:
                txt = self._norm_space(node.text)
                if txt:
                    lines.append(txt)

            for child in node.iterchildren():
                if child.tag.lower() == "p" and child.text:
                    txt = self._norm_space(child.text)
                    if txt:
                        lines.append(txt)
                if child.tail:
                    txt = self._norm_space(child.tail)
                    if txt:
                        lines.append(txt)

            summary = "\n".join(lines)

        chapters: list[ChapterInfoDict] = []
        for a in catalog_tree.xpath(
            "//div[contains(@style,'height:40px') and contains(@style,'border-bottom')]//a"  # noqa: E501
        ):
            href = (a.get("href") or "").strip()
            title = (a.text_content() or "").strip()
            if not href or not title or "/read/" not in href:
                continue
            # "/read/57215_50197663.html" -> "50197663"; the fetcher rebuilds
            # the URL as ``read/{book_id}_{chapter_id}.html``.
            chap_id = chapter_id_from_href(href).rsplit("_", 1)[-1]
            if not chap_id:
                continue
            chapters.append(
                {
                    "title": title,
                    "url": resolve_url(catalog_tree, href, fallback_base=self.BASE_URL)
                    .split("#", 1)[0]
                    .split("?", 1)[0],
                    "chapterId": chap_id,
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

        title = self._norm_space(
            self._first_str(tree.xpath("//h1[@id='ChapterTitle']/text()"))
        )

        paragraphs: list[str] = []
        for p in tree.xpath("//div[@id='Lab_Contents']//p"):
            text = self._norm_space(p.text_content() or "")
            if not text:
                continue
            if self._is_ad_line(text):
                continue
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
