#!/usr/bin/env python3
"""
tests.apps.test_api_rate_limit
------------------------------

Offline tests for the per-IP sliding-window rate limiter
(``novel_downloader.apps.api.middleware.RateLimitMiddleware``).

Two complementary harnesses are used, mirroring ``test_api_middleware.py``:

* end-to-end requests through ``httpx.ASGITransport`` against the real
  ``create_app`` stack, proving the middleware sits inside the CORS layer and
  outside the token auth, and
* the :class:`RateLimitMiddleware` driven directly with hand-crafted ASGI
  scopes, which is the only way to vary the client IP and the clock
  deterministically.

Time is controlled by monkeypatching the module-level ``_monotonic`` hook, so
no test ever sleeps. No server is started and no network access happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import main as api_main_module
from novel_downloader.apps.api import middleware as api_middleware
from novel_downloader.apps.api.auth import AUTH_ERROR_DETAIL
from novel_downloader.apps.api.main import _build_parser
from novel_downloader.apps.api.middleware import (
    DEFAULT_RATE_LIMIT,
    DEFAULT_RATE_LIMIT_WINDOW,
    RATE_LIMIT_EXEMPT_PATHS,
    RateLimitMiddleware,
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


class FakeClock:
    """Deterministic ``time.monotonic`` replacement."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Freeze the rate limiter's clock at a fixed, controllable instant."""
    fake = FakeClock()
    monkeypatch.setattr(api_middleware, "_monotonic", fake)
    return fake


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
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> httpx.AsyncClient:
    """Client against an app limited to 2 requests per window."""
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, rate_limit=2),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# RateLimitMiddleware: direct ASGI harness
# ---------------------------------------------------------------------------


def _limited(**kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """Wrap a counting downstream app in the middleware under test."""
    store: dict[str, Any] = {"downstream": 0}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        store["downstream"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return RateLimitMiddleware(downstream, **kwargs), store


def _scope(
    client: tuple[str, int] = ("10.0.0.1", 5000),
    path: str = "/api/v1/tasks",
    method: str = "GET",
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
        "headers": [],
        "client": client,
        "server": ("testserver", 80),
    }


async def _receive_no_body() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _run(mw: Any, **scope_kwargs: Any) -> tuple[int, dict[str, str], bytes]:
    """Send one request through the middleware and capture the response."""
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await mw(_scope(**scope_kwargs), _receive_no_body, send)
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    headers = {
        name.decode().lower(): value.decode()
        for name, value in start.get("headers", [])
    }
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), headers, body


@pytest.mark.asyncio
async def test_requests_within_limit_pass(clock: FakeClock) -> None:
    mw, store = _limited(limit=3)

    for _ in range(3):
        status, _, _ = await _run(mw)
        assert status == 200

    assert store["downstream"] == 3


@pytest.mark.asyncio
async def test_over_limit_returns_429_with_retry_after(clock: FakeClock) -> None:
    mw, store = _limited(limit=2)

    assert (await _run(mw))[0] == 200
    assert (await _run(mw))[0] == 200
    status, headers, body = await _run(mw)

    assert status == 429
    assert headers["content-type"].startswith("application/json")
    assert "retry-after" in headers
    assert int(headers["retry-after"]) >= 1
    retry_after = int(headers["retry-after"])
    assert body.decode() == (
        f'{{"detail":"Rate limit exceeded. Try again in {retry_after}s."}}'
    )
    # The rejected request never reached the application.
    assert store["downstream"] == 2


@pytest.mark.asyncio
async def test_retry_after_names_the_window_expiry(clock: FakeClock) -> None:
    mw, _ = _limited(limit=2, window=60.0)

    await _run(mw)  # t=1000.0
    await _run(mw)
    clock.advance(3)  # t=1003.0: both hits still inside the window
    status, headers, _ = await _run(mw)

    assert status == 429
    # The oldest hit slides out at t=1060, i.e. 57 seconds from now.
    assert headers["retry-after"] == "57"


@pytest.mark.asyncio
async def test_rejected_requests_are_not_counted(clock: FakeClock) -> None:
    """Hammering must not extend the lockout past the original window."""
    mw, store = _limited(limit=2, window=60.0)

    await _run(mw)
    await _run(mw)
    clock.advance(5)
    for _ in range(10):  # ten rejected requests over 40 fake seconds
        clock.advance(4)
        status, _, _ = await _run(mw)
        assert status == 429
    assert store["downstream"] == 2

    clock.advance(11 + 5)  # t=1096: the two admitted hits are now out of range
    status, _, _ = await _run(mw)
    assert status == 200
    assert store["downstream"] == 3


@pytest.mark.asyncio
async def test_clients_are_counted_independently(clock: FakeClock) -> None:
    mw, store = _limited(limit=1)

    assert (await _run(mw, client=("10.0.0.1", 5000)))[0] == 200
    assert (await _run(mw, client=("10.0.0.1", 5001)))[0] == 429  # same IP, new port
    assert (await _run(mw, client=("10.0.0.2", 5000)))[0] == 200
    assert (await _run(mw, client=("10.0.0.3", 5000)))[0] == 200
    assert store["downstream"] == 3


@pytest.mark.asyncio
async def test_requests_without_client_count_as_unknown(clock: FakeClock) -> None:
    mw, store = _limited(limit=1)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    scope = _scope()
    del scope["client"]

    await mw(scope, _receive_no_body, send)
    await mw(scope, _receive_no_body, send)

    statuses = [
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    ]
    assert statuses == [200, 429]
    assert store["downstream"] == 1


@pytest.mark.asyncio
async def test_window_slide_resets_the_counter(clock: FakeClock) -> None:
    mw, _ = _limited(limit=1, window=60.0)

    assert (await _run(mw))[0] == 200  # t=1000
    clock.advance(0.5)
    assert (await _run(mw))[0] == 429  # t=1000.5, still inside the window

    clock.advance(59.5)  # t=1060: the admitted hit just slid out
    assert (await _run(mw))[0] == 200

    clock.advance(0.1)  # the new hit is now the only one inside the window
    assert (await _run(mw))[0] == 429


@pytest.mark.asyncio
async def test_health_endpoint_is_exempt(clock: FakeClock) -> None:
    mw, store = _limited(limit=1)

    for _ in range(5):
        status, _, _ = await _run(mw, path="/api/v1/health")
        assert status == 200

    # The exempt requests consumed no budget at all.
    assert (await _run(mw))[0] == 200
    assert (await _run(mw))[0] == 429
    assert store["downstream"] == 6


@pytest.mark.asyncio
async def test_health_trailing_slash_is_exempt_but_lookalike_is_not(
    clock: FakeClock,
) -> None:
    mw, _ = _limited(limit=1)

    assert (await _run(mw, path="/api/v1/health/"))[0] == 200
    assert (await _run(mw, path="/api/v1/health"))[0] == 200

    # A look-alike path is an ordinary, limited endpoint.
    assert (await _run(mw, path="/api/v1/healthz"))[0] == 200
    assert (await _run(mw, path="/api/v1/healthz"))[0] == 429


@pytest.mark.asyncio
async def test_limit_zero_disables_the_middleware(clock: FakeClock) -> None:
    mw, store = _limited(limit=0)

    for _ in range(130):
        status, _, _ = await _run(mw)
        assert status == 200

    assert store["downstream"] == 130
    assert mw._hits == {}


@pytest.mark.asyncio
async def test_cleanup_drops_stale_ips_and_keeps_fresh_ones(clock: FakeClock) -> None:
    mw, _ = _limited(limit=5, window=60.0)

    await _run(mw, client=("10.0.0.1", 5000))  # t=1000: first periodic cleanup
    await _run(mw, client=("10.0.0.2", 5000))
    clock.advance(30)
    await _run(mw, client=("10.0.0.3", 5000))  # t=1030: no cleanup mid-window
    assert set(mw._hits) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}

    clock.advance(31)  # t=1061: entries older than the window are dropped
    status, _, _ = await _run(mw, client=("10.0.0.3", 5000))
    assert status == 200
    assert set(mw._hits) == {"10.0.0.3"}


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched(clock: FakeClock) -> None:
    mw, store = _limited(limit=1)
    scope: Scope = {"type": "lifespan"}

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        store.setdefault("messages", []).append(message)

    await mw(scope, receive, send)

    assert store["downstream"] == 1


def test_defaults_match_the_documented_contract() -> None:
    assert DEFAULT_RATE_LIMIT == 120
    assert DEFAULT_RATE_LIMIT_WINDOW == 60.0
    assert list(RATE_LIMIT_EXEMPT_PATHS) == ["/api/v1/health"]


# ---------------------------------------------------------------------------
# Rate limiting through the real application stack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_stack_limits_at_120_per_minute(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager), client=("203.0.113.7", 1234)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        for _ in range(120):
            response = await http_client.get("/api/v1/tasks")
            assert response.status_code == 200
        rejected = await http_client.get("/api/v1/tasks")

    assert rejected.status_code == 429
    assert rejected.json() == {"detail": "Rate limit exceeded. Try again in 60s."}
    assert int(rejected.headers["retry-after"]) == 60


@pytest.mark.asyncio
async def test_stack_within_limit_then_429(
    limited_client: httpx.AsyncClient,
) -> None:
    assert (await limited_client.get("/api/v1/tasks")).status_code == 200
    assert (await limited_client.get("/api/v1/tasks")).status_code == 200

    rejected = await limited_client.get("/api/v1/tasks")
    assert rejected.status_code == 429
    assert rejected.json() == {"detail": "Rate limit exceeded. Try again in 60s."}
    assert int(rejected.headers["retry-after"]) == 60


@pytest.mark.asyncio
async def test_rate_limit_zero_disables_end_to_end(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, rate_limit=0),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        for _ in range(130):
            response = await http_client.get("/api/v1/tasks")
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_window_is_configurable(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, rate_limit=1, rate_limit_window=5),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        assert (await http_client.get("/api/v1/tasks")).status_code == 200
        rejected = await http_client.get("/api/v1/tasks")
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "5"

        clock.advance(4.9)  # t=1004.9: the window has not fully slid yet
        assert (await http_client.get("/api/v1/tasks")).status_code == 429

        clock.advance(0.1)  # t=1005: the admitted hit slid out of the window
        assert (await http_client.get("/api/v1/tasks")).status_code == 200


@pytest.mark.asyncio
async def test_health_is_exempt_end_to_end(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, rate_limit=1),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        assert (await http_client.get("/api/v1/tasks")).status_code == 200
        assert (await http_client.get("/api/v1/tasks")).status_code == 429

        # Health keeps working even while the client is locked out.
        for _ in range(3):
            response = await http_client.get("/api/v1/health")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Ordering: rate limit vs. auth, CORS and cache policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_runs_before_token_auth(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, token="secret-token", rate_limit=2),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        first = await http_client.get("/api/v1/tasks")
        second = await http_client.get("/api/v1/tasks")
        third = await http_client.get("/api/v1/tasks")

    assert first.status_code == 401  # unauthenticated: denied by the router
    assert first.json()["detail"] == AUTH_ERROR_DETAIL
    assert second.status_code == 401
    # The third is 429, not 401: the counter runs *before* the token check,
    # so brute-forcing the token cannot proceed unthrottled.
    assert third.status_code == 429


@pytest.mark.asyncio
async def test_429_keeps_cors_headers(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, cors=True, rate_limit=2),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        for _ in range(2):
            await http_client.get(
                "/api/v1/tasks", headers={"Origin": "http://example.invalid"}
            )
        rejected = await http_client.get(
            "/api/v1/tasks", headers={"Origin": "http://example.invalid"}
        )

    assert rejected.status_code == 429
    # The limiter sits inside the CORS layer, so its 429 keeps the headers.
    assert rejected.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_429_is_not_cached(
    tmp_path: Path,
    fake_manager: FakeManager,
    clock: FakeClock,
) -> None:
    transport = httpx.ASGITransport(
        app=_make_app(tmp_path, fake_manager, rate_limit=1),
        client=("203.0.113.7", 1234),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        await http_client.get("/api/v1/tasks")
        rejected = await http_client.get("/api/v1/tasks")

    assert rejected.status_code == 429
    assert rejected.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_parser_defaults() -> None:
    args = vars(_build_parser().parse_args([]))
    assert args["rate_limit"] == 120
    assert args["rate_limit_window"] == 60.0


def test_api_main_forwards_rate_limit_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()  # uvicorn.run is stubbed out below anyway

    def fake_run(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(api_main_module, "create_app", fake_create_app)
    monkeypatch.setattr(api_main_module.uvicorn, "run", fake_run)

    api_main_module.api_main(["--rate-limit", "30", "--rate-limit-window", "10"])
    assert captured["rate_limit"] == 30
    assert captured["rate_limit_window"] == 10.0

    api_main_module.api_main(["--rate-limit", "0"])
    assert captured["rate_limit"] == 0
    assert captured["rate_limit_window"] == 60.0
