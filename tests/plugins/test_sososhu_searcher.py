#!/usr/bin/env python3
"""Offline unit tests for the sososhu common searcher.

The aiohttp session is replaced with an in-memory double that supports the
async context-manager protocol, covering the anti-bot verification flow
(cookie + nonce + sum POST) and the result-table parser without any network.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from novel_downloader.plugins.common.searcher.sososhu import SososhuSearcher
from novel_downloader.schemas import SearchResult


class FakeCookie:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeCookies:
    """Mapping with SimpleCookie-like access (entry objects)."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data = data or {}

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> FakeCookie:
        return FakeCookie(self._data[key])


class FakeRespCtx:
    """Async context manager returned by session.get/post."""

    def __init__(
        self,
        text: str = "",
        status: int = 200,
        cookies: FakeCookies | None = None,
        raise_status: bool = False,
        charset: str | None = None,
    ) -> None:
        self._text = text
        self.status = status
        self.charset = charset
        self.cookies = cookies or FakeCookies()
        self.raise_status = raise_status
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeRespCtx:
        self.entered += 1
        if self.raise_status:
            raise RuntimeError(f"HTTP {self.status}")
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.exited += 1

    async def read(self) -> bytes:
        return self._text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {"User-Agent": "ua"}
        self.get_queue: list[FakeRespCtx] = []
        self.post_queue: list[FakeRespCtx] = []
        self.get_urls: list[tuple[str, dict[str, str] | None]] = []
        self.post_calls: list[tuple[str, str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeRespCtx:
        self.get_urls.append((url, params))
        ctx = self.get_queue.pop(0)
        return ctx

    def post(
        self, url: str, data: Any = None, headers: dict[str, str] | None = None
    ) -> FakeRespCtx:
        self.post_calls.append((url, str(data), headers or {}))
        return self.post_queue.pop(0)


class _Sososhu(SososhuSearcher):
    site_name = "sososhu"
    priority = 30
    SOSOSHU_KEY: ClassVar[str] = "17"
    BASE_URL: ClassVar[str] = "https://www.sososhu.com"


def _searcher(session: FakeSession) -> _Sososhu:
    return _Sososhu(session=session)  # type: ignore[arg-type]


def _html(*items: tuple[str, str, str, str]) -> str:
    rows = "".join(
        f"<div class='item'><dl><dt>"
        f"<a href='{href}'>{title}</a><span>{author}</span></dt></dl>"
        f"<div class='image'><img src='{cover}'></div></div>"
        for href, title, author, cover in items
    )
    return (
        "<html><body><div class='so_list'><div class='hot'>"
        f"{rows}</div></div></body></html>"
    )


# --------------------------------------------------------------------------- #
# calc_sum
# --------------------------------------------------------------------------- #


def test_calc_sum_matches_js_charcode_logic() -> None:
    # 'a' * 1 * (nonce + 0) + 'b' * 1 * (nonce + 1)
    assert SososhuSearcher.calc_sum("ab", 10) == 97 * 10 + 98 * 11
    # Non-alphanumeric characters are skipped.
    assert SososhuSearcher.calc_sum("a-!", 5) == 97 * 5
    assert SososhuSearcher.calc_sum("", 0) == 0


# --------------------------------------------------------------------------- #
# _fetch_html / _fetch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_html_returns_empty_on_transport_error() -> None:
    session = FakeSession()
    session.get_queue.append(FakeRespCtx(raise_status=True))
    searcher = _searcher(session)
    assert await searcher._fetch_html("keyword") == ""


@pytest.mark.asyncio
async def test_fetch_plain_success_without_antibot() -> None:
    session = FakeSession()
    session.get_queue.append(FakeRespCtx("<html>ok</html>"))
    searcher = _searcher(session)
    text = await searcher._fetch_html("keyword")
    assert text == "<html>ok</html>"
    url, params = session.get_urls[0]
    assert url.startswith("https://www.sososhu.com/?q=")
    assert "site=17" in url
    assert params is None  # query is already embedded in the URL


@pytest.mark.asyncio
async def test_fetch_returns_challenge_page_without_cookie() -> None:
    challenge = "Checking your browser before accessing"
    session = FakeSession()
    session.get_queue.append(FakeRespCtx(challenge))
    searcher = _searcher(session)
    assert await searcher._fetch("https://www.sososhu.com/", {"q": "x"}) == challenge


@pytest.mark.asyncio
async def test_fetch_challenge_without_nonce_returns_page() -> None:
    challenge = "Checking your browser <script>var other = 1;</script>"
    session = FakeSession()
    session.get_queue.append(
        FakeRespCtx(challenge, cookies=FakeCookies({"ge_ua_p": "abc"}))
    )
    searcher = _searcher(session)
    result = await searcher._fetch("https://www.sososhu.com/", {"q": "x"})
    assert result == challenge
    assert session.post_calls == []


@pytest.mark.asyncio
async def test_fetch_full_verification_flow() -> None:
    challenge = "Checking your browser <script>var nonce = 42;</script>"
    session = FakeSession()
    session.get_queue.append(
        FakeRespCtx(challenge, cookies=FakeCookies({"ge_ua_p": "ab1"}))
    )
    session.post_queue.append(FakeRespCtx("verified"))
    session.get_queue.append(FakeRespCtx("<html>final</html>"))

    searcher = _searcher(session)
    result = await searcher._fetch("https://www.sososhu.com/", {"q": "x"})
    assert result == "<html>final</html>"

    url, payload, headers = session.post_calls[0]
    assert url == "https://www.sososhu.com/?q=x"
    assert payload == "sum=" + str(SososhuSearcher.calc_sum("ab1", 42)) + "&nonce=42"
    assert headers["X-GE-UA-Step"] == "prev"
    assert headers["Cookie"] == "ge_ua_p=ab1"

    # The final GET re-requests the original URL and params.
    last_url, last_params = session.get_urls[-1]
    assert last_url == "https://www.sososhu.com/"
    assert last_params == {"q": "x"}


# --------------------------------------------------------------------------- #
# _parse_html
# --------------------------------------------------------------------------- #


def test_parse_html_extracts_results() -> None:
    session = FakeSession()
    searcher = _searcher(session)
    html_str = _html(
        ("//www.example.com/book/1", "Title One", "Alice", "http://c/1.jpg"),
        ("https://www.example.com/book/2", "Title Two", "Bob", ""),
    )
    results = searcher._parse_html(html_str)

    assert len(results) == 2
    first: SearchResult = results[0]
    assert first["site"] == "sososhu"
    assert first["title"] == "Title One"
    assert first["author"] == "Alice"
    assert first["book_url"] == "https://www.example.com/book/1"
    assert first["cover_url"] == "http://c/1.jpg"
    assert first["priority"] == 30
    assert results[1]["priority"] == 31


def test_parse_html_limit_caps_results() -> None:
    session = FakeSession()
    searcher = _searcher(session)
    html_str = _html(
        ("//x/1", "T1", "A1", ""),
        ("//x/2", "T2", "A2", ""),
        ("//x/3", "T3", "A3", ""),
    )
    results = searcher._parse_html(html_str, limit=2)
    assert [r["title"] for r in results] == ["T1", "T2"]


def test_parse_html_skips_rows_without_href() -> None:
    session = FakeSession()
    searcher = _searcher(session)
    html_str = (
        "<html><body><div class='so_list'><div class='hot'>"
        "<div class='item'><dl><dt><a></a></dt></dl></div>"
        "</div></div></body></html>"
    )
    assert searcher._parse_html(html_str) == []


def test_parse_html_empty_document() -> None:
    session = FakeSession()
    searcher = _searcher(session)
    assert searcher._parse_html("<html></html>") == []
