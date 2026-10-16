#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.qimao.parser
-------------------------------------------

"""

import base64
import hashlib
import json
import logging
import re
from typing import Any, ClassVar

from Crypto.Cipher import DES
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
class QimaoParser(BaseParser):
    """
    Parser for 七猫中文网 (www.qimao.com) book and chapter pages.
    """

    site_name: str = "qimao"

    BASE_URL = "https://www.qimao.com"

    # ``chapterData`` holds the chapter body. Free chapters are embedded as
    # escaped HTML, while VIP chapters are an encrypted payload (see _decrypt).
    _RE_CHAPTER_DATA = re.compile(r'chapterData:"((?:\\.|[^"\\])*)"')

    # Chapter id -> title, populated while parsing the book catalog.
    _chapter_titles: ClassVar[dict[str, str]] = {}

    _JS_ESCAPES: ClassVar[dict[str, str]] = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
    }

    def parse_book_info(
        self,
        raw_pages: list[str],
        **kwargs: Any,
    ) -> BookInfoDict | None:
        if len(raw_pages) < 3:
            return None

        try:
            detail = json.loads(raw_pages[0]).get("data", {}).get("book_detail") or {}
            intro = json.loads(raw_pages[1]).get("data", {}) or {}
            catalog = json.loads(raw_pages[2]).get("data", {}) or {}
        except (TypeError, ValueError):
            logger.warning("qimao: failed to parse book info JSON")
            return None

        book_id = str(detail.get("book_id") or "")
        words_raw = str(detail.get("words_num") or "")

        tags: list[str] = []
        for key in ("category_1_name", "category_2_name"):
            value = detail.get(key)
            if isinstance(value, str) and value:
                tags.append(value)

        chapters: list[ChapterInfoDict] = []
        for item in catalog.get("chapters") or []:
            if not isinstance(item, dict):
                continue
            chapter_id = str(item.get("id") or "")
            if not chapter_id:
                continue
            title = str(item.get("title") or "")
            self._chapter_titles[chapter_id] = title
            chapters.append(
                {
                    "title": title,
                    "url": f"{self.BASE_URL}/shuku/{book_id}-{chapter_id}/",
                    "chapterId": chapter_id,
                    "accessible": True,
                }
            )

        if not chapters:
            return None

        volumes: list[VolumeInfoDict] = [{"volume_name": "正文", "chapters": chapters}]

        return {
            "book_name": str(detail.get("title") or ""),
            "author": str(detail.get("author") or ""),
            "cover_url": str(detail.get("image_link") or ""),
            "update_time": str(detail.get("update_time") or ""),
            "word_count": f"{words_raw}万字" if words_raw else "",
            "serial_status": "完结" if str(detail.get("is_over")) == "1" else "连载",
            "summary": str(intro.get("intro") or ""),
            "tags": tags,
            "volumes": volumes,
            "extra": {
                "bookId": book_id,
                "latest_chapter_title": detail.get("latest_chapter_title"),
                "score": detail.get("score"),
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

        match = self._RE_CHAPTER_DATA.search(raw_pages[0])
        if not match:
            logger.warning("qimao: chapterData not found (chapter=%s)", chapter_id)
            return None

        payload = self._unescape(match.group(1))
        if not payload:
            return None

        content_html = payload if "<" in payload else self._decrypt(payload)
        if not content_html:
            return None

        tree = html.fromstring(content_html)
        paragraphs = [
            text.strip()
            for text in (node.text_content() for node in tree.xpath("//p"))
            if text.strip()
        ]
        if not paragraphs:
            return None

        return {
            "id": chapter_id,
            "title": self._chapter_titles.get(chapter_id, ""),
            "content": "\n".join(paragraphs),
            "extra": {"site": self.site_name},
        }

    @classmethod
    def _decrypt(cls, payload: str) -> str:
        """
        Decode a 七猫 VIP chapter payload rendered by the web reader.

        The payload is ``<ciphertext> + <32-char key>``: the trailing 32
        characters are the key, the remainder is the ciphertext. The key is
        derived as the first 8 hex characters of the MD5 digest of that
        string, and the body is DES-ECB encrypted with PKCS#7 padding.

        :param payload: The ``chapterData`` string from the reader page.
        :return: The decrypted HTML content, or "" on failure.
        """
        try:
            key = hashlib.md5(payload[-32:].encode()).hexdigest()[:8].encode()
            inner = cls._b64decode(payload[:-32]).decode("ascii")
            ciphertext = cls._b64decode(inner)
            plain = DES.new(key, DES.MODE_ECB).decrypt(ciphertext)
        except Exception as exc:
            logger.warning("qimao: failed to decrypt chapter content: %s", exc)
            return ""

        pad = plain[-1] if plain else 0
        if 1 <= pad <= 8:
            plain = plain[:-pad]
        return plain.decode("utf-8", errors="replace")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        """Base64-decode a chunk, tolerating missing padding."""
        chunk = data.encode()
        padding = b"=" * (-len(chunk) % 4)
        return base64.b64decode(chunk + padding)

    @classmethod
    def _unescape(cls, text: str) -> str:
        """Resolve the JavaScript string escapes used inside ``chapterData``."""
        result: list[str] = []
        i = 0
        length = len(text)
        while i < length:
            char = text[i]
            if char != "\\" or i + 1 >= length:
                result.append(char)
                i += 1
                continue

            esc = text[i + 1]
            if esc == "u":
                code = int(text[i + 2 : i + 6], 16)
                i += 6
                if 0xD800 <= code <= 0xDBFF and text[i : i + 2] == "\\u":
                    low = int(text[i + 2 : i + 6], 16)
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    i += 6
                result.append(chr(code))
            elif esc == "x":
                result.append(chr(int(text[i + 2 : i + 4], 16)))
                i += 4
            else:
                result.append(cls._JS_ESCAPES.get(esc, esc))
                i += 2
        return "".join(result)
