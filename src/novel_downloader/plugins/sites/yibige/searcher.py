#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.yibige.searcher
----------------------------------------------

"""

import logging
import re
from urllib.parse import quote, urlsplit

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

#: ``encryptedCookieValue = "..."`` inside the anti-bot verification page
_COOKIE_VALUE_RE = re.compile(r'encryptedCookieValue\s*=\s*"([^"]+)"')

#: quoted path inside ``javascript:sovote(132316183,'/132316183/')``
_JS_PATH_RE = re.compile(r"""['"]([^'"]+)['"]""")


@registrar.register_searcher()
class YibigeSearcher(BaseSearcher):
    site_name = "yibige"
    priority = 100
    BASE_URL = "https://www.yibige.org"
    SEARCH_URL = "https://www.yibige.org/modules/article/search.php"

    async def _fetch_html(self, keyword: str) -> str:
        url = self._build_url(
            self.SEARCH_URL,
            {"searchkey": self._quote(keyword), "searchtype": "articlename"},
        )

        try:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                text = await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, self.SEARCH_URL, exc)
            return ""

        # The site wraps the first search hit in an anti-bot page that
        # instructs a browser to set an ``is_human`` cookie (valid 1 hour)
        # and reload the same URL. Extract the value and retry once.
        m = _COOKIE_VALUE_RE.search(text)
        if not m:
            return text

        cookie_value = quote(m.group(1).replace("\\/", "/"), safe="")
        headers = {**self.session.headers, "Cookie": f"is_human={cookie_value}"}
        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception as exc:
            self._log_fetch_failure(keyword, f"{self.SEARCH_URL} (verified)", exc)
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//table[contains(@class,"grid")]//tr[@id="nr"]')
        results: list[SearchResult] = []

        for row in rows:
            if limit is not None and len(results) >= limit:
                break

            # Live rows use href="javascript:sovote(132316183,'/132316183/')",
            # but some templates emit a plain relative or absolute link.
            href = self._first_str(row.xpath("./td[1]/a/@href"))
            path = self._book_path(href)
            book_id = self._book_id_from_path(path)
            if not book_id:
                continue
            book_url = self._abs_url(path)

            title = self._first_str(row.xpath("./td[1]/a//text()"))
            author = self._first_str(row.xpath("./td[2]//text()")) or "-"
            update_date = self._first_str(row.xpath("./td[3]//text()")) or "-"

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url="",
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date=update_date,
                    word_count="-",
                    priority=self.priority + len(results),
                )
            )
        return results

    @staticmethod
    def _book_path(href: str) -> str:
        """Extract the book path from a search-result link.

        Supports the site's ``javascript:sovote(<id>,'/<id>/')`` wrapper as
        well as plain relative/absolute links; query strings and fragments are
        dropped so the id stays stable.
        """
        candidate = href.strip()
        if not candidate:
            return ""
        if candidate.lower().startswith("javascript:"):
            m = _JS_PATH_RE.search(candidate)
            if not m or m.group(1).startswith("javascript:"):
                return ""
            candidate = m.group(1)
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        path = urlsplit(candidate).path
        return path or candidate

    @staticmethod
    def _book_id_from_path(path: str) -> str:
        """Pick the numeric book id out of ``/<id>/`` or ``/book/<id>/``."""
        segments = [s for s in path.strip("/").split("/") if s]
        for segment in segments:
            if segment.isdigit():
                return segment
        return segments[0] if segments else ""
