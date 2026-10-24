#!/usr/bin/env python3
"""
novel_downloader.plugins.common.parser.biquge4
----------------------------------------------
"""

import re
from html import unescape
from typing import Any

from lxml import etree, html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
    chapter_id_from_href,
    dedupe_chapters,
    resolve_url,
    strip_page_marker,
)
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)


class Biquge4Parser(BaseParser):
    """
    Base Parser for biquge-related book pages.
    """

    ADS = {
        "关闭小说畅读模式体验更好",
        "内容未完，下一页继续阅读",
        "本章阅读完毕，更多请搜索",
    }
    BASE_URL: str = ""

    NOVELCONTENT_RE = re.compile(
        r'<div[^>]*id=["\']novelcontent["\'][^>]*>(.*?)</div>',
        re.S | re.I,
    )
    TITLE_RE = re.compile(
        r"<h1[^>]*>(.*?)</h1>",
        re.S | re.I,
    )
    TAG_RE = re.compile(r"<[^>]+>")
    P_BR_RE = re.compile(r"</?p\s*[^>]*>|<br\s*/?>", re.I)

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        tree = html.fromstring(raw_pages[0])

        # --- Basic meta extraction ---
        book_name = self._first_str(
            tree.xpath('//meta[@property="og:novel:book_name"]/@content')
        )
        if not book_name:
            book_name = self._first_str(
                tree.xpath('//div[contains(@class,"catalog1")]//h1//text()')
            )

        author = self._first_str(
            tree.xpath('//meta[@property="og:novel:author"]/@content')
        )
        if not author:
            author = self._first_str(
                tree.xpath(
                    '//div[contains(@class,"catalog1")]//p[contains(@class,"p1")]//text()'
                ),
                replaces=[("作者：", "")],
            )

        cover_url = self._first_str(tree.xpath('//meta[@property="og:image"]/@content'))
        if not cover_url:
            cover_url = self._first_str(
                tree.xpath('//div[contains(@class,"tu")]/img/@src')
            )
        cover_url = resolve_url(
            tree,
            cover_url,
            fallback_base=self.BASE_URL,
        )

        update_time = self._first_str(
            tree.xpath('//meta[@property="og:novel:update_time"]/@content')
        )
        if not update_time:
            update_time = self._first_str(
                tree.xpath(
                    '//div[contains(@class,"catalog1")]'
                    '//p[contains(@class,"p2") and contains(text(),"更新")]//text()'
                ),
                replaces=[("更新：", "")],
            )

        serial_status = self._first_str(
            tree.xpath('//meta[@property="og:novel:status"]/@content')
        )
        category = self._first_str(
            tree.xpath('//meta[@property="og:novel:category"]/@content')
        )
        tags = [category] if category else []

        summary = self._first_str(
            tree.xpath('//meta[@property="og:description"]/@content')
        )
        if not summary:
            summary = self._join_strs(
                tree.xpath('//div[contains(@class,"jj")]//text()')
            )
            summary = summary.split("最后观看小说就不会弹出畅读模式了。", 1)[-1]
            summary = summary.rsplit("最新章节推荐地址", 1)[0].strip()

        # --- Chapter list ---
        chapters: list[ChapterInfoDict] = []
        # Find the section that has “全部章节”
        for elem in tree.xpath('//div[contains(@class,"info_chapters")]/*'):
            tag = (elem.tag or "").lower()
            e_cls = (elem.get("class") or "").lower()
            if tag != "ul" or "p2" not in e_cls:
                continue
            previous = elem.getprevious()
            if previous is None or "全部章节" not in (previous.text_content() or ""):
                continue
            chapters.extend(self._chapters_from_element(elem))

        if not chapters:
            # fallback: take all <a> in info_chapters
            for elem in tree.xpath('//div[contains(@class,"info_chapters")]'):
                chapters.extend(self._chapters_from_element(elem))

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
            "summary": summary,
            "tags": tags,
            "volumes": volumes,
            "extra": {},
        }

    def _chapters_from_element(self, elem: Any) -> list[ChapterInfoDict]:
        """Build chapter entries from every link inside ``elem``."""
        chapters: list[ChapterInfoDict] = []
        for a in elem.xpath(".//a[@href]"):
            href = (a.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "#")):
                continue
            title = (a.text_content() or "").strip()
            if not title:
                title = (a.get("title") or "").strip()
            chapters.append(
                {
                    "title": title,
                    "url": href,
                    "chapterId": chapter_id_from_href(href),
                }
            )
        return chapters

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        """
        Extract the content of ``<div id="novelcontent">``.

        The markup inside that block is frequently malformed (unclosed
        ``<tr>``/``<td>``/``<table>`` tags).  ``lxml`` still recovers the text
        reliably, so it is tried first: unlike a non-greedy ``</div>`` regex it
        keeps the paragraphs that follow a nested advertisement block.  The
        regex implementation remains as a fallback for pages that ``lxml``
        cannot parse at all.
        """
        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        title = ""
        paragraphs: list[str] = []

        for curr_html in raw_pages:
            if not title:
                title = self._extract_title(curr_html)

            paragraphs.extend(self._extract_paragraphs(curr_html))

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }

    def _extract_title(self, raw_html: str) -> str:
        titles = self.TITLE_RE.findall(raw_html)
        if not titles:
            return ""
        raw_title = unescape(self.TAG_RE.sub("", titles[-1]))
        return strip_page_marker(raw_title).strip()

    def _extract_paragraphs(self, raw_html: str) -> list[str]:
        try:
            tree = html.fromstring(raw_html)
        except (ValueError, etree.ParserError):
            return self._extract_paragraphs_regex(raw_html)

        nodes = tree.xpath('//div[@id="novelcontent"]')
        if not nodes:
            return self._extract_paragraphs_regex(raw_html)

        elem = nodes[0]
        etree.strip_elements(elem, "script", "style", with_tail=False)
        for node in elem.iter():
            tag = node.tag.lower() if isinstance(node.tag, str) else ""
            if tag not in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
                continue
            if node.tail:
                node.tail = "\n" + node.tail
            if tag != "br":
                node.text = "\n" + (node.text or "")

        return self._lines_to_paragraphs(elem.text_content())

    def _extract_paragraphs_regex(self, raw_html: str) -> list[str]:
        match = self.NOVELCONTENT_RE.search(raw_html)
        if not match:
            return []
        text = match.group(1).replace("&nbsp;", " ")
        text = self.P_BR_RE.sub("\n", text)  # p/br -> newline
        text = self.TAG_RE.sub("", text)  # strip other tags
        return self._lines_to_paragraphs(unescape(text))

    def _lines_to_paragraphs(self, text: str) -> list[str]:
        paragraphs: list[str] = []
        for line in text.splitlines():
            line = line.replace("\xa0", " ").strip()
            if not line or self._is_ad_line(line):
                continue
            paragraphs.append(line)
        return paragraphs
