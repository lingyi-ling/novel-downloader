#!/usr/bin/env python3
"""Fetcher tests: rule-driven URL expansion and pagination walking."""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.plugins.rule.fetcher import RuleFetcher
from novel_downloader.plugins.rule.model import Rule, parse_rule

BASE = "https://demo.example"

RULE_DICT: dict[str, Any] = {
    "site_key": "demo",
    "base_url": BASE,
    "book": {
        "url": "{base_url}/book/{book_id}/",
        "pages": {"url": "{base_url}/book/{book_id}/index_{page}.html", "start": 2},
        "chapters": {"item": "div.list a"},
    },
    "chapter": {
        "url": "{base_url}/book/{book_id}/{chapter_id}.html",
        "pages": {
            "url": "{base_url}/book/{book_id}/{chapter_id}_{page}.html",
            "start": 2,
        },
        "content": {"selector": "article"},
    },
}

BOOK_URL = f"{BASE}/book/7/"
CHAPTER_URL = f"{BASE}/book/7/5.html"


def _pager(next_page: int) -> str:
    return f'<a href="index_{next_page}.html">下一页</a>'


class _StubFetcher(RuleFetcher):
    """A :class:`RuleFetcher` whose HTTP layer is replaced by a URL->HTML map."""

    site_name = "demo"

    def __init__(
        self,
        rule: Rule,
        pages: dict[str, str],
        failing: set[str] | None = None,
    ) -> None:
        self._rule = rule
        self._base_url = ""
        self.pages = pages
        self.failing = failing or set()
        self.sent_headers: list[dict[str, str]] = []

    async def fetch(self, url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
        self.sent_headers.append(dict(kwargs.get("headers") or {}))
        if url in self.failing:
            raise RuntimeError("boom")
        return self.pages[url]


def make_fetcher(
    pages: dict[str, str], failing: set[str] | None = None
) -> _StubFetcher:
    return _StubFetcher(parse_rule(RULE_DICT), pages, failing)


# --------------------------------------------------------------------------- #
# book info
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_book_info_without_pages_section() -> None:
    pages = {BOOK_URL: "<div>only one page</div>"}

    assert await make_fetcher(pages).fetch_book_info("7") == [pages[BOOK_URL]]


@pytest.mark.asyncio
async def test_book_info_walks_extra_pages_once_each() -> None:
    pages = {
        BOOK_URL: _pager(2),
        f"{BASE}/book/7/index_2.html": _pager(3),
        f"{BASE}/book/7/index_3.html": "<div>最后一页</div>",
    }

    fetched = await make_fetcher(pages).fetch_book_info("7")

    assert fetched == [
        _pager(2),
        _pager(3),
        "<div>最后一页</div>",
    ]


@pytest.mark.asyncio
async def test_book_info_stops_when_next_link_is_absent() -> None:
    pages = {
        BOOK_URL: _pager(2),
        f"{BASE}/book/7/index_2.html": "<div>没有下一页链接</div>",
    }

    fetched = await make_fetcher(pages).fetch_book_info("7")

    assert fetched == [_pager(2), "<div>没有下一页链接</div>"]


@pytest.mark.asyncio
async def test_book_info_requires_book_section() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
        }
    )
    with pytest.raises(NotImplementedError, match="no 'book' section"):
        await _StubFetcher(rule, {}).fetch_book_info("7")


# --------------------------------------------------------------------------- #
# chapter content
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chapter_content_walks_extra_pages_once_each() -> None:
    pages = {
        CHAPTER_URL: '<article>第一页</article><a href="5_2.html">下一页</a>',
        f"{BASE}/book/7/5_2.html": "<article>第二页</article>",
    }

    fetched = await make_fetcher(pages).fetch_chapter_content("7", "5")

    assert fetched == [
        '<article>第一页</article><a href="5_2.html">下一页</a>',
        "<article>第二页</article>",
    ]


@pytest.mark.asyncio
async def test_chapter_content_requires_chapter_section() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "book": {"url": "{base_url}/{book_id}/", "chapters": {"item": "a"}},
        }
    )
    with pytest.raises(NotImplementedError, match="no 'chapter' section"):
        await _StubFetcher(rule, {}).fetch_chapter_content("7", "5")


@pytest.mark.asyncio
async def test_section_headers_override_global_headers() -> None:
    rule_dict = {
        **RULE_DICT,
        "headers": {"Referer": f"{BASE}/", "User-Agent": "global"},
        "chapter": {
            **RULE_DICT["chapter"],
            "headers": {
                "User-Agent": "chapter",
                "Referer": "{base_url}/book/{book_id}/",
            },
        },
    }
    pages = {CHAPTER_URL: "<article>正文</article>"}

    fetcher = _StubFetcher(parse_rule(rule_dict), pages)
    await fetcher.fetch_chapter_content("7", "5")

    assert fetcher.sent_headers == [
        {
            "Referer": f"{BASE}/book/7/",
            "User-Agent": "chapter",
        }
    ]


# --------------------------------------------------------------------------- #
# resilience
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_failed_extra_page_stops_walking() -> None:
    pages = {
        BOOK_URL: _pager(2),
        f"{BASE}/book/7/index_2.html": _pager(3),
    }

    fetcher = make_fetcher(pages, failing={f"{BASE}/book/7/index_2.html"})
    assert await fetcher.fetch_book_info("7") == [_pager(2)]


@pytest.mark.asyncio
async def test_pagination_is_capped() -> None:
    pages = {BOOK_URL: _pager(2)}
    for page in range(2, 52):
        pages[f"{BASE}/book/7/index_{page}.html"] = _pager(page + 1)

    fetched = await make_fetcher(pages).fetch_book_info("7")

    assert len(fetched) == 51


# --------------------------------------------------------------------------- #
# fixed-range pagination (max_pages)
# --------------------------------------------------------------------------- #
def _max_pages_rule(max_pages: int) -> Rule:
    return parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "book": {
                "url": "{base_url}/book/{book_id}/",
                "pages": {
                    "url": "{base_url}/book/{book_id}/index_{page}.html",
                    "start": 2,
                    "max_pages": max_pages,
                },
                "chapters": {"item": "div.list a"},
            },
        }
    )


@pytest.mark.asyncio
async def test_max_pages_fetches_the_fixed_range() -> None:
    # JSON-style endpoints never reference the next page, so the fetcher must
    # not depend on the "next link present" heuristic.
    pages = {
        BOOK_URL: "<div>无下一页链接</div>",
        f"{BASE}/book/7/index_2.html": "<div>第二页</div>",
        f"{BASE}/book/7/index_3.html": "<div>第三页</div>",
    }

    fetched = await _StubFetcher(_max_pages_rule(3), pages).fetch_book_info("7")

    assert fetched == [
        "<div>无下一页链接</div>",
        "<div>第二页</div>",
        "<div>第三页</div>",
    ]


@pytest.mark.asyncio
async def test_max_pages_stops_at_the_limit() -> None:
    pages = {BOOK_URL: _pager(2)}
    for page in range(2, 10):
        pages[f"{BASE}/book/7/index_{page}.html"] = _pager(page + 1)

    fetched = await _StubFetcher(_max_pages_rule(3), pages).fetch_book_info("7")

    assert len(fetched) == 3


@pytest.mark.asyncio
async def test_max_pages_tolerates_a_missing_page() -> None:
    pages = {BOOK_URL: "<div>第一页</div>"}

    fetched = await _StubFetcher(_max_pages_rule(4), pages).fetch_book_info("7")

    assert fetched == ["<div>第一页</div>"]


@pytest.mark.asyncio
async def test_max_pages_applies_to_chapter_content() -> None:
    rule = parse_rule(
        {
            "site_key": "demo",
            "base_url": BASE,
            "chapter": {
                "url": "{base_url}/book/{book_id}/{chapter_id}.html",
                "pages": {
                    "url": "{base_url}/book/{book_id}/{chapter_id}_{page}.html",
                    "start": 2,
                    "max_pages": 3,
                },
                "content": {"selector": "article"},
            },
        }
    )
    pages = {
        CHAPTER_URL: "<article>第一页</article>",
        f"{BASE}/book/7/5_2.html": "<article>第二页</article>",
        f"{BASE}/book/7/5_3.html": "<article>第三页</article>",
    }

    fetched = await _StubFetcher(rule, pages).fetch_chapter_content("7", "5")

    assert fetched == [
        "<article>第一页</article>",
        "<article>第二页</article>",
        "<article>第三页</article>",
    ]
