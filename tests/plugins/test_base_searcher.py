#!/usr/bin/env python3
"""Offline tests for :mod:`novel_downloader.plugins.base.searcher` helpers.

The searchers never open a real connection here: response objects are tiny
in-memory stand-ins exposing ``read()`` and ``charset``.
"""

from __future__ import annotations

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.schemas import SearchResult


class _Response:
    def __init__(self, data: bytes, charset: str | None = None) -> None:
        self._data = data
        self.charset = charset

    async def read(self) -> bytes:
        return self._data


class _Searcher(BaseSearcher):
    site_name = "demo"

    def __init__(self, html: str = "", parse_error: bool = False) -> None:
        super().__init__(session=object())  # type: ignore[arg-type]
        self.html = html
        self.parse_error = parse_error
        self.fetch_calls = 0
        self.parse_calls = 0
        self.last_limit: int | None = None

    async def _fetch_html(self, keyword: str) -> str:
        self.fetch_calls += 1
        return self.html

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        self.parse_calls += 1
        self.last_limit = limit
        if self.parse_error:
            raise AssertionError("_parse_html must not be called")
        return [
            {
                "site": self.site_name,
                "book_id": "1",
                "book_url": "https://demo/1",
                "cover_url": "",
                "title": "t",
                "author": "a",
                "latest_chapter": "",
                "update_date": "",
                "word_count": "",
                "priority": 1,
            }
        ]


class _ExplodingFetcher(BaseSearcher):
    site_name = "exploding"

    async def _fetch_html(self, keyword: str) -> str:
        raise RuntimeError("transport down")

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


# --------------------------------------------------------------------------- #
# search() orchestration boundary
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_fetch_result_short_circuits_without_parsing() -> None:
    searcher = _Searcher(html="", parse_error=True)

    assert await searcher.search("x") == []
    assert searcher.fetch_calls == 1
    assert searcher.parse_calls == 0


@pytest.mark.asyncio
async def test_non_empty_fetch_result_is_parsed_with_limit() -> None:
    searcher = _Searcher(html="<html/>")

    result = await searcher.search("x", limit=3)

    assert len(result) == 1
    assert searcher.parse_calls == 1
    assert searcher.last_limit == 3


@pytest.mark.asyncio
async def test_fetch_errors_propagate_to_the_caller() -> None:
    # Isolation is the orchestrator's job (asyncio.gather(return_exceptions=True)
    # / per-site except in search_stream), not BaseSearcher's.
    with pytest.raises(RuntimeError, match="transport down"):
        await _ExplodingFetcher(session=object()).search("x")  # type: ignore[arg-type]


def test_default_flags() -> None:
    assert BaseSearcher.priority == 1000
    assert BaseSearcher.explicitly_selected is False
    assert _Searcher().nsfw is False


# --------------------------------------------------------------------------- #
# _response_to_str(): encoding fallbacks
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_response_to_str_uses_requested_encoding() -> None:
    resp = _Response("正文".encode(), charset="gbk")

    assert await BaseSearcher._response_to_str(resp, "utf-8") == "正文"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_response_to_str_uses_declared_charset() -> None:
    resp = _Response("正文".encode("gbk"), charset="gbk")

    assert await BaseSearcher._response_to_str(resp) == "正文"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_response_to_str_falls_back_to_gb_family() -> None:
    resp = _Response("正文".encode("gbk"), charset=None)

    # utf-8 fails, the GB fallbacks decode successfully.
    assert await BaseSearcher._response_to_str(resp, "utf-8") == "正文"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_response_to_str_handles_unknown_codec_names() -> None:
    resp = _Response("正文".encode("gbk"), charset=None)

    # ``str.decode`` raises LookupError (not UnicodeDecodeError) here; the
    # helper must keep trying the remaining candidates.
    assert (
        await BaseSearcher._response_to_str(resp, "not-a-real-codec")  # type: ignore[arg-type]
        == "正文"
    )


@pytest.mark.asyncio
async def test_response_to_str_empty_body() -> None:
    assert await BaseSearcher._response_to_str(_Response(b"")) == ""  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_response_to_str_never_raises_for_undecodable_text() -> None:
    # GB18030/GBK accept almost any byte sequence, but the final fallback still
    # guarantees a string instead of an exception.
    weird = b"\xff\xfe\x00\x01"
    result = await BaseSearcher._response_to_str(
        _Response(weird, charset="utf-8"),
        "utf-8",  # type: ignore[arg-type]
    )
    assert isinstance(result, str)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def test_quote_percent_encodes_spaces_and_unicode() -> None:
    assert BaseSearcher._quote("a b") == "a+b"
    assert BaseSearcher._quote("中") == "%E4%B8%AD"


def test_build_url_does_not_encode_parameters() -> None:
    # Documented current behaviour: callers must pre-encode values.
    assert (
        BaseSearcher._build_url("https://x/s", {"q": "a b", "p": "1"})
        == "https://x/s?q=a b&p=1"
    )


def test_abs_url_variants() -> None:
    class _S(BaseSearcher):
        site_name = "s"
        BASE_URL = "https://example.com/root/"

        async def _fetch_html(self, keyword: str) -> str:
            return ""

        def _parse_html(
            self, html_str: str, limit: int | None = None
        ) -> list[SearchResult]:
            return []

    assert _S._abs_url("//cdn.example/x") == "https://cdn.example/x"
    assert _S._abs_url("https://other/x") == "https://other/x"
    assert _S._abs_url("chapter/1") == "https://example.com/root/chapter/1"


def test_first_str_and_join_strs() -> None:
    assert BaseSearcher._first_str([]) == ""
    assert BaseSearcher._first_str(["  x  "]) == "x"
    assert BaseSearcher._first_str(["a-b"], [("-", "_")]) == "a_b"

    # Searchers concatenate fragments (unlike parsers, which join with "\n").
    assert BaseSearcher._join_strs([" a ", "", " b "]) == "ab"
    assert BaseSearcher._join_strs([]) == ""
