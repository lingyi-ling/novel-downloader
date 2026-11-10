#!/usr/bin/env python3
"""Offline tests for the shared sososhu searcher's request/URL handling.

These complement ``test_sososhu_searcher.py`` and focus on the parts that are
shared with every site configured through ``SOSOSHU_KEY``: query encoding,
cover/URL normalisation, tolerant result parsing and the text decoder.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from novel_downloader.plugins.common.searcher.sososhu import SososhuSearcher


class _Cookie:
    def __init__(self, value: str) -> None:
        self.value = value


class _Cookies:
    """Minimal ``SimpleCookie`` lookalike (``in`` + ``[key].value``)."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data = dict(data or {})

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> _Cookie:
        return _Cookie(self._data[key])


class _Resp:
    """Async context manager mimicking an aiohttp response."""

    def __init__(
        self,
        text: str = "",
        *,
        cookies: _Cookies | None = None,
        charset: str | None = None,
        status: int = 200,
        fail: bool = False,
    ) -> None:
        self._text = text
        self.cookies = cookies or _Cookies()
        self.charset = charset
        self.status = status
        self._fail = fail

    async def __aenter__(self) -> _Resp:
        if self._fail:
            raise RuntimeError(f"HTTP {self.status}")
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def read(self) -> bytes:
        return self._text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status >= 400 or self._fail:
            raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.get_queue: list[_Resp] = []
        self.post_queue: list[_Resp] = []
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []
        self.post_calls: list[str] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Resp:
        self.get_calls.append((url, params))
        return self.get_queue.pop(0)

    def post(
        self, url: str, data: Any = None, headers: dict[str, str] | None = None
    ) -> _Resp:
        self.post_calls.append(url)
        return self.post_queue.pop(0)


class _Searcher(SososhuSearcher):
    site_name = "sososhu"
    priority = 30
    SOSOSHU_KEY: ClassVar[str] = "17"
    BASE_URL: ClassVar[str] = "https://www.sososhu.com"


def _searcher(session: _Session) -> _Searcher:
    return _Searcher(session=session)  # type: ignore[arg-type]


def _result_page(*items: tuple[str, str, str]) -> str:
    """Build a result table from ``(href, title, cover)`` triples."""
    rows = "".join(
        f"<div class='item'><dl><dt><a href='{href}'>{title}</a>"
        "<span>作者</span></dt></dl>"
        f"<div class='image'><img src='{cover}'></div></div>"
        for href, title, cover in items
    )
    return (
        "<html><body><div class='so_list'><div class='hot'>"
        f"{rows}</div></div></body></html>"
    )


# --------------------------------------------------------------------------- #
# request building
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_keyword_is_percent_encoded_in_the_request_url() -> None:
    keyword = "斗罗 大陆&x=1#frag"
    session = _Session()
    session.get_queue.append(_Resp("<html>ok</html>"))

    await _searcher(session)._fetch_html(keyword)

    url, params = session.get_calls[0]
    # The query must stay a *single* q parameter, with ``site`` intact.
    parsed = parse_qs(urlparse(url).query, keep_blank_values=True)
    assert parsed == {"q": [keyword], "site": ["17"]}
    assert " " not in url
    assert params is None


@pytest.mark.asyncio
async def test_verification_post_reuses_the_encoded_url() -> None:
    keyword = "a&b c"
    session = _Session()
    session.get_queue.append(
        _Resp(
            "Checking your browser <script>var nonce = 7;</script>",
            cookies=_Cookies({"ge_ua_p": "ab1"}),
        )
    )
    session.post_queue.append(_Resp("verified"))
    session.get_queue.append(_Resp("<html>final</html>"))

    result = await _searcher(session)._fetch_html(keyword)

    assert result == "<html>final</html>"
    assert parse_qs(urlparse(session.post_calls[0]).query) == {
        "q": [keyword],
        "site": ["17"],
    }


@pytest.mark.asyncio
async def test_quoted_nonce_in_the_challenge_is_accepted() -> None:
    session = _Session()
    session.get_queue.append(
        _Resp(
            'Checking your browser var nonce = "42";',
            cookies=_Cookies({"ge_ua_p": "ab1"}),
        )
    )
    session.post_queue.append(_Resp("verified"))
    session.get_queue.append(_Resp("<html>final</html>"))

    result = await _searcher(session)._fetch_html("kw")

    assert result == "<html>final</html>"
    assert session.post_calls


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_string() -> None:
    session = _Session()
    session.get_queue.append(_Resp(fail=True))

    assert await _searcher(session)._fetch_html("kw") == ""


# --------------------------------------------------------------------------- #
# result parsing
# --------------------------------------------------------------------------- #
def test_covers_are_made_absolute_and_priorities_stay_row_based() -> None:
    html_str = _result_page(
        ("//www.example.com/book/1", "T1", "//cdn.example.com/1.jpg"),
        ("/book/2", "T2", "/img/2.jpg"),
        ("https://www.example.com/book/3", "T3", ""),
    )

    results = _searcher(_Session())._parse_html(html_str)

    assert [r["book_url"] for r in results] == [
        "https://www.example.com/book/1",
        "https://www.sososhu.com/book/2",
        "https://www.example.com/book/3",
    ]
    assert [r["cover_url"] for r in results] == [
        "https://cdn.example.com/1.jpg",
        "https://www.sososhu.com/img/2.jpg",
        "",
    ]
    assert [r["priority"] for r in results] == [30, 31, 32]


def test_limit_counts_returned_rows_not_skipped_ones() -> None:
    html_str = (
        "<html><body><div class='so_list'><div class='hot'>"
        "<div class='item'><dl><dt><a>无链接</a></dt></dl></div>"
        + "".join(
            f"<div class='item'><dl><dt><a href='/b/{i}'>{i}</a></dt></dl></div>"
            for i in (1, 2)
        )
        + "</div></div></body></html>"
    )

    results = _searcher(_Session())._parse_html(html_str, limit=2)

    assert [r["title"] for r in results] == ["1", "2"]


@pytest.mark.parametrize(
    "payload",
    ["", "   \n", "<!DOCTYPE html>", '{"code": 403}', "Checking your browser"],
)
def test_unusable_payloads_yield_no_results(payload: str) -> None:
    assert _searcher(_Session())._parse_html(payload) == []


# --------------------------------------------------------------------------- #
# text decoding
# --------------------------------------------------------------------------- #
class _RawResp:
    def __init__(self, data: bytes, charset: str | None = None) -> None:
        self._data = data
        self.charset = charset

    async def read(self) -> bytes:
        return self._data


@pytest.mark.asyncio
async def test_response_decoder_handles_gbk_and_utf8() -> None:
    gbk = "<html><title>中文标题</title></html>".encode("gbk")
    assert "中文标题" in await SososhuSearcher._response_to_str(_RawResp(gbk))

    utf8 = "<html><title>中文标题</title></html>".encode()
    resp = _RawResp(utf8, charset="utf-8")
    assert "中文标题" in await SososhuSearcher._response_to_str(resp)
