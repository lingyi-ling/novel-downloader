#!/usr/bin/env python3
"""
tests.apps.test_api_e2e
-----------------------

End-to-end verification of the whole ``/api/v1`` surface.

Every test drives a fully assembled application (``create_app`` with all its
middleware) through ``httpx.ASGITransport``: no server process is started and
no real network access happens. External effects are replaced at the edges:

* the task manager is an in-memory fake (same pattern as ``test_api.py``);
* the aggregate search and the music engine calls are monkeypatched;
* ``NOVEL_HOME`` points at a temporary directory, so the watchlist file and
  the credential broker state are host-independent;
* the exported-file history scans a temporary directory.

The music endpoints that only exercise offline code paths (``/music/platforms``,
``/music/export``, ``/music/health``) run against the *real* music components;
they are skipped when the optional ``music`` extras are not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.constants import (
    DOWNLOAD_SUPPORT_SITES,
    SEARCH_SUPPORT_SITES,
)
from novel_downloader.apps.core import cred_broker
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.schemas import LoginField

API = "/api/v1"

#: Every path the OpenAPI document must expose (the complete route table).
EXPECTED_OPENAPI_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/api/v1/sites",
        "/api/v1/search",
        "/api/v1/search/stream",
        "/api/v1/tasks",
        "/api/v1/tasks/stream",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/cancel",
        "/api/v1/login/requests",
        "/api/v1/login/requests/stream",
        "/api/v1/login/requests/{req_id}",
        "/api/v1/watchlist",
        "/api/v1/watchlist/check",
        "/api/v1/watchlist/{site}/{book_id}",
        "/api/v1/history",
        "/api/v1/music/platforms",
        "/api/v1/music/search",
        "/api/v1/music/download",
        "/api/v1/music/jobs",
        "/api/v1/music/jobs/{job_id}",
        "/api/v1/music/export",
        "/api/v1/music/health",
    }
)


def _music_extras_available() -> bool:
    """Whether the optional music dependencies are importable."""
    try:
        import mutagen  # noqa: F401
        import qrcode  # noqa: F401
        import websocket  # noqa: F401
    except ImportError:
        return False
    return True


#: Marker for tests that run against the real (offline) music components.
requires_music_extras = pytest.mark.skipif(
    not _music_extras_available(),
    reason="optional music extras are not installed",
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeManager:
    """Minimal in-memory stand-in for the process-wide task manager."""

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


@dataclass
class _FakeTrack:
    """Stands in for ``novel_downloader.apps.music.providers.base.Track``."""

    provider: str = ""
    id: str = ""
    name: str = "未知歌曲"
    artists: str = "未知歌手"
    album: str = "未知专辑"
    duration_s: int = 0
    cover_url: str = ""
    track_no: int = 0
    ext: str = ""
    vip: bool = False
    download_url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    path: str = ""
    lyric: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class _FakeMusicProvider:
    """Provider double whose ``search`` returns a canned (tracks, total)."""

    def __init__(
        self,
        calls: list[dict[str, Any]],
        result: tuple[list[_FakeTrack], int],
    ) -> None:
        self._calls = calls
        self._result = result

    def search(
        self, keyword: str, *, limit: int, offset: int
    ) -> tuple[list[_FakeTrack], int]:
        self._calls.append({"keyword": keyword, "limit": limit, "offset": offset})
        return self._result


def _fake_music_components(**overrides: Any) -> Any:
    """
    Build an :class:`api_routes._MusicComponents` from harmless defaults.

    Individual tests override the pieces they exercise (``create``,
    ``download_many``, ...) via keyword arguments.
    """
    defaults: dict[str, Any] = {
        "paths": SimpleNamespace(default_download_dir=lambda: Path("unused")),
        "download_options": lambda **kwargs: SimpleNamespace(data=kwargs),
        "download_many": lambda *args, **kwargs: (0, 0, 0, [], []),
        "available_labels": lambda: [("netease", "netease")],
        "create": lambda platform: object(),
        "platform_note": lambda platform: "",
        "track_cls": _FakeTrack,
        "export_m3u": lambda tracks, path: Path(path),
        "health_check": lambda platform, *args, **kwargs: {
            "ok": True,
            "elapsed_ms": 1,
            "error": "",
        },
        "load_settings": lambda: {},
    }
    defaults.update(overrides)
    return api_routes._MusicComponents(**defaults)


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


async def _wait_for_music_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll an async music job until it reaches a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"{API}/music/jobs/{job_id}")
        assert response.status_code == 200
        payload: dict[str, Any] = response.json()
        if payload["status"] in {"done", "failed"}:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"music job {job_id} did not finish: {payload}")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep watchlist/config/broker-side state outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    # A token in the developer's environment must not leak into these tests.
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


def _build_app(
    tmp_path: Path,
    fake_manager: FakeManager,
    **kwargs: Any,
) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest.fixture
def app(tmp_path: Path, fake_manager: FakeManager) -> FastAPI:
    return _build_app(tmp_path, fake_manager)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def auth_client(tmp_path: Path, fake_manager: FakeManager) -> httpx.AsyncClient:
    application = _build_app(tmp_path, fake_manager, token="secret-token")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def tiny_body_client(tmp_path: Path, fake_manager: FakeManager) -> httpx.AsyncClient:
    application = _build_app(tmp_path, fake_manager, max_body_bytes=100)
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def clean_music_jobs() -> Any:
    api_routes._MUSIC_JOBS.clear()
    yield
    api_routes._MUSIC_JOBS.clear()


@pytest.fixture
def clean_cred_broker() -> Any:
    cred_broker._CRED_REQS.clear()
    yield
    cred_broker._CRED_REQS.clear()


# ---------------------------------------------------------------------------
# System: /health, /sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_status_and_version(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{API}/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"status": "ok", "version": __version__}


@pytest.mark.asyncio
async def test_sites_returns_search_and_download_groups(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{API}/sites")

    assert response.status_code == 200
    payload = response.json()
    assert payload["search"] == dict(SEARCH_SUPPORT_SITES)
    assert payload["download"] == dict(DOWNLOAD_SUPPORT_SITES)
    assert payload["search"] and payload["download"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_ranked_results(
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
                "title": "Demo Book",
                "author": "Author",
                "latest_chapter": "第一章",
                "update_date": "2026-01-01",
                "word_count": "123456",
                "priority": 1,
            }
        ]

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await client.post(
        f"{API}/search",
        json={
            "keyword": "斗破苍穹",
            "sites": ["demo"],
            "limit": 10,
            "per_site_limit": 3,
            "timeout": 2.5,
            "sort": "title",
            "order": "asc",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["results"][0]["book_id"] == "42"
    assert payload["results"][0]["title"] == "Demo Book"
    assert calls["keyword"] == "斗破苍穹"
    assert calls["kwargs"]["sites"] == ["demo"]
    assert calls["kwargs"]["limit"] == 10
    assert calls["kwargs"]["per_site_limit"] == 3
    assert calls["kwargs"]["timeout"] == 2.5
    assert calls["kwargs"]["sort"] == "title"
    assert calls["kwargs"]["order"] == "asc"


@pytest.mark.asyncio
async def test_search_stream_emits_results_then_done(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE envelope check with a finite stand-in for the endless-capable stream."""

    async def fake_search_stream(
        keyword: str,
        *,
        sites: list[str] | None = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
    ) -> Any:
        assert keyword == "斗破"
        assert sites == ["demo"]
        yield [{"site": "demo", "book_id": "1", "title": "Book"}]

    monkeypatch.setattr(api_routes, "search_stream", fake_search_stream)

    async with client.stream(
        "GET",
        f"{API}/search/stream",
        params={"keyword": "斗破", "sites": "demo"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [name for name, _ in events] == ["results", "done"]
    assert events[0][1][0]["book_id"] == "1"
    assert events[1][1] == {"total": 1}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_returns_full_task_dict(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"{API}/tasks",
        json={"title": "斗破苍穹", "site": "b520", "book_id": "8_8187"},
    )

    assert response.status_code == 200
    task = response.json()
    assert task["title"] == "斗破苍穹"
    assert task["site"] == "b520"
    assert task["book_id"] == "8_8187"
    assert task["status"] == "queued"
    assert task["chapters_done"] == 0
    assert task["chapters_total"] == 0
    assert task["progress"] == 0.0
    assert task["error"] is None
    assert task["exported_paths"] == {}
    assert task["export_stage"] is None
    assert isinstance(task["task_id"], str) and task["task_id"]
    assert isinstance(task["created_at"], float)
    assert task["finished_at"] is None


@pytest.mark.asyncio
async def test_create_task_defaults_title(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(f"{API}/tasks", json={"site": "demo", "book_id": "1"})

    assert response.status_code == 200
    assert response.json()["title"] == "demo (id = 1)"


@pytest.mark.asyncio
async def test_list_tasks_returns_three_groups(
    client: httpx.AsyncClient,
    fake_manager: FakeManager,
) -> None:
    pending = DownloadTask(title="p", site="demo", book_id="1")
    running = DownloadTask(title="r", site="demo", book_id="2")
    done = DownloadTask(title="d", site="demo", book_id="3")
    fake_manager.pending = [pending]
    fake_manager.running = [running]
    fake_manager.completed = [done]

    response = await client.get(f"{API}/tasks")

    assert response.status_code == 200
    payload = response.json()
    assert {"pending", "running", "completed", "total", "offset", "limit"} <= (
        set(payload)
    )
    assert payload["total"] == 3
    assert [t["task_id"] for t in payload["pending"]] == [pending.task_id]
    assert [t["task_id"] for t in payload["running"]] == [running.task_id]
    assert [t["task_id"] for t in payload["completed"]] == [done.task_id]
    assert payload["offset"] == 0
    assert payload["limit"] == 3


@pytest.mark.asyncio
async def test_get_single_task(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(f"{API}/tasks", json={"site": "demo", "book_id": "7"})
    task_id = created.json()["task_id"]

    response = await client.get(f"{API}/tasks/{task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["status"] == "queued"


@pytest.mark.asyncio
async def test_cancel_task_moves_it_to_completed(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(f"{API}/tasks", json={"site": "demo", "book_id": "1"})
    task_id = created.json()["task_id"]

    response = await client.post(f"{API}/tasks/{task_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    payload = (await client.get(f"{API}/tasks")).json()
    assert payload["pending"] == []
    assert [t["task_id"] for t in payload["completed"]] == [task_id]


@pytest.mark.asyncio
async def test_tasks_stream_emits_snapshot_frames(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"pending": [], "running": [], "completed": []}

    async def fake_stream(mgr: Any, interval: float) -> Any:
        assert interval == api_routes.TASK_STREAM_INTERVAL
        yield api_routes._sse_frame("snapshot", snapshot)
        yield api_routes._sse_frame("snapshot", snapshot)

    monkeypatch.setattr(api_routes, "_task_event_stream", fake_stream)

    async with client.stream("GET", f"{API}/tasks/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [name for name, _ in events] == ["snapshot", "snapshot"]
    assert events[0][1] == snapshot


# ---------------------------------------------------------------------------
# Login credential requests (real broker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_request_lifecycle_with_real_broker(
    client: httpx.AsyncClient,
    clean_cred_broker: Any,
) -> None:
    request = await cred_broker.create_cred_request(
        task_id="task-1",
        title="B520 登录",
        fields=[
            LoginField(name="username", label="用户名", type="text", required=True),
            LoginField(name="password", label="密码", type="password", required=True),
        ],
    )

    listed = await client.get(f"{API}/login/requests")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    entry = payload["requests"][0]
    assert entry["req_id"] == request.req_id
    assert entry["task_id"] == "task-1"
    assert entry["title"] == "B520 登录"
    assert [f["name"] for f in entry["fields"]] == ["username", "password"]
    assert entry["done"] is False
    assert entry["prefill"] == {}

    submitted = await client.post(
        f"{API}/login/requests/{request.req_id}",
        json={"values": {"username": "alice", "password": "secret"}},
    )
    assert submitted.status_code == 200
    assert submitted.json() == {"ok": True}
    # The waiting task receives the values through the broker.
    assert request.result == {"username": "alice", "password": "secret"}

    after = await client.get(f"{API}/login/requests")
    assert after.status_code == 200
    assert after.json() == {"total": 0, "requests": []}

    # Done-but-not-reaped requests conflict; unknown ids are not found.
    resubmit = await client.post(
        f"{API}/login/requests/{request.req_id}", json={"values": {}}
    )
    assert resubmit.status_code == 409
    unknown = await client.post(
        f"{API}/login/requests/does-not-exist", json={"values": {}}
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_login_requests_stream_emits_requests_frames(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_stream(interval: float) -> Any:
        yield api_routes._sse_frame("requests", [{"req_id": "r1"}])

    monkeypatch.setattr(api_routes, "_login_request_event_stream", fake_stream)

    async with client.stream("GET", f"{API}/login/requests/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [name for name, _ in events] == ["requests"]
    assert events[0][1] == [{"req_id": "r1"}]


# ---------------------------------------------------------------------------
# Watchlist (追更)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_add_list_remove_lifecycle(
    client: httpx.AsyncClient,
) -> None:
    added = await client.post(
        f"{API}/watchlist",
        json={"site": "b520", "book_id": "8_8187", "title": "斗破苍穹"},
    )

    assert added.status_code == 200
    payload = added.json()
    assert payload["created"] is True
    entry = payload["entry"]
    assert entry["site"] == "b520"
    assert entry["book_id"] == "8_8187"
    assert entry["title"] == "斗破苍穹"
    assert entry["last_chapter"] == ""
    assert entry["updated_at"]

    listed = await client.get(f"{API}/watchlist")
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert listed_payload["total"] == 1
    assert listed_payload["books"][0]["book_id"] == "8_8187"

    # Re-adding refreshes the entry instead of duplicating it.
    refreshed = await client.post(
        f"{API}/watchlist", json={"site": "b520", "book_id": "8_8187"}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["created"] is False
    assert refreshed.json()["entry"]["title"] == "斗破苍穹"
    assert (await client.get(f"{API}/watchlist")).json()["total"] == 1

    removed = await client.delete(f"{API}/watchlist/b520/8_8187")
    assert removed.status_code == 200
    assert removed.json() == {"ok": True}

    assert (await client.get(f"{API}/watchlist")).json()["total"] == 0

    # Removing a book that is no longer followed is a 404.
    again = await client.delete(f"{API}/watchlist/b520/8_8187")
    assert again.status_code == 404
    assert again.json()["detail"] == "Book not in watchlist: b520/8_8187"


@pytest.mark.asyncio
async def test_watchlist_check_reports_new_chapter(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added = await client.post(
        f"{API}/watchlist",
        json={"site": "b520", "book_id": "8_8187", "title": "斗破苍穹"},
    )
    assert added.status_code == 200

    async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "site": "b520",
                "book_id": "8_8187",
                "title": "斗破苍穹",
                "latest_chapter": "第1601章 大结局",
            }
        ]

    monkeypatch.setattr(api_routes.search_module, "search", fake_search)

    response = await client.post(f"{API}/watchlist/check", json={"download": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["checked"] == 1
    assert payload["updated"] == 1
    assert payload["failed"] == 0
    assert payload["download"] is True
    item = payload["items"][0]
    assert item["found"] is True
    assert item["changed"] is True
    assert item["needs_download"] is True
    assert item["previous_chapter"] == ""
    assert item["last_chapter"] == "第1601章 大结局"
    assert item["task"] == {
        "site": "b520",
        "book_id": "8_8187",
        "title": "斗破苍穹",
    }
    assert len(payload["needs_download"]) == 1

    # The chapter was persisted, so an immediate second check reports no change.
    second = await client.post(f"{API}/watchlist/check", json={})
    assert second.status_code == 200
    assert second.json()["updated"] == 0
    assert second.json()["items"][0]["changed"] is False


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
    old = output_dir / "旧书.epub"
    new = output_dir / "new_book.txt"
    old.write_text("x", encoding="utf-8")
    new.write_text("hello", encoding="utf-8")
    (output_dir / "notes.pdf").write_text("ignored", encoding="utf-8")
    os.utime(old, (100.0, 100.0))
    os.utime(new, (200.0, 200.0))

    monkeypatch.setattr(history_files, "_output_dir", lambda: output_dir)

    response = await client.get(f"{API}/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 0
    assert payload["limit"] == 2
    # Newest first, non-txt/epub files excluded.
    assert [item["name"] for item in payload["files"]] == ["new_book.txt", "旧书.epub"]
    first = payload["files"][0]
    assert first["ext"] == "txt"
    assert first["size"] == 5
    assert first["mtime"] == 200.0
    assert first["download_url"] == "/downloads/new_book.txt"
    # Non-ASCII names are percent-encoded into a directly usable URL.
    assert payload["files"][1]["download_url"] == ("/downloads/%E6%97%A7%E4%B9%A6.epub")


@pytest.mark.asyncio
async def test_history_empty_directory(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "empty-exports"
    output_dir.mkdir()
    monkeypatch.setattr(history_files, "_output_dir", lambda: output_dir)

    response = await client.get(f"{API}/history")

    assert response.status_code == 200
    assert response.json() == {"total": 0, "offset": 0, "limit": 0, "files": []}


# ---------------------------------------------------------------------------
# Music: platforms / search / download / jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_music_extras
async def test_music_platforms_lists_registered_platforms(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{API}/music/platforms")

    assert response.status_code == 200
    platforms = response.json()["platforms"]
    assert platforms
    assert all(set(item) == {"key", "label", "note"} for item in platforms)
    assert "netease" in [item["key"] for item in platforms]
    netease = next(item for item in platforms if item["key"] == "netease")
    assert netease["label"]


@pytest.mark.asyncio
async def test_music_search_returns_tracks(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    tracks = [
        _FakeTrack(id="186016", name="晴天", artists="周杰伦", duration_s=269),
        _FakeTrack(id="186017", name="七里香", artists="周杰伦", duration_s=280),
    ]
    provider = _FakeMusicProvider(calls, (tracks, 2))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: provider),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "周杰伦", "limit": 2, "offset": 4},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [track["id"] for track in payload["tracks"]] == ["186016", "186017"]
    assert payload["tracks"][0]["name"] == "晴天"
    assert calls == [{"keyword": "周杰伦", "limit": 2, "offset": 4}]


@pytest.mark.asyncio
async def test_music_download_returns_ordered_results(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def fake_download_many(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        calls["out_dir"] = options.data["out_dir"]
        calls["level"] = options.data["level"]
        # Deliver out of order to prove the API restores track order.
        on_result(
            1,
            tracks[1],
            SimpleNamespace(status="skip", reason="vip", path="", bytes_=0),
        )
        on_result(
            0,
            tracks[0],
            SimpleNamespace(status="ok", reason="", path="music/a.mp3", bytes_=123),
        )
        return 1, 1, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(download_many=fake_download_many),
    )

    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": [{"id": "a"}, {"id": "b"}],
            "out_dir": str(tmp_path),
            "level": "exhigh",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": 1,
        "skip": 1,
        "fail": 0,
        "results": [
            {"status": "ok", "reason": "", "path": "music/a.mp3", "bytes": 123},
            {"status": "skip", "reason": "vip", "path": "", "bytes": 0},
        ],
    }
    assert calls["out_dir"] == str(tmp_path)
    assert calls["level"] == "exhigh"


@pytest.mark.asyncio
async def test_music_job_lifecycle(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_music_jobs: Any,
) -> None:
    def fake_download_many(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        for index, track in enumerate(tracks):
            on_result(
                index,
                track,
                SimpleNamespace(
                    status="ok",
                    reason="",
                    path=f"music/{track.id}.mp3",
                    bytes_=index + 1,
                ),
            )
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(download_many=fake_download_many),
    )

    created = await client.post(
        f"{API}/music/jobs",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}, {"id": "2"}],
            "out_dir": str(tmp_path),
        },
    )

    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert job_id

    payload = await _wait_for_music_job(client, job_id)

    assert payload["status"] == "done"
    assert payload["progress"] == {"done": 2, "total": 2}
    assert payload["error"] is None
    assert [item["path"] for item in payload["results"]] == [
        "music/1.mp3",
        "music/2.mp3",
    ]

    unknown = await client.get(f"{API}/music/jobs/nope")
    assert unknown.status_code == 404
    assert "detail" in unknown.json()


# ---------------------------------------------------------------------------
# Music: export and health (real offline components)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_music_extras
async def test_music_export_writes_m3u8_playlist(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    target = tmp_path / "favorites.m3u8"

    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "netease",
            "tracks": [
                {
                    "id": "186016",
                    "name": "晴天",
                    "artists": "周杰伦",
                    "duration_s": 269,
                    "path": str(tmp_path / "晴天.mp3"),
                },
                {"id": "186017", "name": "没有本地文件"},
            ],
            "out_path": str(target),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["path"] == str(target)
    assert payload["tracks_written"] == 1
    assert payload["skipped"] == 1

    content = target.read_text(encoding="utf-8")
    assert content.startswith("#EXTM3U")
    assert "晴天" in content
    assert "没有本地文件" not in content


@pytest.mark.asyncio
@requires_music_extras
async def test_music_export_unknown_platform_returns_400(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "definitely-not-a-platform",
            "tracks": [{"id": "1", "path": str(tmp_path / "a.mp3")}],
        },
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"] == "Unknown music platform: definitely-not-a-platform"
    )


@pytest.mark.asyncio
@requires_music_extras
async def test_music_health_probes_platform(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music.providers import health as music_health

    calls: list[dict[str, Any]] = []

    def fake_health_check(
        platform: str,
        keyword: str = "test",
        *,
        timeout: float = 15.0,
        limit: int = 1,
    ) -> dict[str, Any]:
        calls.append({"platform": platform, "timeout": timeout})
        return {"ok": True, "elapsed_ms": 7, "error": ""}

    monkeypatch.setattr(music_health, "health_check", fake_health_check)

    response = await client.get(
        f"{API}/music/health", params={"platform": "netease", "timeout": 2.5}
    )

    assert response.status_code == 200
    assert response.json() == [
        {"platform": "netease", "ok": True, "elapsed_ms": 7, "error": ""}
    ]
    assert calls == [{"platform": "netease", "timeout": 2.5}]


@pytest.mark.asyncio
@requires_music_extras
async def test_music_health_unknown_platform_returns_400(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{API}/music/health", params={"platform": "nope"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown music platform: nope"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_is_open_without_token(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_token_missing_header_returns_401(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get(f"{API}/health")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing API token"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_token_wrong_value_returns_401(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get(
        f"{API}/health", headers={"Authorization": "Bearer wrong-token"}
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_token_accepted_via_bearer_and_api_key(
    auth_client: httpx.AsyncClient,
) -> None:
    bearer = await auth_client.get(
        f"{API}/health", headers={"Authorization": "Bearer secret-token"}
    )
    assert bearer.status_code == 200

    api_key = await auth_client.get(
        f"{API}/health", headers={"X-API-Key": "secret-token"}
    )
    assert api_key.status_code == 200


# ---------------------------------------------------------------------------
# Error contract: 404 / 422 / 413
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_task_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/tasks/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Task not found: does-not-exist"


@pytest.mark.asyncio
async def test_unknown_route_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/definitely-not-a-route")

    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_search_without_keyword_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(f"{API}/search", json={})

    assert response.status_code == 422
    payload = response.json()
    assert "keyword" in payload["detail"]
    assert payload["errors"]


@pytest.mark.asyncio
async def test_invalid_json_body_returns_422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"{API}/tasks",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_task_create_requires_site_and_book_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(f"{API}/tasks", json={"title": "only a title"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_oversized_body_returns_413(
    tiny_body_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    response = await tiny_body_client.post(f"{API}/search", json={"keyword": "x" * 500})

    assert response.status_code == 413
    assert "100" in response.json()["detail"]

    # A body under the limit still goes through.
    ok = await tiny_body_client.post(f"{API}/search", json={"keyword": "ok"})
    assert ok.status_code == 200
    assert ok.json() == {"total": 0, "results": []}


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_page_reachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_openapi_json_lists_all_endpoints(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert document["info"]["version"] == __version__

    paths = set(document["paths"])
    missing = EXPECTED_OPENAPI_PATHS - paths
    assert not missing, f"endpoints missing from /openapi.json: {sorted(missing)}"

    # Spot-check the method map of the multi-method paths.
    assert set(document["paths"]["/api/v1/tasks"]) == {"get", "post"}
    assert set(document["paths"]["/api/v1/watchlist"]) == {"get", "post"}
    assert set(document["paths"]["/api/v1/watchlist/{site}/{book_id}"]) == {"delete"}
    assert set(document["paths"]["/api/v1/login/requests/{req_id}"]) == {"post"}
