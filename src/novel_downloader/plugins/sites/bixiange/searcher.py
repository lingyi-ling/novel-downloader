#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.bixiange.searcher
------------------------------------------------

Searcher for 笔仙阁 (m.bixiange.top).  The old domain ``m.bixiange.me`` no
longer resolves (NXDOMAIN); the EmpireCMS search backend lives on under the
new domain with one change: a keyword that *has* hits now answers the POST
with ``302 -> /e/search/result/?searchid=N`` (followed transparently by the
session wrappers), while a keyword with no hits answers ``200`` with the
"没有搜索到相关的内容" info page, which simply parses to zero rows.

Three site quirks the searcher compensates for:

* The keyword must be form-encoded as GBK -- UTF-8 bytes silently match
  nothing on this site.
* The backend silently answers "no results" for any keyword whose GBK
  encoding exceeds 20 bytes (``MAX_KEYWORD_BYTES``), even when a shorter
  prefix would match, so longer queries are truncated on GBK character
  boundaries before posting (verified 2026-10-31: "从气运词条开始成就人皇"
  matches nothing while its 20-byte prefix finds /wxxz/20921).
* The site sets an ``eltphlastsearchtime`` cookie and enforces a 15-second
  interval between searches *within one cookie session* ("系统限制的搜索
  时间间隔为 15 秒").  The aggregate search orchestrator builds a fresh
  ``aiohttp.ClientSession`` per call, so normal usage never trips it; a
  caller reusing one session for rapid repeated searches gets an empty
  result list (logged, never crashes) until the interval elapses.
"""

import logging

from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


@registrar.register_searcher()
class BixiangeSearcher(BaseSearcher):
    site_name = "bixiange"
    priority = 30
    BASE_URL = "https://m.bixiange.top"
    SEARCH_URL = "https://m.bixiange.top/e/search/indexpage.php"

    #: GBK byte cap enforced by the EmpireCMS ``keyboard`` backend above
    #: which every query is answered with the "no results" page.
    MAX_KEYWORD_BYTES = 20

    #: Marker of the per-cookie-session search cooldown page ("系统限制的
    #: 搜索时间间隔为 15 秒,请稍后再搜索").
    RATELIMIT_MARKER = "系统限制的搜索时间间隔"

    #: keyword captured in _fetch_html for cooldown diagnostics
    _last_keyword: str = ""

    @classmethod
    def _truncate_keyword(cls, keyword: str) -> str:
        """Clamp ``keyword`` to :data:`MAX_KEYWORD_BYTES` GBK bytes."""
        raw = keyword.encode("gbk", errors="replace")
        if len(raw) <= cls.MAX_KEYWORD_BYTES:
            return keyword
        raw = raw[: cls.MAX_KEYWORD_BYTES]
        while raw:
            try:
                return raw.decode("gbk")
            except UnicodeDecodeError:
                raw = raw[:-1]
        return ""

    async def _fetch_html(self, keyword: str) -> str:
        self._last_keyword = keyword
        kw = self._quote(
            self._truncate_keyword(keyword), encoding="gbk", errors="replace"
        )
        data = f"keyboard={kw}&show=title&classid=0"
        headers = {
            **self.session.headers,
            "Referer": "https://m.bixiange.top/",
            "Origin": "https://m.bixiange.top",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self.session.post(
                self.SEARCH_URL, headers=headers, data=data
            ) as resp:
                resp.raise_for_status()
                return await self._response_to_str(resp, encoding="gbk")
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
        if self.RATELIMIT_MARKER in html_str:
            logger.warning(
                "bixiange search hit the 15s per-session interval limit; "
                "returning no results for keyword '%s'",
                self._last_keyword,
            )
            return []

        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[contains(@class, "list")]//li')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            href = self._first_str(row.xpath('.//div[@class="cover"]/a/@href'))
            if not href:
                continue

            book_url = self._abs_url(href)
            # "/khjj/11945.html" -> "khjj-11945"
            # "/trxs/19408" -> "trxs-19408"
            href_path = href.strip("/").split(".", 1)[0].split("/")
            book_id = "-".join(href_path)

            cover_url = self._first_str(row.xpath('.//div[@class="cover"]//img/@src'))
            if cover_url:
                cover_url = self._abs_url(cover_url)

            title = self._first_str(row.xpath('.//div[@class="title"]//a/text()'))

            update_date = self._first_str(
                row.xpath(
                    './/div[@class="tips"]/span[contains(text(), "时间")]/text()'
                ),  # noqa: E501
                replaces=[("时间：", "")],
            )

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author="-",
                    latest_chapter="-",
                    update_date=update_date,
                    word_count="-",
                    priority=self.priority + idx,
                )
            )
        return results
