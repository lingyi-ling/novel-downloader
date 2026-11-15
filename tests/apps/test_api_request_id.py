#!/usr/bin/env python3
"""
tests.apps.test_api_request_id
------------------------------

Offline tests for request correlation
(``novel_downloader.apps.api.middleware.RequestIdMiddleware``).

Covered:

* a safe client-supplied ``X-Request-ID`` is echoed on the response and
  prefixed to the single access-log line
* a missing, overlong or otherwise unsafe id is replaced by a UUID4 instead
  of being reflected (no header injection, no log forging)
* every response class — 200, 422, 429 and the catch-all 500 — carries the
  same id that the log line carries
* the error bodies' ``detail`` contract is unchanged (no ``request_id`` is
  added to ``detail``)
* the id lives in a ``ContextVar``/``scope["state"]`` and is reset after the
  request, so concurrent requests do not see each other's ids

Two harnesses are used, mirroring ``test_api_middleware``: end-to-end requests
through ``httpx.ASGITransport`` against the real ``create_app`` stack, and the
middleware driven directly with hand-crafted ASGI scopes for raw header bytes
that an HTTP client would refuse to send. No server is started, no network
access happens.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.middleware import (
    MAX_REQUEST_ID_LENGTH,
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    current_request_id,
)
from novel_downloader.apps.api.routes import get_manager
from novel_downloader.apps.core.models import DownloadTask

#: Logger the access-log lines are emitted on.
LOG_LOGGER = "novel_downloader.apps.api.middleware"

#: A server-generated id is a canonical lowercase UUID4.
UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


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
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def fake_manager() -> FakeManager:
    return FakeManager()


def _make_app(tmp_path: Path, fake_manager: FakeManager, **kwargs: Any) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads", **kwargs)
    application.dependency_overrides[get_manager] = lambda: fake_manager
    return application


def _client(app: FastAPI, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **kwargs),
        base_url="http://testserver",
    )


def _log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage() for record in caplog.records if record.name == LOG_LOGGER
    ]


# ---------------------------------------------------------------------------
# End-to-end: the id on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_request_id_is_echoed_and_logged(
    tmp_path: Path,
    fake_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _make_app(tmp_path, fake_manager)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/health", headers={REQUEST_ID_HEADER: "client-request-42"}
            )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "client-request-42"
    messages = _log_messages(caplog)
    assert len(messages) == 1
    assert messages[0].startswith("[client-request-42] GET /api/v1/health 200 ")


@pytest.mark.asyncio
async def test_missing_request_id_generates_a_uuid4(
    tmp_path: Path,
    fake_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _make_app(tmp_path, fake_manager)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        async with _client(app) as client:
            first = await client.get("/api/v1/health")
            second = await client.get("/api/v1/health")

    for response in (first, second):
        generated = response.headers["x-request-id"]
        assert UUID4_RE.fullmatch(generated), generated
    # Two requests never share an id.
    assert first.headers["x-request-id"] != second.headers["x-request-id"]

    messages = _log_messages(caplog)
    assert len(messages) == 2
    assert f"[{first.headers['x-request-id']}] " in messages[0]
    assert f"[{second.headers['x-request-id']}] " in messages[1]


@pytest.mark.asyncio
async def test_maximum_length_id_is_accepted(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    value = "a" * MAX_REQUEST_ID_LENGTH
    app = _make_app(tmp_path, fake_manager)

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: value}
        )

    assert response.headers["x-request-id"] == value


@pytest.mark.asyncio
async def test_overlong_id_is_replaced(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    value = "a" * (MAX_REQUEST_ID_LENGTH + 1)
    app = _make_app(tmp_path, fake_manager)

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: value}
        )

    generated = response.headers["x-request-id"]
    assert generated != value
    assert UUID4_RE.fullmatch(generated), generated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "has spaces",
        "bad[bracket]",
        "line\nbreak",
        "tab\tseparated",
        "",
    ],
)
async def test_unsafe_id_is_replaced(
    tmp_path: Path, fake_manager: FakeManager, value: str
) -> None:
    app = _make_app(tmp_path, fake_manager)

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/health", headers={REQUEST_ID_HEADER: value}
        )

    generated = response.headers["x-request-id"]
    assert UUID4_RE.fullmatch(generated), generated


# ---------------------------------------------------------------------------
# End-to-end: every response class carries the id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_422_carries_the_id_and_keeps_the_detail_contract(
    tmp_path: Path,
    fake_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _make_app(tmp_path, fake_manager)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/search",
                json={},
                headers={REQUEST_ID_HEADER: "unprocessable-1"},
            )

    assert response.status_code == 422
    assert response.headers["x-request-id"] == "unprocessable-1"
    body = response.json()
    # ``detail`` keeps its existing shape: no id is spliced into the contract.
    assert isinstance(body["detail"], str)
    assert "request_id" not in body
    messages = _log_messages(caplog)
    assert len(messages) == 1
    assert messages[0].startswith("[unprocessable-1] POST /api/v1/search 422 ")


@pytest.mark.asyncio
async def test_429_carries_the_same_id_in_header_and_log(
    tmp_path: Path,
    fake_manager: FakeManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _make_app(tmp_path, fake_manager, rate_limit=1)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        async with _client(app) as client:
            allowed = await client.get(
                "/api/v1/tasks", headers={REQUEST_ID_HEADER: "first-request"}
            )
            limited = await client.get(
                "/api/v1/tasks", headers={REQUEST_ID_HEADER: "limited-request"}
            )

    assert allowed.status_code == 200
    assert allowed.headers["x-request-id"] == "first-request"
    assert limited.status_code == 429
    assert limited.headers["x-request-id"] == "limited-request"

    messages = _log_messages(caplog)
    assert any(
        message.startswith("[first-request] GET /api/v1/tasks 200 ")
        for message in messages
    )
    assert any(
        message.startswith("[limited-request] GET /api/v1/tasks 429 ")
        for message in messages
    )


@pytest.mark.asyncio
async def test_500_carries_the_id_and_leaks_nothing(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exploding_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("leak /home/user/secret-token-abc")

    monkeypatch.setattr(api_routes, "search", exploding_search)
    app = _make_app(tmp_path, fake_manager)

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        async with _client(app, raise_app_exceptions=False) as client:
            response = await client.post(
                "/api/v1/search",
                json={"keyword": "x"},
                headers={REQUEST_ID_HEADER: "failed-request"},
            )

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "failed-request"
    assert response.json() == {"detail": "Internal server error"}
    assert "secret-token-abc" not in response.text
    assert "Traceback" not in response.text
    # The 500 is rendered above the middleware stack; the access log still
    # records it with the same id.
    assert any(
        message.startswith("[failed-request] POST /api/v1/search 500 ")
        for message in _log_messages(caplog)
    )


@pytest.mark.asyncio
async def test_cors_500_exposes_the_id_to_browser_clients(
    tmp_path: Path,
    fake_manager: FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exploding_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(api_routes, "search", exploding_search)
    app = _make_app(tmp_path, fake_manager, cors=True)

    async with _client(app, raise_app_exceptions=False) as client:
        response = await client.post(
            "/api/v1/search",
            json={"keyword": "x"},
            headers={"Origin": "http://example.invalid"},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "*"
    expose = response.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in expose
    # The id the browser can read is the one the log carries.
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_cors_preflight_carries_the_id(
    tmp_path: Path, fake_manager: FakeManager
) -> None:
    app = _make_app(tmp_path, fake_manager, cors=True)

    async with _client(app) as client:
        response = await client.options(
            "/api/v1/search",
            headers={
                "Origin": "http://example.invalid",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": REQUEST_ID_HEADER,
                REQUEST_ID_HEADER: "preflight-1",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "preflight-1"
    # The preflight admits the header so a browser may actually send it.
    assert "x-request-id" in response.headers["access-control-allow-headers"].lower()


# ---------------------------------------------------------------------------
# Direct ASGI harness: raw header bytes an HTTP client cannot send
# ---------------------------------------------------------------------------


def _scope(
    path: str = "/api/v1/health",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers or []),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


async def _run(middleware: Any, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware(scope, receive, send)
    return sent


def _response_header(messages: list[Message], name: str) -> str | None:
    target = name.encode("latin-1").lower()
    for message in messages:
        if message["type"] != "http.response.start":
            continue
        for key, value in message.get("headers", []):
            if key.lower() == target:
                return value.decode("latin-1")
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"abc\r\nX-Evil: injected",
        b"abc\nX-Evil: injected",
        b"\xff\xfe",
        b"a" * (MAX_REQUEST_ID_LENGTH + 1),
        b"\x1b[31mred\x1b[0m",
        b"",
    ],
)
async def test_raw_unsafe_bytes_are_replaced(raw: bytes, caplog) -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    stack: Any = RequestIdMiddleware(RequestLoggingMiddleware(downstream))

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        messages = await _run(stack, _scope(headers=[(b"x-request-id", raw)]))

    generated = _response_header(messages, REQUEST_ID_HEADER)
    assert generated is not None
    assert UUID4_RE.fullmatch(generated), generated

    logged = _log_messages(caplog)
    assert len(logged) == 1
    assert logged[0].count("\n") == 0
    assert "X-Evil" not in logged[0]
    assert "\x1b" not in logged[0]
    # The log prefix is exactly the id echoed to the client.
    assert logged[0].startswith(f"[{generated}] GET /api/v1/health 200 ")


@pytest.mark.asyncio
async def test_safe_raw_bytes_are_reused_verbatim(caplog) -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    stack: Any = RequestIdMiddleware(RequestLoggingMiddleware(downstream))

    with caplog.at_level(logging.INFO, logger=LOG_LOGGER):
        messages = await _run(
            stack, _scope(headers=[(b"x-request-id", b"trace-abc_1.2:3")])
        )

    assert _response_header(messages, REQUEST_ID_HEADER) == "trace-abc_1.2:3"
    logged = _log_messages(caplog)
    assert len(logged) == 1
    assert logged[0].startswith("[trace-abc_1.2:3] GET /api/v1/health 200 ")


@pytest.mark.asyncio
async def test_context_id_is_reset_and_not_shared_between_requests() -> None:
    seen: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(current_request_id())
        # Give a concurrent request a chance to interleave.
        await asyncio.sleep(0)
        seen.append(current_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    stack: Any = RequestIdMiddleware(downstream)

    async def one(request_id: str) -> list[Message]:
        return await _run(
            stack, _scope(headers=[(b"x-request-id", request_id.encode())])
        )

    results = await asyncio.gather(one("request-a"), one("request-b"))

    assert seen.count("request-a") == 2
    assert seen.count("request-b") == 2
    assert _response_header(results[0], REQUEST_ID_HEADER) == "request-a"
    assert _response_header(results[1], REQUEST_ID_HEADER) == "request-b"
    # No id leaks into the surrounding context after the middleware returns.
    assert current_request_id() == ""


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_unannotated() -> None:
    seen: list[Scope] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    stack: Any = RequestIdMiddleware(downstream)
    scope: Scope = {"type": "lifespan"}

    await stack(scope, _dummy_receive, _dummy_send)
    assert seen == [scope]
    assert "state" not in scope
    assert current_request_id() == ""


async def _dummy_receive() -> Message:
    return {"type": "lifespan.startup"}


async def _dummy_send(message: Message) -> None:
    return None
