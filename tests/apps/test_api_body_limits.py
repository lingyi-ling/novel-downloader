#!/usr/bin/env python3
"""
tests.apps.test_api_body_limits
--------------------------------

Route-matrix coverage for the request-body guards of the REST API
(:class:`novel_downloader.apps.api.middleware.BodySizeLimitMiddleware`) and
the embedded stdlib transport.

Every route that binds a JSON body is exercised with:

* a body of exactly the configured limit (accepted and parsed),
* a body one byte over the limit (``413`` before parsing),
* a chunked body without ``Content-Length`` that crosses the limit while
  streaming (``413`` from the running total),
* a lying ``Content-Length`` that declares the limit is exceeded while the
  real body is tiny (rejected without reading a single byte),
* non-JSON ``Content-Type`` values (``text/plain``, form, multipart) and
  compressed bodies (``gzip``), all answered with ``415`` before any read.

The error contract is pinned end to end: the fixed ``detail`` text, the
``X-Request-ID`` echo, ``Cache-Control: no-store`` and the absence of partial
route execution (no task queued, no watchlist entry written).

Embedded parity: the stdlib engine shares the same default cap, guard
methods/prefix and 413/415 wording. Its documented difference is chunked
request bodies (``501``, because the stdlib server does not implement
``Transfer-Encoding: chunked``), which is pinned by
``tests.apps.test_embedded_server``.

No server is started and no network access happens.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.middleware import (
    BODY_LIMIT_METHODS,
    BODY_LIMIT_PREFIX,
    DEFAULT_MAX_BODY_BYTES,
    UNSUPPORTED_CONTENT_TYPE_DETAIL,
    BodySizeLimitMiddleware,
)
from novel_downloader.apps.api.routes import get_manager, get_watchlist_store
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.core.watchlist import WatchlistStore

#: Small cap so every route can be exercised quickly; the production default
#: (1 MiB) is asserted separately.
LIMIT = 512

JSON_HEADERS = {"Content-Type": "application/json"}

#: Exact ``413`` detail for :data:`LIMIT`.
TOO_LARGE_DETAIL = (
    f"Request body too large: the limit for this endpoint is {LIMIT} bytes"
)

#: ``(method, path, minimal payload, status at the exact limit)``. The status
#: is what the route answers once the guard lets a valid body through; music
#: routes are forced to ``503`` by the fixture because the extras are not the
#: subject here.
ROUTES: tuple[tuple[str, str, dict[str, Any], int], ...] = (
    ("POST", "/api/v1/browse", {"site": "fanqienovel", "limit": 1}, 200),
    ("POST", "/api/v1/search", {"keyword": "matrix"}, 200),
    ("POST", "/api/v1/tasks", {"site": "qidian", "book_id": "1"}, 200),
    (
        "POST",
        "/api/v1/login/requests/matrix-route-missing",
        {"values": {}},
        404,
    ),
    ("POST", "/api/v1/watchlist", {"site": "qidian", "book_id": "1"}, 200),
    ("POST", "/api/v1/watchlist/check", {"download": False}, 200),
    ("POST", "/api/v1/music/search", {"keyword": "matrix", "limit": 1}, 503),
    (
        "POST",
        "/api/v1/music/download",
        {"tracks": [{"provider": "netease", "id": "1", "name": "track"}]},
        503,
    ),
    (
        "POST",
        "/api/v1/music/export",
        {"tracks": [{"path": "track.mp3"}]},
        503,
    ),
    (
        "POST",
        "/api/v1/music/jobs",
        {"tracks": [{"provider": "netease", "id": "1", "name": "track"}]},
        503,
    ),
)

ROUTE_IDS = [f"{method} {path}" for method, path, _payload, _status in ROUTES]


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


class _StubSearcher:
    """Searcher double so browse never opens a provider connection."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def rank(self, _limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def category_books(
        self,
        _category_id: str,
        *,
        gender: str = "1",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return []


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def watchlist_store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    fake_manager: FakeManager,
    watchlist_store: WatchlistStore,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Real app stack with every outbound provider call replaced."""

    async def fake_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    def unavailable_music() -> Any:
        raise HTTPException(status_code=503, detail="Music support is not available")

    monkeypatch.setattr(api_routes, "search", fake_search)
    monkeypatch.setattr(
        api_routes, "_browse_searcher", lambda _site, _method: _StubSearcher
    )
    monkeypatch.setattr(api_routes, "_load_music_components", unavailable_music)

    application = create_app(
        output_dir=tmp_path / "downloads",
        max_body_bytes=LIMIT,
        rate_limit=None,
    )
    application.dependency_overrides[get_manager] = lambda: fake_manager
    application.dependency_overrides[get_watchlist_store] = lambda: watchlist_store
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _compact(payload: dict[str, Any]) -> bytes:
    """Serialise a payload compactly so the byte budget is predictable."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def _padded(payload: dict[str, Any], size: int) -> bytes:
    """
    Return ``payload`` padded with trailing JSON whitespace to ``size`` bytes.

    Trailing spaces are legal JSON and are ignored by the parser, so the same
    semantic request can be sent at exactly the limit and one byte above it.
    """
    base = _compact(payload)
    assert len(base) <= size
    return base + b" " * (size - len(base))


async def _oversized_stream(total: int) -> AsyncIterator[bytes]:
    """Yield ``total`` bytes in chunks; no ``Content-Length`` is declared."""
    sent = 0
    while sent < total:
        size = min(100, total - sent)
        sent += size
        yield b"x" * size


# ---------------------------------------------------------------------------
# End-to-end route matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_status"),
    ROUTES,
    ids=ROUTE_IDS,
)
async def test_route_accepts_body_exactly_at_limit(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    """At the limit the guard passes and the route parses the body normally."""
    body = _padded(payload, LIMIT)
    assert len(body) == LIMIT

    response = await client.request(method, path, content=body, headers=JSON_HEADERS)

    assert response.status_code == expected_status
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_status"),
    ROUTES,
    ids=ROUTE_IDS,
)
async def test_route_rejects_body_one_byte_over_limit(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    """One byte over the limit is a 413 with the fixed contract."""
    body = _padded(payload, LIMIT + 1)

    response = await client.request(
        method,
        path,
        content=body,
        headers={**JSON_HEADERS, "X-Request-ID": "matrix-over"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE_DETAIL}
    assert response.headers["x-request-id"] == "matrix-over"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "expected_status"),
    ROUTES,
    ids=ROUTE_IDS,
)
async def test_route_rejects_streamed_body_over_limit(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    """A chunked body is counted as it arrives and rejected mid-stream."""
    response = await client.request(
        method,
        path,
        content=_oversized_stream(LIMIT * 4),
        headers=JSON_HEADERS,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE_DETAIL}
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_rejected_bodies_never_partially_execute_the_route(
    client: httpx.AsyncClient,
    fake_manager: FakeManager,
    watchlist_store: WatchlistStore,
) -> None:
    """Neither a 413 nor a 415 may queue a task or write a watchlist entry."""
    task_body = {"site": "qidian", "book_id": "1"}

    declared_over = await client.post(
        "/api/v1/tasks", content=_padded(task_body, LIMIT + 1), headers=JSON_HEADERS
    )
    assert declared_over.status_code == 413
    assert fake_manager.pending == []

    streamed_over = await client.post(
        "/api/v1/tasks",
        content=_oversized_stream(LIMIT * 4),
        headers=JSON_HEADERS,
    )
    assert streamed_over.status_code == 413
    assert fake_manager.pending == []

    watchlist_over = await client.post(
        "/api/v1/watchlist", content=_padded(task_body, LIMIT + 1), headers=JSON_HEADERS
    )
    assert watchlist_over.status_code == 413
    assert watchlist_store.entries() == []

    wrong_type = await client.post(
        "/api/v1/tasks",
        content=_compact(task_body),
        headers={"Content-Type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    assert fake_manager.pending == []


# ---------------------------------------------------------------------------
# Content-Type guard (415)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=matrix",
    ],
)
async def test_non_json_content_type_is_415_without_parsing(
    client: httpx.AsyncClient,
    fake_manager: FakeManager,
    content_type: str,
) -> None:
    """Malformed bytes prove nothing was parsed: the answer is 415, not 422."""
    response = await client.post(
        "/api/v1/tasks",
        content=b"{not-json",
        headers={"Content-Type": content_type, "X-Request-ID": "matrix-415"},
    )

    assert response.status_code == 415
    assert response.json() == {"detail": UNSUPPORTED_CONTENT_TYPE_DETAIL}
    assert response.headers["x-request-id"] == "matrix-415"
    assert response.headers["cache-control"] == "no-store"
    assert fake_manager.pending == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "application/json; charset=utf-8",
        "application/vnd.api+json",
    ],
)
async def test_json_content_type_variants_are_accepted(
    client: httpx.AsyncClient, content_type: str
) -> None:
    response = await client.post(
        "/api/v1/search",
        content=_compact({"keyword": "matrix"}),
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_missing_content_type_is_deferred_to_fastapi_not_415(
    client: httpx.AsyncClient,
) -> None:
    """
    A headerless body is not manufactured into a 415.

    FastAPI does not treat a body without ``Content-Type`` as JSON, so it
    reports its own ``422``; the guard deliberately leaves that routing
    behaviour alone instead of rejecting headerless clients at the edge.
    """
    response = await client.post(
        "/api/v1/search", content=_compact({"keyword": "matrix"})
    )

    assert "content-type" not in response.request.headers
    assert response.status_code == 422
    assert "valid dictionary" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Compressed bodies (415)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "compressed",
    [
        gzip.compress(b'{"keyword": "matrix"}'),
        # A "bomb": tiny on the wire, 100 kB once inflated.
        gzip.compress(b" " * 100_000),
    ],
    ids=["small", "expands-past-limit"],
)
async def test_gzip_body_is_refused_before_it_can_be_inflated(
    client: httpx.AsyncClient,
    fake_manager: FakeManager,
    compressed: bytes,
) -> None:
    response = await client.post(
        "/api/v1/search",
        content=compressed,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )

    assert response.status_code == 415
    assert response.json() == {
        "detail": (
            "Unsupported Content-Encoding: send the request body "
            "uncompressed (identity)"
        )
    }
    assert response.headers["x-request-id"]


def test_default_guard_covers_every_json_route() -> None:
    """Prefix mode must guard every route in the matrix, not a fixed list."""
    guard = BodySizeLimitMiddleware(_unreachable_app, max_bytes=DEFAULT_MAX_BODY_BYTES)

    for method, path, _payload, _status in ROUTES:
        assert guard._applies(method, path), f"{method} {path} is not guarded"
    # Bodyless write endpoints are covered too (a future body cannot bypass).
    assert guard._applies("DELETE", "/api/v1/watchlist/qidian/1")
    assert guard._applies("POST", "/api/v1/tasks/matrix/cancel")


# ---------------------------------------------------------------------------
# Direct ASGI harness: no-read and cumulative-stream proofs
# ---------------------------------------------------------------------------


async def _unreachable_app(scope: Scope, receive: Receive, send: Send) -> None:
    """A downstream app that fails the test if it is ever invoked."""
    raise AssertionError(f"guarded request reached the app: {scope.get('path')}")


def _scope(
    path: str = "/api/v1/search",
    method: str = "POST",
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


def _capture_send(store: dict[str, Any]) -> Send:
    async def send(message: Message) -> None:
        store.setdefault("messages", []).append(message)

    return send


def _response(store: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    start = next(
        message
        for message in store["messages"]
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in store["messages"]
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body)


@pytest.mark.asyncio
async def test_declared_length_over_limit_rejected_without_reading() -> None:
    """A big ``Content-Length`` is rejected even when the real body is tiny."""
    store: dict[str, Any] = {}
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"{}", "more_body": False}

    app = BodySizeLimitMiddleware(_unreachable_app, max_bytes=LIMIT)
    await app(
        _scope(
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(LIMIT + 1).encode()),
            ]
        ),
        receive,
        _capture_send(store),
    )

    status, payload = _response(store)
    assert status == 413
    assert payload == {"detail": TOO_LARGE_DETAIL}
    assert reads == 0


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_at_the_crossing_chunk() -> None:
    """The running total crosses 512 on the third 200-byte chunk."""
    store: dict[str, Any] = {}
    reads = 0
    messages = [
        {"type": "http.request", "body": b"x" * 200, "more_body": True}
        for _ in range(5)
    ]

    async def receive() -> Message:
        nonlocal reads
        message = messages[reads]
        reads += 1
        return message

    app = BodySizeLimitMiddleware(_unreachable_app, max_bytes=LIMIT)
    await app(
        _scope(headers=[(b"content-type", b"application/json")]),
        receive,
        _capture_send(store),
    )

    status, payload = _response(store)
    assert status == 413
    assert payload == {"detail": TOO_LARGE_DETAIL}
    assert reads == 3  # 600 bytes: the crossing chunk aborts the read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        b"text/plain",
        b"application/x-www-form-urlencoded",
        b"multipart/form-data; boundary=x",
    ],
)
async def test_non_json_content_type_is_415_without_reading(
    content_type: bytes,
) -> None:
    store: dict[str, Any] = {}
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"{not-json", "more_body": False}

    app = BodySizeLimitMiddleware(_unreachable_app, max_bytes=LIMIT)
    await app(
        _scope(
            headers=[
                (b"content-type", content_type),
                (b"content-length", b"8"),
            ]
        ),
        receive,
        _capture_send(store),
    )

    status, payload = _response(store)
    assert status == 415
    assert payload == {"detail": UNSUPPORTED_CONTENT_TYPE_DETAIL}
    assert reads == 0


@pytest.mark.asyncio
async def test_compressed_oversized_declaration_is_415_before_413() -> None:
    """Encoding is refused first: no decompressor, no size ambiguity."""
    store: dict[str, Any] = {}
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    app = BodySizeLimitMiddleware(_unreachable_app, max_bytes=LIMIT)
    await app(
        _scope(
            headers=[
                (b"content-type", b"application/json"),
                (b"content-encoding", b"gzip"),
                (b"content-length", str(LIMIT * 4).encode()),
            ]
        ),
        receive,
        _capture_send(store),
    )

    status, payload = _response(store)
    assert status == 415
    assert "Unsupported Content-Encoding" in payload["detail"]
    assert reads == 0


# ---------------------------------------------------------------------------
# Embedded transport parity
# ---------------------------------------------------------------------------


def test_embedded_guard_matches_the_rest_matrix() -> None:
    """
    The stdlib engine shares the cap, guard set and 413/415 wording.

    Documented differences, each pinned by its own transport tests:

    * the embedded server answers a chunked request with ``501`` (it does not
      implement ``Transfer-Encoding: chunked``), while the REST middleware
      counts the stream and answers ``413``;
    * a body with no ``Content-Type`` is decoded as JSON by the embedded
      engine, while FastAPI answers ``422`` on the REST transport.
    """
    from novel_downloader.apps.embedded import server as embedded

    assert embedded._MAX_BODY_BYTES == DEFAULT_MAX_BODY_BYTES == 1024 * 1024
    assert embedded._BODY_LIMIT_METHODS == BODY_LIMIT_METHODS
    assert embedded._BODY_LIMIT_PREFIX == BODY_LIMIT_PREFIX
    expected_detail = (
        "Request body too large: the limit for this endpoint is "
        f"{DEFAULT_MAX_BODY_BYTES} bytes"
    )
    assert expected_detail == embedded._MSG_BODY_TOO_LARGE
    assert embedded._MSG_UNSUPPORTED_CONTENT_TYPE == UNSUPPORTED_CONTENT_TYPE_DETAIL

    for method, path, _payload, _status in ROUTES:
        assert embedded._is_body_guarded(method, path), f"{method} {path}"
