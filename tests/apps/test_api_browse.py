#!/usr/bin/env python3
"""REST API tests for the browse endpoints (rankings and categories)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.app import create_app

_ROWS = [
    {
        "site": "fanqienovel",
        "book_id": "7617740782291651646",
        "book_url": "https://fanqienovel.com/page/7617740782291651646",
        "cover_url": "",
        "title": "骇夜：我在恐怖游戏里暴富",
        "author": "阿一不是阿二",
        "latest_chapter": "",
        "update_date": "2026-11-06",
        "word_count": "",
        "priority": 5,
    }
]

_GROUPS = {
    "male": [{"id": "1141", "name": "西方奇幻"}],
    "female": [{"id": "8", "name": "现代言情"}],
}


class _StubSearcher:
    calls: list[tuple[Any, ...]] = []

    def __init__(self, session: Any) -> None:
        self.session = session

    async def rank(self, limit: int = 10) -> list[dict[str, Any]]:
        _StubSearcher.calls.append(("rank", limit))
        return _ROWS

    async def categories(self) -> dict[str, list[dict[str, str]]]:
        _StubSearcher.calls.append(("categories",))
        return _GROUPS

    async def category_books(
        self,
        category_id: str,
        *,
        gender: str = "1",
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        _StubSearcher.calls.append(("category_books", category_id, gender, limit))
        return _ROWS


class _RanklessSearcher:
    def __init__(self, session: Any) -> None:
        self.session = session


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVELDL_API_TOKEN", raising=False)


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(output_dir=tmp_path / "downloads")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def _stub_searcher(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubSearcher.calls = []
    monkeypatch.setattr(
        api_routes, "_browse_searcher", lambda site, method: _StubSearcher
    )


@pytest.mark.asyncio
async def test_browse_categories_returns_audience_groups(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/browse/categories", params={"site": "fanqienovel"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["site"] == "fanqienovel"
    assert payload["groups"]["female"] == [{"id": "8", "name": "现代言情"}]
    assert _StubSearcher.calls == [("categories",)]


@pytest.mark.asyncio
async def test_browse_without_category_lists_the_ranking(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/browse", json={"site": "fanqienovel", "limit": 3}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["results"][0]["title"] == "骇夜：我在恐怖游戏里暴富"
    assert _StubSearcher.calls == [("rank", 3)]


@pytest.mark.asyncio
async def test_browse_category_maps_the_gender_code(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/browse",
        json={
            "site": "fanqienovel",
            "category_id": "8",
            "gender": "female",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    assert _StubSearcher.calls == [("category_books", "8", "2", 5)]


@pytest.mark.asyncio
async def test_browse_rejects_a_site_without_browse_support(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unsupported(site: str, method: str) -> type:
        raise api_routes.HTTPException(status_code=400, detail="unsupported")

    monkeypatch.setattr(api_routes, "_browse_searcher", _unsupported)

    response = await client.post("/api/v1/browse", json={"site": "b520"})

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported"


@pytest.mark.asyncio
async def test_browse_validation_rejects_a_bad_limit(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/browse", json={"site": "fanqienovel", "limit": 0}
    )

    assert response.status_code == 422
