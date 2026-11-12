#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.kadokado.parser
----------------------------------------------
"""

import json
from typing import Any

from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    MediaResource,
    VolumeInfoDict,
)


@registrar.register_parser()
class KadokadoParser(BaseParser):
    """
    Parser for KadoKado book pages.
    """

    site_name: str = "kadokado"
    BASE_URL = "https://www.kadokado.com.tw"

    @staticmethod
    def _load_json(raw: str) -> Any:
        """
        Decode an API page, returning ``None`` for a blocked/error body.

        KadoKado's JSON endpoints can answer with an HTML WAF/rate-limit page
        instead of JSON; that must degrade to "no data" rather than raise.
        """
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 2 or not raw_pages[0].strip() or not raw_pages[1].strip():
            return None

        info = self._load_json(raw_pages[0])
        catalog = self._load_json(raw_pages[1])
        if not isinstance(info, dict) or not isinstance(catalog, list):
            return None

        book_name = info.get("displayName", "")
        author = info.get("ownerDisplayName", "")
        summary = info.get("logline", "") or info.get("oneLineIntro", "")
        raw_tags = info.get("tags") or []
        tags = (
            [t for t in raw_tags if isinstance(t, str)]
            if isinstance(raw_tags, list)
            else []
        )
        cover_urls = info.get("coverUrls") or []
        cover_url = cover_urls[0] if isinstance(cover_urls, list) and cover_urls else ""
        word_count = str(info.get("wordCount", "")) if info.get("wordCount") else ""
        update_time = ""

        volumes: list[VolumeInfoDict] = []

        for idx, vol in enumerate(catalog, start=1):
            if not isinstance(vol, dict):
                continue
            vol_name = vol.get("collectionDisplayName") or f"未命名卷 {idx}"
            chapters_data = vol.get("chapters") or []
            if not isinstance(chapters_data, list):
                continue
            vol_chaps: list[ChapterInfoDict] = []

            for ch in chapters_data:
                if not isinstance(ch, dict):
                    continue
                chap_id = str(ch.get("chapterId") or "").strip()
                if not chap_id:
                    continue
                title = str(ch.get("chapterDisplayName") or "").strip()
                accessible = bool(ch.get("isFree") or ch.get("isPurchased"))

                vol_chaps.append(
                    {
                        "title": title,
                        "url": f"{self.BASE_URL}/chapter/{chap_id}",
                        "chapterId": chap_id,
                        "accessible": accessible,
                    }
                )

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
            "update_time": update_time,
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
        if len(raw_pages) < 2 or not raw_pages[0].strip() or not raw_pages[1].strip():
            return None

        info = self._load_json(raw_pages[0])
        content_resp = self._load_json(raw_pages[1])
        if not isinstance(info, dict) or not isinstance(content_resp, dict):
            return None
        raw_content = content_resp.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            return None

        tree = html.fromstring(raw_content)

        raw_title = info.get("chapterDisplayName") or ""
        title = raw_title if isinstance(raw_title, str) else str(raw_title)

        paragraphs: list[str] = []
        resources: list[MediaResource] = []
        curr_paragraph_idx = 0

        for elem in tree.iter():
            tag = str(elem.tag).lower() if elem.tag is not None else ""

            if tag == "p":
                text = elem.text_content().strip()
                if text:
                    paragraphs.append(text)
                    curr_paragraph_idx += 1

            elif tag == "img":
                src = elem.get("src")
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    resources.append(
                        {
                            "type": "image",
                            "paragraph_index": curr_paragraph_idx,
                            "url": src,
                        }
                    )

        # After content
        extra_imgs = content_resp.get("imageUrls") or []
        if not isinstance(extra_imgs, list):
            extra_imgs = []
        for url in extra_imgs:
            if isinstance(url, str) and url.strip():
                url = url.strip()
                if url.startswith("//"):
                    url = "https:" + url
                resources.append(
                    {
                        "type": "image",
                        "paragraph_index": curr_paragraph_idx,
                        "url": url,
                    }
                )

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
