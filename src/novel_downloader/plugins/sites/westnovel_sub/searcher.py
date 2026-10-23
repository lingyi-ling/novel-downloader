#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.westnovel_sub.searcher
-----------------------------------------------------

Searcher for the 西方奇幻小说网轻小说频道 (www.westnovel.com/q/),
the channel handled by the ``westnovel_sub`` fetcher/parser.

The search form on ``/q/list/`` posts to the channel-local EmpireCMS
backend::

    POST https://www.westnovel.com/q/e/search/index.php
    show=title,writer & tempid=1 & keyboard=<keyword> & tbname=sinfo

which answers with a 302 to ``result/?searchid=<id>``; the followed page
holds the result cards. Requests must use UTF-8 percent-encoded form data
(the channel pages are UTF-8).

``book_id`` follows the fetcher/resolver convention for this site:
``q-list-<numeric>``, i.e. ``/q/list/8156.html`` -> ``q-list-8156``.
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class WestnovelSubSearcher(BaseSearcher):
    site_name = "westnovel_sub"
    priority = 100
    BASE_URL = "https://www.westnovel.com"
    SEARCH_URL = "https://www.westnovel.com/q/e/search/index.php"

    async def _fetch_html(self, keyword: str) -> str:
        headers = {
            **self.session.headers,
            "Origin": "https://www.westnovel.com",
            "Referer": "https://www.westnovel.com/q/list/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # aiohttp form-encodes with UTF-8 percent-encoding, matching the
        # UTF-8 channel pages. The 302 to result/?searchid=<id> is followed
        # automatically (POST -> GET), landing on the result page.
        form = {
            "show": "title,writer",
            "tempid": "1",
            "keyboard": keyword,
            "tbname": "sinfo",
        }
        try:
            async with self.session.post(
                self.SEARCH_URL, data=form, headers=headers
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp)
        except Exception:
            logger.error(
                "Failed to fetch HTML for keyword '%s' from '%s'",
                keyword,
                self.SEARCH_URL,
            )
            return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[@id="sitebox"]//dl')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            href = self._first_str(row.xpath("./dd/h3/a[1]/@href"))
            if not href:
                continue

            if limit is not None and idx >= limit:
                break

            # '/q/list/8156.html' -> 'q-list-8156'
            parts = href.strip("/").split("/")
            if len(parts) != 3 or parts[-1].split(".", 1)[0] == "":
                continue
            book_id = "-".join([parts[0], parts[1], parts[-1].split(".", 1)[0]])
            book_url = self._abs_url(href)

            title = self._first_str(row.xpath("./dd/h3/a[1]//text()"))

            cover_url = self._abs_url(self._first_str(row.xpath("./dt//img/@src")))

            prio = self.priority + idx
            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author="-",
                    latest_chapter="-",
                    update_date="-",
                    word_count="-",
                    priority=prio,
                )
            )
        return results
