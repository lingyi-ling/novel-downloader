#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.yibige.parser
--------------------------------------------

"""

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    dedupe_chapters,
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
class YibigeParser(BaseParser):
    """
    Parser for 一笔阁 book pages.
    """

    site_name: str = "yibige"
    #: Last-resort base for relative media links; the book page's own
    #: ``og:url`` wins when present so the traditional/regional hosts keep
    #: working.
    BASE_URL: str = "https://www.yibige.org/"
    ADS = {
        "首发无广告",
        "请分享",
        "读之阁",
        "小说网",
        "首发地址",
        "手机阅读",
        "一笔阁",
        "site_con_ad",
        "chapter_content",
    }

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 2:
            return None

        # Parse trees
        info_tree = html.fromstring(raw_pages[0])
        catalog_tree = html.fromstring(raw_pages[1])

        # --- From <meta> data ---
        book_name = self._meta(info_tree, "og:novel:book_name") or self._first_str(
            info_tree.xpath("//div[@id='info']/h1/text()")
        )

        author = self._meta(info_tree, "og:novel:author") or self._first_str(
            info_tree.xpath("//div[@id='info']/p[a]/a/text()")
        )

        # The book page's own URL is the most reliable base for the catalogue
        # links: the regional hosts (tw./sg./hk.) share the markup but not the
        # hostname, and ``og:url`` reflects whichever host served the page.
        book_base = (
            self._meta(info_tree, "og:url")
            or self._first_str(info_tree.xpath("//link[@rel='canonical']/@href"))
            or self.BASE_URL
        )

        cover_url = resolve_url(
            info_tree,
            self._meta(info_tree, "og:image")
            or self._first_str(info_tree.xpath("//div[@id='fmimg']//img/@src")),
            fallback_base=book_base,
        )

        update_time = self._meta(info_tree, "og:novel:update_time").replace("T", " ")
        serial_status = self._meta(info_tree, "og:novel:status") or "连载中"

        word_count = self._first_str(
            info_tree.xpath("//div[@id='info']/p[contains(., '字数：')]/text()[1]"),
            replaces=[("字数：", "")],
        )

        # Summary: first paragraph under #intro
        summary = self._first_str(info_tree.xpath("//div[@id='intro']//p[1]/text()"))

        # Category and tags.  ``book:tag`` also carries SEO copies of the book
        # name and author, so those are dropped instead of polluting the tags.
        book_type = self._meta(info_tree, "og:novel:category")
        tags: list[str] = []
        for tag in [book_type, *self._meta_all(info_tree, "book:tag")]:
            tag = tag.strip()
            if not tag or tag in (book_name, author):
                continue
            if tag not in tags:
                tags.append(tag)

        # --- Chapters from the catalog page ---
        # Links appear relative, absolute or protocol relative, sometimes with
        # a query string or fragment; only the ``.html`` chapter pages are
        # real entries (the catalogue also carries book/anchor links such as
        # ``javascript:`` helpers).
        chapters: list[ChapterInfoDict] = []
        for a in catalog_tree.xpath("//div[@id='list']/dl/dd/a"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            if not urlsplit(href).path.endswith(".html"):
                continue
            title = (a.text_content() or "").strip()
            if not title:
                continue
            # /6238/2496.html?from=catalog -> 2496
            chap_id = chapter_id_from_href(href)
            if not chap_id:
                continue
            resolved = resolve_url(catalog_tree, href, fallback_base=book_base)
            parts = urlsplit(resolved)
            chapters.append(
                {
                    "title": title,
                    # Canonical link: the query string / fragment of the
                    # catalogue entry carries no chapter information.
                    "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                    "chapterId": chap_id,
                }
            )
        # Some templates repeat the latest chapter in a second block; the
        # downloader keys chapters by id, so keep the first occurrence only.
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
            "word_count": word_count,
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
        tree = html.fromstring(raw_pages[0])

        title = self._first_str(tree.xpath("//div[@class='bookname']/h1/text()"))

        paragraphs = [
            txt
            for p in tree.xpath("//div[@id='content']//p")
            if (txt := self._norm_space(p.text_content())) and not self._is_ad(txt)
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

    def _is_ad(self, s: str) -> bool:
        """
        Filter for footer junk inside #content.
        """
        if self._is_ad_line(s):
            return True

        ss = s.replace(" ", "")
        # return any(b in s or b in ss for b in self.ADS)
        return self._is_ad_line(ss)

    @classmethod
    def _meta(cls, tree: html.HtmlElement, prop: str) -> str:
        """
        Get a single meta property content
        """
        return cls._first_str(tree.xpath(f"//meta[@property='{prop}']/@content"))

    @staticmethod
    def _meta_all(tree: html.HtmlElement, prop: str) -> list[str]:
        """
        Get all meta property content values
        """
        return tree.xpath(f"//meta[@property='{prop}']/@content") or []
