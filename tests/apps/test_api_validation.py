#!/usr/bin/env python3
"""
tests.apps.test_api_validation
------------------------------

Boundary tests for the REST API request schemas.

These cover the malformed-input classes that used to reach the service layer
(or the JSON encoder) unchanged: blank/oversized/control-character strings,
unpaired surrogates in optional fields, non-finite timeouts, unbounded page
sizes, path-like site names and non-http(s) media URLs. Every case must be a
``422`` (or be normalized), never a ``500``.

No network access happens: the search function and the task manager are
replaced with local fakes.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager, get_watchlist_store
from novel_downloader.apps.api.schemas import (
    MAX_KEYWORD_LENGTH,
    MAX_PER_SITE_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_SITES,
    MAX_SEARCH_WORDS,
    MAX_SITE_KEY_LENGTH,
    BrowseRequest,
    MusicDownloadRequest,
    MusicExportRequest,
    MusicSearchRequest,
    MusicTrack,
)
from novel_downloader.apps.core.watchlist import WatchlistStore

# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class _FakeManager:
    """Task manager stand-in that records accepted requests."""

    def __init__(self) -> None:
        self.pending: list[dict[str, str]] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> Any:
        self.pending.append({"title": title, "site": site, "book_id": book_id})
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


@pytest.fixture
def fake_manager() -> _FakeManager:
    return _FakeManager()


@pytest_asyncio.fixture
async def client(
    tmp_path: Path, fake_manager: _FakeManager
) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_manager] = lambda: fake_manager
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def watchlist_client(
    tmp_path: Path, fake_manager: _FakeManager
) -> AsyncIterator[httpx.AsyncClient]:
    application: FastAPI = create_app(output_dir=tmp_path / "downloads")
    store = WatchlistStore(path=tmp_path / "watchlist.json")
    application.dependency_overrides[get_manager] = lambda: fake_manager
    application.dependency_overrides[get_watchlist_store] = lambda: store
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _json_bytes(payload: Any) -> bytes:
    """Encode JSON with escapes, so surrogates/controls survive the client."""
    return json.dumps(payload).encode("utf-8")


def _assert_422(response: httpx.Response) -> None:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "Traceback" not in detail
    assert "Internal server error" not in detail


# ---------------------------------------------------------------------------
# Search body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keyword",
    ["   ", "\t\n", "a\x00b", "\ud800", "x" * (MAX_KEYWORD_LENGTH + 1)],
)
async def test_search_rejects_invalid_keywords(
    client: httpx.AsyncClient, keyword: str
) -> None:
    """Blank, control-containing, surrogate and overlong keywords are 422."""
    response = await client.post(
        "/api/v1/search", content=_json_bytes({"keyword": keyword})
    )

    _assert_422(response)


@pytest.mark.asyncio
async def test_search_keyword_length_boundary(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly the documented ceiling still passes validation."""
    calls: list[str] = []

    async def fake_search(keyword: str, **_kwargs: Any) -> list[Any]:
        calls.append(keyword)
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search", json={"keyword": "x" * MAX_KEYWORD_LENGTH}
    )

    assert response.status_code == 200
    assert calls


@pytest.mark.asyncio
async def test_search_rejects_oversized_page_parameters(
    client: httpx.AsyncClient,
) -> None:
    for field, value in (
        ("per_site_limit", MAX_PER_SITE_LIMIT + 1),
        ("per_site_limit", 0),
        ("limit", MAX_SEARCH_LIMIT + 1),
        ("limit", 0),
    ):
        response = await client.post(
            "/api/v1/search", json={"keyword": "x", field: value}
        )
        _assert_422(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", ["Infinity", "-Infinity", "NaN", "1e999"])
async def test_search_rejects_non_finite_timeout(
    client: httpx.AsyncClient, timeout: str
) -> None:
    """JSON's non-finite literals must not reach aiohttp's timeout timer."""
    body = f'{{"keyword":"x","timeout":{timeout}}}'.encode()
    response = await client.post("/api/v1/search", content=body)

    _assert_422(response)


@pytest.mark.asyncio
async def test_search_rejects_out_of_range_timeout(
    client: httpx.AsyncClient,
) -> None:
    for timeout in (0, -1, 121, 1e6):
        response = await client.post(
            "/api/v1/search", json={"keyword": "x", "timeout": timeout}
        )
        _assert_422(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sites",
    [
        [""],
        ["   "],
        ["a/b"],
        ["..\\escape"],
        ["a\x00b"],
        ["x" * (MAX_SITE_KEY_LENGTH + 1)],
    ],
)
async def test_search_rejects_malformed_sites(
    client: httpx.AsyncClient, sites: list[str]
) -> None:
    response = await client.post(
        "/api/v1/search", content=_json_bytes({"keyword": "x", "sites": sites})
    )

    _assert_422(response)


@pytest.mark.asyncio
async def test_search_rejects_too_many_sites(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/search",
        json={"keyword": "x", "sites": ["demo"] * (MAX_SEARCH_SITES + 1)},
    )

    _assert_422(response)


@pytest.mark.asyncio
async def test_search_dedupes_sites_before_forwarding(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    forwarded: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[Any]:
        forwarded["keyword"] = keyword
        forwarded["sites"] = kwargs["sites"]
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search",
        json={"keyword": "x", "sites": ["demo", "DEMO", " qidian "]},
    )

    assert response.status_code == 200
    assert forwarded["sites"] == ["demo", "qidian"]


@pytest.mark.asyncio
async def test_search_ignores_unknown_fields(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown keys stay ignored (both transports share that policy)."""

    async def fake_search(_keyword: str, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search", json={"keyword": "x", "bogus": {"nested": [1, 2]}}
    )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {"keyword": None},
        {"keyword": "x", "per_site_limit": None},
        {"keyword": "x", "timeout": None},
        {"keyword": "x", "group": None},
    ],
)
async def test_search_rejects_null_values(client: httpx.AsyncClient, body: Any) -> None:
    """Explicit JSON ``null`` on a scalar/body field is a 422, not a 500."""
    response = await client.post("/api/v1/search", json=body)

    _assert_422(response)


# ---------------------------------------------------------------------------
# Search stream (query parameters)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_stream_rejects_invalid_query(client: httpx.AsyncClient) -> None:
    for params in (
        {"keyword": "   "},
        {"keyword": "x", "timeout": "Infinity"},
        {"keyword": "x", "timeout": "121"},
        {"keyword": "x", "sites": "a/b"},
        {"keyword": "x", "per_site_limit": "0"},
    ):
        response = await client.get("/api/v1/search/stream", params=params)
        _assert_422(response)


@pytest.mark.asyncio
async def test_search_stream_empty_sites_searches_nothing(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/search/stream", params={"keyword": "x", "sites": ""}
    )

    assert response.status_code == 200
    assert 'event: done\ndata: {"total":0}' in response.text


# ---------------------------------------------------------------------------
# Tasks and watchlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    ["a\r\nX-Injected: 1", "a\x00b", "\ud800", "x" * 513],
)
async def test_task_rejects_harmful_titles(
    client: httpx.AsyncClient, fake_manager: _FakeManager, title: str
) -> None:
    """A surrogate title used to be accepted and then 500'd in serialization."""
    response = await client.post(
        "/api/v1/tasks",
        content=_json_bytes({"site": "demo", "book_id": "1", "title": title}),
    )

    _assert_422(response)
    assert fake_manager.pending == []


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["   ", "a b", "a/b", "..", "a\x00b", "x" * 65])
async def test_task_rejects_malformed_sites(
    client: httpx.AsyncClient, fake_manager: _FakeManager, site: str
) -> None:
    response = await client.post(
        "/api/v1/tasks", content=_json_bytes({"site": site, "book_id": "1"})
    )

    _assert_422(response)
    assert fake_manager.pending == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows device-name semantics")
@pytest.mark.parametrize("book_id", ["CON", "NUL", "aux", "LPT1", "CON.txt"])
async def test_task_rejects_windows_device_names(
    client: httpx.AsyncClient, fake_manager: _FakeManager, book_id: str
) -> None:
    """A name that addresses a device is not a directory name.

    The queue refuses it too; validating here keeps the response a ``422``
    instead of a ``500`` raised by the shared path check.
    """
    response = await client.post(
        "/api/v1/tasks", content=_json_bytes({"site": "demo", "book_id": book_id})
    )

    _assert_422(response)
    assert fake_manager.pending == []


@pytest.mark.asyncio
async def test_task_blank_title_falls_back_to_generated_name(
    client: httpx.AsyncClient, fake_manager: _FakeManager
) -> None:
    response = await client.post(
        "/api/v1/tasks", json={"site": "demo", "book_id": "1", "title": "   "}
    )

    assert response.status_code == 200
    assert fake_manager.pending[0]["title"] == "demo (id = 1)"


@pytest.mark.asyncio
async def test_watchlist_add_rejects_harmful_title(
    watchlist_client: httpx.AsyncClient,
) -> None:
    response = await watchlist_client.post(
        "/api/v1/watchlist",
        content=_json_bytes({"site": "demo", "book_id": "1", "title": "a\x00b"}),
    )

    _assert_422(response)


@pytest.mark.asyncio
async def test_remove_watchlist_validates_path_components(
    watchlist_client: httpx.AsyncClient,
) -> None:
    bad_site = await watchlist_client.delete("/api/v1/watchlist/bad%20site/1")
    long_id = await watchlist_client.delete(f"/api/v1/watchlist/demo/{'x' * 129}")

    _assert_422(bad_site)
    _assert_422(long_id)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["   ", "a b", "a/b", "a\x00b"])
async def test_browse_rejects_malformed_site(
    client: httpx.AsyncClient, site: str
) -> None:
    response = await client.post("/api/v1/browse", content=_json_bytes({"site": site}))

    _assert_422(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("category_id", ["a\x00b", "\ud800", "x" * 129])
async def test_browse_rejects_malformed_category(
    client: httpx.AsyncClient, category_id: str
) -> None:
    response = await client.post(
        "/api/v1/browse",
        content=_json_bytes({"site": "demo", "category_id": category_id}),
    )

    _assert_422(response)


def test_browse_blank_category_means_ranking_page() -> None:
    assert BrowseRequest(site="demo", category_id="   ").category_id is None


# ---------------------------------------------------------------------------
# Login payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_values_are_bounded(client: httpx.AsyncClient) -> None:
    cases = [
        {f"field{i}": "v" for i in range(51)},
        {"cookie": "a\r\nX-Injected: 1"},
        {"cookie": "a\x00b"},
        {"cookie": "\ud800"},
        {"cookie": "x" * 65537},
        {"": "value"},
    ]
    for values in cases:
        response = await client.post(
            "/api/v1/login/requests/whatever",
            content=_json_bytes({"values": values}),
        )
        _assert_422(response)


@pytest.mark.asyncio
async def test_login_accepts_ordinary_cookie_payload(
    client: httpx.AsyncClient,
) -> None:
    """Schema validation runs before the broker lookup: a good body is a 404."""
    response = await client.post(
        "/api/v1/login/requests/unknown",
        json={"values": {"cookie": "a=b; c=d"}},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Music schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", ["   ", "a\x00b", "\ud800", "x" * 201])
def test_music_search_rejects_invalid_keywords(keyword: str) -> None:
    with pytest.raises(ValueError):
        MusicSearchRequest(keyword=keyword)


def test_music_search_bounds_offset() -> None:
    with pytest.raises(ValueError):
        MusicSearchRequest(keyword="x", offset=10_001)


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (MusicSearchRequest, {"keyword": "x"}),
        (MusicDownloadRequest, {"tracks": [{"id": "1"}]}),
        (MusicExportRequest, {"tracks": [{"id": "1"}]}),
    ],
)
def test_music_platform_empty_is_rejected(
    model: type[MusicSearchRequest | MusicDownloadRequest | MusicExportRequest],
    extra: dict[str, Any],
) -> None:
    """``platform=""`` stays a 422 (the old ``min_length=1`` contract).

    A whitespace-only value keeps its historical "all platforms" meaning and
    is deliberately not stripped.
    """
    with pytest.raises(ValueError):
        model(platform="", **extra)
    assert model(platform=" ", **extra).platform == " "


@pytest.mark.parametrize(
    "track",
    [
        {"download_url": "file:///etc/passwd"},
        {"download_url": "ftp://example.test/song.mp3"},
        {"cover_url": "javascript:alert(1)"},
        {"name": "\ud800"},
        {"path": "a\x00b"},
        {"http_headers": {"X-Test": "a\r\nX-Injected: 1"}},
        {"http_headers": {"Bad Name": "v"}},
        {"http_headers": {f"H{i}": "v" for i in range(33)}},
        {"sources": ["ok"] * 33},
    ],
)
def test_music_track_rejects_unsafe_fields(track: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MusicTrack(**track)


def test_music_track_accepts_http_urls_and_newline_lyrics() -> None:
    model = MusicTrack(
        download_url="https://example.test/song.mp3",
        cover_url="http://example.test/cover.jpg",
        lyric="line one\nline two",
    )

    assert model.download_url.startswith("https://")
    assert "\n" in model.lyric


def test_music_track_serialization_never_emits_non_finite() -> None:
    """A provider-supplied NaN/inf duration cannot reach the JSON response.

    Starlette's ``JSONResponse`` uses ``allow_nan=False``, so a leaked
    non-finite float would turn a successful search into a ``500``.
    """
    from novel_downloader.apps.music.serialize import track_dict

    payload = track_dict(
        SimpleNamespace(duration_s=float("nan"), track_no=float("inf"))
    )

    encoded = json.dumps(payload, allow_nan=False)
    assert json.loads(encoded)["duration_s"] == 0
    assert json.loads(encoded)["track_no"] == 0


# ---------------------------------------------------------------------------
# Optional local search filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_forwards_local_filters(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    forwarded: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[Any]:
        forwarded["keyword"] = keyword
        forwarded.update(kwargs)
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search",
        json={
            "keyword": "x",
            "min_words": 10_000,
            "max_words": 500_000,
            "updated_after": "2024-01-01",
        },
    )

    assert response.status_code == 200
    # The response envelope is unchanged by the new request fields.
    assert set(response.json()) == {"total", "results"}
    assert forwarded["min_words"] == 10_000
    assert forwarded["max_words"] == 500_000
    assert forwarded["updated_after"] == date(2024, 1, 1)


@pytest.mark.asyncio
async def test_search_accepts_local_filter_boundaries(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(_keyword: str, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search",
        json={
            "keyword": "x",
            "min_words": 0,
            "max_words": MAX_SEARCH_WORDS,
            "updated_after": "2024-02-29",
        },
    )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_words", -1),
        ("min_words", MAX_SEARCH_WORDS + 1),
        ("max_words", -1),
        ("max_words", MAX_SEARCH_WORDS + 1),
        ("updated_after", "2024/01/01"),
        ("updated_after", "20240101"),
        ("updated_after", "2024-13-01"),
        ("updated_after", "2024-02-30"),
        ("updated_after", ""),
        ("updated_after", "soon"),
    ],
)
async def test_search_rejects_invalid_local_filters(
    client: httpx.AsyncClient, field: str, value: Any
) -> None:
    response = await client.post("/api/v1/search", json={"keyword": "x", field: value})

    _assert_422(response)


@pytest.mark.asyncio
async def test_search_stream_forwards_local_filters(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    forwarded: dict[str, Any] = {}

    async def fake_search_stream(_keyword: str, **kwargs: Any) -> AsyncIterator[Any]:
        forwarded.update(kwargs)
        yield [{"site": "alpha", "book_id": "1"}]

    monkeypatch.setattr(api_routes, "search_stream", fake_search_stream)

    response = await client.get(
        "/api/v1/search/stream",
        params={
            "keyword": "x",
            "min_words": 10_000,
            "max_words": 500_000,
            "updated_after": "2024-01-01",
        },
    )

    assert response.status_code == 200
    assert forwarded["min_words"] == 10_000
    assert forwarded["max_words"] == 500_000
    assert forwarded["updated_after"] == date(2024, 1, 1)
