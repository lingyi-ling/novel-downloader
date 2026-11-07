#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.fetcher
--------------------------------------------------
"""

import contextlib
import json
import logging
import os
import re
from typing import Any

from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.sites.fanqienovel.appapi import (
    DEVICE_FILENAME,
    FanqieDevice,
    batch_url,
    decrypt_content,
    discover_device,
    reader_url,
    sign_query,
    to_reader_page,
)
from novel_downloader.plugins.sites.fanqienovel.argus import x_argus
from novel_downloader.plugins.sites.fanqienovel.ladon import x_ladon
from novel_downloader.plugins.sites.fanqienovel.register import (
    CLIENT_USER_AGENT,
    mint_device,
)

logger = logging.getLogger(__name__)


@registrar.register_fetcher()
class FanqienovelFetcher(GenericFetcher):
    """
    A session class for interacting with the 番茄小说网 (fanqienovel.com) novel.
    """

    site_name: str = "fanqienovel"

    BOOK_INFO_URL = "https://fanqienovel.com/page/{book_id}"
    CHAPTER_URL = "https://fanqienovel.com/reader/{chapter_id}"

    #: The batch endpoint accepts roughly this many chapters per call.
    BATCH_SIZE = 20
    _ITEM_ID_RE = re.compile(r'"itemId":"(\d+)"')

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._device_checked = False
        self._device_cache: FanqieDevice | None = None
        self._catalog: dict[str, list[str]] = {}
        self._page_cache: dict[str, str] = {}

    async def init(self, **kwargs: Any) -> None:
        """Initialise the session, registering a device identity if needed."""
        await super().init(**kwargs)
        if self._app_device() is not None:
            return
        if os.environ.get("NOVELDL_FANQIE_AUTO_REGISTER", "1") == "0":
            return
        try:
            device = await mint_device(self.session, self._cache_dir)
        except (ConnectionError, TimeoutError, ValueError, OSError):
            logger.info("fanqienovel: automatic device registration failed")
            return
        if device is not None:
            self._device_cache = device
            self._device_checked = True

    def _app_device(self) -> FanqieDevice | None:
        """Device identity for the app API, looked up at most once per fetcher."""
        if not self._device_checked:
            self._device_cache = discover_device(self._cache_dir)
            self._device_checked = True
            if self._device_cache is None:
                logger.info(
                    "fanqienovel: no app device identity found; locked chapters "
                    "will fall back to the web reader"
                )
        return self._device_cache

    @property
    def handles_locked_chapters(self) -> bool:
        """Whether paywalled chapters can be read through the app API.

        The planner uses this to keep locked chapters in the download queue
        instead of dropping them as unreadable.
        """
        return self._app_device() is not None

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        pages = await super().fetch_book_info(book_id, **kwargs)
        for page in pages:
            ids = self._ITEM_ID_RE.findall(page)
            if ids:
                self._catalog[book_id] = ids
                break
        return pages

    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """
        Fetch a chapter, preferring the official app API.

        The web reader returns a short teaser for paywalled chapters, while the
        app API returns the whole chapter to any device holding a content key.
        When no key is available the request is skipped entirely, so behaviour
        is unchanged for installations without a device identity.
        """
        page = self._page_cache.get(chapter_id)
        if page is None:
            page = await self._fetch_chapter_via_app(book_id, chapter_id)
        if page is not None:
            return [page]
        return await super().fetch_chapter_content(book_id, chapter_id, **kwargs)

    async def _renew_device(self) -> bool:
        """Drop the cached identity and register a fresh one."""
        with contextlib.suppress(OSError):
            (self._cache_dir / DEVICE_FILENAME).unlink(missing_ok=True)
        try:
            device = await mint_device(self.session, self._cache_dir)
        except (ConnectionError, TimeoutError, ValueError, OSError):
            return False
        if device is None:
            return False
        self._device_cache = device
        self._device_checked = True
        logger.info("fanqienovel: replaced the device identity after a key rejection")
        return True

    def _batch_window(self, book_id: str, chapter_id: str) -> list[str]:
        """Chapter ids to request together, aligned to the batch size."""
        ids = self._catalog.get(book_id)
        if not ids or chapter_id not in ids:
            return [chapter_id]
        start = (ids.index(chapter_id) // self.BATCH_SIZE) * self.BATCH_SIZE
        return ids[start : start + self.BATCH_SIZE]

    async def _fetch_batch(
        self, device: FanqieDevice, book_id: str, chapter_id: str
    ) -> str | None:
        window = self._batch_window(book_id, chapter_id)
        if len(window) <= 1:
            return None
        url = batch_url(device, window)
        query = url.split("?", 1)[1]
        timestamp = int(__import__("time").time())
        gorgon, khronos = sign_query(query, khronos=timestamp)
        headers = {
            "User-Agent": CLIENT_USER_AGENT,
            "Accept-Encoding": "identity",
            "X-Gorgon": gorgon,
            "X-Khronos": str(khronos),
            "x-argus": x_argus(query, timestamp, device_id=device.device_id),
            "x-ladon": x_ladon(timestamp),
        }
        try:
            text = await self.fetch(url, headers=headers)
        except (ConnectionError, TimeoutError):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            return None
        for item_id, item in data.items():
            if not isinstance(item, dict) or item.get("crypt_status") != 0:
                continue
            blob = item.get("content")
            if not isinstance(blob, str) or not blob:
                continue
            try:
                html = decrypt_content(blob, device.content_key)
            except (ValueError, TypeError):
                continue
            if html.strip():
                title = item.get("title")
                self._page_cache[item_id] = to_reader_page(
                    title if isinstance(title, str) else "", html
                )
        if len(self._page_cache) > 4 * self.BATCH_SIZE:
            for stale in list(self._page_cache)[: 2 * self.BATCH_SIZE]:
                del self._page_cache[stale]
        return self._page_cache.get(chapter_id)

    async def _fetch_chapter_via_app(
        self, book_id: str, chapter_id: str, *, renewed: bool = False
    ) -> str | None:
        device = self._app_device()
        if device is None or not chapter_id.isdigit():
            return None

        batched = await self._fetch_batch(device, book_id, chapter_id)
        if batched is not None:
            return batched

        url = reader_url(device, chapter_id)
        query = url.split("?", 1)[1]
        gorgon, khronos = sign_query(query)
        headers = {
            "User-Agent": "com.dragon.read",
            "X-Gorgon": gorgon,
            "X-Khronos": str(khronos),
        }
        try:
            text = await self.fetch(url, headers=headers)
        except (ConnectionError, TimeoutError):
            logger.info("fanqienovel: app API unavailable for chapter %s", chapter_id)
            return None

        try:
            payload = json.loads(text)
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        if data.get("crypt_status") != 0:
            # The server refused our key (rotated or revoked).  Register a
            # fresh identity once instead of silently dropping the chapter.
            if not renewed and await self._renew_device():
                return await self._fetch_chapter_via_app(
                    book_id, chapter_id, renewed=True
                )
            return None
        blob = data.get("content")
        if not isinstance(blob, str) or not blob:
            return None

        try:
            content_html = decrypt_content(blob, device.content_key)
        except (ValueError, TypeError):
            logger.warning(
                "fanqienovel: content key rejected for chapter %s", chapter_id
            )
            return None
        if not content_html.strip():
            return None

        title = data.get("title")
        return to_reader_page(title if isinstance(title, str) else "", content_html)
