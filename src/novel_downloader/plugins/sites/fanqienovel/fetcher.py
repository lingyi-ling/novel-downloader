#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.fanqienovel.fetcher
--------------------------------------------------
"""

import json
import logging
import os
from typing import Any

from novel_downloader.plugins.base.fetcher import GenericFetcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.sites.fanqienovel.appapi import (
    FanqieDevice,
    decrypt_content,
    discover_device,
    reader_url,
    sign_query,
    to_reader_page,
)
from novel_downloader.plugins.sites.fanqienovel.register import mint_device

logger = logging.getLogger(__name__)


@registrar.register_fetcher()
class FanqienovelFetcher(GenericFetcher):
    """
    A session class for interacting with the 番茄小说网 (fanqienovel.com) novel.
    """

    site_name: str = "fanqienovel"

    BOOK_INFO_URL = "https://fanqienovel.com/page/{book_id}"
    CHAPTER_URL = "https://fanqienovel.com/reader/{chapter_id}"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._device_checked = False
        self._device_cache: FanqieDevice | None = None

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
        page = await self._fetch_chapter_via_app(chapter_id)
        if page is not None:
            return [page]
        return await super().fetch_chapter_content(book_id, chapter_id, **kwargs)

    async def _fetch_chapter_via_app(self, chapter_id: str) -> str | None:
        device = self._app_device()
        if device is None or not chapter_id.isdigit():
            return None

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
        if not isinstance(data, dict) or data.get("crypt_status") != 0:
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
