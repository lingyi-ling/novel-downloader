#!/usr/bin/env python3
"""
novel_downloader.plugins.sites.n17k.searcher
--------------------------------------------
"""

import contextlib
import functools
import logging
import re
import ssl

import aiohttp
from lxml import html

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

_ARG1_RE = re.compile(r"var\s+arg1='([0-9A-Fa-f]+)'")

#: Aliyun WAF (acw_sc__v2) challenge solver constants.
_WAF_KEY = "3000176000856006061501533003690027800375"
_WAF_POS = [
    15,
    35,
    29,
    24,
    33,
    16,
    1,
    38,
    10,
    9,
    19,
    31,
    40,
    27,
    22,
    23,
    25,
    13,
    6,
    11,
    39,
    18,
    20,
    8,
    14,
    21,
    32,
    26,
    2,
    30,
    7,
    4,
    17,
    5,
    3,
    28,
    34,
    37,
    12,
    36,
]

#: Chrome-like cipher order; the search host rejects the default
#: Python TLS fingerprint with HTTP 405.
_CHROME_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:"
    "AES128-SHA:"
    "AES256-SHA"
)


@functools.lru_cache(maxsize=1)
def _chrome_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    with contextlib.suppress(ssl.SSLError):
        ctx.set_ciphers(_CHROME_CIPHERS)
    return ctx


@registrar.register_searcher()
class N17kSearcher(BaseSearcher):
    """
    Searcher for 17K小说网 (www.17k.com).

    Uses the header search form target ``search.17k.com/search.xhtml``.
    The search host sits behind an Aliyun WAF: the first response is a
    JavaScript challenge that must be solved to obtain the ``acw_sc__v2``
    cookie, and the default Python TLS fingerprint is rejected outright,
    so a Chrome-like cipher order is used.
    """

    site_name = "n17k"
    priority = 100
    BASE_URL = "https://www.17k.com"
    SEARCH_URL = "https://search.17k.com/search.xhtml"

    async def _fetch_html(self, keyword: str) -> str:
        params = {"c.st": "0", "c.q": keyword}
        headers = {
            **self.session.headers,
            "Referer": "https://www.17k.com/",
        }
        ssl_ctx = _chrome_ssl_context()
        try:
            async with self.session.get(
                self.SEARCH_URL, params=params, headers=headers, ssl=ssl_ctx
            ) as resp:
                resp.raise_for_status()
                text = await self._response_to_str(resp)

            if _ARG1_RE.search(text):
                cookie = self._solve_acw_sc_v2(text)
                if cookie:
                    jar = self.session.cookie_jar
                    if isinstance(jar, aiohttp.abc.AbstractCookieJar):
                        jar.update_cookies({"acw_sc__v2": cookie})
                async with self.session.get(
                    self.SEARCH_URL, params=params, headers=headers, ssl=ssl_ctx
                ) as resp:
                    resp.raise_for_status()
                    text = await self._response_to_str(resp)
            return text
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
        if not html_str:
            return []

        doc = html.fromstring(html_str)
        rows = doc.xpath('//div[@class="textlist"]')
        results: list[SearchResult] = []

        for idx, row in enumerate(rows):
            if limit is not None and idx >= limit:
                break

            href = self._first_str(
                row.xpath('.//div[@class="textleft"]/a[1]/@href')
            ) or self._first_str(
                row.xpath('.//div[@class="textmiddle"]//dt/a[1]/@href')
            )
            if not href:
                continue

            # '//www.17k.com/book/753943.html' -> "753943"
            book_id = href.rstrip(".html").rsplit("/", 1)[-1]
            book_url = self._abs_url(href)

            title = self._join_strs(
                row.xpath('.//div[@class="textmiddle"]//dt/a[1]//text()')
            )
            author = self._join_strs(
                row.xpath('.//li[@class="bq"]//span[@class="ls"]/a//text()')
            )

            wc_raw = self._first_str(
                row.xpath('.//span[starts-with(normalize-space(.),"字数")]/code/text()')
            )
            word_count = self._fmt_word_count(wc_raw)

            update_date = self._first_str(
                row.xpath('.//li[contains(.,"更新时间")]/code//text()')
            )

            cover_rel = self._first_str(
                row.xpath('.//div[@class="textleft"]//img/@src')
            )
            cover_url = self._abs_url(cover_rel) if cover_rel else ""

            prio = self.priority + idx

            results.append(
                SearchResult(
                    site=self.site_name,
                    book_id=book_id,
                    book_url=book_url,
                    cover_url=cover_url,
                    title=title,
                    author=author,
                    latest_chapter="-",
                    update_date=update_date or "-",
                    word_count=word_count,
                    priority=prio,
                )
            )
        return results

    @staticmethod
    def _fmt_word_count(raw: str) -> str:
        digits = raw.replace(",", "").strip()
        if digits.isdigit():
            return f"{int(digits) / 10000:.0f}万字"
        return raw or "-"

    @classmethod
    def _solve_acw_sc_v2(cls, text: str) -> str:
        """
        Solve the Aliyun WAF JS challenge embedded in ``text`` and return
        the value for the ``acw_sc__v2`` cookie.
        """
        m = _ARG1_RE.search(text)
        if not m:
            return ""
        arg1 = m.group(1)
        unsboxed = [""] * len(_WAF_POS)
        for i, ch in enumerate(arg1):
            for j, pos in enumerate(_WAF_POS):
                if pos == i + 1:
                    unsboxed[j] = ch
        a = "".join(unsboxed)
        b = _WAF_KEY
        out = []
        for i in range(0, min(len(a), len(b)) - 1, 2):
            out.append(format(int(a[i : i + 2], 16) ^ int(b[i : i + 2], 16), "02x"))
        return "".join(out)
