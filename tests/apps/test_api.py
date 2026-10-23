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

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.core.models import DownloadTask


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
    # Chinese characters and spaces are percent-encoded, so the URL is usable
    # verbatim (see test_api_security for the fetch-it-back round trip).
    assert (
        payload["files"][1]["download_url"] == "/downloads/%E7%A9%BA%20%E6%A0%BC.epub"
    )


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


@pytest.mark.asyncio
async def test_search_stream_emits_results_then_done(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    batches = [
        [
            {
                "site": "alpha",
                "book_id": "1",
                "book_url": "https://example.invalid/1",
                "cover_url": "",
                "title": "Book A",
                "author": "Author",
                "latest_chapter": "1",
                "update_date": "",
                "word_count": "",
                "priority": 1,
            }
        ],
        [
            {
                "site": "beta",
                "book_id": "2",
                "book_url": "https://example.invalid/2",
                "cover_url": "",
                "title": "Book B",
                "author": "Author",
                "latest_chapter": "2",
                "update_date": "",
                "word_count": "",
                "priority": 2,
            },
            {
                "site": "beta",
                "book_id": "3",
                "book_url": "https://example.invalid/3",
                "cover_url": "",
                "title": "Book C",
                "author": "Author",
                "latest_chapter": "3",
                "update_date": "",
                "word_count": "",
                "priority": 3,
            },
        ],
    ]

    async def fake_search_stream(
        keyword: str,
        *,
        sites: list[str] | None = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
    ) -> Any:
        calls["keyword"] = keyword
        calls["sites"] = sites
        calls["per_site_limit"] = per_site_limit
        calls["timeout"] = timeout
        for batch in batches:
            yield batch

    monkeypatch.setattr(api_routes, "search_stream", fake_search_stream)

    async with client.stream(
        "GET",
        "/api/v1/search/stream",
        params={
            "keyword": "斗破",
            "sites": "alpha, beta",
            "per_site_limit": 2,
            "timeout": 1.5,
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [event for event, _ in events] == ["results", "results", "done"]
    assert [item["book_id"] for item in events[0][1]] == ["1"]
    assert [item["book_id"] for item in events[1][1]] == ["2", "3"]
    assert events[2][1] == {"total": 3}
    assert calls["keyword"] == "斗破"
    assert calls["sites"] == ["alpha", "beta"]
    assert calls["per_site_limit"] == 2
    assert calls["timeout"] == 1.5


@pytest.mark.asyncio
async def test_search_stream_closes_source_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = asyncio.Event()

    async def fake_search_stream(
        keyword: str,
        *,
        sites: list[str] | None = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
    ) -> Any:
        try:
            yield [{"site": "alpha", "book_id": "1"}]
            await asyncio.sleep(3600)
        finally:
            closed.set()

    monkeypatch.setattr(api_routes, "search_stream", fake_search_stream)

    stream = api_routes._search_event_stream("斗破", ["alpha"], 5, 5.0)
    first = await anext(stream)
    assert "event: results" in first

    # Simulates the client disconnecting: Starlette closes the response
    # generator, which must close the underlying search generator too.
    await stream.aclose()

    assert closed.is_set()


@pytest.mark.asyncio
async def test_search_stream_validation_rejects_missing_keyword(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/search/stream")

    assert response.status_code == 422


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
async def test_tasks_get_single_task(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/v1/tasks", json={"title": "Single", "site": "demo_site", "book_id": "7"}
    )
    task_id = created.json()["task_id"]

    response = await client.get(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["title"] == "Single"
    assert payload["status"] == "queued"


@pytest.mark.asyncio
async def test_tasks_get_unknown_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/tasks/does-not-exist")

    assert response.status_code == 404
    assert "detail" in response.json()


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


# ---------------------------------------------------------------------------
# Music: asynchronous jobs
# ---------------------------------------------------------------------------


class _FakeTrack:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakeDownloadOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.data = kwargs


class _FakePaths:
    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir

    def default_download_dir(self) -> Path:
        return self._out_dir


def _fake_music_components(download_many: Any, out_dir: Path) -> Any:
    return api_routes._MusicComponents(
        paths=_FakePaths(out_dir),
        download_options=_FakeDownloadOptions,
        download_many=download_many,
        available_labels=lambda: [],
        create=lambda platform: object(),
        platform_note=lambda platform: "",
        track_cls=_FakeTrack,
    )


@pytest.fixture
def clean_music_jobs() -> Any:
    api_routes._MUSIC_JOBS.clear()
    yield
    api_routes._MUSIC_JOBS.clear()


async def _wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll a music job until it leaves the queued/running states."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/api/v1/music/jobs/{job_id}")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        if payload["status"] in {"done", "failed"}:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not finish: {payload}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_music_job_runs_to_completion(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clean_music_jobs: Any,
) -> None:
    calls: dict[str, Any] = {}

    def fake_download_many(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        calls["count"] = len(tracks)
        calls["out_dir"] = options.data["out_dir"]
        calls["level"] = options.data["level"]
        for index, track in enumerate(tracks):
            on_result(
                index,
                track,
                SimpleNamespace(
                    status="ok",
                    reason="",
                    path=f"/music/{index}.mp3",
                    bytes_=index + 1,
                ),
            )
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(fake_download_many, tmp_path),
    )

    response = await client.post(
        "/api/v1/music/jobs",
        json={
            "platform": "demo",
            "tracks": [{"id": "1"}, {"id": "2"}],
            "level": "standard",
        },
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]

    payload = await _wait_for_job(client, job_id)

    assert payload["status"] == "done"
    assert payload["progress"] == {"done": 2, "total": 2}
    assert payload["error"] is None
    assert [item["path"] for item in payload["results"]] == [
        "/music/0.mp3",
        "/music/1.mp3",
    ]
    assert calls["count"] == 2
    assert calls["out_dir"] == str(tmp_path)
    assert calls["level"] == "standard"


@pytest.mark.asyncio
async def test_music_job_reports_failure(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clean_music_jobs: Any,
) -> None:
    def failing_download_many(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(failing_download_many, tmp_path),
    )

    response = await client.post(
        "/api/v1/music/jobs",
        json={"platform": "demo", "tracks": [{"id": "1"}]},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    payload = await _wait_for_job(client, job_id)

    assert payload["status"] == "failed"
    assert "boom" in payload["error"]
    assert payload["progress"] == {"done": 0, "total": 1}


@pytest.mark.asyncio
async def test_music_job_unknown_returns_404(
    client: httpx.AsyncClient,
    clean_music_jobs: Any,
) -> None:
    response = await client.get("/api/v1/music/jobs/nope")

    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_music_job_returns_503_when_deps_missing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mutagen", None)

    response = await client.post(
        "/api/v1/music/jobs",
        json={"platform": "netease", "tracks": [{"id": "1"}]},
    )

    assert response.status_code == 503
