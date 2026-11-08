#!/usr/bin/env python3
"""Offline tests for searcher behaviour on slow, blocked or far-away sites.

These tests pin the changes made while triaging aggregate-search timeouts:

* an expected site-level failure (timeout, connection/DNS error, HTTP error
  status) must be logged as a WARNING, not as an ERROR, while unexpected
  exceptions keep the historical ERROR level;
* ``westnovel_sub``'s listing fallback must respect a wall-clock budget and
  stop at ``limit`` instead of always running into the caller's per-site
  deadline;
* the ``mangg_net`` search-page parser is exercised against the real result
  markup (``col-12 col-md-6`` cards with ``dd.book_other`` rows), and the
  searchers of the sibling domains are shown to share that one implementation
  (which is why one failing base searcher logs several domains).

No test touches the network: sessions and responses are in-memory doubles.
"""

from __future__ import annotations

import logging
import types
from typing import Any

import aiohttp
import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.sites.biquge5.searcher import Biquge5Searcher
from novel_downloader.plugins.sites.biquguo.searcher import BiquguoSearcher
from novel_downloader.plugins.sites.bxwx9.searcher import Bxwx9Searcher
from novel_downloader.plugins.sites.ciluke.searcher import CilukeSearcher
from novel_downloader.plugins.sites.fsshu.searcher import FsshuSearcher
from novel_downloader.plugins.sites.ktshu.searcher import KtshuSearcher
from novel_downloader.plugins.sites.mangg_net.searcher import ManggNetSearcher
from novel_downloader.plugins.sites.n37yue.searcher import N37yueSearcher
from novel_downloader.plugins.sites.westnovel_sub.searcher import (
    WestnovelSubSearcher,
)
from novel_downloader.schemas import SearchResult


class _LogSearcher(BaseSearcher):
    """Searcher used only to reach the shared logging helper."""

    site_name = "log_demo"

    async def _fetch_html(self, keyword: str) -> str:  # pragma: no cover
        return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:  # pragma: no cover
        return []


def levels(records: list[logging.LogRecord]) -> list[int]:
    return [record.levelno for record in records]


# --------------------------------------------------------------------------- #
# Log levels for expected site-level failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientError("blocked by the network"),
        aiohttp.ClientResponseError(
            request_info=types.SimpleNamespace(  # type: ignore[arg-type]
                real_url="https://example.invalid/search"
            ),
            history=(),
            status=403,
            message="Forbidden",
        ),
        TimeoutError(),
        OSError("connection reset"),
    ],
)
def test_expected_fetch_failures_log_at_warning(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    searcher = _LogSearcher(session=object())  # type: ignore[arg-type]

    with caplog.at_level(
        logging.DEBUG, logger="novel_downloader.plugins.base.searcher"
    ):
        searcher._log_fetch_failure("斗破", "https://example.invalid/search", exc)

    records = [
        r for r in caplog.records if r.name == "novel_downloader.plugins.base.searcher"
    ]
    assert levels(records) == [logging.WARNING]
    assert "斗破" in caplog.text


def test_unexpected_fetch_failures_stay_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    searcher = _LogSearcher(session=object())  # type: ignore[arg-type]

    with caplog.at_level(
        logging.DEBUG, logger="novel_downloader.plugins.base.searcher"
    ):
        searcher._log_fetch_failure(
            "斗破", "https://example.invalid/search", ValueError("bad selector")
        )

    records = [
        r for r in caplog.records if r.name == "novel_downloader.plugins.base.searcher"
    ]
    assert levels(records) == [logging.ERROR]


class _BoomSession:
    """Session whose ``get`` raises immediately (no socket is opened)."""

    headers: dict[str, str] = {}

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_mangg_net_transport_failure_is_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    searcher = ManggNetSearcher(_BoomSession(aiohttp.ClientError("blocked")))  # type: ignore[arg-type]

    with caplog.at_level(
        logging.DEBUG, logger="novel_downloader.plugins.base.searcher"
    ):
        assert await searcher._fetch_html("斗破") == ""

    records = [
        r for r in caplog.records if r.name == "novel_downloader.plugins.base.searcher"
    ]
    assert levels(records) == [logging.WARNING]


@pytest.mark.asyncio
async def test_mangg_net_unexpected_failure_is_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    searcher = ManggNetSearcher(_BoomSession(ValueError("broken")))  # type: ignore[arg-type]

    with caplog.at_level(
        logging.DEBUG, logger="novel_downloader.plugins.base.searcher"
    ):
        assert await searcher._fetch_html("斗破") == ""

    records = [
        r for r in caplog.records if r.name == "novel_downloader.plugins.base.searcher"
    ]
    assert levels(records) == [logging.ERROR]


# --------------------------------------------------------------------------- #
# mangg_net search-page parsing (markup taken from the live results page)
# --------------------------------------------------------------------------- #
_MANGG_SEARCH_HTML = """
<html><body>
<div class="row">
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="/id21201/"><img src="/images/21/21201/21201s.jpg"></a></dt>
      <dd><h3><a href="/id21201/">[网游]斗破苍穹之扶弟狂魔</a></h3></dd>
      <dd class="book_other">作者：<span>没感情的喵</span></dd>
      <dd class="book_other">状态：连载</dd>
      <dd class="book_other">更新时间：09-17 04:08:53</dd>
      <dd class="book_other">
        最新章节：<a href="/id21201/644621.html">第六十六章 让我猜猜你是什么系统</a>
      </dd>
    </dl>
  </div>
  <div class="col-12 col-md-6">
    <dl>
      <dt><a href="/id34665/"><img src="/images/34/34665/34665s.jpg"></a></dt>
      <dd><h3><a href="/id34665/">[网游]斗破里的鸿蒙灵宝</a></h3></dd>
      <dd class="book_other">作者：<span>手撕洋葱</span></dd>
      <dd class="book_other">状态：连载</dd>
      <dd class="book_other">更新时间：09-24 02:20:07</dd>
      <dd class="book_other">
        最新章节：<a href="/id34665/149833.html">第293章 骄傲</a>
      </dd>
    </dl>
  </div>
</div>
</body></html>
"""


def test_mangg_net_parses_live_search_result_markup() -> None:
    searcher = ManggNetSearcher(session=object())  # type: ignore[arg-type]

    rows = searcher._parse_html(_MANGG_SEARCH_HTML)

    assert [r["book_id"] for r in rows] == ["id21201", "id34665"]
    first = rows[0]
    assert first["book_url"] == "https://www.mangg.net/id21201/"
    assert first["cover_url"] == "https://www.mangg.net/images/21/21201/21201s.jpg"
    assert first["title"] == "[网游]斗破苍穹之扶弟狂魔"
    assert first["author"] == "没感情的喵"
    assert first["latest_chapter"] == "第六十六章 让我猜猜你是什么系统"
    assert first["update_date"] == "09-17 04:08:53"
    assert first["site"] == "mangg_net"


def test_mangg_net_parse_respects_limit() -> None:
    searcher = ManggNetSearcher(session=object())  # type: ignore[arg-type]

    rows = searcher._parse_html(_MANGG_SEARCH_HTML, limit=1)

    assert [r["book_id"] for r in rows] == ["id21201"]


def test_mangg_template_domains_share_one_searcher() -> None:
    # The log for ``mangg_net`` shows ciluke/ktshu/bxwx9/biquguo/fsshu/37yue
    # because those sites subclass the mangg_net searcher and inherit its
    # ``_fetch_html``; only the site key and BASE_URL differ.
    for cls in (
        CilukeSearcher,
        KtshuSearcher,
        Bxwx9Searcher,
        BiquguoSearcher,
        Biquge5Searcher,
        FsshuSearcher,
        N37yueSearcher,
    ):
        assert issubclass(cls, ManggNetSearcher)
        assert cls._fetch_html is ManggNetSearcher._fetch_html
        assert cls._parse_html is ManggNetSearcher._parse_html
        assert cls.SEARCH_URL.endswith("/search.php")


# --------------------------------------------------------------------------- #
# westnovel_sub: bounded listing fallback
# --------------------------------------------------------------------------- #
class _HTMLResponse:
    def __init__(self, body: str) -> None:
        self._body = body
        self.charset = "utf-8"

    async def read(self) -> bytes:
        return self._body.encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> _HTMLResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _ListingSession:
    """In-memory stand-in for the listing site."""

    headers: dict[str, str] = {}

    def __init__(self, pages: dict[str, str], *, total: float | None = 8.0) -> None:
        self.pages = pages
        self.timeout = aiohttp.ClientTimeout(total=total)
        self.urls: list[str] = []

    def get(self, url: str, timeout: Any = None) -> _HTMLResponse:
        self.urls.append(url)
        return _HTMLResponse(self.pages.get(url, ""))


def _listing_page(title: str, book_id: str = "8156") -> str:
    href = f"/q/list/{book_id}.html"
    return (
        '<html><body><div id="sitebox"><dl>'
        f'<dt><a href="{href}"><img src="/q/images/{book_id}.jpg"></a></dt>'
        f'<dd><h3><a href="{href}">{title}</a></h3></dd>'
        "</dl></div></body></html>"
    )


@pytest.mark.asyncio
async def test_westnovel_sub_listing_parses_and_stops_at_limit() -> None:
    page = _listing_page("斗破苍穹")
    session = _ListingSession({WestnovelSubSearcher.LIST_URL: page})
    searcher = WestnovelSubSearcher(session)  # type: ignore[arg-type]
    searcher._query = "斗破"

    results = await searcher._search_listing("斗破", 1)

    assert [r["book_id"] for r in results] == ["q-list-8156"]
    assert results[0]["book_url"] == "https://www.westnovel.com/q/list/8156.html"
    assert results[0]["cover_url"] == "https://www.westnovel.com/q/images/8156.jpg"
    # One batch of concurrent page fetches is enough; the remaining pages are
    # never requested once ``limit`` is reached.
    assert len(session.urls) == WestnovelSubSearcher._LIST_CONCURRENCY


@pytest.mark.asyncio
async def test_westnovel_sub_listing_honours_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ListingSession({}, total=None)
    searcher = WestnovelSubSearcher(session)  # type: ignore[arg-type]
    searcher._query = "斗破"
    monkeypatch.setattr(WestnovelSubSearcher, "SCAN_BUDGET_SECONDS", 0.0)

    assert await searcher._search_listing("斗破", 1) == []
    # An exhausted budget means no listing request is started at all.
    assert session.urls == []


def test_westnovel_sub_budget_is_bounded_by_session_timeout() -> None:
    default = WestnovelSubSearcher(_ListingSession({}, total=None))  # type: ignore[arg-type]
    capped = WestnovelSubSearcher(_ListingSession({}, total=1.0))  # type: ignore[arg-type]
    generous = WestnovelSubSearcher(_ListingSession({}, total=60.0))  # type: ignore[arg-type]

    assert default._budget() == WestnovelSubSearcher.SCAN_BUDGET_SECONDS
    assert capped._budget() == 0.5
    assert generous._budget() == WestnovelSubSearcher.SCAN_BUDGET_SECONDS
