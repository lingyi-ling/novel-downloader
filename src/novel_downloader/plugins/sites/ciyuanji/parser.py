#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.ciyuanji.parser
----------------------------------------------
"""

import json
import logging
from base64 import b64decode
from collections import defaultdict
from contextlib import suppress
from typing import Any

from Crypto.Cipher import DES
from Crypto.Util.Padding import unpad
from lxml import html

from novel_downloader.plugins.base.parser import BaseParser
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookInfoDict,
    ChapterDict,
    ChapterInfoDict,
    VolumeInfoDict,
)

logger = logging.getLogger(__name__)


@registrar.register_parser()
class CiyuanjiParser(BaseParser):
    """
    Parser for 次元姬 book pages.
    """

    site_name: str = "ciyuanji"
    BASE_URL = "https://www.ciyuanji.com"
    _CHAPTER_KEY = b"ZUreQN0E"

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if not raw_pages:
            return None

        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            return None

        data = self._find_next_data(raw_pages[0])
        if not data:
            logger.warning("ciyuanji book_info: __NEXT_DATA__ not found")
            return None

        book_data = self._extract_book_data(data)
        raw_chapters = self._extract_chapter_list(data)
        volumes = self._build_volumes(raw_chapters)
        tag_list = book_data.get("tagList") or []
        tags = [
            t["tagName"] for t in tag_list if isinstance(t, dict) and t.get("tagName")
        ]

        if not volumes:
            return None

        return {
            "book_name": book_data.get("bookName") or "",
            "author": book_data.get("authorName") or "",
            "cover_url": book_data.get("imgUrl") or "",
            "update_time": book_data.get("latestUpdateTime") or "",
            "word_count": str(book_data.get("wordCount") or ""),
            "serial_status": self._map_serial_status(book_data.get("endState")),
            "summary": book_data.get("notes") or "",
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
            logger.warning("ciyuanji chapter %s: raw_pages is empty", chapter_id)
            return None

        raw_pages = self._usable_pages(raw_pages)
        if not raw_pages:
            logger.warning("ciyuanji chapter %s: raw page is blank", chapter_id)
            return None

        if self._check_login(raw_pages[0]):
            logger.warning("ciyuanji chapter %s: VIP login required", chapter_id)
            return None

        if self._check_unlock(raw_pages[0]):
            logger.warning("ciyuanji chapter %s: locked (need purchase)", chapter_id)
            return None

        data = self._find_next_data(raw_pages[0])
        if not data:
            logger.warning("ciyuanji chapter %s: __NEXT_DATA__ not found", chapter_id)
            return None

        chapter_content = self._extract_chapter_content(data)
        if not chapter_content:
            logger.warning("ciyuanji chapter %s: chapterContent not found", chapter_id)
            return None

        enc_content = chapter_content.get("content", "")
        if not enc_content:
            logger.warning("ciyuanji chapter %s: no encrypted content", chapter_id)
            return None

        # Build result
        return {
            "id": str(chapter_content.get("chapterId") or chapter_id),
            "title": chapter_content.get("chapterName") or "",
            "content": self._decrypt_chapter(enc_content),
            "extra": {
                "site": self.site_name,
                "wordCount": chapter_content.get("wordCount"),
                "raw_content": enc_content,
                "latestUpdateTime": chapter_content.get("latestUpdateTime"),
                "resources": self._build_resources(chapter_content),
            },
        }

    def _build_volumes(
        self, chapter_list: list[dict[str, Any]]
    ) -> list[VolumeInfoDict]:
        """
        Build structured volumes + chapters from raw chapter list.
        Groups by volumeId, sorts volumes and chapters.
        """
        if not chapter_list:
            return []

        grouped: dict[int, dict[str, Any]] = defaultdict(lambda: {"chapters": []})

        for ch in chapter_list:
            if not isinstance(ch, dict):
                continue
            volume_id = ch.get("volumeId")
            if not volume_id:
                continue
            volume_title = ch.get("title") or f"卷 {volume_id}"
            volume_sort = ch.get("volumeSortNum")
            if not isinstance(volume_sort, (int, float)):
                volume_sort = 0
            sort_num = ch.get("sortNum")
            if not isinstance(sort_num, (int, float)):
                sort_num = 0

            # Build individual chapter entry
            chapter_info: ChapterInfoDict = {
                "title": ch.get("chapterName") or "",
                "url": f"/chapter/{ch.get('bookId')}_{ch.get('chapterId')}.html",
                "chapterId": str(ch.get("chapterId") or ""),
                "accessible": ch.get("isFee", "0") == "0"
                or ch.get("isBuy", "0") == "1",
            }

            vol = grouped[volume_id]
            vol["volume_name"] = volume_title
            vol["volumeSortNum"] = volume_sort
            vol["chapters"].append((sort_num, chapter_info))

        # Sort volumes by volumeSortNum
        sorted_volumes = sorted(
            grouped.values(), key=lambda v: v.get("volumeSortNum", 0)
        )

        # Build final structured list
        volumes: list[VolumeInfoDict] = []
        for vol in sorted_volumes:
            chapters_sorted = [
                ci for _, ci in sorted(vol["chapters"], key=lambda x: x[0])
            ]
            volumes.append(
                {
                    "volume_name": vol["volume_name"],
                    "chapters": chapters_sorted,
                }
            )

        return volumes

    @staticmethod
    def _map_serial_status(status: str | int | None) -> str:
        """
        Map endState value to human-readable Chinese status.
        """
        if status is None:
            return ""
        status_str = str(status)
        mapping = {
            "1": "完结",
            "2": "连载",
        }
        return mapping.get(status_str, status_str)

    @staticmethod
    def _find_next_data(html_str: str) -> dict[str, Any]:
        """
        Extract SSR JSON from <script id="__NEXT_DATA__">.
        """
        tree = html.fromstring(html_str)
        script = tree.xpath('//script[@id="__NEXT_DATA__"]/text()')
        if not script:
            return {}
        try:
            data = json.loads(script[0].strip())
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_book_data(data: dict[str, Any]) -> dict[str, Any]:
        props = data.get("props") or {}
        if not isinstance(props, dict):
            return {}
        page_props = props.get("pageProps") or {}
        if not isinstance(page_props, dict):
            return {}
        book_data = page_props.get("book") or {}
        return book_data if isinstance(book_data, dict) else {}

    @staticmethod
    def _extract_chapter_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        props = data.get("props") or {}
        if not isinstance(props, dict):
            return []
        page_props = props.get("pageProps") or {}
        if not isinstance(page_props, dict):
            return []
        book_chapter = page_props.get("bookChapter") or {}
        if not isinstance(book_chapter, dict):
            return []
        chapter_list = book_chapter.get("chapterList") or []
        return chapter_list if isinstance(chapter_list, list) else []

    @staticmethod
    def _extract_chapter_content(data: dict[str, Any]) -> dict[str, Any]:
        props = data.get("props") or {}
        if not isinstance(props, dict):
            return {}
        page_props = props.get("pageProps") or {}
        if not isinstance(page_props, dict):
            return {}
        chapter_content = page_props.get("chapterContent") or {}
        return chapter_content if isinstance(chapter_content, dict) else {}

    @staticmethod
    def _check_login(html_str: str) -> bool:
        keywords = {
            "其他登录方式",
            "注册&amp;登录",
        }
        return any(kw in html_str for kw in keywords)

    @staticmethod
    def _check_unlock(html_str: str) -> bool:
        return "需订阅后才能阅读" in html_str

    @classmethod
    def _decrypt_chapter(cls, content: str) -> str:
        content = content.replace("\n", "")
        ciphertext = b64decode(content)
        cipher = DES.new(cls._CHAPTER_KEY, DES.MODE_ECB)
        plaintext: str = unpad(cipher.decrypt(ciphertext), 8).decode("utf-8")
        return plaintext

    @staticmethod
    def _build_resources(chapter_content: dict[str, Any]) -> list[dict[str, Any]]:
        """Build unified resources list from Ciyuanji chapter_content."""
        resources: list[dict[str, Any]] = []

        for img in chapter_content.get("imgList") or []:
            if not isinstance(img, dict):
                continue
            pidx = img.get("paragraphIndex")
            url = img.get("imgUrl")

            if pidx is None or not url:
                continue
            try:
                pidx_int = int(pidx)
            except (TypeError, ValueError):
                continue

            entry: dict[str, Any] = {
                "type": "image",
                "paragraph_index": pidx_int,
                "url": url,
            }

            # Optional fields
            if img.get("width"):
                with suppress(TypeError, ValueError):
                    entry["width"] = int(img["width"])
            if img.get("height"):
                with suppress(TypeError, ValueError):
                    entry["height"] = int(img["height"])
            if img.get("imgName"):
                entry["alt"] = img["imgName"]

            resources.append(entry)

        for w in chapter_content.get("wordList") or []:
            if not isinstance(w, dict):
                continue
            pidx = w.get("paragraphIndex")
            img_url = w.get("imgUrl")
            text_alt = w.get("content")

            if pidx is None or not img_url:
                continue
            try:
                pidx_int = int(pidx)
            except (TypeError, ValueError):
                continue

            entry = {
                "type": "image",
                "paragraph_index": pidx_int,
                "url": img_url,
            }

            if text_alt:
                entry["alt"] = text_alt

            resources.append(entry)

        resources.sort(key=lambda r: r.get("paragraph_index", 0))
        return resources
