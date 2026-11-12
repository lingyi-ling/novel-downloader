#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.jpxs123.fetcher
----------------------------------------------

"""

from typing import Any
from urllib.parse import urljoin

from lxml import html

from novel_downloader.plugins.base.errors import HttpStatusError, TransportError
from novel_downloader.plugins.base.fetcher import BaseFetcher
from novel_downloader.plugins.registry import registrar


@registrar.register_fetcher()
class Jpxs123Fetcher(BaseFetcher):
    """
    A session class for interacting with the 精品小说网 (www.jpxs123.com) novel.
    """

    site_name: str = "jpxs123"

    BASE_URL = "https://www.jpxs123.com"
    BOOK_INFO_URL = "https://www.jpxs123.com/{book_id}.html"
    CHAPTER_URL = "https://www.jpxs123.com/{book_id}/{chapter_id}.html"

    #: Pages are served as GBK without a ``charset`` in the HTTP header, so
    #: ``BaseFetcher.fetch``'s UTF-8 default decodes every title into mojibake.
    #: gb18030 is a superset of gb2312/gbk and also covers the traditional
    #: characters used in the catalogue.
    PAGE_ENCODING = "gb18030"

    async def _fetch_page(self, url: str, **kwargs: Any) -> str:
        """
        Fetch an HTML page and decode it as GB18030 leniently.

        A few pages carry one byte that is invalid in every Chinese codec
        (inside an obfuscated ad block).  ``Response.text`` then fails the
        strict GB decode and falls back to a UTF-8 decode of the whole body,
        turning every title into mojibake.  Decoding the raw bytes with
        ``errors="replace"`` loses only that stray byte.
        """
        if self._rate_limiter:
            await self._rate_limiter.wait()

        last_exc: BaseException | None = None
        for attempt in range(self._retry_times + 1):
            try:
                resp = await self.session.get(
                    url, encoding=self.PAGE_ENCODING, **kwargs
                )
            except (TimeoutError, ConnectionError) as exc:
                last_exc = exc
            else:
                if resp.ok:
                    try:
                        return resp.content.decode(self.PAGE_ENCODING)
                    except UnicodeDecodeError:
                        return resp.content.decode(self.PAGE_ENCODING, errors="replace")
                self._register_shared_cooldown(resp.status, resp.headers)
                last_exc = HttpStatusError(url, resp.status)

            if attempt < self._retry_times:
                await self._retry_backoff(attempt)

        raise TransportError(url, last_exc or RuntimeError("fetch failed"))

    async def fetch_book_info(
        self,
        book_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """
        Fetch the raw HTML of the book info page asynchronously.

        Order: [info, download]

        :param book_id: The book identifier.
        :return: The page content as string list.
        """
        book_id = book_id.replace("-", "/")
        url = self.book_info_url(book_id=book_id)
        info_html = await self._fetch_page(url, **kwargs)
        try:
            info_tree = html.fromstring(info_html)
            txt_link = info_tree.xpath(
                '//div[@class="booktips"]//a[contains(text(), "txt下载")]/@href'
            )
            download_url = self._absolute_url(txt_link[0]) if txt_link else None
        except Exception:
            download_url = None

        download_html = (
            await self._fetch_page(download_url, **kwargs) if download_url else ""
        )
        return [info_html, download_html]

    @classmethod
    def _absolute_url(cls, href: str) -> str:
        """Resolve a download-page href without double-prefixing absolutes."""
        href = (href or "").strip()
        return urljoin(cls.BASE_URL + "/", href) if href else ""

    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        book_id = book_id.replace("-", "/")
        url = self.chapter_url(book_id=book_id, chapter_id=chapter_id)
        return [await self._fetch_page(url, **kwargs)]

    @classmethod
    def book_info_url(cls, book_id: str) -> str:
        """
        Construct the URL for fetching a book's info page.

        :param book_id: The identifier of the book.
        :return: Fully qualified URL for the book info page.
        """
        return cls.BOOK_INFO_URL.format(book_id=book_id)

    @classmethod
    def chapter_url(cls, book_id: str, chapter_id: str) -> str:
        """
        Construct the URL for fetching a specific chapter.

        :param book_id: The identifier of the book.
        :param chapter_id: The identifier of the chapter.
        :return: Fully qualified chapter URL.
        """
        return cls.CHAPTER_URL.format(book_id=book_id, chapter_id=chapter_id)
