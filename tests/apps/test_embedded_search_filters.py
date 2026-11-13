#!/usr/bin/env python3
"""
tests.apps.test_embedded_search_filters
---------------------------------------

Cross-transport contract for the optional local search filters
(``min_words`` / ``max_words`` / ``updated_after``).

The REST endpoints (``POST /search`` and ``GET /search/stream``) and the
embedded engine's ``POST /search`` must accept the same names, bounds and
date spelling: every invalid payload is a ``422`` on all three, every
boundary payload is accepted, and a request without filters keeps the
historical forwarded-argument set.

No network access: both search engines are local fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.embedded import server as eserver

#: ``(field, value)`` payloads the REST model and the embedded handler must
#: both reject with ``422``.
_INVALID_FILTERS: list[tuple[str, Any]] = [
    ("min_words", -1),
    ("min_words", 1_000_000_001),
    ("max_words", -1),
    ("max_words", 1_000_000_001),
    ("updated_after", "2024/01/01"),
    ("updated_after", "20240101"),
    ("updated_after", "2024-13-01"),
    ("updated_after", "2024-02-30"),
    ("updated_after", ""),
    ("updated_after", "soon"),
]

#: Boundary payloads every transport accepts.
_VALID_FILTERS: list[dict[str, Any]] = [
    {"min_words": 0},
    {"max_words": 1_000_000_000},
    {"min_words": 0, "max_words": 1_000_000_000},
    {"updated_after": "2024-02-29"},
    {"min_words": 10_000, "max_words": 500_000, "updated_after": "2024-01-01"},
]


class _FakeManager:
    """Task manager stand-in; the filter tests never touch tasks."""

    async def add_task(self, *, title: str, site: str, book_id: str) -> Any:
        return SimpleNamespace(
            task_id="t1",
            title=title,
            site=site,
            book_id=book_id,
            status=SimpleNamespace(value="pending"),
            chapters_done=0,
            chapters_total=0,
            progress=lambda: 0.0,
            created_at=None,
            finished_at=None,
            error=None,
            export_stage=None,
            exported_paths={},
        )

    async def cancel_task(self, _task_id: str) -> bool:
        return False

    def snapshot(self) -> dict[str, list[Any]]:
        return {"pending": [], "running": [], "completed": []}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest_asyncio.fixture
async def rest_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_manager] = lambda: _FakeManager()
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture()
def fake_engines(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Replace the aggregate search on both transports and record kwargs."""
    calls: dict[str, list[dict[str, Any]]] = {"search": [], "stream": []}

    async def fake_search(_keyword: str, **kwargs: Any) -> list[Any]:
        calls["search"].append(kwargs)
        return []

    async def fake_stream(_keyword: str, **kwargs: Any) -> AsyncIterator[Any]:
        calls["stream"].append(kwargs)
        if False:  # pragma: no cover - make this an async generator
            yield []

    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr(api_routes, "search_stream", fake_stream)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    return calls


def _stream_params(payload: dict[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in payload.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _INVALID_FILTERS)
async def test_invalid_filters_are_422_on_every_transport(
    rest_client: httpx.AsyncClient,
    fake_engines: dict[str, list[dict[str, Any]]],
    field: str,
    value: Any,
) -> None:
    payload = {"keyword": "x", field: value}

    response = await rest_client.post("/api/v1/search", json=payload)
    assert response.status_code == 422, (payload, response.text)

    with pytest.raises(eserver._ApiError) as embedded_error:
        eserver._handle_search(payload, {}, {})
    assert embedded_error.value.status == 422

    stream = await rest_client.get(
        "/api/v1/search/stream", params=_stream_params(payload)
    )
    assert stream.status_code == 422, (payload, stream.text)
    assert fake_engines["search"] == []
    assert fake_engines["stream"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", _VALID_FILTERS)
async def test_boundary_filters_are_accepted_on_every_transport(
    rest_client: httpx.AsyncClient,
    fake_engines: dict[str, list[dict[str, Any]]],
    filters: dict[str, Any],
) -> None:
    payload = {"keyword": "x", **filters}

    response = await rest_client.post("/api/v1/search", json=payload)
    assert response.status_code == 200, (payload, response.text)
    assert set(response.json()) == {"total", "results"}

    assert eserver._handle_search(payload, {}, {}) == {"total": 0, "results": []}

    stream = await rest_client.get(
        "/api/v1/search/stream", params=_stream_params(payload)
    )
    assert stream.status_code == 200, (payload, stream.text)

    # Both engines only receive the filters that were actually requested.
    for calls in (fake_engines["search"], fake_engines["stream"]):
        forwarded = calls[-1]
        for field in ("min_words", "max_words"):
            if field in filters:
                assert forwarded[field] == filters[field]
            else:
                assert field not in forwarded
        if "updated_after" in filters:
            assert forwarded["updated_after"] == date.fromisoformat(
                filters["updated_after"]
            )
        else:
            assert "updated_after" not in forwarded


@pytest.mark.asyncio
async def test_absent_filters_keep_the_historical_argument_set(
    rest_client: httpx.AsyncClient,
    fake_engines: dict[str, list[dict[str, Any]]],
) -> None:
    payload = {"keyword": "x"}

    response = await rest_client.post("/api/v1/search", json=payload)
    assert response.status_code == 200
    stream = await rest_client.get("/api/v1/search/stream", params=payload)
    assert stream.status_code == 200
    eserver._handle_search(payload, {}, {})

    for calls in (fake_engines["search"], fake_engines["stream"]):
        for field in ("min_words", "max_words", "updated_after"):
            assert field not in calls[-1]
