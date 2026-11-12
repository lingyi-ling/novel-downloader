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
import sys
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
    sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class _FakeMusicProvider:
    """Provider double whose ``search`` returns a canned (tracks, total)."""

    #: Same opt-in attribute the real providers expose: ``search(offset)``
    #: actually skips hits.  Off by default, like the musicdl backend.
    supports_offset = False

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
        "download_mixed": lambda *args, **kwargs: (0, 0, 0, [], []),
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
async def test_music_search_hides_provider_private_data(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extra`` (provider raw payloads/cookies) must never reach the client."""
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS

    calls: list[dict[str, Any]] = []
    track = _FakeTrack(
        provider="netease",
        id="186016",
        name="晴天",
        artists="周杰伦",
        duration_s=269,
        sources=["netease", "qq"],
        extra={
            "raw": {"cookie": "MUSIC_U=secret"},
            "songinfo": object(),
        },
    )
    # Process-local download fallback list: not a dataclass field, never serialised.
    track.__dict__["_alternatives"] = [_FakeTrack(provider="qq", id="1")]
    provider = _FakeMusicProvider(calls, ([track], 1))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: provider),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["search_id"]
    assert len(payload["tracks"]) == 1
    public = payload["tracks"][0]
    assert "extra" not in public
    assert "_alternatives" not in public
    assert tuple(public) == PUBLIC_TRACK_FIELDS
    assert public["provider"] == "netease"
    assert public["id"] == "186016"
    assert public["name"] == "晴天"
    assert public["artists"] == "周杰伦"
    assert public["duration_s"] == 269
    assert public["sources"] == ["netease", "qq"]


@pytest.mark.asyncio
async def test_music_download_restores_tracks_from_search_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reduced (public) rows plus ``search_id`` still reach the engine intact."""
    calls: list[dict[str, Any]] = []
    original = _FakeTrack(
        provider="netease",
        id="186016",
        name="晴天",
        artists="周杰伦",
        extra={"raw": {"cookie": "MUSIC_U=secret"}},
    )
    provider = _FakeMusicProvider(calls, ([original], 1))
    captured: dict[str, Any] = {}

    def fake_download_mixed(
        create_provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        captured["tracks"] = tracks
        return 0, 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            create=lambda platform: provider, download_mixed=fake_download_mixed
        ),
    )

    searched = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": 1},
    )
    payload = searched.json()
    search_id = payload["search_id"]
    submitted = payload["tracks"]  # exactly what a client echoes back

    response = await client.post(
        f"{API}/music/download",
        json={"platform": "netease", "tracks": submitted, "search_id": search_id},
    )

    assert response.status_code == 200
    assert captured["tracks"] == [original]
    assert captured["tracks"][0] is original
    assert captured["tracks"][0].extra == {"raw": {"cookie": "MUSIC_U=secret"}}


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
        lambda: _fake_music_components(
            download_mixed=fake_download_many,
            load_settings=lambda: {"out_dir": str(tmp_path)},
        ),
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
            {
                "status": "ok",
                "reason": "",
                "path": "music/a.mp3",
                "download_url": "",
                "bytes": 123,
            },
            {
                "status": "skip",
                "reason": "vip",
                "path": "",
                "download_url": "",
                "bytes": 0,
            },
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
        lambda: _fake_music_components(
            download_mixed=fake_download_many,
            load_settings=lambda: {"out_dir": str(tmp_path)},
        ),
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import settings as music_settings

    # Exports are confined to the configured music directory.
    monkeypatch.setattr(
        music_settings, "load_settings", lambda: {"out_dir": str(tmp_path)}
    )
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
    from novel_downloader.apps.music.providers import available_labels

    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "definitely-not-a-platform",
            "tracks": [{"id": "1", "path": str(tmp_path / "a.mp3")}],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Unknown music platform: definitely-not-a-platform. "
        f"Available: {', '.join(key for key, _label in available_labels())}"
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
    from novel_downloader.apps.music.providers import available_labels

    response = await client.get(f"{API}/music/health", params={"platform": "nope"})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Unknown music platform: nope. "
        f"Available: {', '.join(key for key, _label in available_labels())}"
    )


# ---------------------------------------------------------------------------
# Music: field-level response contract
#
# These pin the wire shape (key set + JSON type of every field) of each music
# endpoint's success and error response. They intentionally assert *exact*
# key sets: an accidental extra key (e.g. provider-private data) and a
# silently missing one are both contract drift, and the Android client decodes
# these payloads without a schema of its own. The embedded engine serves the
# same contract; test_embedded_music.py mirrors these assertions.
# ---------------------------------------------------------------------------


#: String-typed fields of a public track payload.
_TRACK_STR_FIELDS = (
    "provider",
    "id",
    "name",
    "artists",
    "album",
    "cover_url",
    "ext",
    "download_url",
    "path",
    "lyric",
)
#: Integer-typed fields of a public track payload.
_TRACK_INT_FIELDS = ("duration_s", "track_no")


def _assert_public_track(track: dict[str, Any]) -> None:
    """A search row: exact public field set and JSON types."""
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS

    assert tuple(track) == PUBLIC_TRACK_FIELDS
    for name in _TRACK_STR_FIELDS:
        assert isinstance(track[name], str), (name, track[name])
    for name in _TRACK_INT_FIELDS:
        assert type(track[name]) is int, (name, track[name])
    assert type(track["vip"]) is bool
    assert isinstance(track["http_headers"], dict)
    assert all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in track["http_headers"].items()
    )
    assert isinstance(track["sources"], list)
    assert all(isinstance(source, str) for source in track["sources"])


def _assert_validation_body(payload: Any) -> None:
    """A 422 body: readable ``detail`` plus machine-readable ``errors``."""
    assert isinstance(payload, dict)
    assert set(payload) == {"detail", "errors"}
    assert isinstance(payload["detail"], str) and payload["detail"]
    assert isinstance(payload["errors"], list) and payload["errors"]
    for error in payload["errors"]:
        assert set(error) == {"type", "loc", "msg"}
        assert all(isinstance(part, str) for part in error["loc"])


def _assert_error_body(payload: Any) -> None:
    """A non-422 error body: exactly ``{"detail": str}``."""
    assert isinstance(payload, dict)
    assert set(payload) == {"detail"}
    assert isinstance(payload["detail"], str) and payload["detail"]


def test_music_public_track_fields_match_the_request_model() -> None:
    """``MusicTrack`` may additionally accept ``extra``; responses never echo it."""
    from novel_downloader.apps.api.schemas import MusicTrack
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS

    assert tuple(MusicTrack.model_fields) == (*PUBLIC_TRACK_FIELDS, "extra")


@pytest.mark.asyncio
async def test_music_platforms_response_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: _fake_music_components()
    )

    response = await client.get(f"{API}/music/platforms")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"platforms"}
    assert isinstance(payload["platforms"], list) and payload["platforms"]
    for item in payload["platforms"]:
        assert set(item) == {"key", "label", "note"}
        assert all(isinstance(item[key], str) for key in ("key", "label", "note"))


@pytest.mark.asyncio
async def test_music_search_single_platform_response_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    track = _FakeTrack(
        provider="netease",
        id="1",
        name="晴天",
        artists="周杰伦",
        album="叶惠美",
        duration_s=269,
        cover_url="https://img.example/1.jpg",
        track_no=3,
        ext="mp3",
        vip=True,
        download_url="https://cdn.example/1.mp3",
        http_headers={"Referer": "https://example/"},
        path="music/1.mp3",
        lyric="[00:00]",
        sources=["netease", "qq"],
    )
    provider = _FakeMusicProvider(calls, ([track], 7))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: provider),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "total",
        "tracks",
        "search_id",
        "platform",
        "offset",
        "supports_offset",
    }
    assert type(payload["total"]) is int and payload["total"] == 7
    assert isinstance(payload["search_id"], str) and payload["search_id"]
    assert payload["platform"] == "netease"
    assert type(payload["offset"]) is int and payload["offset"] == 0
    assert payload["supports_offset"] is False
    assert isinstance(payload["tracks"], list) and len(payload["tracks"]) == 1
    _assert_public_track(payload["tracks"][0])


@pytest.mark.asyncio
async def test_music_search_aggregate_response_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music.aggregate import (
        ALL_PLATFORM_KEY,
        AggregateResult,
        PlatformOutcome,
    )

    merged = AggregateResult(
        tracks=[
            _FakeTrack(
                provider="netease",
                id="1",
                name="晴天",
                artists="周杰伦",
                sources=["netease", "qq"],
            )
        ],
        platforms=[
            PlatformOutcome("netease", True, count=1),
            PlatformOutcome("qq", False, error="搜索超时"),
        ],
        total=3,
    )
    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: _fake_music_components()
    )
    monkeypatch.setattr(
        "novel_downloader.apps.music.aggregate.search_all",
        lambda *args, **kwargs: merged,
    )

    response = await client.post(
        f"{API}/music/search", json={"platform": "all", "keyword": "晴天"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "total",
        "tracks",
        "search_id",
        "platforms",
        "platform",
        "offset",
        "supports_offset",
    }
    assert type(payload["total"]) is int and payload["total"] == 3
    assert payload["platform"] == ALL_PLATFORM_KEY
    assert payload["offset"] == 0
    assert payload["supports_offset"] is False
    assert isinstance(payload["search_id"], str) and payload["search_id"]
    assert isinstance(payload["platforms"], list) and len(payload["platforms"]) == 2
    for item in payload["platforms"]:
        assert set(item) == {"platform", "ok", "count", "error"}
        assert isinstance(item["platform"], str)
        assert type(item["ok"]) is bool
        assert type(item["count"]) is int
        assert isinstance(item["error"], str)
    _assert_public_track(payload["tracks"][0])
    assert payload["tracks"][0]["sources"] == ["netease", "qq"]


@pytest.mark.asyncio
async def test_music_download_response_contract_and_reason_bounds(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-track results carry a bounded, query-free, single-line reason."""
    reason = (
        "provider said no\n"
        "while fetching https://cdn.example/a.mp3?token=SECRET#frag " + "x" * 400
    )

    def fake_download(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        on_result(
            0,
            tracks[0],
            SimpleNamespace(status="fail", reason=reason, path="", bytes_=0),
        )
        on_result(
            1,
            tracks[1],
            SimpleNamespace(status="ok", reason="", path="music/2.mp3", bytes_=321),
        )
        return 1, 0, 1, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            download_mixed=fake_download,
            load_settings=lambda: {"out_dir": str(tmp_path)},
        ),
    )

    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}, {"id": "2"}],
            "out_dir": str(tmp_path),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"ok", "skip", "fail", "results"}
    assert all(type(payload[key]) is int for key in ("ok", "skip", "fail"))
    assert (payload["ok"], payload["skip"], payload["fail"]) == (1, 0, 1)
    assert isinstance(payload["results"], list) and len(payload["results"]) == 2
    for item in payload["results"]:
        assert set(item) == {"status", "reason", "path", "download_url", "bytes"}
        assert isinstance(item["status"], str)
        assert isinstance(item["reason"], str)
        assert isinstance(item["path"], str)
        # REST writes on the server machine: no loopback download route.
        assert item["download_url"] == ""
        assert type(item["bytes"]) is int

    first = payload["results"][0]
    assert len(first["reason"]) <= 200
    assert "SECRET" not in first["reason"]
    assert "#frag" not in first["reason"]
    assert "\n" not in first["reason"]
    assert first["reason"].startswith("provider said no while fetching")
    assert payload["results"][1]["bytes"] == 321


@pytest.mark.asyncio
async def test_music_job_response_contract(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_music_jobs: Any,
) -> None:
    def fake_download(
        provider: Any,
        tracks: list[Any],
        options: Any,
        *,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        on_result(
            0,
            tracks[0],
            SimpleNamespace(status="ok", reason="", path="music/1.mp3", bytes_=42),
        )
        return 1, 0, 0, [], []

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            download_mixed=fake_download,
            load_settings=lambda: {"out_dir": str(tmp_path)},
        ),
    )

    created = await client.post(
        f"{API}/music/jobs",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "out_dir": str(tmp_path),
        },
    )

    assert created.status_code == 202
    created_body = created.json()
    assert set(created_body) == {"job_id"}
    assert isinstance(created_body["job_id"], str) and created_body["job_id"]

    payload = await _wait_for_music_job(client, created_body["job_id"])

    assert set(payload) == {
        "job_id",
        "status",
        "progress",
        "results",
        "error",
        "created_at",
        "finished_at",
    }
    assert payload["job_id"] == created_body["job_id"]
    assert isinstance(payload["status"], str)
    assert set(payload["progress"]) == {"done", "total"}
    assert all(type(payload["progress"][key]) is int for key in ("done", "total"))
    assert isinstance(payload["results"], list) and len(payload["results"]) == 1
    assert set(payload["results"][0]) == {
        "status",
        "reason",
        "path",
        "download_url",
        "bytes",
    }
    assert payload["error"] is None or isinstance(payload["error"], str)
    assert type(payload["created_at"]) is float
    assert payload["finished_at"] is None or type(payload["finished_at"]) is float


@pytest.mark.asyncio
async def test_music_export_response_contract(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            load_settings=lambda: {"out_dir": str(tmp_path)}
        ),
    )

    response = await client.post(
        f"{API}/music/export",
        json={
            "platform": "netease",
            "tracks": [
                {"id": "1", "path": str(tmp_path / "a.mp3")},
                {"id": "2"},
            ],
            "out_path": "favorites.m3u8",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    # Both spellings of the written count are present on both transports.
    assert set(payload) == {"path", "tracks_written", "skipped", "written"}
    assert isinstance(payload["path"], str) and payload["path"].endswith(".m3u8")
    assert type(payload["tracks_written"]) is int
    assert type(payload["written"]) is int
    assert type(payload["skipped"]) is int
    assert payload["tracks_written"] == payload["written"] == 1
    assert payload["skipped"] == 1


@pytest.mark.asyncio
async def test_music_health_response_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            health_check=lambda platform, *args, **kwargs: {
                "ok": True,
                "elapsed_ms": 3,
                "error": "",
            }
        ),
    )

    response = await client.get(f"{API}/music/health", params={"platform": "netease"})

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list) and len(payload) == 1
    item = payload[0]
    assert set(item) == {"platform", "ok", "elapsed_ms", "error"}
    assert item["platform"] == "netease"
    assert type(item["ok"]) is bool
    assert type(item["elapsed_ms"]) is int
    assert isinstance(item["error"], str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("POST", f"{API}/music/search", {"platform": "netease"}, 422),
        ("POST", f"{API}/music/search", {"platform": None, "keyword": "x"}, 422),
        ("POST", f"{API}/music/search", {"platform": "", "keyword": "x"}, 422),
        ("POST", f"{API}/music/search", {"platform": "nope", "keyword": "x"}, 400),
        (
            "POST",
            f"{API}/music/download",
            {"platform": "nope", "tracks": [{"id": "1"}]},
            400,
        ),
        ("POST", f"{API}/music/download", {"tracks": []}, 422),
        (
            "POST",
            f"{API}/music/download",
            {"tracks": [{"id": str(index)} for index in range(201)]},
            422,
        ),
        ("POST", f"{API}/music/export", {"tracks": []}, 422),
        ("POST", f"{API}/music/jobs", {"tracks": []}, 422),
        ("GET", f"{API}/music/health?platform=nope", None, 400),
        ("GET", f"{API}/music/health?timeout=0", None, 422),
        ("GET", f"{API}/music/health?timeout=61", None, 422),
        ("GET", f"{API}/music/health?timeout=abc", None, 422),
        ("GET", f"{API}/music/jobs/missing", None, 404),
    ],
)
async def test_music_error_response_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: Any,
    expected: int,
) -> None:
    """Every music error is ``{"detail"}`` (422 adds ``{"errors"}``)."""

    def _create(platform: str) -> Any:
        # Only the registered key resolves; unknown platforms behave like the
        # real registry (KeyError → 400), not like an untyped fake object.
        if platform != "netease":
            raise KeyError(platform)
        return object()

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=_create),
    )

    response = await client.request(method, path, json=body)

    assert response.status_code == expected, response.text
    if expected == 422:
        _assert_validation_body(response.json())
    else:
        _assert_error_body(response.json())


# ---------------------------------------------------------------------------
# Music search bounds and paging contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 20, 100])
async def test_music_search_accepts_limit_within_bounds(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    """``1..100`` reaches the provider unchanged."""
    calls: list[dict[str, Any]] = []
    provider = _FakeMusicProvider(calls, ([], 0))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: provider),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": limit},
    )

    assert response.status_code == 200, response.text
    assert calls == [{"keyword": "晴天", "limit": limit, "offset": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 101, 10_000])
async def test_music_search_rejects_limit_outside_bounds(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    """An out-of-range limit is a 422 and never reaches a provider."""
    calls: list[dict[str, Any]] = []
    provider = _FakeMusicProvider(calls, ([], 0))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: provider),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": limit},
    )

    assert response.status_code == 422, response.text
    _assert_validation_body(response.json())
    assert calls == []


@pytest.mark.asyncio
async def test_music_search_aggregate_caps_per_platform_fanout(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal per-platform limit stays 8..15 whatever the client asks."""
    from novel_downloader.apps.music.aggregate import AggregateResult

    seen: list[int] = []

    def fake_search_all(*args: Any, **kwargs: Any) -> AggregateResult:
        seen.append(kwargs["per_platform_limit"])
        return AggregateResult(tracks=[], platforms=[], total=0)

    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: _fake_music_components()
    )
    monkeypatch.setattr(
        "novel_downloader.apps.music.aggregate.search_all", fake_search_all
    )

    for limit, expected in ((1, 8), (8, 8), (20, 15), (100, 15)):
        response = await client.post(
            f"{API}/music/search",
            json={"platform": "all", "keyword": "晴天", "limit": limit},
        )
        assert response.status_code == 200, response.text
        assert seen[-1] == expected, (limit, expected, seen)


@pytest.mark.asyncio
async def test_music_search_reports_paging_support(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``offset`` is echoed and ``supports_offset`` mirrors the provider."""
    calls: list[dict[str, Any]] = []
    paged = _FakeMusicProvider(calls, ([], 0))
    paged.supports_offset = True
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: paged),
    )

    response = await client.post(
        f"{API}/music/search",
        json={"platform": "netease", "keyword": "晴天", "limit": 10, "offset": 30},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["offset"] == 30
    assert payload["supports_offset"] is True
    assert calls == [{"keyword": "晴天", "limit": 10, "offset": 30}]

    # A musicdl-backed provider answers an empty page for any offset; the
    # response must say so instead of looking like a normal end-of-list.
    unpaged = _FakeMusicProvider([], ([], 0))
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(create=lambda platform: unpaged),
    )
    response = await client.post(
        f"{API}/music/search",
        json={"platform": "qq", "keyword": "晴天", "limit": 10, "offset": 30},
    )
    payload = response.json()
    assert payload["offset"] == 30
    assert payload["supports_offset"] is False


# ---------------------------------------------------------------------------
# Music id shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_job_id_shape_is_validated(
    client: httpx.AsyncClient,
) -> None:
    """Malformed job ids are 422; unknown well-formed ids stay 404."""

    malformed = "z" * 65
    response = await client.get(f"{API}/music/jobs/{malformed}")
    assert response.status_code == 422, response.text
    _assert_validation_body(response.json())
    assert response.json()["errors"][0]["loc"] == ["path", "job_id"]

    # A decoded control character is malformed too (never a log-injection
    # channel into the 404 detail).
    newline = await client.get(f"{API}/music/jobs/{'a' * 32}%0A")
    assert newline.status_code == 422, newline.text

    unknown = await client.get(f"{API}/music/jobs/{'a' * 32}")
    assert unknown.status_code == 404
    _assert_error_body(unknown.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [f"{API}/music/download", f"{API}/music/jobs"])
@pytest.mark.parametrize("search_id", ["x" * 65, "bad/../id", "has space"])
async def test_music_search_id_shape_is_validated(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    search_id: str,
) -> None:
    """A malformed ``search_id`` is a 422 before the cache lookup."""
    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: _fake_music_components()
    )

    response = await client.post(
        path,
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "search_id": search_id,
        },
    )

    assert response.status_code == 422, response.text
    _assert_validation_body(response.json())
    assert response.json()["errors"][0]["loc"] == ["body", "search_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [f"{API}/music/download", f"{API}/music/jobs"])
@pytest.mark.parametrize("search_id", [None, "", "   "])
async def test_blank_search_id_still_means_absent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    search_id: Any,
) -> None:
    """Blank is the historical "no id" spelling and keeps working."""
    monkeypatch.setattr(
        api_routes, "_load_music_components", lambda: _fake_music_components()
    )

    response = await client.post(
        path,
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "search_id": search_id,
        },
    )

    assert response.status_code in (200, 202), response.text


# ---------------------------------------------------------------------------
# Music export: duplicate / blank metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_export_dedupes_files_and_fills_blank_metadata(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One playlist entry per file; blank title/artist get placeholders."""
    from novel_downloader.apps.music.downloader import export_m3u

    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            export_m3u=export_m3u,
            load_settings=lambda: {"out_dir": str(tmp_path)},
        ),
    )

    response = await client.post(
        f"{API}/music/export",
        json={
            "tracks": [
                # Blank metadata on the first copy: filled with placeholders.
                {"id": "1", "name": "", "artists": "", "path": "songs/a.mp3"},
                # Same file through a "./" prefix: a duplicate, not a song.
                {"id": "2", "name": "A", "artists": "B", "path": "./songs/a.mp3"},
                {"id": "3", "name": "C", "artists": "D", "path": "songs/b.mp3"},
                {"id": "4", "name": "NoPath"},
            ],
            "out_path": "playlist.m3u8",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # 3 tracks had a path but two pointed at the same file; 1 had none.
    assert payload["tracks_written"] == 2
    assert payload["written"] == 2
    assert payload["skipped"] == 2

    content = (tmp_path / "playlist.m3u8").read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "#EXTM3U"
    assert len(lines) == 5  # header + two (EXTINF, location) pairs
    assert "未知歌手 - 未知歌曲" in content
    # The kept KEEP entry is the first occurrence, blank metadata and all.
    assert any(line.endswith("songs/a.mp3") for line in lines)
    assert any(line.endswith("songs/b.mp3") for line in lines)
    assert content.count("a.mp3") == 1


@pytest.mark.asyncio
async def test_music_out_dir_escape_is_a_400_without_echo(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path.parent / "elsewhere"
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            load_settings=lambda: {"out_dir": str(tmp_path)}
        ),
    )

    response = await client.post(
        f"{API}/music/download",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "out_dir": str(outside),
        },
    )

    assert response.status_code == 400
    payload = response.json()
    _assert_error_body(payload)
    assert "elsewhere" not in payload["detail"]
    assert str(tmp_path) not in payload["detail"]


@pytest.mark.asyncio
async def test_music_jobs_busy_is_a_429(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_music_jobs: Any,
) -> None:
    monkeypatch.setattr(api_routes, "MAX_ACTIVE_MUSIC_JOBS", 0)
    monkeypatch.setattr(
        api_routes,
        "_load_music_components",
        lambda: _fake_music_components(
            load_settings=lambda: {"out_dir": str(tmp_path)}
        ),
    )

    response = await client.post(
        f"{API}/music/jobs",
        json={
            "platform": "netease",
            "tracks": [{"id": "1"}],
            "out_dir": str(tmp_path),
        },
    )

    assert response.status_code == 429
    _assert_error_body(response.json())


@pytest.mark.asyncio
async def test_music_missing_extras_is_a_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All seven music routes answer the same 503 shape when extras are absent."""
    monkeypatch.setitem(sys.modules, "mutagen", None)

    calls: list[tuple[str, str, Any]] = [
        ("GET", f"{API}/music/platforms", None),
        ("POST", f"{API}/music/search", {"platform": "netease", "keyword": "x"}),
        (
            "POST",
            f"{API}/music/download",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            f"{API}/music/jobs",
            {"platform": "netease", "tracks": [{"id": "1"}]},
        ),
        (
            "POST",
            f"{API}/music/export",
            {"platform": "netease", "tracks": [{"id": "1", "path": "a.mp3"}]},
        ),
        ("GET", f"{API}/music/health", None),
    ]
    for method, path, body in calls:
        response = await client.request(method, path, json=body)
        assert response.status_code == 503, (method, path, response.text)
        _assert_error_body(response.json())


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
    assert response.json()["detail"] == (
        "Invalid or missing API token. "
        "Send 'Authorization: Bearer <token>' or 'X-API-Key: <token>'."
    )
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
