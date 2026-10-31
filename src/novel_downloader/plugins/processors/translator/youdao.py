#!/usr/bin/env python3
"""
novel_downloader.plugins.processors.translator.youdao
-----------------------------------------------------
"""

import base64
import copy
import hashlib
import json
import logging
import threading
import time
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from novel_downloader.infra.http_defaults import DEFAULT_USER_AGENT
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import BookInfoDict, ChapterDict

from .cache import build_cache, dedupe_settings, translate_chunks

logger = logging.getLogger(__name__)


@registrar.register_processor()
class YoudaoTranslaterProcessor:
    """
    Translate book and chapter data using the Youdao Translator.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._source: str = config.get("source") or "auto"
        self._target: str = config.get("target") or "zh-CHS"
        self._sleep: float = max(0.0, float(config.get("sleep", 1.0)))
        self._client = _YoudaoWebFanyi()
        self._cache = build_cache("youdao", self._source, self._target, config)
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

    def _translate(self, text: str) -> str:
        """
        Translate text using Youdao Translator API.
        """
        return self._translate_with_status(text)[0]

    def _translate_with_status(self, text: str) -> tuple[str, bool]:
        """
        Translate ``text``, returning ``(result, succeeded)``.

        Blank or whitespace-only input is returned untouched without a
        request, matching the Google and Edge processors. A client exception
        means the original text is returned with ``succeeded=False``; such a
        result is never cached or deduplicated.
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
        """Perform one translation; cache the result only on success."""
        try:
            out = self._client.translate(text, self._source, self._target)
            if self._sleep > 0:
                time.sleep(self._sleep)
        except Exception as e:
            logger.warning(
                "Youdao translate failed; returning original text. Error: %s", e
            )
            return text, False

        if key is not None:
            self._cache.put(key, out)
        return out, True


class _YoudaoWebFanyi:
    """Minimal client for youdao web fanyi."""

    KEY_URL = "https://dict.youdao.com/webtranslate/key"
    TRANS_URL = "https://dict.youdao.com/webtranslate"

    _KEY_CONST = "asdjnjfenknafdfsdfsd"  # public constant used by the website JS
    _KEYIDS = ("webfanyi-key-getter-2025", "webfanyi-key-getter")

    def __init__(self, session: requests.Session | None = None) -> None:
        self.sess = session or requests.Session()
        self.sess.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en,zh;q=0.9,zh-CN;q=0.8",
                "Origin": "https://fanyi.youdao.com",
                "Referer": "https://fanyi.youdao.com/",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "User-Agent": DEFAULT_USER_AGENT,
            }
        )

        self._secret_key: str = ""
        self._aes_key_str: str = ""
        self._aes_iv_str: str = ""
        self._aes_key_bytes: bytes = b""
        self._aes_iv_bytes: bytes = b""
        self._key_fetched_at: float = 0.0
        self._key_lock = threading.Lock()

    @staticmethod
    def _md5_hex(s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _md5_bytes(s: str) -> bytes:
        return hashlib.md5(s.encode("utf-8")).digest()

    def _sign_for_key(self, mystic_time_ms: int) -> str:
        s = f"client=fanyideskweb&mysticTime={mystic_time_ms}&product=webfanyi&key={self._KEY_CONST}"  # noqa: E501
        return self._md5_hex(s)

    def _sign_for_translate(self, mystic_time_ms: int, secret_key: str) -> str:
        s = f"client=fanyideskweb&mysticTime={mystic_time_ms}&product=webfanyi&key={secret_key}"  # noqa: E501
        return self._md5_hex(s)

    def _keys_are_fresh(self) -> bool:
        """Whether the cached signing keys are present and recent enough."""
        return bool(self._secret_key) and (time.time() - self._key_fetched_at) < 600

    def _ensure_keys(self, force: bool = False) -> None:
        """
        Make sure signing keys are available, fetching them when needed.

        The fetch runs under ``_key_lock`` and re-checks freshness after
        acquiring it, so concurrent callers coalesce into a single key
        request instead of racing one another.
        """
        if not force and self._keys_are_fresh():
            return

        with self._key_lock:
            # Another thread may have refreshed the keys while this one
            # waited on the lock; only the winner performs the request.
            if not force and self._keys_are_fresh():
                return

            last_err = None
            mt = int(time.time() * 1000)

            for keyid in self._KEYIDS:
                try:
                    sign = self._sign_for_key(mt)
                    params = {
                        "keyid": keyid,
                        "sign": sign,
                        "client": "fanyideskweb",
                        "product": "webfanyi",
                        "appVersion": "1.0.0",
                        "vendor": "web",
                        "pointParam": "client,mysticTime,product",
                        "mysticTime": str(mt),
                        "keyfrom": "fanyi.web",
                        "mid": "1",
                        "screen": "1",
                        "model": "1",
                        "network": "wifi",
                        "abtest": "0",
                        "yduuid": "abcdefg",
                    }
                    r = self.sess.get(self.KEY_URL, params=params, timeout=10)
                    r.raise_for_status()
                    js = r.json()
                    if js.get("code") != 0:
                        raise RuntimeError(f"key endpoint error: {js}")
                    data = js["data"]
                    self._secret_key = data["secretKey"]
                    self._aes_key_str = data["aesKey"]
                    self._aes_iv_str = data["aesIv"]
                    self._aes_key_bytes = self._md5_bytes(self._aes_key_str)
                    self._aes_iv_bytes = self._md5_bytes(self._aes_iv_str)
                    self._key_fetched_at = time.time()
                    logger.debug("Youdao keys fetched via %s", keyid)
                    return
                except Exception as e:  # try the next keyid
                    last_err = e
                    logger.debug("Key fetch failed via %s: %s", keyid, e)

            raise RuntimeError(f"Failed to fetch Youdao keys: {last_err}")

    def _decrypt_payload(self, payload_text: str) -> dict[str, Any]:
        if not (self._aes_key_bytes and self._aes_iv_bytes):
            raise RuntimeError("AES bytes not ready")

        b = base64.b64decode(payload_text, altchars=b"-_", validate=False)
        cipher = AES.new(self._aes_key_bytes, AES.MODE_CBC, self._aes_iv_bytes)
        raw = cipher.decrypt(b)
        dec = unpad(raw, AES.block_size).decode("utf-8", errors="strict")
        return json.loads(dec)  # type: ignore[no-any-return]

    @staticmethod
    def _norm_lang(code: str) -> str:
        """Map a few common aliases to what the service expects."""
        c = (code or "").strip()
        if not c:
            return ""  # let server auto-detect
        c_low = c.lower()
        if c_low in (
            "zh",
            "zh_cn",
            "zh-cn",
            "zh_cns",
            "zh-cns",  # legacy typo kept for backwards compatibility
            "zh_chs",
            "zh-chs",
            "zh_hans",
            "zh-hans",
        ):
            return "zh-CHS"  # simplified
        if c_low in ("zh_tw", "zh-tw", "zh_cht", "zh-cht", "zh_hant", "zh-hant"):
            return "zh-CHT"  # traditional
        return c

    def translate(self, text: str, src: str = "auto", tgt: str = "zh-CHS") -> str:
        if not text:
            return ""

        self._ensure_keys()

        mt = int(time.time() * 1000)
        sign = self._sign_for_translate(mt, self._secret_key)
        data = {
            "i": text,
            "from": self._norm_lang(src) or "auto",
            "to": self._norm_lang(tgt),
            "useTerm": "false",
            "domain": "0",
            "dictResult": "true",
            "keyid": "webfanyi",
            "sign": sign,
            "client": "fanyideskweb",
            "product": "webfanyi",
            "appVersion": "1.0.0",
            "vendor": "web",
            "pointParam": "client,mysticTime,product",
            "mysticTime": str(mt),
            "keyfrom": "fanyi.web",
            "mid": "1",
            "screen": "1",
            "model": "1",
            "network": "wifi",
            "abtest": "0",
            "yduuid": "abcdefg",
        }

        r = self.sess.post(self.TRANS_URL, data=data, timeout=20)
        if r.status_code != 200:
            # Occasionally keys expire mid-flight -> refetch and retry once
            self._ensure_keys(force=True)
            mt = int(time.time() * 1000)
            data["mysticTime"] = str(mt)
            data["sign"] = self._sign_for_translate(mt, self._secret_key)
            r = self.sess.post(self.TRANS_URL, data=data, timeout=20)
            r.raise_for_status()

        js = self._decrypt_payload(r.text)

        if js.get("code") != 0:
            self._ensure_keys(force=True)
            mt = int(time.time() * 1000)
            data["mysticTime"] = str(mt)
            data["sign"] = self._sign_for_translate(mt, self._secret_key)
            r = self.sess.post(self.TRANS_URL, data=data, timeout=20)
            r.raise_for_status()
            js = self._decrypt_payload(r.text)
            if js.get("code") != 0:
                raise RuntimeError(f"Youdao returned error: {js}")

        out_parts: list[str] = []
        for row in js.get("translateResult", []):
            for seg in row:
                tgt_text = seg.get("tgt", "")
                out_parts.append(tgt_text)
        return "".join(out_parts)
