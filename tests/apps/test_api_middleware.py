#!/usr/bin/env python3
"""
tests.apps.test_api_middleware
------------------------------

Offline tests for the request-hardening ASGI middleware
(``novel_downloader.apps.api.middleware``).

Two complementary harnesses are used:

* end-to-end requests through ``httpx.ASGITransport`` against the real
  ``create_app`` stack (body limit + downloads auth + optional CORS), with the
  task manager replaced by a fake and the search function monkeypatched, and
* the :class:`BodySizeLimitMiddleware` driven directly with hand-crafted ASGI
  scopes, which is the only way to exercise chunked upload sequences, lying
  ``Content-Length`` headers and client disconnects deterministically.

No server is started and no network access happens.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.auth import AUTH_ERROR_DETAIL
from novel_downloader.apps.api.middleware import (
    DEFAULT_MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
)
from novel_downloader.apps.api.routes import get_manager
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


def _make_app(
    tmp_path: Path,
    fake_manager: FakeManager,
    **kwargs: Any,
) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


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


@pytest_asyncio.fixture
async def auth_limited_client(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> httpx.AsyncClient:
    application = _make_app(
        tmp_path,
        fake_manager,
        token="secret-token",
        max_body_bytes=16,
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


@pytest.fixture
def fake_search(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the aggregated search with an offline echo of the request."""
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["keyword"] = keyword
        calls["kwargs"] = kwargs
        return []

    monkeypatch.setattr(api_routes, "search", fake_search)
    return calls


# ---------------------------------------------------------------------------
# BodySizeLimitMiddleware: direct ASGI harness
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


def _capture_send(store: dict[str, Any]) -> Send:
    async def send(message: Message) -> None:
        store.setdefault("messages", []).append(message)

    return send


def _downstream(store: dict[str, Any]) -> Any:
    """Minimal ASGI app that records the request it was handed."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        store["body"] = b"".join(chunks)
        store["scope"] = scope
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


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


def _middleware(
    max_bytes: int = 8,
    endpoints: Iterable[tuple[str, str]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Wrap the capturing downstream app in the middleware under test."""
    store: dict[str, Any] = {}
    app: Any = BodySizeLimitMiddleware(
        _downstream(store),
        max_bytes=max_bytes,
        endpoints=endpoints,
    )
    return app, store


def _response(store: dict[str, Any]) -> tuple[int, bytes]:
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
    return start["status"], body


def _content_type(store: dict[str, Any]) -> str:
    start = next(
        message
        for message in store["messages"]
        if message["type"] == "http.response.start"
    )
    return dict(start.get("headers", [])).get(b"content-type", b"").decode()


@pytest.mark.asyncio
async def test_content_length_over_limit_rejected_without_reading() -> None:
    app, store = _middleware(max_bytes=8)
    reads: list[Message] = []

    async def receive() -> Message:
        reads.append({"type": "http.receive"})
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(
        _scope(headers=[(b"content-length", b"9")]),
        receive,
        _capture_send(store),
    )

    status, body = _response(store)
    assert status == 413
    assert _content_type(store).startswith("application/json")
    payload = json.loads(body)
    assert payload == {
        "detail": "Request body too large: the limit for this endpoint is 8 bytes"
    }
    # The oversized body was never read and the app never ran.
    assert reads == []
    assert "body" not in store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "expected_status"),
    [(8, 200), (9, 413)],
)
async def test_content_length_boundary_is_inclusive(
    declared: int,
    expected_status: int,
) -> None:
    app, store = _middleware(max_bytes=8)
    await app(
        _scope(headers=[(b"content-length", str(declared).encode())]),
        _receive_of([{"type": "http.request", "body": b"abcd", "more_body": False}]),
        _capture_send(store),
    )
    status, _ = _response(store)
    assert status == expected_status


@pytest.mark.asyncio
async def test_accepted_body_is_replayed_byte_identical() -> None:
    app, store = _middleware(max_bytes=32)
    payload = '关键字 {"nested": true} \x00 raw'
    body = payload.encode("utf-8")

    await app(
        _scope(headers=[(b"content-length", str(len(body)).encode())]),
        _receive_of([{"type": "http.request", "body": body, "more_body": False}]),
        _capture_send(store),
    )

    status, _ = _response(store)
    assert status == 200
    assert store["body"] == body


@pytest.mark.asyncio
async def test_chunked_body_within_limit_is_replayed_byte_identical() -> None:
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(),  # no Content-Length: the middleware must read the stream
        _receive_of(
            [
                {"type": "http.request", "body": b"he", "more_body": True},
                {"type": "http.request", "body": b"ll", "more_body": True},
                {"type": "http.request", "body": b"o!", "more_body": False},
            ]
        ),
        _capture_send(store),
    )

    status, _ = _response(store)
    assert status == 200
    assert store["body"] == b"hello!"


@pytest.mark.asyncio
async def test_chunked_body_over_limit_aborts_midway() -> None:
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(),
        _receive_of(
            [
                {"type": "http.request", "body": b"a" * 3, "more_body": True},
                {"type": "http.request", "body": b"b" * 3, "more_body": True},
                {"type": "http.request", "body": b"c" * 3, "more_body": False},
            ]
        ),
        _capture_send(store),
    )

    status, body = _response(store)
    assert status == 413
    assert b"8 bytes" in body
    assert "body" not in store  # downstream never saw a body


@pytest.mark.asyncio
async def test_lying_content_length_is_still_enforced() -> None:
    """A small declared length does not smuggle a large chunked body."""
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(headers=[(b"content-length", b"2")]),
        _receive_of(
            [
                {"type": "http.request", "body": b"x" * 4, "more_body": True},
                {"type": "http.request", "body": b"y" * 5, "more_body": False},
            ]
        ),
        _capture_send(store),
    )

    status, _ = _response(store)
    assert status == 413
    assert "body" not in store


@pytest.mark.asyncio
async def test_unparseable_content_length_falls_back_to_streaming() -> None:
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(headers=[(b"content-length", b"not-a-number")]),
        _receive_of([{"type": "http.request", "body": b"tiny", "more_body": False}]),
        _capture_send(store),
    )

    status, _ = _response(store)
    assert status == 200
    assert store["body"] == b"tiny"


@pytest.mark.asyncio
async def test_client_disconnect_while_streaming_sends_no_response() -> None:
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(),
        _receive_of(
            [
                {"type": "http.request", "body": b"x", "more_body": True},
                {"type": "http.disconnect"},
            ]
        ),
        _capture_send(store),
    )

    # Nobody is left to answer; no response may be produced.
    assert "messages" not in store
    assert "body" not in store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("POST", "/api/v1/search", 413),  # exact endpoint is guarded
        ("POST", "/api/v1/search/", 413),  # trailing slash is normalized away
        ("POST", "/api/v1/search/extra", 200),  # a longer path is not the endpoint
        ("GET", "/api/v1/search", 200),  # only POST bodies are bounded
        ("POST", "/api/v1/tasks", 200),  # not in the guarded endpoint set
    ],
)
async def test_guard_matches_method_and_exact_path(
    method: str,
    path: str,
    expected_status: int,
) -> None:
    app, store = _middleware(max_bytes=8)

    await app(
        _scope(path=path, method=method, headers=[(b"content-length", b"999")]),
        _receive_of([{"type": "http.request", "body": b"", "more_body": False}]),
        _capture_send(store),
    )

    status, _ = _response(store)
    assert status == expected_status


@pytest.mark.asyncio
async def test_custom_endpoint_set_replaces_the_default_guard() -> None:
    app, store = _middleware(
        max_bytes=4,
        endpoints={("PUT", "/api/v1/watchlist")},
    )

    await app(
        _scope(
            path="/api/v1/watchlist",
            method="PUT",
            headers=[(b"content-length", b"5")],
        ),
        _receive_of([{"type": "http.request", "body": b"12345", "more_body": False}]),
        _capture_send(store),
    )
    status, _ = _response(store)
    assert status == 413

    other, other_store = _middleware(
        max_bytes=4,
        endpoints={("PUT", "/api/v1/watchlist")},
    )
    await other(
        _scope(path="/api/v1/search", headers=[(b"content-length", b"999")]),
        _receive_of([{"type": "http.request", "body": b"", "more_body": False}]),
        _capture_send(other_store),
    )
    status, _ = _response(other_store)
    assert status == 200


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched() -> None:
    app, store = _middleware(max_bytes=4)
    scope: Scope = {"type": "lifespan"}

    await app(scope, _receive_of([]), _capture_send(store))

    assert store["scope"]["type"] == "lifespan"


@pytest.mark.asyncio
async def test_default_limit_is_one_mebibyte() -> None:
    assert DEFAULT_MAX_BODY_BYTES == 1024 * 1024


# ---------------------------------------------------------------------------
# Body size limit through the real application stack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_rejects_oversized_body_before_parsing(
    limited_client: httpx.AsyncClient,
) -> None:
    # Malformed JSON on purpose: a 422 would mean FastAPI parsed the body,
    # so a 413 proves the middleware intercepted it first.
    raw = b'{"keyword": "' + b"x" * 200
    response = await limited_client.post(
        "/api/v1/search",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "detail": "Request body too large: the limit for this endpoint is 64 bytes"
    }


@pytest.mark.asyncio
async def test_search_accepts_body_at_exact_limit(
    limited_client: httpx.AsyncClient,
    fake_search: dict[str, Any],
) -> None:
    base = len(json.dumps({"keyword": ""}, separators=(",", ":")))
    keyword = "x" * (64 - base)
    body = json.dumps({"keyword": keyword}, separators=(",", ":")).encode("utf-8")
    assert len(body) == 64  # exactly at the limit: still accepted

    response = await limited_client.post(
        "/api/v1/search",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    # The accepted body reached the route unchanged.
    assert fake_search["keyword"] == keyword


@pytest.mark.asyncio
async def test_unguarded_endpoint_accepts_large_body(
    limited_client: httpx.AsyncClient,
    fake_manager: FakeManager,
) -> None:
    response = await limited_client.post(
        "/api/v1/tasks",
        json={"title": "x" * 500, "site": "demo", "book_id": "1"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "x" * 500
    assert len(fake_manager.pending) == 1


# ---------------------------------------------------------------------------
# Body size limit + CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_413_and_success_keep_cors_headers(
    tmp_path: Path,
    fake_manager: FakeManager,
    fake_search: dict[str, Any],
) -> None:
    application = _make_app(tmp_path, fake_manager, cors=True, max_body_bytes=64)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        oversized = await http_client.post(
            "/api/v1/search",
            json={"keyword": "x" * 200},
            headers={"Origin": "http://example.invalid"},
        )
        accepted = await http_client.post(
            "/api/v1/search",
            json={"keyword": "斗破"},
            headers={"Origin": "http://example.invalid"},
        )
        preflight = await http_client.options(
            "/api/v1/search",
            headers={
                "Origin": "http://example.invalid",
                "Access-Control-Request-Method": "POST",
            },
        )

    # The size limit sits *inside* the CORS layer, so its 413 keeps the headers.
    assert oversized.status_code == 413
    assert oversized.headers["access-control-allow-origin"] == "*"

    assert accepted.status_code == 200
    assert accepted.headers["access-control-allow-origin"] == "*"

    # Preflights are answered by the CORS middleware itself.
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "POST" in preflight.headers["access-control-allow-methods"]


@pytest.mark.asyncio
async def test_413_without_cors_has_no_cors_headers(
    limited_client: httpx.AsyncClient,
) -> None:
    response = await limited_client.post(
        "/api/v1/search",
        json={"keyword": "x" * 200},
        headers={"Origin": "http://example.invalid"},
    )

    assert response.status_code == 413
    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Body size limit + token auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_limit_runs_before_token_auth(
    auth_limited_client: httpx.AsyncClient,
) -> None:
    oversized = await auth_limited_client.post(
        "/api/v1/search",
        json={"keyword": "x" * 200},
    )
    assert oversized.status_code == 413  # not 401: the body is rejected first

    authenticated_oversized = await auth_limited_client.post(
        "/api/v1/search",
        json={"keyword": "x" * 200},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert authenticated_oversized.status_code == 413


@pytest.mark.asyncio
async def test_authenticated_small_body_reaches_the_route(
    auth_limited_client: httpx.AsyncClient,
    fake_search: dict[str, Any],
) -> None:
    denied = await auth_limited_client.post(
        "/api/v1/search",
        json={"keyword": "ok"},
    )
    assert denied.status_code == 401
    assert denied.json()["detail"] == AUTH_ERROR_DETAIL

    allowed = await auth_limited_client.post(
        "/api/v1/search",
        json={"keyword": "ok"},
        headers={"X-API-Key": "secret-token"},
    )
    assert allowed.status_code == 200
    assert fake_search["keyword"] == "ok"


@pytest.mark.asyncio
async def test_downloads_static_mount_respects_token(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "visible.txt").write_text("hello", encoding="utf-8")

    application = _make_app(tmp_path, fake_manager, token="secret-token")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        missing_token = await http_client.get("/downloads/visible.txt")
        wrong_token = await http_client.get(
            "/downloads/visible.txt",
            headers={"Authorization": "Bearer nope"},
        )
        api_key = await http_client.get(
            "/downloads/visible.txt",
            headers={"X-API-Key": "secret-token"},
        )
        bearer = await http_client.get(
            "/downloads/visible.txt",
            headers={"Authorization": "Bearer secret-token"},
        )
        # A look-alike path outside the mount is not guarded by this middleware.
        lookalike = await http_client.get("/downloads-extra/visible.txt")

    assert missing_token.status_code == 401
    assert missing_token.json() == {"detail": AUTH_ERROR_DETAIL}
    assert missing_token.headers["www-authenticate"] == "Bearer"
    assert wrong_token.status_code == 401

    assert api_key.status_code == 200
    assert api_key.text == "hello"
    assert bearer.status_code == 200
    assert bearer.text == "hello"

    # Not guarded: the request reaches the router and misses there (404).
    assert lookalike.status_code == 404


@pytest.mark.asyncio
async def test_downloads_mount_open_when_no_token_configured(
    tmp_path: Path,
    fake_manager: FakeManager,
) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "visible.txt").write_text("hello", encoding="utf-8")

    application = _make_app(tmp_path, fake_manager)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        response = await http_client.get("/downloads/visible.txt")

    assert response.status_code == 200
    assert response.text == "hello"
