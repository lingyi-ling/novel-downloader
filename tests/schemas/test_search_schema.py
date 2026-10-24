#!/usr/bin/env python3
"""Contract tests for :class:`~novel_downloader.schemas.search.SearchResult`."""

from __future__ import annotations

import json
from typing import get_type_hints

from novel_downloader.schemas import SearchResult


def _sample(**overrides: object) -> SearchResult:
    data: dict[str, object] = {
        "site": "demo",
        "book_id": "42",
        "book_url": "https://example.com/book/42",
        "cover_url": "https://example.com/42.jpg",
        "title": "Book",
        "author": "Author",
        "latest_chapter": "Chapter 10",
        "update_date": "2026-01-02",
        "word_count": "10万字",
        "priority": 100,
    }
    data.update(overrides)
    return data  # type: ignore[return-value]


def test_all_fields_are_required() -> None:
    assert SearchResult.__required_keys__ == frozenset(
        {
            "site",
            "book_id",
            "book_url",
            "cover_url",
            "title",
            "author",
            "latest_chapter",
            "update_date",
            "word_count",
            "priority",
        }
    )
    assert SearchResult.__optional_keys__ == frozenset()


def test_type_hints() -> None:
    hints = get_type_hints(SearchResult)

    for name in (
        "site",
        "book_id",
        "book_url",
        "cover_url",
        "title",
        "author",
        "latest_chapter",
        "update_date",
        "word_count",
    ):
        assert hints[name] is str
    assert hints["priority"] is int


def test_construction() -> None:
    result = _sample()

    assert result["site"] == "demo"
    assert result["priority"] == 100


def test_unknown_fields_are_tolerated() -> None:
    result = _sample(extra_rank=1.5)

    assert result["extra_rank"] == 1.5


def test_no_runtime_type_validation() -> None:
    # TypedDict construction is just dict construction.
    result = SearchResult(
        site=1,
        book_id=None,
        book_url=[],
        cover_url={},
        title=2,
        author=3,
        latest_chapter=4,
        update_date=5,
        word_count=6,
        priority="high",
    )  # type: ignore[typeddict-item]

    assert result["priority"] == "high"


def test_json_round_trip() -> None:
    result = _sample()

    assert json.loads(json.dumps(result)) == result


def test_empty_string_fields_are_allowed() -> None:
    result = _sample(cover_url="", latest_chapter="", update_date="", word_count="")

    assert result["cover_url"] == ""
    assert result["word_count"] == ""
