#!/usr/bin/env python3
"""
tests.apps.test_api_music_contract
----------------------------------

Contract tests for the music REST endpoints.

Every provider and download-engine call is replaced with a local fake and the
server is driven through ``httpx.ASGITransport``, so nothing here touches the
network.  The tests pin:

* the documented success field sets of ``/music/platforms``,
  ``/music/search``, ``/music/download``, ``/music/export`` and
  ``/music/health``;
* the ``400`` / ``422`` / ``502`` / ``503`` error bodies, including that an
  unknown platform and an unknown audio level fail at the request boundary;
* the export request-body checks (missing/empty tracks, hostile paths) and the
  fixed, non-echoing ``400`` detail messages;
* that one unhealthy platform does not turn ``/music/health`` into a ``500``;
* that every response carries ``X-Request-ID`` (the existing middleware);
* that the REST routes and the embedded engine return the same public
  payloads for the same fakes, with the two documented transport differences
  (``search_id`` values and the embedded ``download_url``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.constants import (
    MSG_MUSIC_DOWNLOAD_FAILED,
    MSG_UNKNOWN_MUSIC_PLATFORM,
)
from novel_downloader.apps.api.schemas import (
    MUSIC_AUDIO_LEVELS,
    MUSIC_ID_PATTERN,
)
from novel_downloader.apps.embedded import server as eserver
from novel_downloader.apps.music import aggregate as music_aggregate
from novel_downloader.apps.music.aggregate import AggregateResult, PlatformOutcome
from novel_downloader.apps.music.downloader import export_m3u
from novel_downloader.apps.music.export_path import (
    MSG_OUT_DIR_INVALID,
    MSG_PLAYLIST_TARGET_INVALID,
    MSG_PLAYLIST_WRITE_FAILED,
)
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS
from novel_downloader.apps.music.trackcache import CACHE

API = "/api/v1"

#: Registered platform keys used by every fake in this module.
LABELS: tuple[tuple[str, str], ...] = (
    ("netease", "网易云音乐"),
    ("broken", "Broken Platform"),
)


@dataclass
class FakeDownloadResult:
    """Minimal stand-in for the engine's per-track result object."""

    status: str = "ok"
    reason: str = ""
    path: str = ""
    bytes_: int = 0


class FakeProvider:
    """Offline provider returning one deterministic track per search."""

    supports_offset = True

    def __init__(self, key: str, calls: list[tuple[str, str, int, int]]) -> None:
        self.key = key
        self._calls = calls
        self.last_tracks: list[Track] = []

    def search(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[Track], int]:
        self._calls.append((self.key, keyword, limit, offset))
        self.last_tracks = [
            Track(
                provider=self.key,
                id=f"{self.key}-1",
                name=keyword,
                artists="A",
                album="B",
                duration_s=123,
            )
        ]
        return list(self.last_tracks), len(self.last_tracks)


@dataclass
class MusicFixture:
    root: Path
    components: Any
    providers: dict[str, FakeProvider]
    provider_calls: list[tuple[str, str, int, int]]
    download_calls: list[dict[str, Any]]
    health_calls: list[tuple[str, float]]


def _make_components(
    root: Path,
    *,
    download_mixed: Callable[..., Any] | None = None,
    health_check: Callable[..., dict[str, Any]] | None = None,
) -> MusicFixture:
    root.mkdir(parents=True, exist_ok=True)
    provider_calls: list[tuple[str, str, int, int]] = []
    download_calls: list[dict[str, Any]] = []
    health_calls: list[tuple[str, float]] = []
    providers = {key: FakeProvider(key, provider_calls) for key, _ in LABELS}

    def create(key: str) -> FakeProvider:
        try:
            return providers[key]
        except KeyError:
            raise KeyError(f"unknown platform: {key}") from None

    def download(
        create_provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any = None,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        download_calls.append(
            {
                "create_provider": create_provider,
                "tracks": list(tracks),
                "options": options,
            }
        )
        for index, track in enumerate(tracks):
            if on_result is not None:
                on_result(
                    index,
                    track,
                    FakeDownloadResult(
                        path=f"/music/{index}.mp3",
                        bytes_=index + 1,
                    ),
                )
        return len(tracks), 0, 0, [], []

    def healthy(key: str, *, timeout: float = 5.0) -> dict[str, Any]:
        health_calls.append((key, float(timeout)))
        if key == "broken":
            return {"ok": False, "elapsed_ms": 7, "error": "probe failed"}
        return {"ok": True, "elapsed_ms": 3, "error": ""}

    components = api_routes._MusicComponents(
        paths=SimpleNamespace(default_download_dir=lambda: root),
        download_options=lambda out_dir, level: SimpleNamespace(
            out_dir=out_dir, level=level
        ),
        download_many=download,
        download_mixed=download_mixed or download,
        available_labels=lambda: list(LABELS),
        create=create,
        platform_note=lambda key: f"note:{key}",
        track_cls=Track,
        export_m3u=export_m3u,
        health_check=health_check or healthy,
        load_settings=lambda: {"out_dir": str(root)},
    )
    return MusicFixture(
        root=root,
        components=components,
        providers=providers,
        provider_calls=provider_calls,
        download_calls=download_calls,
        health_calls=health_calls,
    )


@pytest.fixture(autouse=True)
def _isolated_music_contract_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep application state out of the developer profile and reset caches."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)
    CACHE.clear()
    api_routes._MUSIC_JOBS.clear()
    try:
        yield
    finally:
        CACHE.clear()
        api_routes._MUSIC_JOBS.clear()


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(output_dir=tmp_path / "downloads", rate_limit=None)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def music(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MusicFixture:
    fixture = _make_components(tmp_path / "music-root")
    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: fixture.components
    )
    monkeypatch.setattr(eserver, "_music_components", lambda: fixture.components)
    return fixture


_MISSING = object()


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: Any = _MISSING,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if payload is _MISSING:
        return await client.request(method, path, headers=headers)
    return await client.request(method, path, json=payload, headers=headers)


def _assert_request_id(response: httpx.Response) -> None:
    value = response.headers.get("X-Request-ID")
    assert value and value.strip(), response.headers


def _assert_detail(response: httpx.Response, status_code: int, detail: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.json() == {"detail": detail}


def _assert_validation(response: httpx.Response, loc: tuple[str, ...]) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"detail", "errors"}
    assert isinstance(body["detail"], str)
    assert body["detail"].startswith("Request validation failed")
    assert isinstance(body["errors"], list) and body["errors"]
    for error in body["errors"]:
        assert set(error) == {"type", "loc", "msg"}
    assert loc in [tuple(error["loc"]) for error in body["errors"]], body["errors"]
    _assert_request_id(response)


def _embedded_error(
    handler: Callable[..., Any],
    body: dict[str, Any],
    query: dict[str, list[str]] | None = None,
) -> eserver._ApiError:
    with pytest.raises(eserver._ApiError) as excinfo:
        handler(body, {}, query or {})
    return excinfo.value


# ---------------------------------------------------------------------------
# Success shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platforms_contract(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    response = await client.get(f"{API}/music/platforms")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"platforms"}
    assert [item["key"] for item in payload["platforms"]] == [
        "netease",
        "broken",
    ]
    for item in payload["platforms"]:
        assert set(item) == {"key", "label", "note"}
        assert item["note"] == f"note:{item['key']}"
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_search_single_platform_success_shape(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    response = await client.post(
        f"{API}/music/search",
        json={
            "platform": "netease",
            "keyword": "晴天",
            "limit": 5,
            "offset": 2,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {
        "total",
        "tracks",
        "search_id",
        "platform",
        "offset",
        "supports_offset",
    }
    assert payload["platform"] == "netease"
    assert payload["total"] == 1
    assert payload["offset"] == 2
    assert payload["supports_offset"] is True
    assert re.fullmatch(MUSIC_ID_PATTERN, payload["search_id"])
    # ``keyword`` is a request field; it is forwarded to the provider and is
    # not echoed in the documented response.
    assert music.provider_calls == [("netease", "晴天", 5, 2)]

    assert len(payload["tracks"]) == 1
    track = payload["tracks"][0]
    assert set(track) == set(PUBLIC_TRACK_FIELDS)
    assert track["provider"] == "netease"
    assert track["id"] == "netease-1"
    assert track["name"] == "晴天"
    assert track["artists"] == "A"
    assert track["sources"] == []
    assert "extra" not in track
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_search_all_success_shape(
    client: httpx.AsyncClient,
    music: MusicFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_search_all(
        keyword: str,
        platforms: list[str] | None,
        limit: int,
        *,
        per_platform_limit: int,
        timeout: float,
        **_kwargs: Any,
    ) -> AggregateResult:
        seen.update(
            keyword=keyword,
            platforms=platforms,
            limit=limit,
            per_platform_limit=per_platform_limit,
            timeout=timeout,
        )
        return AggregateResult(
            tracks=[
                Track(
                    provider="netease",
                    id="1",
                    name=keyword,
                    artists="A",
                    sources=["netease", "qq"],
                )
            ],
            platforms=[
                PlatformOutcome(platform="netease", ok=True, count=1),
                PlatformOutcome(platform="qq", ok=False, count=0, error="probe failed"),
            ],
            total=2,
        )

    monkeypatch.setattr(music_aggregate, "search_all", fake_search_all)

    response = await client.post(f"{API}/music/search", json={"keyword": "晴天"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {
        "total",
        "tracks",
        "search_id",
        "platform",
        "platforms",
        "offset",
        "supports_offset",
    }
    assert payload["platform"] == "all"
    assert payload["total"] == 2
    assert payload["offset"] == 0
    assert payload["supports_offset"] is False
    assert re.fullmatch(MUSIC_ID_PATTERN, payload["search_id"])
    assert payload["tracks"][0]["sources"] == ["netease", "qq"]
    assert payload["platforms"] == [
        {"platform": "netease", "ok": True, "count": 1, "error": ""},
        {"platform": "qq", "ok": False, "count": 0, "error": "probe failed"},
    ]
    assert seen == {
        "keyword": "晴天",
        "platforms": None,
        "limit": 20,
        "per_platform_limit": 15,
        "timeout": 5.0,
    }
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_download_success_shape_and_result_fields(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": [
                {
                    "provider": "netease",
                    "id": "1",
                    "name": "晴天",
                    "artists": "A",
                }
            ],
            "level": "exhigh",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"ok", "skip", "fail", "results"}
    assert (payload["ok"], payload["skip"], payload["fail"]) == (1, 0, 0)
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert set(result) == {
        "status",
        "reason",
        "path",
        "download_url",
        "bytes",
    }
    assert result == {
        "status": "ok",
        "reason": "",
        "path": "/music/0.mp3",
        # The REST server writes files on its own machine and has no
        # loopback download route; the key is still present for field-set
        # parity with the embedded engine.
        "download_url": "",
        "bytes": 1,
    }
    assert music.download_calls[0]["options"].level == "exhigh"
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_download_restores_tracks_from_search_id(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    searched = await client.post(
        f"{API}/music/search", json={"platform": "netease", "keyword": "晴天"}
    )
    assert searched.status_code == 200, searched.text
    payload = searched.json()
    original = music.providers["netease"].last_tracks[0]

    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": payload["tracks"],
            "search_id": payload["search_id"],
        },
    )

    assert response.status_code == 200, response.text
    # The cache hands the provider its original object, not the reduced JSON
    # copy, so resolve data survives the round trip.
    assert music.download_calls[-1]["tracks"][0] is original


@pytest.mark.asyncio
async def test_export_success_shape_and_skipped_counts(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    audio = music.root / "audio" / "song one.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"fake")
    target = music.root / "playlists" / "mine.m3u8"

    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "netease",
            "tracks": [
                {
                    "id": "1",
                    "name": "晴天",
                    "artists": "周杰伦",
                    "duration_s": 269,
                    "path": str(audio),
                },
                # No local file: counted in ``skipped``.
                {"id": "2", "name": "No path"},
                # Duplicate of the first location: not a second playlist row.
                {"id": "3", "name": "Duplicate", "path": str(audio)},
            ],
            "out_path": str(target),
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"path", "tracks_written", "skipped", "written"}
    assert Path(payload["path"]) == target.resolve()
    assert payload["tracks_written"] == 1
    assert payload["written"] == 1
    assert payload["skipped"] == 2
    assert target.read_text(encoding="utf-8").startswith("#EXTM3U\n")
    assert "#EXTINF:269,周杰伦 - 晴天" in target.read_text(encoding="utf-8")
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_health_reports_partial_failure_without_500(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    response = await client.get(f"{API}/music/health", params={"timeout": 1.5})

    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["platform"] for item in items] == ["netease", "broken"]
    for item in items:
        assert set(item) == {"platform", "ok", "elapsed_ms", "error"}
    # ``failed`` is derived from the list; one bad platform does not fail the
    # endpoint or hide the healthy ones.
    assert sum(1 for item in items if not item["ok"]) == 1
    assert items[0] == {
        "platform": "netease",
        "ok": True,
        "elapsed_ms": 3,
        "error": "",
    }
    assert items[1] == {
        "platform": "broken",
        "ok": False,
        "elapsed_ms": 7,
        "error": "probe failed",
    }
    assert sorted(music.health_calls) == [("broken", 1.5), ("netease", 1.5)]
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Search validation and platform errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "loc"),
    [
        ({"platform": "netease"}, ("body", "keyword")),
        ({"platform": "netease", "keyword": ""}, ("body", "keyword")),
        ({"platform": "netease", "keyword": "   "}, ("body", "keyword")),
        ({"platform": "netease", "keyword": "bad\x00"}, ("body", "keyword")),
        (
            {"platform": "netease", "keyword": "x" * 201},
            ("body", "keyword"),
        ),
        ({"platform": "", "keyword": "x"}, ("body", "platform")),
        (
            {"platform": "p" * 65, "keyword": "x"},
            ("body", "platform"),
        ),
    ],
)
async def test_search_rejects_invalid_keyword_and_platform(
    client: httpx.AsyncClient,
    music: MusicFixture,
    payload: dict[str, Any],
    loc: tuple[str, ...],
) -> None:
    response = await client.post(f"{API}/music/search", json=payload)

    _assert_validation(response, loc)
    assert music.provider_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limit", 0),
        ("limit", 101),
        ("offset", -1),
        ("offset", 10_001),
    ],
)
async def test_search_rejects_out_of_range_paging(
    client: httpx.AsyncClient,
    music: MusicFixture,
    field: str,
    value: int,
) -> None:
    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "x", field: value},
    )

    _assert_validation(response, ("body", field))
    assert music.provider_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            f"{API}/music/search",
            {"platform": "nope", "keyword": "x"},
        ),
        (
            "POST",
            f"{API}/music/download",
            {"platform": "nope", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            f"{API}/music/export",
            {"platform": "nope", "tracks": [{"id": "1", "path": "x"}]},
        ),
        ("GET", f"{API}/music/health?platform=nope", _MISSING),
    ],
)
async def test_unknown_platform_is_400_with_shared_detail(
    client: httpx.AsyncClient,
    music: MusicFixture,
    method: str,
    path: str,
    payload: Any,
) -> None:
    response = await _request(client, method, path, payload)

    _assert_detail(
        response,
        400,
        MSG_UNKNOWN_MUSIC_PLATFORM.format(platform="nope", keys="netease, broken"),
    )
    assert music.provider_calls == []
    assert music.download_calls == []
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Download: audio levels, paths and engine failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [f"{API}/music/download", f"{API}/music/jobs"])
async def test_unknown_audio_level_is_422(
    client: httpx.AsyncClient, music: MusicFixture, path: str
) -> None:
    response = await client.post(
        path,
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "level": "ultra",
        },
    )

    _assert_validation(response, ("body", "level"))
    assert music.download_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("level", MUSIC_AUDIO_LEVELS)
async def test_documented_audio_levels_are_forwarded(
    client: httpx.AsyncClient, music: MusicFixture, level: str
) -> None:
    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "level": level,
        },
    )

    assert response.status_code == 200, response.text
    assert music.download_calls[-1]["options"].level == level


def test_audio_level_constant_matches_the_embedded_engine() -> None:
    """The REST and embedded allow-lists cannot drift apart silently."""
    assert tuple(eserver._MUSIC_AUDIO_LEVELS) == MUSIC_AUDIO_LEVELS


@pytest.mark.asyncio
async def test_download_invalid_out_dir_is_400(
    client: httpx.AsyncClient,
    music: MusicFixture,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"

    for value in (str(outside), "../escape"):
        response = await client.post(
            f"{API}/music/download",
            json={
                "platform": "netease",
                "tracks": [{"id": "1"}],
                "out_dir": value,
            },
        )
        _assert_detail(response, 400, MSG_OUT_DIR_INVALID)
        # The fixed detail never echoes the submitted (server-internal) path.
        assert value not in response.json()["detail"]

    assert music.download_calls == []


@pytest.mark.asyncio
async def test_download_engine_failure_is_502_without_leaking_text(
    client: httpx.AsyncClient,
    music: MusicFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("secret https://cdn/x?token=abc")

    components = replace(music.components, download_mixed=boom)
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)

    response = await client.post(
        f"{API}/music/download",
        json={"platform": "netease", "tracks": [{"id": "1"}]},
    )

    _assert_detail(
        response,
        502,
        MSG_MUSIC_DOWNLOAD_FAILED.format(error="RuntimeError"),
    )
    assert "secret" not in response.json()["detail"]
    assert "token" not in response.json()["detail"]
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Export request-body validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "loc"),
    [
        ({}, ("body", "tracks")),
        ({"tracks": []}, ("body", "tracks")),
        ({"tracks": None}, ("body", "tracks")),
        ({"tracks": "x"}, ("body", "tracks")),
        (
            {"tracks": [{"id": "1", "path": "bad\x00"}]},
            ("body", "tracks", "0", "path"),
        ),
        (
            {
                "tracks": [{"id": "1", "path": "x"}],
                "out_path": "bad\x00.m3u8",
            },
            ("body", "out_path"),
        ),
    ],
)
async def test_export_requires_valid_tracks(
    client: httpx.AsyncClient,
    music: MusicFixture,
    payload: dict[str, Any],
    loc: tuple[str, ...],
) -> None:
    response = await client.post(f"{API}/music/export", json=payload)

    _assert_validation(response, loc)


@pytest.mark.asyncio
async def test_export_rejects_invalid_target_paths(
    client: httpx.AsyncClient, music: MusicFixture, tmp_path: Path
) -> None:
    targets = [
        # Wrong extension: a playlist must be .m3u/.m3u8.
        "favorites.txt",
        "nested/playlist.mp3",
        # Relative escape from the configured music root.
        "../escape.m3u8",
        # Absolute path outside every allowed root.
        str(tmp_path / "outside" / "escape.m3u8"),
    ]

    for target in targets:
        response = await client.post(
            f"{API}/music/export",
            json={
                "platform": "netease",
                "tracks": [{"id": "1", "path": "somewhere.mp3"}],
                "out_path": target,
            },
        )
        _assert_detail(response, 400, MSG_PLAYLIST_TARGET_INVALID)
        assert target not in response.json()["detail"]
        _assert_request_id(response)


@pytest.mark.asyncio
async def test_export_write_failure_is_500_without_leaking_paths(
    client: httpx.AsyncClient,
    music: MusicFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_export(_tracks: list[Any], _target: Path) -> str:
        raise OSError("disk full at C:/secret/music")

    components = replace(music.components, export_m3u=failing_export)
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)

    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "netease",
            "tracks": [{"id": "1", "path": "song.mp3"}],
            "out_path": str(music.root / "favorites.m3u8"),
        },
    )

    _assert_detail(
        response,
        500,
        MSG_PLAYLIST_WRITE_FAILED.format(error="OSError"),
    )
    detail = response.json()["detail"]
    assert "C:/secret" not in detail
    assert "disk full" not in detail
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_health_rejects_invalid_timeout(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    for value in ("0", "61", "abc", "nan", "inf"):
        response = await client.get(f"{API}/music/health", params={"timeout": value})
        _assert_validation(response, ("query", "timeout"))

    assert music.health_calls == []


# ---------------------------------------------------------------------------
# Optional dependency guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            f"{API}/music/search",
            {"platform": "netease", "keyword": "x"},
        ),
        (
            "POST",
            f"{API}/music/download",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            f"{API}/music/export",
            {"platform": "netease", "tracks": [{"id": "1", "path": "x"}]},
        ),
        ("GET", f"{API}/music/health", None),
    ],
)
async def test_missing_music_extras_is_503(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: Any,
) -> None:
    # ``None`` in sys.modules makes the optional import raise ImportError.
    monkeypatch.setitem(sys.modules, "mutagen", None)

    response = await _request(client, method, path, payload)

    assert response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"].startswith("Music support is not available:")
    assert 'pip install "novel-downloader[music]"' in body["detail"]
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# X-Request-ID on every outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_music_response_carries_request_id(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    cases: list[tuple[str, str, Any]] = [
        ("GET", f"{API}/music/platforms", _MISSING),
        (
            "POST",
            f"{API}/music/search",
            {"platform": "netease", "keyword": "x"},
        ),
        ("POST", f"{API}/music/search", {"platform": "netease"}),
        (
            "POST",
            f"{API}/music/search",
            {"platform": "nope", "keyword": "x"},
        ),
        (
            "POST",
            f"{API}/music/download",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        ("POST", f"{API}/music/download", {"tracks": []}),
        (
            "POST",
            f"{API}/music/export",
            {"tracks": [{"id": "1", "path": "x"}], "out_path": "x.txt"},
        ),
        ("GET", f"{API}/music/health", _MISSING),
        ("GET", f"{API}/music/health?timeout=0", _MISSING),
    ]

    for method, path, payload in cases:
        response = await _request(
            client,
            method,
            path,
            payload,
            headers={"X-Request-ID": "music-contract-1"},
        )
        assert response.status_code != 500, (method, path, response.text)
        assert response.headers.get("X-Request-ID") == "music-contract-1", (
            method,
            path,
            response.headers,
        )


# ---------------------------------------------------------------------------
# Embedded / REST parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_and_embedded_single_platform_search_agree(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    body = {"platform": "netease", "keyword": "晴天"}
    response = await client.post(f"{API}/music/search", json=body)
    assert response.status_code == 200, response.text
    rest_payload = response.json()

    embedded_payload = eserver._handle_music_search(dict(body), {}, {})

    assert set(rest_payload) == set(embedded_payload)
    for key in set(rest_payload) - {"search_id"}:
        assert rest_payload[key] == embedded_payload[key]
    assert re.fullmatch(MUSIC_ID_PATTERN, rest_payload["search_id"])
    assert re.fullmatch(MUSIC_ID_PATTERN, embedded_payload["search_id"])


@pytest.mark.asyncio
async def test_rest_and_embedded_aggregate_search_agree(
    client: httpx.AsyncClient,
    music: MusicFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_search_all(
        keyword: str,
        platforms: list[str] | None,
        limit: int,
        **_kwargs: Any,
    ) -> AggregateResult:
        return AggregateResult(
            tracks=[
                Track(
                    provider="netease",
                    id="1",
                    name=keyword,
                    sources=["netease", "qq"],
                )
            ],
            platforms=[PlatformOutcome(platform="netease", ok=True, count=1)],
            total=1,
        )

    monkeypatch.setattr(music_aggregate, "search_all", fake_search_all)
    body = {"keyword": "晴天"}

    response = await client.post(f"{API}/music/search", json=body)
    assert response.status_code == 200, response.text
    rest_payload = response.json()
    embedded_payload = eserver._handle_music_search(dict(body), {}, {})

    assert set(rest_payload) == set(embedded_payload)
    for key in set(rest_payload) - {"search_id"}:
        assert rest_payload[key] == embedded_payload[key]


@pytest.mark.asyncio
async def test_rest_and_embedded_health_agree(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    all_platforms = await client.get(f"{API}/music/health", params={"timeout": 1.5})
    assert all_platforms.status_code == 200, all_platforms.text
    embedded_all = eserver._handle_music_health({}, {}, {"timeout": ["1.5"]})
    assert all_platforms.json() == embedded_all

    one = await client.get(f"{API}/music/health", params={"platform": "broken"})
    assert one.status_code == 200, one.text
    embedded_one = eserver._handle_music_health({}, {}, {"platform": ["broken"]})
    assert one.json() == embedded_one


@pytest.mark.asyncio
async def test_rest_and_embedded_export_agree(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    audio = music.root / "parity.mp3"
    audio.write_bytes(b"fake")
    target = music.root / "parity.m3u8"
    body = {
        "platform": "netease",
        "tracks": [{"id": "1", "name": "Song", "artists": "A", "path": str(audio)}],
        "out_path": str(target),
    }

    response = await client.post(f"{API}/music/export", json=body)
    assert response.status_code == 200, response.text

    embedded_payload = eserver._handle_music_export(dict(body), {}, {})
    assert response.json() == embedded_payload


@pytest.mark.asyncio
async def test_rest_and_embedded_download_agree(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    body = {
        "platform": "netease",
        "tracks": [{"provider": "netease", "id": "1", "name": "Song", "artists": "A"}],
        "out_dir": str(music.root),
    }

    response = await client.post(f"{API}/music/download", json=body)
    assert response.status_code == 200, response.text
    rest_payload = response.json()
    embedded_payload = eserver._handle_music_download(dict(body), {}, {})

    assert (
        set(rest_payload)
        == set(embedded_payload)
        == {
            "ok",
            "skip",
            "fail",
            "results",
        }
    )
    assert rest_payload["ok"] == embedded_payload["ok"]
    assert rest_payload["skip"] == embedded_payload["skip"]
    assert rest_payload["fail"] == embedded_payload["fail"]
    assert len(rest_payload["results"]) == len(embedded_payload["results"])
    for rest_item, embedded_item in zip(
        rest_payload["results"], embedded_payload["results"], strict=True
    ):
        assert set(rest_item) == set(embedded_item)
        for key in ("status", "reason", "path", "bytes"):
            assert rest_item[key] == embedded_item[key]
        # Documented transport difference: REST has no loopback download route.
        assert rest_item["download_url"] == ""
        assert embedded_item["download_url"] == "/downloads/music/0.mp3"


@pytest.mark.asyncio
async def test_rest_and_embedded_reject_the_same_bad_requests(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    unknown_platform = {"platform": "nope", "keyword": "x"}
    response = await client.post(f"{API}/music/search", json=unknown_platform)
    embedded = _embedded_error(eserver._handle_music_search, unknown_platform)
    assert response.status_code == embedded.status == 400
    assert response.json() == {"detail": embedded.detail}

    unknown_level = {
        "platform": "netease",
        "tracks": [{"id": "1"}],
        "level": "ultra",
    }
    response = await client.post(f"{API}/music/download", json=unknown_level)
    embedded = _embedded_error(eserver._handle_music_download, unknown_level)
    assert response.status_code == embedded.status == 422
    rest_body = response.json()
    assert set(rest_body) == {"detail", "errors"}
    assert rest_body["detail"] == embedded.detail
    assert embedded.errors is not None
    assert [str(part) for part in embedded.errors[0]["loc"]] == [
        "body",
        "level",
    ]
    assert rest_body["errors"][0]["loc"] == ["body", "level"]
    assert rest_body["errors"][0]["type"] == embedded.errors[0]["type"]
    assert rest_body["errors"][0]["msg"] == embedded.errors[0]["msg"]

    response = await client.get(f"{API}/music/health", params={"platform": "nope"})
    embedded = _embedded_error(eserver._handle_music_health, {}, {"platform": ["nope"]})
    assert response.status_code == embedded.status == 400
    assert response.json() == {"detail": embedded.detail}
