#!/usr/bin/env python3
"""
tests.apps.test_api
-------------------

Unit tests for the local REST API.

Everything runs through ``httpx.ASGITransport``: no server is started and no
real network access happens. The task manager is replaced with a fake and the
search function is monkeypatched where needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.web.models import DownloadTask


class FakeManager:
    """Minimal in-memory stand-in for the web task manager."""

    def __init__(self) -> None:
        self.pending: list[DownloadTask] = []
        self.running: list[DownloadTask] = []
        self.completed: list[DownloadTask] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        task = DownloadTask(title=title, site=site, book_id=book_id)
        self.pending.append(task)
        return task

    async def cancel_task(self, task_id: str) -> bool:
        for index, task in enumerate(self.pending):
            if task.task_id == task_id:
                self.pending.pop(index)
                self.completed.append(task)
                return True
        return False

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
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    # A token in the developer's environment must not leak into these tests;
    # the auth tests configure their own token explicitly.
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def app(tmp_path: Path, fake_manager: FakeManager) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def auth_app(tmp_path: Path, fake_manager: FakeManager) -> FastAPI:
    application = create_app(
        token="secret-token",
        output_dir=tmp_path / "downloads",
    )
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["version"], str)


@pytest.mark.asyncio
async def test_sites(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/sites")

    assert response.status_code == 200
    payload = response.json()
    assert "qidian" in payload["search"]
    assert "qidian" in payload["download"]
    assert isinstance(payload["search"]["qidian"], str)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_lists_exported_files(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    old = output_dir / "空 格.epub"
    new = output_dir / "b.txt"
    old.write_text("x", encoding="utf-8")
    new.write_text("hello", encoding="utf-8")
    (output_dir / "ignored.pdf").write_text("nope", encoding="utf-8")
    os.utime(old, (100.0, 100.0))
    os.utime(new, (200.0, 200.0))

    monkeypatch.setattr(history_files, "_output_dir", lambda: output_dir)

    response = await client.get("/api/v1/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["name"] for item in payload["files"]] == ["b.txt", "空 格.epub"]
    first = payload["files"][0]
    assert first["ext"] == "txt"
    assert first["size"] == 5
    assert first["download_url"] == "/downloads/b.txt"
    assert payload["files"][1]["download_url"].startswith("/downloads/")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_validation_rejects_missing_keyword(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/search", json={})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_validation_rejects_bad_limit(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/search", json={"keyword": "x", "per_site_limit": 0}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_returns_plugin_results(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["keyword"] = keyword
        calls["kwargs"] = kwargs
        return [
            {
                "site": "demo",
                "book_id": "42",
                "book_url": "https://example.invalid/42",
                "cover_url": "",
                "title": "Book",
                "author": "Author",
                "latest_chapter": "1",
                "update_date": "",
                "word_count": "",
                "priority": 1,
            }
        ]

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        "/api/v1/search",
        json={
            "keyword": "斗破",
            "sites": ["demo"],
            "per_site_limit": 3,
            "timeout": 2.5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["results"][0]["book_id"] == "42"
    assert calls["keyword"] == "斗破"
    assert calls["kwargs"]["per_site_limit"] == 3
    assert calls["kwargs"]["timeout"] == 2.5


# ---------------------------------------------------------------------------
# Tasks (fake manager)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_create_list_and_cancel(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"site": "demo_site", "book_id": "123"}
    )

    assert created.status_code == 200
    task = created.json()
    assert task["title"] == "demo_site (id = 123)"
    assert task["status"] == "queued"
    assert task["progress"] == 0.0
    assert task["exported_paths"] == {}
    task_id = task["task_id"]

    listed = await client.get("/api/v1/tasks")
    assert listed.status_code == 200
    payload = listed.json()
    assert [item["task_id"] for item in payload["pending"]] == [task_id]
    assert payload["running"] == []
    assert payload["completed"] == []

    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True}

    listed_again = await client.get("/api/v1/tasks")
    assert listed_again.json()["pending"] == []


@pytest.mark.asyncio
async def test_tasks_cancel_unknown_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/tasks/nope/cancel")

    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_tasks_custom_title(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "My Book", "site": "demo_site", "book_id": "9"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "My Book"


@pytest.mark.asyncio
async def test_tasks_validation_requires_site_and_book_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/tasks", json={"book_id": "1"})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_rejects_missing_token(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get("/api/v1/health")

    assert response.status_code == 401
    assert response.json()["detail"]


@pytest.mark.asyncio
async def test_auth_accepts_bearer_token(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_accepts_api_key_header(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/health",
        headers={"X-API-Key": "secret-token"},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_rejects_wrong_token(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Music: missing optional dependencies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_platforms_returns_503_when_deps_missing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``None`` in sys.modules makes ``import mutagen`` raise ImportError,
    # regardless of whether the extra is installed in this environment.
    monkeypatch.setitem(sys.modules, "mutagen", None)

    response = await client.get("/api/v1/music/platforms")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "music" in detail.lower()
    assert "novel-downloader[music]" in detail


@pytest.mark.asyncio
async def test_music_search_returns_503_when_deps_missing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mutagen", None)

    response = await client.post(
        "/api/v1/music/search",
        json={"platform": "netease", "keyword": "hello"},
    )

    assert response.status_code == 503
