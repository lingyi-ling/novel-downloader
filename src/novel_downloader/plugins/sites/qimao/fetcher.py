#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.qimao.fetcher
--------------------------------------------

"""

import logging
from typing import Any

from novel_downloader.plugins.base.fetcher import BaseFetcher
from novel_downloader.plugins.registry import registrar

logger = logging.getLogger(__name__)


@registrar.register_fetcher()
class QimaoFetcher(BaseFetcher):
    """
    A session class for interacting with the 七猫中文网 (www.qimao.com) novel.
    """

    site_name: str = "qimao"

    BASE_URL = "https://www.qimao.com"
    BOOK_DETAIL_API = "https://www.qimao.com/qimaoapi/api/book-detail/main-info"
    BOOK_INTRO_API = "https://www.qimao.com/qimaoapi/api/book-detail/intro"
    CATALOG_API = "https://www.qimao.com/qimaoapi/api/book/chapter-list"
    CHAPTER_URL = "https://www.qimao.com/shuku/{book_id}-{chapter_id}/"

    async def fetch_book_info(
        self,
        book_id: str,
        **kwargs: Any,
    ) -> list[str]:
        headers = {
            **self.headers,
            "Referer": f"{self.BASE_URL}/",
        }
        params = {"book_id": book_id}
        detail = await self.fetch(self.BOOK_DETAIL_API, params=params, headers=headers)
        intro = await self.fetch(self.BOOK_INTRO_API, params=params, headers=headers)
        catalog = await self.fetch(self.CATALOG_API, params=params, headers=headers)
        return [detail, intro, catalog]

    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        headers = {
            **self.headers,
            "Referer": f"{self.BASE_URL}/",
        }
        url = self.chapter_url(book_id=book_id, chapter_id=chapter_id)
        return [await self.fetch(url, headers=headers, **kwargs)]

    @classmethod
    def chapter_url(cls, book_id: str, chapter_id: str) -> str:
        return cls.CHAPTER_URL.format(book_id=book_id, chapter_id=chapter_id)
