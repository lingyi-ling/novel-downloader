#!/usr/bin/env python3
"""
tests.apps.test_music_export_error_contract
-------------------------------------------

Failure contract for music playlist export destinations.

``export_m3u`` used to raise a bare ``ValueError`` when the destination could
not be represented by the OS (embedded NUL).  REST and embedded only mapped
``OSError`` to a fixed 500, so that value escaped as an unhandled exception --
and any handler that *did* surface it would leak the rejected path.  These
tests pin the fixed boundary:

* NUL, C0/C1 controls, lone surrogates and over-long values are rejected at
  the request boundary with a fixed detail (400 from the path resolver, 422
  from the schema) and are never echoed;
* the engine raises :class:`PlaylistWriteError`, an ``OSError`` subclass, so
  every transport (REST, embedded, desktop, CLI) can catch one type;
* an exporter that still raises ``ValueError`` is mapped to the same fixed
  400, so a traceback cannot reach the client;
* directory and relative targets keep the documented behaviour (the playlist
  is written inside the directory / below the primary music root);
* unwritable destinations (a path below a regular file, a read-only
  directory) become a fixed 500 that names only the exception class;
* a Windows cross-drive ``relpath`` failure falls back to an absolute
  location instead of raising;
* REST responses carry ``X-Request-ID``.

REST and embedded are exercised side by side so their status codes and fixed
details cannot drift apart.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.embedded import server as eserver
from novel_downloader.apps.music import downloader as music_downloader
from novel_downloader.apps.music.downloader import PlaylistWriteError, export_m3u
from novel_downloader.apps.music.export_path import (
    MAX_PLAYLIST_PATH_LENGTH,
    MSG_PLAYLIST_TARGET_INVALID,
    MSG_PLAYLIST_WRITE_FAILED,
    PlaylistTargetError,
    resolve_playlist_target,
)
from novel_downloader.apps.music.providers.base import Track
from tests.apps.test_api_music_contract import (
    API,
    MusicFixture,
    _assert_request_id,
    _embedded_error,
    _make_components,
)

#: NUL is rejected by the REST schema (pydantic) and by the embedded mirror
#: before the resolver runs, so both answer 422.
_SCHEMA_REJECTED: list[tuple[str, str]] = [
    ("list\x00.m3u8", "nul"),
    ("list\x01.m3u8", "c0-control"),
    ("list\ud800.m3u8", "lone-surrogate"),
    ("x" * (MAX_PLAYLIST_PATH_LENGTH + 1) + ".m3u8", "oversized"),
]

#: C1 controls and the Unicode line separators pass the schema (which only
#: blocks C0/DEL) and are caught by ``resolve_playlist_target`` -> 400.
_RESOLVER_REJECTED: list[tuple[str, str]] = [
    ("list\u0085.m3u8", "nel"),
    ("list\u2028.m3u8", "line-separator"),
]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep embedded defaults out of the developer profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    yield


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


def _body(out_path: Any, *, path: str = "song.mp3") -> dict[str, Any]:
    return {
        "platform": "netease",
        "tracks": [{"id": "1", "name": "Song", "artists": "A", "path": path}],
        "out_path": out_path,
    }


# ---------------------------------------------------------------------------
# Resolver boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(value, id=name)
        for value, name in [*_SCHEMA_REJECTED, *_RESOLVER_REJECTED]
    ],
)
def test_resolver_rejects_unusable_targets_with_fixed_message(
    tmp_path: Path, target: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()

    with pytest.raises(PlaylistTargetError) as excinfo:
        resolve_playlist_target(
            target,
            base_dirs=[root],
            default_dir=root,
            default_name="playlist-1.m3u8",
        )

    # The fixed message is the one the transports send; it never contains the
    # rejected value.
    assert str(excinfo.value) == MSG_PLAYLIST_TARGET_INVALID
    assert target not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Engine exception type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["list\x00.m3u8", "list\x01.m3u8", "list\ud800.m3u8"],
)
def test_export_m3u_normalises_unusable_target_to_oserror(
    tmp_path: Path, target: str
) -> None:
    destination = tmp_path / "out"
    destination.mkdir()

    with pytest.raises(PlaylistWriteError) as excinfo:
        export_m3u([], str(destination / target))

    error = excinfo.value
    assert isinstance(error, OSError)
    assert not isinstance(error, ValueError)
    # The fixed message never embeds the destination.
    assert target not in str(error)
    assert not any(destination.iterdir())


def test_export_m3u_sanitises_control_characters_in_track_paths(
    tmp_path: Path,
) -> None:
    """A track path is metadata, not a destination: controls are folded."""
    track = Track(
        name="Song",
        artists="A",
        duration_s=1,
        path=str(tmp_path / "broken\x00name.mp3"),
    )

    playlist = tmp_path / "list.m3u8"
    export_m3u([track], playlist, absolute=True)

    location = playlist.read_text(encoding="utf-8").splitlines()[-1]
    assert location == str(tmp_path / "broken name.mp3").replace(os.sep, "/")


# ---------------------------------------------------------------------------
# REST / embedded parity for rejected targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected_status"),
    [
        *[
            pytest.param(value, 422, id=f"schema-{name}")
            for value, name in _SCHEMA_REJECTED
        ],
        *[
            pytest.param(value, 400, id=f"resolver-{name}")
            for value, name in _RESOLVER_REJECTED
        ],
    ],
)
async def test_rest_and_embedded_reject_the_same_unusable_targets(
    client: httpx.AsyncClient,
    music: MusicFixture,
    target: str,
    expected_status: int,
) -> None:
    body = _body(target)

    # ``ensure_ascii=True`` keeps a lone surrogate as a JSON ``\ud800`` escape;
    # httpx's ``json=`` path encodes as UTF-8 and would fail client-side.
    response = await client.post(
        f"{API}/music/export",
        content=json.dumps(body, ensure_ascii=True).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )
    embedded = _embedded_error(eserver._handle_music_export, dict(body))

    assert response.status_code == embedded.status == expected_status
    detail = response.json()["detail"]
    assert detail == embedded.detail
    # Neither transport echoes the submitted value.
    assert target not in detail
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Documented success shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_directory_target_writes_the_playlist_inside_it(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    directory = music.root / "playlists"
    directory.mkdir()
    body = _body(str(directory))

    response = await client.post(f"{API}/music/export", json=body)
    assert response.status_code == 200, response.text
    rest_payload = response.json()
    assert set(rest_payload) == {"path", "tracks_written", "skipped", "written"}
    rest_written = Path(rest_payload["path"])
    assert rest_written.parent == directory.resolve()
    assert rest_written.is_file()

    embedded_payload = eserver._handle_music_export(dict(body), {}, {})
    assert set(embedded_payload) == set(rest_payload)
    embedded_written = Path(embedded_payload["path"])
    assert embedded_written.parent == directory.resolve()
    assert embedded_written.is_file()
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_relative_target_stays_below_the_music_root(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    body = _body("relative.m3u8")

    response = await client.post(f"{API}/music/export", json=body)
    assert response.status_code == 200, response.text
    assert Path(response.json()["path"]) == (music.root / "relative.m3u8").resolve()
    assert (music.root / "relative.m3u8").is_file()

    embedded_payload = eserver._handle_music_export(dict(body), {}, {})
    embedded_written = Path(embedded_payload["path"])
    assert embedded_written.name == "relative.m3u8"
    assert embedded_written.is_file()
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Unwritable destinations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_below_a_regular_file_is_a_fixed_500(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    blocker = music.root / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    body = _body(str(blocker / "list.m3u8"))

    response = await client.post(f"{API}/music/export", json=body)
    embedded = _embedded_error(eserver._handle_music_export, dict(body))

    assert response.status_code == embedded.status == 500
    detail = response.json()["detail"]
    # Only the exception class name is public.
    assert detail == MSG_PLAYLIST_WRITE_FAILED.format(error="FileExistsError")
    assert detail == embedded.detail
    assert str(blocker) not in detail
    assert "not a directory" not in detail
    _assert_request_id(response)


@pytest.mark.asyncio
async def test_read_only_directory_is_a_fixed_500(
    client: httpx.AsyncClient, music: MusicFixture
) -> None:
    readonly = music.root / "readonly"
    readonly.mkdir()
    probe = readonly / ".probe"
    os.chmod(readonly, stat.S_IREAD | stat.S_IEXEC)
    try:
        try:
            probe.write_text("x", encoding="utf-8")
        except OSError:
            pass
        else:
            probe.unlink()
            pytest.skip("this platform/filesystem does not enforce directory modes")
        body = _body(str(readonly / "list.m3u8"))
        response = await client.post(f"{API}/music/export", json=body)
        embedded = _embedded_error(eserver._handle_music_export, dict(body))
        assert response.status_code == embedded.status == 500
        detail = response.json()["detail"]
        assert detail == MSG_PLAYLIST_WRITE_FAILED.format(error="PermissionError")
        assert detail == embedded.detail
        assert str(readonly) not in detail
        _assert_request_id(response)
    finally:
        os.chmod(readonly, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)


@pytest.mark.asyncio
async def test_plain_value_error_from_the_exporter_is_a_fixed_400(
    client: httpx.AsyncClient,
    music: MusicFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth for a legacy/injected exporter."""

    def failing_export(_tracks: list[Any], _target: Path) -> str:
        raise ValueError("embedded null character in C:/secret/path")

    components = replace(music.components, export_m3u=failing_export)
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)
    monkeypatch.setattr(eserver, "_music_components", lambda: components)
    body = _body(str(music.root / "ok.m3u8"))

    response = await client.post(f"{API}/music/export", json=body)
    embedded = _embedded_error(eserver._handle_music_export, dict(body))

    assert response.status_code == embedded.status == 400
    assert response.json() == {"detail": MSG_PLAYLIST_TARGET_INVALID}
    assert embedded.detail == MSG_PLAYLIST_TARGET_INVALID
    assert "C:/secret" not in response.text
    _assert_request_id(response)


# ---------------------------------------------------------------------------
# Cross-drive fallback
# ---------------------------------------------------------------------------


def test_cross_drive_relative_location_falls_back_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")

    def no_relative_path(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(music_downloader.os.path, "relpath", no_relative_path)

    location = music_downloader._playlist_location(str(audio), str(tmp_path), False)

    assert location == str(audio).replace(os.sep, "/")
    assert not location.startswith("./")


def test_export_m3u_writes_absolute_location_on_cross_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    playlist = tmp_path / "list.m3u8"

    def no_relative_path(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(music_downloader.os.path, "relpath", no_relative_path)

    export_m3u(
        [_track(path=str(audio))],
        playlist,
        absolute=False,
    )

    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == str(audio).replace(os.sep, "/")


def _track(*, path: str) -> Track:
    return Track(name="Song", artists="A", duration_s=1, path=path)
