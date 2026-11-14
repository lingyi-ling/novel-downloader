#!/usr/bin/env python3
"""
tests.apps.test_api_health
--------------------------

Diagnostics contract for ``GET /api/v1/health``.

The endpoint must stay a cheap, read-only probe: no network, no plugin import,
no directory creation, and no server path or credential in the response. The
historical ``status``/``version`` fields must remain present and unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask, Status


class FakeManager:
    """Minimal in-memory task manager double (no persistence, no workers)."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []
        self.running: list[DownloadTask] = []
        self.completed: list[DownloadTask] = []

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {
            "pending": list(self.pending),
            "running": list(self.running),
            "completed": list(self.completed),
        }

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config/cache resolution outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


def _build_app(tmp_path: Path, fake_manager: FakeManager, **kwargs: Any) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(tmp_path: Path, fake_manager: FakeManager) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_build_app(tmp_path, fake_manager))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Field set and types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_keeps_legacy_fields_and_adds_typed_diagnostics(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()

    # Backward compatibility: the original two fields are unchanged.
    assert payload["status"] == "ok"
    assert payload["version"] == __version__

    assert isinstance(payload["uptime_s"], (int, float))
    assert payload["uptime_s"] >= 0
    assert isinstance(payload["output_writable"], bool)
    assert isinstance(payload["cache_writable"], bool)

    rate_limit = payload["rate_limit"]
    assert set(rate_limit) == {"enabled", "limit", "window"}
    assert isinstance(rate_limit["enabled"], bool)

    tasks = payload["tasks"]
    assert set(tasks) == {"pending", "running", "completed"}
    assert all(isinstance(count, int) for count in tasks.values())
    assert all(count >= 0 for count in tasks.values())

    assert isinstance(payload["music_platforms_available"], int)
    assert payload["music_platforms_available"] >= 0


@pytest.mark.asyncio
async def test_health_reports_task_counts(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    fake_manager.pending = [
        DownloadTask(title="p1", site="qidian", book_id="1"),
        DownloadTask(title="p2", site="qidian", book_id="2"),
    ]
    fake_manager.running = [
        DownloadTask(
            title="r1",
            site="qidian",
            book_id="3",
            status=Status.RUNNING,
        )
    ]
    fake_manager.completed = [
        DownloadTask(
            title=f"c{index}",
            site="qidian",
            book_id=str(index),
            status=Status.COMPLETED,
        )
        for index in range(3)
    ]

    transport = httpx.ASGITransport(app=_build_app(tmp_path, fake_manager))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        payload = (await http_client.get("/api/v1/health")).json()

    assert payload["tasks"] == {"pending": 2, "running": 1, "completed": 3}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_enabled_rate_limit(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    application = _build_app(
        tmp_path,
        fake_manager,
        rate_limit=7,
        rate_limit_window=2.5,
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        payload = (await http_client.get("/api/v1/health")).json()

    assert payload["rate_limit"] == {"enabled": True, "limit": 7, "window": 2.5}


@pytest.mark.asyncio
async def test_health_reports_disabled_rate_limit(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    application = _build_app(tmp_path, fake_manager, rate_limit=None)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        payload = (await http_client.get("/api/v1/health")).json()

    assert payload["rate_limit"] == {
        "enabled": False,
        "limit": None,
        "window": None,
    }


# ---------------------------------------------------------------------------
# Writability and path redaction
# ---------------------------------------------------------------------------


def test_path_writable_accepts_missing_child_of_existing_dir(tmp_path: Path) -> None:
    assert api_routes._path_writable(tmp_path / "missing" / "deeper") is True


def test_path_writable_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    assert api_routes._path_writable(target) is False


def test_path_writable_reports_read_only_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_routes.os, "access", lambda *_args: False)

    assert api_routes._path_writable(tmp_path) is False


@pytest.mark.asyncio
async def test_health_writability_probe_does_not_create_directories(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "never-created-output"
    cache = tmp_path / "never-created-cache"
    monkeypatch.setattr(api_routes, "_output_path", lambda: output)
    monkeypatch.setattr(api_routes, "_cache_path", lambda: cache)

    transport = httpx.ASGITransport(app=_build_app(tmp_path, fake_manager))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        payload = (await http_client.get("/api/v1/health")).json()

    # Both parents exist, so the probe reports writable without creating them.
    assert payload["output_writable"] is True
    assert payload["cache_writable"] is True
    assert not output.exists()
    assert not cache.exists()


@pytest.mark.asyncio
async def test_health_never_leaks_configured_paths(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = tmp_path / "top-secret-output-directory"
    secret_cache = tmp_path / "top-secret-cache-directory"
    monkeypatch.setattr(api_routes, "_output_path", lambda: secret)
    monkeypatch.setattr(api_routes, "_cache_path", lambda: secret_cache)

    transport = httpx.ASGITransport(app=_build_app(tmp_path, fake_manager))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        response = await http_client.get("/api/v1/health")

    assert response.status_code == 200
    assert "top-secret-output-directory" not in response.text
    assert "top-secret-cache-directory" not in response.text
    assert str(tmp_path) not in response.text
    assert "path" not in response.text.lower()


# ---------------------------------------------------------------------------
# Uptime and read-only behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_uptime_tracks_the_module_start_time(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_routes, "_STARTED_MONOTONIC", time.monotonic() - 12.5)

    payload = (await client.get("/api/v1/health")).json()

    assert 12.0 <= payload["uptime_s"] < 14.0


@pytest.mark.asyncio
async def test_health_is_read_only(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "read-only-output"
    cache = tmp_path / "read-only-cache"
    monkeypatch.setattr(api_routes, "_output_path", lambda: output)
    monkeypatch.setattr(api_routes, "_cache_path", lambda: cache)

    application = _build_app(tmp_path, fake_manager)
    transport = httpx.ASGITransport(app=application)
    before = set(tmp_path.rglob("*"))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        first = (await http_client.get("/api/v1/health")).json()
        second = (await http_client.get("/api/v1/health")).json()

    assert (
        first["tasks"]
        == second["tasks"]
        == {
            "pending": 0,
            "running": 0,
            "completed": 0,
        }
    )
    assert set(tmp_path.rglob("*")) == before
