#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.guidaye.parser
---------------------------------------------

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
    VolumeInfoDict,
)


@registrar.register_parser()
class GuidayeParser(BaseParser):
    """
    Parser for 名著阅读 book pages.
    """

    site_name: str = "guidaye"
    BASE_URL = "https://b.guidaye.com"

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

        # Book metadata
        book_name = self._first_str(
            tree.xpath('//meta[@property="og:novel:book_name"]/@content')
        )
        author = self._first_str(
            tree.xpath('//meta[@property="og:novel:author"]/@content')
        )
        cover_url = resolve_url(
            tree,
            self._first_str(tree.xpath('//meta[@property="og:image"]/@content')),
            fallback_base=self.BASE_URL,
        )
        update_time = self._first_str(
            tree.xpath('//meta[@property="og:novel:update_time"]/@content')
        )
        category = self._first_str(
            tree.xpath('//meta[@property="og:novel:category"]/@content')
        )
        summary = self._first_str(
            tree.xpath('//meta[@property="og:description"]/@content')
        )
        tags = [category] if category else []

        # Chapter volumes & listings
        chapters: list[ChapterInfoDict] = []
        for a in tree.xpath('//ol[@id="list-ol"]//a'):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            title = self._first_str(a.xpath(".//text()"))
            chapter_id = chapter_id_from_href(href)
            chapters.append(
                ChapterInfoDict(
                    title=title,
                    url=href,
                    chapterId=chapter_id,
                )
            )

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [
            VolumeInfoDict(volume_name="正文", chapters=chapters)
        ]

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
        article = tree.xpath('//article[@class="article-post"]')
        if not article:
            return None
        article = article[0]

        # Title from entry-title
        title = self._first_str(
            tree.xpath(
                '//h1[contains(@class,"secondfont") or @class="entry-title"]/text()'
            )
        )

        # One paragraph per <p>; keep inline markup (e.g. <strong>) in place.
        paragraphs = [
            text
            for p in article.xpath(".//p")
            if (text := self._norm_space(p.text_content() or ""))
        ]

        if not paragraphs:
            # Some pages keep the body as bare text separated by <br>.
            paragraphs = [
                text
                for node in article.xpath(
                    ".//text()[not(ancestor::script) and not(ancestor::style) "
                    "and not(ancestor::h1) and not(ancestor::h2)]"
                )
                if (text := self._norm_space(node))
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
