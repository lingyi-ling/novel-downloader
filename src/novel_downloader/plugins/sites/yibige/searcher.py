#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.yibige.searcher
----------------------------------------------

"""

import logging
import re
from urllib.parse import quote

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

#: ``encryptedCookieValue = "..."`` inside the anti-bot verification page
_COOKIE_VALUE_RE = re.compile(r'encryptedCookieValue\s*=\s*"([^"]+)"')

#: book path inside ``javascript:sovote(132316183,'/132316183/')``
_HREF_PATH_RE = re.compile(r"'(/[^']*)'")


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
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'",
                keyword,
                self.SEARCH_URL,
            )
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
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s' (verified)",
                keyword,
                self.SEARCH_URL,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//table[contains(@class,"grid")]//tr[@id="nr"]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            # href="javascript:sovote(132316183,'/132316183/')"
            href = self._first_str(row.xpath("./td[1]/a/@href"))
            m = _HREF_PATH_RE.search(href)
            if not m:
                continue
            path = m.group(1)
            book_id = path.strip("/").split("/")[0]
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
                    priority=self.priority + idx,
                )
            )
        return results
