#!/usr/bin/env python3
"""
tests.apps.test_api_r5
----------------------

Round 5 audit tests: real defects found by reading the API layer against its
own documented contract.

Covered:

* Music endpoint error details must not leak raw exception text (paths,
  provider internals) to clients — the same anti-leak rule the 503 handler
  and the generic 500 handler already follow. The full reason stays in the
  server log.
* ``sites=[]`` / ``sites=`` mean "search no site" per the endpoint
  docstrings; the previous behaviour silently searched *every* site.
* A music job killed by its timeout must freeze its ``progress``: the worker
  thread keeps calling back after the terminal state was recorded.
* A token configured with surrounding whitespace must not permanently lock
  out every client (the provided token is stripped, the configured one was
  not); whitespace-only is treated like an empty token (auth disabled).
* The SDK's ``music_search`` must expose the server's ``offset`` parameter.

Everything runs offline through ``httpx.ASGITransport`` /
``httpx.MockTransport``; no server is started and no network access happens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import main as api_main_module
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.client import NovelApiClient
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask

Handler = Callable[[httpx.Request], httpx.Response]

#: Logger the sanitized music failures are recorded on.
ROUTES_LOGGER = "novel_downloader.apps.api.routes"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep app state/config out of the real user profile and environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


class FakeManager:
    """Minimal in-memory stand-in for the task manager."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []

    async def add_task(self, **kwargs: Any) -> DownloadTask:
        task = DownloadTask(
            title=kwargs["title"], site=kwargs["site"], book_id=kwargs["book_id"]
        )
        self.pending.append(task)
        return task

    async def cancel_task(self, task_id: str) -> bool:
        return False

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": list(self.pending), "running": [], "completed": []}

    async def close(self) -> None:
        return None


@asynccontextmanager
async def _client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture
def make_app(tmp_path: Path) -> Callable[..., FastAPI]:
    """Factory building apps whose ``/downloads`` points at ``tmp_path``."""

    def _make(**kwargs: Any) -> FastAPI:
        kwargs.setdefault("output_dir", tmp_path / "downloads")
        application = create_app(**kwargs)
        application.dependency_overrides[get_manager] = lambda: FakeManager()
        return application

    return _make


@pytest.fixture
def clean_music_jobs() -> Any:
    api_routes._MUSIC_JOBS.clear()
    yield
    api_routes._MUSIC_JOBS.clear()


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    """Parse an SSE payload into ``(event, data)`` pairs."""
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        event = next(
            line[len("event: ") :] for line in lines if line.startswith("event: ")
        )
        data = next(
            line[len("data: ") :] for line in lines if line.startswith("data: ")
        )
        events.append((event, json.loads(data)))
    return events


# ---------------------------------------------------------------------------
# Music fakes
# ---------------------------------------------------------------------------


class _FakeTrack:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakePaths:
    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir

    def default_download_dir(self) -> Path:
        return self._out_dir


def _components(
    *,
    out_dir: Path,
    download_many: Any = None,
    create: Any = None,
    export_m3u: Any = None,
) -> api_routes._MusicComponents:
    provider = object()

    def _download_many(*args: Any, **kwargs: Any) -> Any:
        if download_many is not None:
            return download_many(*args, **kwargs)
        return (0, 0, 0, [], [])

    def _create(platform: str) -> Any:
        if create is not None:
            return create(platform)
        return provider

    def _export(tracks: Any, target: Any) -> Path:
        if export_m3u is not None:
            return export_m3u(tracks, target)
        return Path(target)

    return api_routes._MusicComponents(
        paths=_FakePaths(out_dir),
        download_options=_FakeOptions,
        download_many=_download_many,
        available_labels=lambda: [("demo", "Demo")],
        create=_create,
        platform_note=lambda platform: "",
        track_cls=_FakeTrack,
        export_m3u=_export,
        health_check=lambda platform, **kwargs: {
            "ok": True,
            "elapsed_ms": 1,
            "error": "",
        },
        load_settings=lambda: {"out_dir": str(out_dir)},
    )


# ---------------------------------------------------------------------------
# Defect 1: music error details leak raw exception text
# ---------------------------------------------------------------------------

SECRET = "cookie jar /home/victim/.secret-token-xyz"


@pytest.mark.asyncio
async def test_music_download_502_detail_does_not_leak_exception_text(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 502 detail names the exception class, never its message."""

    def exploding_download(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"provider exploded: {SECRET}")

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, download_many=exploding_download),
    )

    app = make_app()
    async with _client(app) as client:
        with caplog.at_level(logging.WARNING, logger=ROUTES_LOGGER):
            response = await client.post(
                "/api/v1/music/download",
                json={"platform": "demo", "tracks": [{"id": "1"}]},
            )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "RuntimeError" in detail
    # Neither the message nor any of its internals may reach the client.
    assert SECRET not in response.text
    assert "provider exploded" not in response.text
    # The full reason is preserved for the operator in the server log.
    assert any(SECRET in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_music_export_500_detail_does_not_leak_exception_text(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The write-failure detail keeps the caller's target but not the OSError."""

    internal = tmp_path / "internal" / "server-side-playlist.m3u8"

    def failing_export(tracks: Any, target: Any) -> Path:
        raise PermissionError(13, "Permission denied", str(internal))

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, export_m3u=failing_export),
    )

    app = make_app()
    async with _client(app) as client:
        with caplog.at_level(logging.WARNING, logger=ROUTES_LOGGER):
            response = await client.post(
                "/api/v1/music/export",
                json={
                    "platform": "demo",
                    "tracks": [{"id": "1", "path": "song.mp3"}],
                    "out_path": "playlist.m3u8",
                },
            )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    # The caller-supplied target is echoed back (it is their own input) ...
    assert "playlist.m3u8" in detail
    # ... while the raw OS error, with the server-internal path, is not.
    assert "PermissionError" in detail
    assert "Permission denied" not in response.text
    assert "server-side-playlist" not in response.text
    assert any("Permission denied" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_music_provider_init_detail_is_sanitized(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider construction failure reports the class name only."""

    def broken_create(platform: str) -> Any:
        raise ValueError(f"init failed while reading {SECRET}")

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, create=broken_create),
    )

    app = make_app()
    async with _client(app) as client:
        with caplog.at_level(logging.WARNING, logger=ROUTES_LOGGER):
            response = await client.post(
                "/api/v1/music/search",
                json={"platform": "demo", "keyword": "x"},
            )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "ValueError" in detail
    assert SECRET not in response.text
    assert "init failed" not in response.text
    assert any(SECRET in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Defect 2: sites=[] means "no sites", not "every site"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_empty_sites_returns_no_results(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sites: []`` must not fan out to every registered site."""
    calls: list[dict[str, Any]] = []

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"keyword": keyword, **kwargs})
        return [{"site": "demo", "book_id": "1"}]

    monkeypatch.setattr(api_routes, "search", fake_search)

    app = make_app()
    async with _client(app) as client:
        empty = await client.post("/api/v1/search", json={"keyword": "x", "sites": []})
        all_sites = await client.post("/api/v1/search", json={"keyword": "x"})

    assert empty.status_code == 200
    assert empty.json() == {"total": 0, "results": []}
    # Only the omitted-parameter request reached the search plugin.
    assert len(calls) == 1
    assert calls[0]["sites"] is None

    assert all_sites.status_code == 200
    assert all_sites.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_stream_with_empty_sites_parameter_emits_only_done(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?sites=`` (an empty value) must emit a bare ``done`` event."""
    calls: list[dict[str, Any]] = []

    async def fake_search_stream(
        keyword: str,
        *,
        sites: list[str] | None = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
    ) -> Any:
        calls.append({"keyword": keyword, "sites": sites})
        yield [{"site": "demo", "book_id": "1"}]

    monkeypatch.setattr(api_routes, "search_stream", fake_search_stream)

    app = make_app()
    async with (
        _client(app) as client,
        client.stream(
            "GET", "/api/v1/search/stream", params={"keyword": "x", "sites": ""}
        ) as response,
    ):
        assert response.status_code == 200
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert events == [("done", {"total": 0})]
    # The underlying search was never started.
    assert calls == []


# ---------------------------------------------------------------------------
# Defect 3: music job progress must freeze once terminal
# ---------------------------------------------------------------------------


async def _wait_for_job_status(
    client: httpx.AsyncClient,
    job_id: str,
    wanted: set[str],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Poll a music job until it reaches one of the wanted statuses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/api/v1/music/jobs/{job_id}")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        if payload["status"] in wanted:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not finish: {payload}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_music_job_progress_is_frozen_after_timeout(
    make_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clean_music_jobs: Any,
) -> None:
    """
    A job killed by its timeout is terminal; the still-running worker thread
    must not keep mutating its ``progress`` afterwards.
    """

    def slow_download(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        # Outlive the request budget, then report a late result.
        time.sleep(0.3)
        on_result(
            0,
            tracks[0],
            SimpleResult(status="ok", path="/music/0.mp3", bytes_=1),
        )
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _components(out_dir=tmp_path, download_many=slow_download),
    )

    app = make_app(timeouts={"music_download": 0.05})
    async with _client(app) as client:
        created = await client.post(
            "/api/v1/music/jobs",
            json={"platform": "demo", "tracks": [{"id": "1"}]},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        failed = await _wait_for_job_status(client, job_id, {"failed"})
        assert failed["status"] == "failed"
        assert "timed out" in failed["error"]
        frozen_progress = failed["progress"]

        # Wait until the runaway worker thread has finished calling back.
        await asyncio.sleep(0.4)

        after = await client.get(f"/api/v1/music/jobs/{job_id}")

    assert after.status_code == 200
    assert after.json()["status"] == "failed"
    # The terminal snapshot must not keep changing under the client.
    assert after.json()["progress"] == frozen_progress == {"done": 0, "total": 1}


class SimpleResult:
    """Minimal stand-in for a per-track download result."""

    def __init__(self, status: str, path: str, bytes_: int) -> None:
        self.status = status
        self.path = path
        self.bytes_ = bytes_
        self.reason = ""


# ---------------------------------------------------------------------------
# Defect 4: token whitespace handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_with_surrounding_whitespace_is_normalized(
    make_app: Any,
) -> None:
    """A configured token with stray spaces must remain usable."""
    app = make_app(token="  secret  ")

    async with _client(app) as client:
        denied = await client.get("/api/v1/health")
        bearer = await client.get(
            "/api/v1/health", headers={"Authorization": "Bearer secret"}
        )

    assert denied.status_code == 401
    assert bearer.status_code == 200


@pytest.mark.asyncio
async def test_token_whitespace_only_disables_auth(
    make_app: Any,
) -> None:
    """A whitespace-only token is as good as no token (documented for empty)."""
    app = make_app(token="   ")
    assert app.state.api_token is None

    async with _client(app) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_env_token_with_whitespace_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVEL_API_TOKEN", "  spaced  ")
    app = create_app(output_dir=tmp_path / "downloads")

    async with _client(app) as client:
        denied = await client.get("/api/v1/health")
        allowed = await client.get("/api/v1/health", headers={"X-API-Key": "spaced"})

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_api_main_normalizes_whitespace_env_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``novel-api`` strips the token before configuring (and warning about) it."""
    captured: dict[str, Any] = {}

    def fake_create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(api_main_module, "create_app", fake_create_app)
    monkeypatch.setattr(api_main_module.uvicorn, "run", lambda *a, **k: None)

    monkeypatch.setenv("NOVEL_API_TOKEN", "  tok  ")
    api_main_module.api_main([])
    assert captured["token"] == "tok"
    # A local bind with a token never warns.
    assert "WARNING" not in capsys.readouterr().err

    # A whitespace-only token behaves like "no token": the public-bind
    # warning must fire instead of silently opening the API.
    captured.clear()
    monkeypatch.setenv("NOVEL_API_TOKEN", "   ")
    api_main_module.api_main(["--host", "0.0.0.0"])
    assert captured["token"] == ""
    assert "WARNING" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Defect 5: SDK / server drift — music search pagination
# ---------------------------------------------------------------------------


def test_client_music_search_sends_offset() -> None:
    """The server supports ``offset``; the SDK must be able to send it."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"total": 0, "tracks": []})

    with NovelApiClient(
        base_url="http://api.test", transport=httpx.MockTransport(handler)
    ) as client:
        paged = client.music_search("netease", "song", limit=5, offset=10)
        first_page = client.music_search("netease", "song")

    assert paged == {"total": 0, "tracks": []}
    assert first_page == {"total": 0, "tracks": []}
    assert [request.url.path for request in requests] == [
        "/api/v1/music/search",
        "/api/v1/music/search",
    ]
    assert json.loads(requests[0].content) == {
        "platform": "netease",
        "keyword": "song",
        "limit": 5,
        "offset": 10,
    }
    # Omitting the argument keeps the server-side default explicit.
    assert json.loads(requests[1].content)["offset"] == 0
