#!/usr/bin/env python3
"""
tests.apps.test_api_client
--------------------------

Unit tests for :mod:`novel_downloader.apps.api.client`.

Most requests go through ``httpx.MockTransport``: no server is started and no
network access happens. The end-to-end section bridges the same client into
the real ``create_app()`` ASGI application to pin the SDK / API contract.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.types import ASGIApp

from novel_downloader import __version__
from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.client import ApiError, NovelApiClient
from novel_downloader.apps.api.constants import MSG_TASK_NOT_FOUND
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Mock transport handler that records every request it receives."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def json_body(self, index: int = 0) -> dict[str, Any]:
        body = json.loads(self.requests[index].content)
        assert isinstance(body, dict)
        return body


def make_client(handler: Handler, **kwargs: Any) -> NovelApiClient:
    return NovelApiClient(
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


def test_health() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, json={"status": "ok", "version": "1.0"})
    )

    with make_client(recorder) as client:
        assert client.health() == {"status": "ok", "version": "1.0"}

    request = recorder.requests[0]
    assert request.method == "GET"
    assert str(request.url) == "http://127.0.0.1:8081/api/v1/health"


def test_sites() -> None:
    payload = {"search": {"qidian": "起点"}, "download": {"qidian": "起点"}}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.sites() == payload

    assert recorder.requests[0].url.path == "/api/v1/sites"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_sends_all_arguments() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, json={"total": 0, "results": []})
    )

    with make_client(recorder) as client:
        result = client.search(
            "斗破",
            sites=["qidian", "demo"],
            per_site_limit=3,
            timeout=2.5,
            sort="words",
            order="asc",
            limit=10,
        )

    assert result == {"total": 0, "results": []}
    assert recorder.requests[0].method == "POST"
    assert recorder.requests[0].url.path == "/api/v1/search"
    assert recorder.json_body() == {
        "keyword": "斗破",
        "sites": ["qidian", "demo"],
        "per_site_limit": 3,
        "timeout": 2.5,
        "sort": "words",
        "order": "asc",
        "limit": 10,
    }


def test_search_omits_optional_arguments() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, json={"total": 0, "results": []})
    )

    with make_client(recorder) as client:
        client.search("book")

    assert recorder.json_body() == {
        "keyword": "book",
        "per_site_limit": 5,
        "timeout": 5.0,
        "sort": "quality",
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_add_task() -> None:
    created = {"task_id": "t1", "site": "demo", "book_id": "9", "status": "queued"}
    recorder = Recorder(lambda _request: httpx.Response(200, json=created))

    with make_client(recorder) as client:
        assert client.add_task("demo", "9") == created
        assert client.add_task("demo", "9", title="My Book") == created

    assert recorder.requests[0].url.path == "/api/v1/tasks"
    assert recorder.json_body(0) == {"site": "demo", "book_id": "9"}
    assert recorder.json_body(1) == {
        "site": "demo",
        "book_id": "9",
        "title": "My Book",
    }


def test_tasks() -> None:
    payload = {"pending": [{"task_id": "t1"}], "running": [], "completed": []}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.tasks() == payload

    assert recorder.requests[0].url.path == "/api/v1/tasks"


def test_tasks_page_sends_pagination_params() -> None:
    payload = {
        "pending": [],
        "running": [],
        "completed": [],
        "total": 12,
        "offset": 3,
        "limit": 7,
    }
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.tasks_page(offset=3, limit=7) == payload

    request = recorder.requests[0]
    assert request.url.path == "/api/v1/tasks"
    assert dict(request.url.params) == {"offset": "3", "limit": "7"}


def test_tasks_page_defaults() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={}))

    with make_client(recorder) as client:
        client.tasks_page()

    assert dict(recorder.requests[0].url.params) == {"offset": "0", "limit": "50"}


def test_iter_tasks_parses_sse_stream() -> None:
    body = (
        "event: snapshot\n"
        'data: {"pending":[],"running":[],"completed":[]}\n'
        "\n"
        ": keepalive\n"
        "\n"
        "event: snapshot\n"
        'data: {"pending":[{"task_id":"t1","title":"书"}],'
        '"running":[],"completed":[]}\n'
        "\n"
        "event: something-else\n"
        'data: {"ignored":true}\n'
        "\n"
    )
    recorder = Recorder(
        lambda _request: httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )
    )

    with make_client(recorder) as client:
        snapshots = list(client.iter_tasks())

    assert snapshots == [
        {"pending": [], "running": [], "completed": []},
        {
            "pending": [{"task_id": "t1", "title": "书"}],
            "running": [],
            "completed": [],
        },
    ]
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == "/api/v1/tasks/stream"


def test_iter_tasks_raises_api_error_for_error_response() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(401, json={"detail": "Invalid token"})
    )

    with make_client(recorder) as client, pytest.raises(ApiError) as info:
        list(client.iter_tasks())

    assert info.value.status_code == 401
    assert info.value.detail == "Invalid token"


def test_task_uses_dedicated_endpoint() -> None:
    payload = {"task_id": "t1", "status": "running"}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.task("t1") == payload

    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.path == "/api/v1/tasks/t1"


def test_task_falls_back_to_task_lists() -> None:
    wanted = {"task_id": "t2", "status": "completed"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(
                200,
                json={
                    "pending": [],
                    "running": [],
                    "completed": [wanted],
                },
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    recorder = Recorder(handler)

    with make_client(recorder) as client:
        assert client.task("t2") == wanted

    assert [request.url.path for request in recorder.requests] == [
        "/api/v1/tasks/t2",
        "/api/v1/tasks",
    ]


def test_task_not_found_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(
                200, json={"pending": [], "running": [], "completed": []}
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    with make_client(handler) as client, pytest.raises(ApiError) as info:
        client.task("missing")

    assert info.value.status_code == 404
    assert info.value.detail == MSG_TASK_NOT_FOUND.format(task_id="missing")


def test_cancel_task() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"ok": True}))

    with make_client(recorder) as client:
        assert client.cancel_task("t1") == {"ok": True}

    assert recorder.requests[0].method == "POST"
    assert recorder.requests[0].url.path == "/api/v1/tasks/t1/cancel"


# ---------------------------------------------------------------------------
# History / downloads
# ---------------------------------------------------------------------------


def test_history() -> None:
    payload = {
        "total": 1,
        "files": [
            {
                "name": "book.epub",
                "ext": "epub",
                "size": 3,
                "mtime": 1.0,
                "download_url": "/downloads/book.epub",
            }
        ],
    }
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.history() == payload

    assert recorder.requests[0].url.path == "/api/v1/history"


def test_history_page_sends_pagination_params() -> None:
    payload = {"total": 9, "offset": 2, "limit": 3, "files": []}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.history_page(offset=2, limit=3) == payload

    request = recorder.requests[0]
    assert request.url.path == "/api/v1/history"
    assert dict(request.url.params) == {"offset": "2", "limit": "3"}


def test_history_page_defaults() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={}))

    with make_client(recorder) as client:
        client.history_page()

    assert dict(recorder.requests[0].url.params) == {"offset": "0", "limit": "50"}


def test_download_file_writes_bytes_and_encodes_name(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, content=b"novel-bytes"))

    with make_client(recorder) as client:
        target = client.download_file("空 格.epub", tmp_path)

    assert target == tmp_path / "空 格.epub"
    assert target.read_bytes() == b"novel-bytes"
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.raw_path == (
        b"/downloads/%E7%A9%BA%20%E6%A0%BC.epub"
    )


def test_download_file_uses_only_the_basename(tmp_path: Path) -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, content=b"x"))

    with make_client(recorder) as client:
        target = client.download_file("../../evil.txt", tmp_path)

    assert target == tmp_path / "evil.txt"
    assert recorder.requests[0].url.raw_path == b"/downloads/evil.txt"


def test_download_file_error_leaves_no_file(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(404, json={"detail": "missing"})
    )

    with make_client(recorder) as client, pytest.raises(ApiError) as info:
        client.download_file("gone.txt", tmp_path)

    assert info.value.status_code == 404
    assert info.value.detail == "missing"
    assert not (tmp_path / "gone.txt").exists()


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------


def test_music_platforms() -> None:
    payload = {"platforms": [{"key": "netease", "label": "网易云", "note": ""}]}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.music_platforms() == payload

    assert recorder.requests[0].url.path == "/api/v1/music/platforms"


def test_music_search() -> None:
    payload = {"total": 1, "tracks": [{"id": "1", "name": "Song"}]}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))

    with make_client(recorder) as client:
        assert client.music_search("netease", "song", limit=5) == payload

    assert recorder.requests[0].url.path == "/api/v1/music/search"
    # ``offset`` is always sent explicitly (the server-side default is 0),
    # per test_api_r5.test_client_music_search_sends_offset.
    assert recorder.json_body() == {
        "platform": "netease",
        "keyword": "song",
        "limit": 5,
        "offset": 0,
    }


def test_music_download(tmp_path: Path) -> None:
    payload = {"ok": 1, "skip": 0, "fail": 0, "results": []}
    recorder = Recorder(lambda _request: httpx.Response(200, json=payload))
    tracks = [{"provider": "netease", "id": "1", "name": "Song"}]

    with make_client(recorder) as client:
        result = client.music_download(
            "netease", tracks, level="lossless", out_dir=tmp_path
        )

    assert result == payload
    assert recorder.requests[0].url.path == "/api/v1/music/download"
    assert recorder.json_body() == {
        "platform": "netease",
        "tracks": tracks,
        "level": "lossless",
        "out_dir": str(tmp_path),
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_bearer_token_header() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"status": "ok"}))

    with make_client(recorder, token="secret") as client:
        client.health()

    headers = recorder.requests[0].headers
    assert headers["authorization"] == "Bearer secret"
    assert "x-api-key" not in headers


def test_api_key_header() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"status": "ok"}))

    with make_client(recorder, api_key="secret") as client:
        client.health()

    headers = recorder.requests[0].headers
    assert headers["x-api-key"] == "secret"
    assert "authorization" not in headers


def test_token_and_api_key_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        NovelApiClient(token="a", api_key="b")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_non_2xx_raises_api_error_with_payload() -> None:
    payload = {"detail": [{"msg": "bad keyword", "type": "value_error"}]}
    recorder = Recorder(lambda _request: httpx.Response(422, json=payload))

    with make_client(recorder) as client, pytest.raises(ApiError) as info:
        client.search("")

    error = info.value
    assert error.status_code == 422
    assert error.detail == payload["detail"]
    assert error.payload == payload
    assert str(error).startswith("HTTP 422:")


def test_non_json_error_body_is_preserved() -> None:
    recorder = Recorder(lambda _request: httpx.Response(500, text="boom"))

    with make_client(recorder) as client, pytest.raises(ApiError) as info:
        client.health()

    assert info.value.status_code == 500
    assert info.value.detail == "boom"
    assert info.value.payload == "boom"


def test_transport_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with make_client(handler) as client, pytest.raises(ApiError) as info:
        client.health()

    assert info.value.status_code == 0
    assert "ConnectError" in str(info.value)
    assert info.value.payload is None


def test_context_manager_closes_the_client() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"status": "ok"}))

    with make_client(recorder) as client:
        assert client.health() == {"status": "ok"}

    with pytest.raises(RuntimeError):
        client.health()


# ---------------------------------------------------------------------------
# Base URL handling
# ---------------------------------------------------------------------------


def test_base_url_with_api_prefix(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/downloads/"):
            return httpx.Response(200, content=b"data")
        return httpx.Response(200, json={"status": "ok"})

    recorder = Recorder(handler)

    with NovelApiClient(
        base_url="http://api.test/api/v1/",
        transport=httpx.MockTransport(recorder),
    ) as client:
        assert client.health() == {"status": "ok"}
        client.download_file("book.txt", tmp_path)

    assert str(recorder.requests[0].url) == "http://api.test/api/v1/health"
    assert str(recorder.requests[1].url) == "http://api.test/downloads/book.txt"


# ---------------------------------------------------------------------------
# End-to-end against the real ASGI app (SDK / API contract)
# ---------------------------------------------------------------------------


class _SyncASGITransport(httpx.BaseTransport):
    """
    Bridge synchronous ``httpx`` requests into an ASGI application.

    :class:`NovelApiClient` is synchronous while ``httpx.ASGITransport`` only
    serves async clients; this adapter runs the async round-trip on a private
    event loop and re-wraps the buffered response in a plain byte stream. No
    sockets are opened.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _call() -> httpx.Response:
            response = await self._transport.handle_async_request(request)
            await response.aread()
            return response

        response = asyncio.run(_call())
        # Re-wrap with an in-memory stream so the sync client can read it.
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            extensions=response.extensions,
        )


class _E2EManager:
    """Minimal task-manager stand-in for the end-to-end contract test."""

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
        return None


def test_novel_api_client_end_to_end_against_create_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Run the documented SDK methods against the real ``create_app()`` app.

    This pins the contract between the client and the server: every call goes
    through the actual FastAPI routing, token auth and serialisation, with the
    task manager replaced by a fake and the site search stubbed to return no
    results. Requests are bridged into the ASGI app in-process; no network is
    used.
    """
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)
    manager = _E2EManager()
    application = create_app(token="secret-token", output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_manager] = lambda: manager

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, Any]]:
        assert keyword
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)

    with NovelApiClient(
        base_url="http://testserver",
        token="secret-token",
        transport=_SyncASGITransport(application),
    ) as api:
        # 1) health: the SDK reaches the liveness probe through real routing.
        health = api.health()
        assert health["status"] == "ok"
        assert health["version"] == __version__

        # 2) sites: the supported-site metadata comes from the same module the
        #    server serves.
        sites = api.sites()
        assert "qidian" in sites["search"]
        assert "qidian" in sites["download"]

        # 3) search: an empty result set round-trips through validation.
        assert api.search("斗破", sites=["demo"]) == {"total": 0, "results": []}

        # 4) add_task: the created task is serialised by the server's own
        #    ``task_to_dict``.
        created = api.add_task("demo", "9", title="My Book")
        assert created["site"] == "demo"
        assert created["book_id"] == "9"
        assert created["title"] == "My Book"
        assert created["status"] == "queued"
        assert created["task_id"]
        assert len(manager.pending) == 1

        # 5) The 404 ``detail`` the server puts on the wire is the shared
        #    contract constant, surfaced verbatim by the client...
        with pytest.raises(ApiError) as info:
            api.cancel_task("missing")
        assert info.value.status_code == 404
        assert info.value.detail == MSG_TASK_NOT_FOUND.format(task_id="missing")

        # ...and the client's own fallback error (``task()`` against a server
        # without the per-task endpoint) reuses the same constant.
        with pytest.raises(ApiError) as info:
            api.task("missing")
        assert info.value.status_code == 404
        assert info.value.detail == MSG_TASK_NOT_FOUND.format(task_id="missing")
