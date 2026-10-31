#!/usr/bin/env python3
"""
novel_downloader.plugins.processors.translator.edge
---------------------------------------------------
"""

import base64
import copy
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests

from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict

from .cache import build_cache, dedupe_settings, translate_chunks

logger = logging.getLogger(__name__)


@registrar.register_processor()
class EdgeTranslaterProcessor:
    """
    Translate book and chapter data using the Microsoft Edge Translator.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._source: str = config.get("source") or "auto"
        self._target: str = config.get("target") or "zh-Hans"
        # ``time.sleep`` rejects negative values and the delay runs in a
        # ``finally`` block, so clamp instead of crashing.
        self._sleep: float = max(0.0, float(config.get("sleep", 1.0)))
        self._endpoint: str = (
            "https://api-edge.cognitive.microsofttranslator.com/translate"
        )
        self._auth_url: str = "https://edge.microsoft.com/translate/auth"
        self._token_cache: dict[str, Any] | None = None
        self._token_lock = threading.Lock()
        self._cache = build_cache("edge", self._source, self._target, config)
        self._dedupe, self._dedupe_max_length = dedupe_settings(config)

    @property
    def cache_stats(self) -> dict[str, Any]:
        """Live cache counters (hits/misses/size/...), useful for tests."""
        return self._cache.stats

    def clear_cache(self) -> None:
        """Drop every cached translation and reset the counters."""
        self._cache.clear()

    def process_book_info(self, book_info: BookInfoDict) -> BookInfoDict:
        """
        Translate book metadata and nested structures.
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
        Translate a single chapter (title + content).
        Each line is treated as one paragraph.
        """
        ch = copy.deepcopy(chapter)

        ch["title"] = self._tr(ch.get("title", ""))

        content = ch.get("content", "")
        if isinstance(content, str):
            paragraphs = self._split_text(content)
            translated = translate_chunks(
                self._translate_with_status,
                paragraphs,
                dedupe=self._dedupe,
                max_length=self._dedupe_max_length,
            )
            ch["content"] = "\n".join(translated)

        return ch

    def _tr(self, value: Any) -> Any:
        """
        Translate a field, leaving non-string values untouched.

        Parsed metadata occasionally carries ``None`` for an optional field;
        passing that to :meth:`_translate` would raise ``AttributeError`` and
        abort the whole stage.
        """
        return self._translate(value) if isinstance(value, str) else value

    @staticmethod
    def _split_text(text: str, max_length: int = 3000) -> list[str]:
        """
        Each line is treated as one paragraph.
        """
        lines = [ln for line in text.splitlines() if (ln := line.strip())]
        chunks: list[str] = []
        buf: list[str] = []
        buf_len = 0

        for line in lines:
            line_len = len(line)
            if buf_len + line_len + (1 if buf else 0) <= max_length:
                buf.append(line)
                buf_len += line_len + (1 if buf_len > 0 else 0)
            else:
                if buf:
                    chunks.append("\n".join(buf))
                buf = [line]
                buf_len = line_len

        if buf:
            chunks.append("\n".join(buf))

        return chunks

    def _parse_jwt(self, token: str) -> dict[str, Any]:
        """
        Parse Edge's JWT token to extract expiration time.

        Raises:
            ValueError: When the token is not a well-formed JWT carrying an
                ``exp`` claim (wrapped so callers never see a bare
                ``KeyError``/``binascii.Error``).
        """
        parts = token.split(".")
        if len(parts) < 2:
            raise ValueError("Invalid JWT token returned by Edge auth endpoint.")
        base64_url = parts[1].replace("-", "+").replace("_", "/")
        try:
            decoded = base64.b64decode(base64_url + "===")
            payload = json.loads(decoded)
            expire = datetime.fromtimestamp(payload["exp"])
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"Unable to parse Edge auth token payload: {e!r}.") from e
        return {
            "token": token,
            "expire": expire,
        }

    def _get_token(self) -> str:
        """
        Fetch a fresh token from Edge if none cached or expired.

        The fetch runs under ``_token_lock`` and re-checks the cache after
        acquiring it, so concurrent callers coalesce into a single auth
        request instead of racing one another.
        """
        if self._token_cache is None or datetime.now() >= self._token_cache["expire"]:
            with self._token_lock:
                # Another thread may have refreshed the token while this
                # one waited on the lock; only the winner performs the
                # request.
                if (
                    self._token_cache is None
                    or datetime.now() >= self._token_cache["expire"]
                ):
                    logger.debug("Fetching new Microsoft Edge translator token...")
                    r = requests.get(self._auth_url, timeout=10)
                    r.raise_for_status()
                    token = r.text.strip()
                    self._token_cache = self._parse_jwt(token)
        return self._token_cache["token"]  # type: ignore[no-any-return]

    def _translate(self, text: str) -> str:
        """
        Translate text using Edge Translator API.
        """
        return self._translate_with_status(text)[0]

    def _translate_with_status(self, text: str) -> tuple[str, bool]:
        """
        Translate ``text``, returning ``(result, succeeded)``.

        ``succeeded`` is false for blank input, auth failures, non-200
        responses and malformed payloads, so failures are never cached.
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
        params = {
            "to": self._target,
            "api-version": "3.0",
            "includeSentenceLength": "true",
        }
        if self._source.lower() != "auto":
            params["from"] = self._source

        endpoint = f"{self._endpoint}?{urlencode(params)}"

        # Token retrieval performs its own network call: it used to sit
        # outside the guarded block below, so any auth/parse failure escaped
        # ``_translate`` and aborted the whole pipeline stage.
        try:
            token = self._get_token()
        except Exception as e:
            logger.error(
                "Edge authentication failed; returning original text. Error: %s", e
            )
            return text, False

        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
        }
        body = json.dumps([{"text": text.strip()}])

        result = text
        ok = False
        try:
            r = requests.post(endpoint, headers=headers, data=body, timeout=20)
            if r.status_code == 200:
                data = r.json()
                result = data[0]["translations"][0]["text"]
                ok = True
            else:
                logger.warning(
                    "HTTP %d during Edge translation: %s",
                    r.status_code,
                    r.text[:120],
                )
        except Exception as e:
            logger.error("Edge translation failed: %s", e)
        finally:
            time.sleep(self._sleep)

        if ok and key is not None:
            self._cache.put(key, result)

        return result, ok
