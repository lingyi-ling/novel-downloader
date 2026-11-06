#!/usr/bin/env python3
"""Chapter parsing tests for the fanqienovel plugin.

The site answers a locked chapter with a short teaser in exactly the same HTML
shape as a real chapter, so the only thing that distinguishes "downloaded" from
"preview" is ``isChapterLock``.  Treating the teaser as content silently
truncates the book; these tests pin the distinction.
"""

from __future__ import annotations

import json
from typing import Any

from novel_downloader.plugins.sites.fanqienovel.parser import FanqienovelParser
from novel_downloader.schemas import ParserConfig


def _parser() -> FanqienovelParser:
    return FanqienovelParser(ParserConfig())


def _page(chapter_data: dict[str, Any]) -> str:
    state = {"reader": {"chapterData": chapter_data}}
    payload = json.dumps(state, ensure_ascii=False)
    return (
        "<html><head><title>x</title></head><body>"
        f"<script>window.__INITIAL_STATE__ = {payload};</script>"
        "</body></html>"
    )


def test_unlocked_chapter_is_parsed() -> None:
    raw = _page(
        {
            "title": "第1章 部族生死战",
            "content": "<p>第一段</p><p>第二段</p>",
            "isChapterLock": False,
            "needPay": 0,
        }
    )

    result = _parser().parse_chapter_content([raw], "7069952706521236001")

    assert result is not None
    assert result["title"] == "第1章 部族生死战"
    assert result["content"] == "第一段\n第二段"
    assert result["id"] == "7069952706521236001"


def test_locked_chapter_is_reported_as_missing() -> None:
    """A locked chapter carries a teaser; it must not be stored as content."""
    raw = _page(
        {
            "title": "第280章 练题",
            "content": "<p>只有两百字的预览</p>",
            "isChapterLock": True,
            "needPay": 0,
        }
    )

    assert _parser().parse_chapter_content([raw], "7704667598306165273") is None


def test_paid_chapter_is_treated_as_locked() -> None:
    """``needPay`` is the fallback when the lock flag is absent."""
    raw = _page(
        {
            "title": "第2章",
            "content": "<p>预览</p>",
            "needPay": 30,
        }
    )

    assert _parser().parse_chapter_content([raw], "2") is None


def test_empty_content_returns_none() -> None:
    raw = _page({"title": "第1章", "content": "", "isChapterLock": False})

    assert _parser().parse_chapter_content([raw], "1") is None


def test_missing_chapter_data_returns_none() -> None:
    raw = "<html><script>window.__INITIAL_STATE__ = {};</script></html>"

    assert _parser().parse_chapter_content([raw], "1") is None


def test_no_pages_returns_none() -> None:
    assert _parser().parse_chapter_content([], "1") is None


def test_whitespace_only_paragraphs_return_none() -> None:
    raw = _page(
        {"title": "第1章", "content": "<p>   </p><p></p>", "isChapterLock": False}
    )

    assert _parser().parse_chapter_content([raw], "1") is None
