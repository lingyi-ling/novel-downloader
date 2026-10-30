#!/usr/bin/env python3
"""
tests.apps.test_api_logging
---------------------------

Offline tests for the API request-logging middleware and the Cache-Control
response policy (``novel_downloader.apps.api.middleware``).

Covered:

* one access-log line per request (``method path status duration``), INFO for
  fast requests and WARNING above the slow-request threshold
* the query string and response bodies (notably ``/downloads`` file content)
  never appear in the log
* ``Cache-Control`` values: ``private, max-age=60`` for the reference
  endpoints, ``no-store`` for live task data, ``public, max-age=3600`` for
  exported files
* error responses (401 / 404 / 413) carry ``no-store``
* route-provided headers (the SSE streams' ``no-cache``) are preserved

Two harnesses are used, mirroring ``test_api_middleware``: end-to-end
requests through ``httpx.ASGITransport`` against the real ``create_app``
stack, and the middleware classes driven directly with hand-crafted ASGI
scopes where that is the only deterministic option (slow requests, requests
that never produce a response). No server is started, no network access.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.middleware import (
    CACHE_CONTROL_NO_STORE,
    CACHE_CONTROL_PRIVATE,
    CACHE_CONTROL_PUBLIC,
    CacheControlMiddleware,
    RequestLoggingMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.common import history_files
from novel_downloader.apps.core.models import DownloadTask

#: Logger the access-log lines are emitted on.
LOG_LOGGER = "novel_downloader.apps.api.middleware"


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
def fake_music_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``/music/platforms`` independent of the optional music extra."""
    components = SimpleNamespace(
        available_labels=lambda: [("netease", "NetEase Music")],
        platform_note=lambda key: f"note for {key}",
    )
    monkeypatch.setattr(api_routes, "_load_music_components", lambda: components)


@pytest.fixture
def isolated_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``GET /history`` at an empty temporary output dir."""
    output_dir = tmp_path / "history-scan"
    output_dir.mkdir()
    monkeypatch.setattr(history_files, "_output_dir", lambda: output_dir)
    return output_dir


def _make_app(
    tmp_path: Path,
    fake_manager: FakeManager,
    **kwargs: Any,
) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


@pytest_asyncio.fixture
async def client(tmp_path: Path, fake_manager: FakeManager) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_make_app(tmp_path, fake_manager))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def auth_client(tmp_path: Path, fake_manager: FakeManager) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, token="secret-token")
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def limited_client(
    tmp_path: Path, fake_manager: FakeManager
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, max_body_bytes=64)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _downloads_file(tmp_path: Path, name: str = "book.txt") -> Path:
    """Write one exported file into the app's ``/downloads`` directory."""
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(exist_ok=True)
    target = output_dir / name
    target.write_text("download-body-canary", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Direct ASGI harness helpers
# ---------------------------------------------------------------------------


def _receive_of(messages: list[Message]) -> Receive:
    """Build a ``receive`` yielding the given messages, then empty requests."""
    iterator = iter(messages)

    async def receive() -> Message:
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _scope(
    path: str = "/api/v1/tasks",
    method: str = "GET",
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _run_asgi(app: Any, scope: Scope) -> tuple[int, dict[str, str], bytes]:
    """Drive an ASGI app and return ``(status, headers, body)``."""
    store: dict[str, Any] = {}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            store["start"] = message
        else:
            store["body"] = store.get("body", b"") + message.get("body", b"")

    await app(scope, _receive_of([]), send)
    start = store["start"]
    raw_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    return int(start["status"]), raw_headers, store.get("body", b"")


def _plain_downstream(
    status: int = 200,
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> Any:
    """Minimal ASGI app answering with a fixed status and headers."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": list(headers),
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _plain_send() -> Send:
    """A ``send`` sink for direct-harness runs that ignore the response."""

    async def send(message: Message) -> None:
        return None

    return send


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """All access-log records captured for the middleware logger."""
    return [record for record in caplog.records if record.name == LOG_LOGGER]


# ---------------------------------------------------------------------------
# Cache-Control: successful responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/sites", CACHE_CONTROL_PRIVATE),
        ("/api/v1/history", CACHE_CONTROL_PRIVATE),
        ("/api/v1/music/platforms", CACHE_CONTROL_PRIVATE),
    ],
)
async def test_reference_endpoints_are_privately_cached(
    client: httpx.AsyncClient,
    fake_music_components: None,
    isolated_history: Path,
    path: str,
    expected: str,
) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == expected


@pytest.mark.asyncio
async def test_music_platforms_cached_without_music_extra(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache policy holds even when the music engine answers 503."""
    monkeypatch.setitem(sys.modules, "mutagen", None)

    response = await client.get("/api/v1/music/platforms")

    assert response.status_code == 503
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_tasks_endpoint_is_no_store(
    client: httpx.AsyncClient, fake_manager: FakeManager
) -> None:
    fake_manager.pending.append(DownloadTask(title="t", site="demo", book_id="1"))

    response = await client.get("/api/v1/tasks")

    assert response.status_code == 200
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_task_subresources_are_no_store_too(
    client: httpx.AsyncClient,
) -> None:
    """The ``/tasks`` policy covers ``/tasks/{id}``, not just the list."""
    response = await client.get("/api/v1/tasks/does-not-exist")

    assert response.status_code == 404
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_downloads_files_are_publicly_cached(
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> None:
    _downloads_file(tmp_path)

    response = await client.get("/downloads/book.txt")

    assert response.status_code == 200
    assert response.text == "download-body-canary"
    assert response.headers["cache-control"] == CACHE_CONTROL_PUBLIC


# ---------------------------------------------------------------------------
# Cache-Control: error responses and header preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_api_path_is_no_store(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_missing_download_is_no_store(client: httpx.AsyncClient) -> None:
    response = await client.get("/downloads/missing.txt")

    assert response.status_code == 404
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_401_responses_are_no_store(
    tmp_path: Path,
    auth_client: httpx.AsyncClient,
) -> None:
    _downloads_file(tmp_path)

    api_response = await auth_client.get("/api/v1/tasks")
    download_response = await auth_client.get("/downloads/book.txt")

    assert api_response.status_code == 401
    assert api_response.headers["cache-control"] == CACHE_CONTROL_NO_STORE
    assert download_response.status_code == 401
    assert download_response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_401_endpoint_keeps_its_own_cache_policy(
    tmp_path: Path,
    auth_client: httpx.AsyncClient,
) -> None:
    """A 401 on a reference endpoint is still an error: never cached."""
    response = await auth_client.get("/api/v1/sites")

    assert response.status_code == 401
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_413_response_is_no_store(
    limited_client: httpx.AsyncClient,
) -> None:
    response = await limited_client.post(
        "/api/v1/search",
        content=b'{"keyword": "' + b"x" * 200,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_sse_stream_keeps_its_no_cache_header(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware must not override headers a route sets itself."""
    frame = api_routes._sse_frame(
        "snapshot", {"pending": [], "running": [], "completed": []}
    )

    async def fake_stream(mgr: Any, interval: float) -> Any:
        yield frame

    monkeypatch.setattr(api_routes, "_task_event_stream", fake_stream)

    async with client.stream("GET", "/api/v1/tasks/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        raw = "".join([chunk async for chunk in response.aiter_text()])

    assert "snapshot" in raw


@pytest.mark.asyncio
async def test_cache_policy_passes_unrelated_paths_through() -> None:
    """``/docs`` and friends are outside the policy and stay untouched."""
    middleware: Any = CacheControlMiddleware(_plain_downstream())

    status, headers, _ = await _run_asgi(middleware, _scope(path="/docs"))

    assert status == 200
    assert "cache-control" not in headers


@pytest.mark.asyncio
async def test_cache_rules_do_not_match_lookalike_paths() -> None:
    """``/api/v1/sites-extra`` is not ``/api/v1/sites``."""
    middleware: Any = CacheControlMiddleware(_plain_downstream())

    status, headers, _ = await _run_asgi(middleware, _scope(path="/api/v1/sites-extra"))

    assert status == 200
    assert headers["cache-control"] == CACHE_CONTROL_NO_STORE


@pytest.mark.asyncio
async def test_cache_middleware_preserves_route_headers() -> None:
    middleware: Any = CacheControlMiddleware(
        _plain_downstream(headers=[(b"cache-control", b"private, max-age=5")])
    )

    status, headers, _ = await _run_asgi(middleware, _scope(path="/api/v1/tasks"))

    assert status == 200
    assert headers["cache-control"] == "private, max-age=5"


@pytest.mark.asyncio
async def test_cache_middleware_covers_trailing_slash_paths() -> None:
    middleware: Any = CacheControlMiddleware(_plain_downstream())

    _status, headers, _ = await _run_asgi(middleware, _scope(path="/api/v1/sites/"))

    assert headers["cache-control"] == CACHE_CONTROL_PRIVATE


# ---------------------------------------------------------------------------
# Request logging: format and levels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_request_is_logged_once_at_info(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    records = _access_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert re.fullmatch(r"GET /api/v1/health 200 \d+\.\d+ms", records[0].getMessage())


@pytest.mark.asyncio
async def test_error_requests_are_logged_with_their_status(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        missing = await client.get("/api/v1/tasks/does-not-exist")
        bad_method = await client.delete("/api/v1/health")

    assert missing.status_code == 404
    assert bad_method.status_code == 405
    messages = [record.getMessage() for record in _access_records(caplog)]
    assert len(messages) == 2
    assert re.fullmatch(r"GET /api/v1/tasks/does-not-exist 404 \d+\.\d+ms", messages[0])
    assert re.fullmatch(r"DELETE /api/v1/health 405 \d+\.\d+ms", messages[1])


@pytest.mark.asyncio
async def test_query_string_is_never_logged(
    client: httpx.AsyncClient,
    fake_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_manager.pending.append(DownloadTask(title="t", site="demo", book_id="1"))

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        response = await client.get("/api/v1/tasks", params={"api_key": "supersecret"})

    assert response.status_code == 200
    messages = [record.getMessage() for record in _access_records(caplog)]
    assert len(messages) == 1
    assert re.fullmatch(r"GET /api/v1/tasks 200 \d+\.\d+ms", messages[0])
    assert "supersecret" not in caplog.text
    assert "api_key" not in caplog.text


@pytest.mark.asyncio
async def test_downloads_body_is_never_logged(
    tmp_path: Path,
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _downloads_file(tmp_path)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        response = await client.get("/downloads/book.txt")

    assert response.status_code == 200
    assert response.text == "download-body-canary"
    # The access line is present, but the file content never is.
    messages = [record.getMessage() for record in _access_records(caplog)]
    assert len(messages) == 1
    assert re.fullmatch(r"GET /downloads/book.txt 200 \d+\.\d+ms", messages[0])
    assert "download-body-canary" not in caplog.text


@pytest.mark.asyncio
async def test_slow_request_is_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Above the threshold the same line format is logged at WARNING."""

    async def slow_downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.06)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware: Any = RequestLoggingMiddleware(slow_downstream, slow_seconds=0.05)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        await middleware(_scope(path="/api/v1/tasks"), _receive_of([]), _plain_send())

    records = _access_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert re.fullmatch(r"GET /api/v1/tasks 200 \d+\.\d+ms", records[0].getMessage())


@pytest.mark.asyncio
async def test_default_threshold_is_one_second() -> None:
    """Requests at or above one second must end up in the WARNING path."""

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware: Any = RequestLoggingMiddleware(downstream)
    assert middleware.slow_seconds == 1.0


@pytest.mark.asyncio
async def test_request_without_response_is_logged_as_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request that never produces a response is still logged, as failed."""

    async def failing_downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    middleware: Any = RequestLoggingMiddleware(failing_downstream)

    with (
        caplog.at_level(logging.INFO, logger=LOG_LOGGER),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await middleware(
            _scope(path="/api/v1/search", method="POST"),
            _receive_of([]),
            _plain_send(),
        )

    records = _access_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert re.fullmatch(r"POST /api/v1/search 500 \d+\.\d+ms", records[0].getMessage())


@pytest.mark.asyncio
async def test_non_http_scopes_are_not_logged_and_pass_through() -> None:
    seen: list[Scope] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware: Any = RequestLoggingMiddleware(downstream)
    scope: Scope = {"type": "lifespan"}

    await middleware(scope, _receive_of([]), _plain_send())

    assert seen and seen[0]["type"] == "lifespan"
