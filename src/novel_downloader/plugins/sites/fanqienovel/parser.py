#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.parser
-------------------------------------------------
"""

import json
import logging
import re
from typing import Any

from lxml import html

from novel_downloader.infra.paths import FANQIENOVEL_MAP_PATH
from novel_downloader.libs.mini_js import MiniJS
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
class FanqienovelParser(BaseParser):
    """
    Parser for 番茄小说网 book pages.
    """

    site_name: str = "fanqienovel"

    _FONT_MAP: dict[str, str] = {}
    _RE_INITIAL_STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*({.*});", re.S)

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if not raw_pages:
            return None

        data = self._extract_initial_state(raw_pages[0])
        page = data.get("page") or {}
        if not isinstance(page, dict):
            return None

        book_name = page.get("bookName") or ""
        author = page.get("authorName") or page.get("author") or ""
        cover_url = page.get("thumbUrl") or page.get("thumbUri") or ""
        update_time = page.get("lastPublishTime") or ""
        summary = page.get("abstract") or page.get("description") or ""

        tags: list[str] = []
        category_str = page.get("categoryV2")
        if isinstance(category_str, str):
            try:
                category_data = json.loads(category_str)
                tags = [c.get("Name", "") for c in category_data if c.get("Name")]
            except Exception:
                pass

        word_count = str(page.get("wordNumber") or "")
        serial_status = "完结" if page.get("creationStatus") == 2 else "连载"

        # --- Volumes and chapters ---
        volumes: list[VolumeInfoDict] = []
        volume_names: list[str] = page.get("volumeNameList") or []
        chapter_groups: list[list[dict[str, Any]]] = (
            page.get("chapterListWithVolume") or []
        )

        for i, chapter_list in enumerate(chapter_groups):
            if not isinstance(chapter_list, list):
                continue

            if i < len(volume_names):
                volume_name: str = str(volume_names[i])
            elif chapter_list:
                volume_name = str(chapter_list[0].get("volume_name") or f"卷 {i + 1}")
            else:
                volume_name = f"卷 {i + 1}"

            def _sort_key(ch_item: dict[str, Any]) -> Any:
                v = ch_item.get("realChapterOrder") or ch_item.get("itemId") or "0"
                try:
                    return int(v)
                except Exception:
                    return str(v or "")

            chapter_list.sort(key=_sort_key)

            chapters: list[ChapterInfoDict] = []
            for ch in chapter_list:
                chapter_id = str(ch.get("itemId") or "")
                title = ch.get("title") or ""
                accessible = not bool(ch.get("isChapterLock"))
                chapters.append(
                    {
                        "title": title,
                        "url": f"https://fanqienovel.com/reader/{chapter_id}",
                        "chapterId": chapter_id,
                        "accessible": accessible,
                    }
                )

            volumes.append(
                {
                    "volume_name": volume_name,
                    "chapters": chapters,
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
            "serial_status": serial_status,
            "summary": summary,
            "tags": tags,
            "volumes": volumes,
            "extra": {
                "bookId": page.get("bookId"),
                "authorId": page.get("authorId"),
            },
        }

    def parse_chapter_content(
        self,
        raw_pages: list[str],
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        if not raw_pages:
            return None

        html_str = raw_pages[0]
        self._check_font(html_str)

        data = self._extract_initial_state(html_str)
        chapter_data = self._extract_chapter_data(data)
        if not chapter_data:
            logger.warning(
                "fanqienovel: chapterData not found (chapter=%s)", chapter_id
            )
            return None

        raw_content = chapter_data.get("content")
        if not raw_content:
            logger.warning("fanqienovel: empty content (chapter=%s)", chapter_id)
            return None

        tree = html.fromstring(raw_content)
        paragraphs: list[str] = []
        for p in tree.xpath("//p"):
            text = self._apply_font_mapping(p.text_content()).strip()
            if text:
                paragraphs.append(text)

        if not paragraphs:
            return None

        content = "\n".join(paragraphs)
        title = chapter_data.get("title") or ""

        return {
            "id": chapter_id,
            "title": title,
            "content": content,
            "extra": {"site": self.site_name},
        }

    @classmethod
    def _apply_font_mapping(cls, text: str) -> str:
        """
        Apply font mapping to the input text.
        """
        if not cls._FONT_MAP:
            cls._FONT_MAP = json.loads(FANQIENOVEL_MAP_PATH.read_text(encoding="utf-8"))
        return "".join(cls._FONT_MAP.get(ch, ch) for ch in text)

    @classmethod
    def _extract_initial_state(cls, html_str: str) -> dict[str, Any]:
        """
        Extract and parse the JavaScript object assigned to `__INITIAL_STATE__`

        The payload is serialized data (JSON-style object literal), so it is
        evaluated with the shared :class:`MiniJS` interpreter from
        :mod:`novel_downloader.libs.mini_js`.

        :param html_str: The HTML source containing the assignment.
        :return: A Python dictionary parsed from the JS object.
        :raises ValueError: If the pattern is not found or parsing fails.
        """
        match = cls._RE_INITIAL_STATE.search(html_str)
        if not match:
            raise ValueError("Could not find 'window.__INITIAL_STATE__' in the HTML.")

        try:
            result = MiniJS().eval(match.group(1))
        except Exception as e:
            raise ValueError(f"Failed to parse JS object: {e}") from e
        if not isinstance(result, dict):
            raise ValueError("Parsed value is not an object.")
        return result

    @staticmethod
    def _extract_chapter_data(data: dict[str, Any]) -> dict[str, Any]:
        reader = data.get("reader", {})
        chapterData = reader.get("chapterData", {})
        return chapterData if isinstance(chapterData, dict) else {}

    @staticmethod
    def _check_font(html_str: str) -> None:
        if "dc027189e0ba4cd.woff" not in html_str:
            logger.warning(
                "fanqienovel font check: Did not etected 'dc027189e0ba4cd.woff'. "
                "This may cause the content to be incorrect. "
                "Please report this issue so the handler can be updated."
            )
