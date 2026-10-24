#!/usr/bin/env python3
"""
novel_downloader.plugins.processors.translator.google
-----------------------------------------------------
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import requests

from novel_downloader.infra.http_defaults import DEFAULT_USER_AGENT
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict

from .cache import build_cache

logger = logging.getLogger(__name__)


@registrar.register_processor()
class GoogleTranslaterProcessor:
    """
    Translate novel metadata and chapters using the Google Translate endpoint.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._source: str = config.get("source") or "auto"
        self._target: str = config.get("target") or "zh-CN"
        # ``time.sleep`` rejects negative values, and the delay is applied
        # outside the guarded request block, so clamp instead of crashing.
        self._sleep: float = max(0.0, float(config.get("sleep", 2.0)))
        self._endpoint = "https://translate.googleapis.com/translate_a/single"
        self._headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        self._cache = build_cache("google", self._source, self._target, config)

    @property
    def cache_stats(self) -> dict[str, Any]:
        """Live cache counters (hits/misses/size/...), useful for tests."""
        return self._cache.stats

    def clear_cache(self) -> None:
        """Drop every cached translation and reset the counters."""
        self._cache.clear()

    def process_book_info(self, book_info: BookInfoDict) -> BookInfoDict:
        """
        Apply translate to book metadata and nested structures.
        """
        bi = copy.deepcopy(book_info)
        bi["book_name"] = self._tr(bi.get("book_name", ""))
        bi["summary"] = self._tr(bi.get("summary", ""))

        if "summary_brief" in bi:
            bi["summary_brief"] = self._tr(bi["summary_brief"])

        for vol in bi.get("volumes", []):
            vol["volume_name"] = self._tr(vol.get("volume_name", ""))
            if "volume_intro" in vol:
                vol["volume_intro"] = self._tr(vol["volume_intro"])
            for ch in vol.get("chapters", []):
                ch["title"] = self._tr(ch.get("title", ""))

        return bi

    def process_chapter(self, chapter: ChapterDict) -> ChapterDict:
        """
        Apply cleaning rules to a single chapter (title + content).
        """
        ch = copy.deepcopy(chapter)
        ch["title"] = self._tr(ch.get("title", ""))
        ch["content"] = self._tr(ch.get("content", ""))
        return ch

    def _tr(self, value: Any) -> Any:
        """
        Translate a field, leaving non-string values untouched.

        Parsed metadata occasionally carries ``None`` for an optional field;
        passing that to :meth:`_translate` would raise ``AttributeError`` and
        abort the whole stage.
        """
        return self._translate(value) if isinstance(value, str) else value

    def _translate(self, text: str) -> str:
        """Send text to the unofficial Google Translate endpoint."""
        return self._translate_with_status(text)[0]

    def _translate_with_status(self, text: str) -> tuple[str, bool]:
        """
        Translate ``text``, returning ``(result, succeeded)``.

        ``succeeded`` is only true for a 2xx response with a parsable payload;
        callers use it to make sure failures are never cached.
        """
        if not text.strip():
            return text, False

        key = self._cache.make_key(text)
        if key is None:  # caching disabled or a paragraph is over the limit
            return self._request_translation(text, key=None)

        with self._cache.single_flight(key):
            cached = self._cache.get(key)
            if cached is not None:
                return cached, True
            return self._request_translation(text, key)

    def _request_translation(self, text: str, key: str | None) -> tuple[str, bool]:
        """Perform one HTTP request; cache the result only on success."""
        data = {
            "client": "gtx",
            "sl": self._source,
            "tl": self._target,
            "dt": "t",
            "dj": "1",
            "q": text,
        }
        trans: str = text
        ok = False

        try:
            r = requests.post(
                self._endpoint, data=data, headers=self._headers, timeout=20
            )
            if r.status_code == 200:
                resp = r.json()
                trans = "".join(s["trans"] for s in resp["sentences"])
                ok = True
            else:
                logger.warning(
                    "HTTP %d while translating text: %s",
                    r.status_code,
                    r.text,
                )
        except Exception as e:
            logger.warning("Translation request failed: %s", e)

        # Respectful delay between requests
        time.sleep(self._sleep)

        if ok and key is not None:
            self._cache.put(key, trans)

        return trans, ok
