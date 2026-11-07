#!/usr/bin/env python3
"""Offline tests for the embedded server's browse handlers.

The handlers are called directly: the searcher layer is stubbed, so no network
is touched and the assertions only cover validation and response shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.apps.embedded import server as eserver

_ROWS = [
    {
        "site": "fanqienovel",
        "book_id": "42",
        "book_url": "https://fanqienovel.com/page/42",
        "cover_url": "",
        "title": "示例",
        "author": "作者",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 5,
    }
]


class _StubSearcher:
    calls: list[tuple[Any, ...]] = []

    def __init__(self, session: Any) -> None:
        self.session = session

    async def categories(self) -> dict[str, list[dict[str, str]]]:
        _StubSearcher.calls.append(("categories",))
        return {"male": [{"id": "1141", "name": "西方奇幻"}]}

    async def rank(self, limit: int = 10) -> list[dict[str, Any]]:
        _StubSearcher.calls.append(("rank", limit))
        return _ROWS

    async def category_books(
        self,
        category_id: str,
        *,
        gender: str = "1",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        _StubSearcher.calls.append(("category_books", category_id, gender, limit))
        return _ROWS


@pytest.fixture(autouse=True)
def _stub_searcher(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubSearcher.calls = []
    monkeypatch.setattr(eserver, "_browse_searcher", lambda site, method: _StubSearcher)


def test_categories_returns_groups() -> None:
    payload = eserver._handle_browse_categories({}, {}, {"site": ["fanqienovel"]})

    assert payload == {
        "site": "fanqienovel",
        "groups": {"male": [{"id": "1141", "name": "西方奇幻"}]},
    }


def test_categories_requires_the_site_query() -> None:
    with pytest.raises(eserver._ApiError) as err:
        eserver._handle_browse_categories({}, {}, {})

    assert err.value.status == 422
    assert err.value.errors is not None
    assert err.value.errors[0]["loc"] == ["query", "site"]


def test_browse_defaults_to_the_ranking() -> None:
    payload = eserver._handle_browse({"site": "fanqienovel"}, {}, {})

    assert payload["total"] == 1
    assert payload["results"][0]["book_id"] == "42"
    assert _StubSearcher.calls == [("rank", 20)]


def test_browse_maps_the_gender_code() -> None:
    eserver._handle_browse(
        {"site": "fanqienovel", "category_id": "8", "gender": "female", "limit": 5},
        {},
        {},
    )

    assert _StubSearcher.calls == [("category_books", "8", "2", 5)]


def test_browse_validation_matches_the_rest_api() -> None:
    for payload in (
        {},
        {"site": ""},
        {"site": "x", "limit": 0},
        {"site": "x", "limit": 500},
        {"site": "x", "gender": "other"},
        {"site": "x", "category_id": 12},
    ):
        with pytest.raises(eserver._ApiError) as err:
            eserver._handle_browse(payload, {}, {})
        assert err.value.status == 422, payload
