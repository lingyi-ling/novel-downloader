#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.novelpia.parser
----------------------------------------------
"""

import json
import re
from typing import Any

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.common.parser._utils import (
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
class NovelpiaParser(BaseParser):
    """
    Parser for ノベルピア book pages.
    """

    site_name: str = "novelpia"
    BASE_URL = "https://novelpia.jp"

    IMG_SRC_PATTERN = re.compile(r'src="([^"]+)"')

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 2 or not raw_pages[0].strip():
            return None

        try:
            info = json.loads(raw_pages[0])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(info, dict):
            return None
        novel = info.get("novel")
        if not isinstance(novel, dict) or not novel:
            return None

        # --- base info ---
        book_name = str(novel.get("novel_name", "")).strip()
        author = str(novel.get("writer_nick", "")).strip()
        cover_url = str(novel.get("cover_img") or novel.get("novel_img_all") or "")
        cover_url = resolve_url(None, cover_url, fallback_base=self.BASE_URL)

        update_time = str(
            novel.get("last_write_date") or novel.get("status_date") or ""
        )
        raw_tags = novel.get("novel_genre_arr") or []
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []

        summary = str(novel.get("novel_story", "") or "")
        summary = summary.replace("\r", "").replace("\n", "")
        summary = summary.replace("<br />", "\n").replace("<br>", "\n")

        # --- Chapter volumes & listings ---
        chapters: list[ChapterInfoDict] = []
        for curr_html in raw_pages[1:]:
            t = parse_html_tree(curr_html)
            if t is None:
                continue
            rows = t.xpath("//tr[contains(@class,'ep_style5')]")
            for r in rows:
                chapter_id = r.xpath(
                    ".//td[contains(@class,'font12')]/@data-content-no"
                )
                if not chapter_id:
                    continue
                cid = str(chapter_id[0]).strip()
                if not cid:
                    continue

                b_elems = r.xpath(".//td[contains(@class,'font12')]//b")
                if not b_elems:
                    continue
                title = "".join(b_elems[0].itertext()).strip()
                title = self._norm_space(title.replace("無料", ""))

                url = f"{self.BASE_URL}/viewer/{cid}"
                chapters.append(
                    {
                        "chapterId": cid,
                        "title": title,
                        "url": url,
                    }
                )

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
            "extra": {"novel_id": novel.get("novel_no")},
        }

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        if not raw_pages:
            return None

        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        try:
            data = json.loads(raw_pages[0])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        s = data.get("s")
        if not isinstance(s, list):
            return None

        html_fragments: list[str] = []
        for p in s:
            if not isinstance(p, dict):
                continue
            text = str(p.get("text") or "").strip()
            if not text or "<div class='cover-wrapper'>" in text:
                continue
            html_fragments.append(f"<p>{text}</p>")

        html_doc = "<root>" + "".join(html_fragments) + "</root>"
        doc = html.fromstring(html_doc)

        paragraphs: list[str] = []
        resources: list[MediaResource] = []
        curr_paragraph_idx = 0

        for p_elem in doc.xpath(".//p"):
            # ---- collect images ----
            for src in p_elem.xpath(".//img/@src"):
                src = src.strip()
                if not src:
                    continue
                if src.startswith("//"):
                    src = "https:" + src

                resources.append(
                    {
                        "type": "image",
                        "paragraph_index": curr_paragraph_idx,
                        "url": src,
                    }
                )

            for ruby in p_elem.xpath(".//ruby"):
                base = "".join(ruby.xpath(".//text()[not(parent::rt)]")).strip()
                rt_text = "".join(ruby.xpath(".//rt/text()")).strip()
                tail = ruby.tail or ""
                replacement_text = (
                    f"{base}({rt_text}){tail}" if rt_text else f"{base}{tail}"
                )

                parent = ruby.getparent()
                if parent is not None:
                    pos = parent.index(ruby)
                    parent.remove(ruby)
                    if pos == 0:
                        parent.text = (parent.text or "") + replacement_text
                    else:
                        prev = parent[pos - 1]
                        prev.tail = (prev.tail or "") + replacement_text

            text_content = p_elem.text_content().strip()
            if text_content:
                paragraphs.append(text_content)
                curr_paragraph_idx += 1

        if not (paragraphs or resources):
            return None

        content = "\n".join(paragraphs)

        return {
            "id": chapter_id,
            "title": "",
            "content": content,
            "extra": {
                "site": self.site_name,
                "resources": resources,
            },
        }
