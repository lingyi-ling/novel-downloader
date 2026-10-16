#!/usr/bin/env python3
import json
from pathlib import Path

import pytest

from novel_downloader.plugins.sites.fanqienovel.searcher import FanqienovelSearcher
from novel_downloader.schemas import SearchResult

_SEARCH_PAYLOAD = {
    "code": 0,
    "data": {
        "search_book_data_list": [
            {
                "book_id": "7143038691195",
                "book_name": "斗破苍穹",
                "author": "天蚕土豆",
                "thumb_url": "//example.com/cover.jpg",
                "word_count": 5353400,
                "last_chapter_title": "第1623章 大结局",
                "last_chapter_time": 1706000000,
            },
            {
                "book_id": "7143038691196",
                "book_name": "武破苍穹",
                "author": "破苍天",
                "thumb_url": "http://example.com/cover2.jpg",
                "word_count": 540000,
                "last_chapter_title": "第四十五章 神奇小兽",
                "last_chapter_time": "2023-04-20 18:22:08",
            },
        ],
        "search_author_data_list": [],
        "total_count": 2,
    },
}


def test_searcher_parse_html() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    results = searcher._parse_html(json.dumps(_SEARCH_PAYLOAD, ensure_ascii=False))
    assert results == [
        SearchResult(
            site="fanqienovel",
            book_id="7143038691195",
            book_url="https://fanqienovel.com/page/7143038691195",
            cover_url="https://example.com/cover.jpg",
            title="斗破苍穹",
            author="天蚕土豆",
            latest_chapter="第1623章 大结局",
            update_date="2024-01-23",
            word_count="5353400",
            priority=5,
        ),
        SearchResult(
            site="fanqienovel",
            book_id="7143038691196",
            book_url="https://fanqienovel.com/page/7143038691196",
            cover_url="https://example.com/cover2.jpg",
            title="武破苍穹",
            author="破苍天",
            latest_chapter="第四十五章 神奇小兽",
            update_date="2023-04-20 18:22:08",
            word_count="540000",
            priority=6,
        ),
    ]


def test_searcher_parse_html_handles_garbage() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    assert searcher._parse_html("<<<not json>>>") == []
    assert searcher._parse_html("") == []
    assert searcher._parse_html("[]") == []
    assert searcher._parse_html(json.dumps({"data": {}})) == []


def test_searcher_skips_items_without_book_id() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    payload = {"data": {"search_book_data_list": [{"book_name": "无ID"}, "junk"]}}
    assert searcher._parse_html(json.dumps(payload)) == []


def test_searcher_respects_limit() -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    items = [{"book_id": str(i), "book_name": f"书{i}"} for i in range(5)]
    payload = json.dumps({"data": {"search_book_data_list": items}})
    assert len(searcher._parse_html(payload, limit=2)) == 2


def test_load_cookies_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FANQIENOVEL_COOKIE", "a=1; b=2")
    assert FanqienovelSearcher._load_cookies() == {"a": "1", "b": "2"}


def test_load_cookies_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FANQIENOVEL_COOKIE", raising=False)
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    cookie_dir = tmp_path / "fanqienovel"
    cookie_dir.mkdir()
    (cookie_dir / "aiohttp.cookies").write_text(
        json.dumps([{"name": "sessionid", "value": "abc"}]),
        encoding="utf-8",
    )
    assert FanqienovelSearcher._load_cookies() == {"sessionid": "abc"}


@pytest.mark.asyncio
async def test_fetch_without_cookie_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = FanqienovelSearcher.__new__(FanqienovelSearcher)
    searcher._cookies = {}
    assert await searcher._fetch_html("斗破苍穹") == ""
