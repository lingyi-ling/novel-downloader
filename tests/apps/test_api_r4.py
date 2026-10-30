#!/usr/bin/env python3
"""
tests.apps.test_api_r4
----------------------

Offline tests for the Round 4 API surface: the login credential request
endpoints backed by the in-memory credential broker
(``apps.core.cred_broker``).

Covered:

* ``GET  /api/v1/login/requests`` (empty / pending / done filtering)
* ``POST /api/v1/login/requests/{req_id}`` (success, unknown 404, done 409)
* ``GET  /api/v1/login/requests/stream`` (SSE push + drain event)
* ``WebLoginUI.prompt`` end-to-end: submitting credentials wakes the waiting
  task, prefill fallback, cookie parsing
* SDK client methods and their auth / error shapes

Everything runs through ``httpx.ASGITransport`` / ``httpx.MockTransport``; no
server is started and no real network access happens.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes
from novel_downloader.apps.api.client import ApiError, NovelApiClient
from novel_downloader.apps.api.routes import get_manager, get_watchlist_store
from novel_downloader.apps.core import cred_broker
from novel_downloader.apps.core.adapters import WebLoginUI
from novel_downloader.apps.core.cred_broker import (
    create_cred_request,
    get_req_state,
)
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.core.watchlist import WatchlistStore
from novel_downloader.schemas import LoginField

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep app state/config out of the real environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)
    monkeypatch.delenv("NOVEL_API_WEBHOOK", raising=False)


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Give every test a pristine credential broker.

    The broker keeps a module-global request store and an ``asyncio.Lock``
    created at import time; a contended lock binds itself to the first event
    loop that queues on it, which would poison later tests on a fresh loop.
    """
    monkeypatch.setattr(cred_broker, "_CRED_LOCK", asyncio.Lock())
    monkeypatch.setattr(cred_broker, "_CRED_REQS", {})


class RecordingManager:
    """Task manager stand-in that records whether a task was queued."""

    def __init__(self) -> None:
        self.added: list[dict[str, str]] = []

    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        self.added.append({"title": title, "site": site, "book_id": book_id})
        return DownloadTask(title=title, site=site, book_id=book_id)

    async def cancel_task(self, task_id: str) -> bool:
        return False

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        return {"pending": [], "running": [], "completed": []}

    async def close(self) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


@pytest.fixture
def manager() -> RecordingManager:
    return RecordingManager()


@pytest.fixture
def app(tmp_path: Path, store: WatchlistStore, manager: RecordingManager) -> FastAPI:
    application = create_app(output_dir=tmp_path / "downloads")
    application.dependency_overrides[get_watchlist_store] = lambda: store
    application.dependency_overrides[get_manager] = lambda: manager
    return application


@asynccontextmanager
async def _client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


def _field(name: str, type_: str = "text", **kwargs: Any) -> LoginField:
    return LoginField(
        name=name,
        label=kwargs.pop("label", f"{name}.label"),
        type=type_,  # type: ignore[arg-type]
        required=kwargs.pop("required", False),
        **kwargs,
    )


async def _wait_for_total(
    client: httpx.AsyncClient, want: int, timeout: float = 5.0
) -> dict[str, Any]:
    """Poll the login request list until ``want`` requests are pending."""
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get("/api/v1/login/requests")
        payload = response.json()
        if response.status_code == 200 and payload["total"] >= want:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {want} pending request(s), last={payload!r}")


# ---------------------------------------------------------------------------
# GET /login/requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_requests_empty(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/api/v1/login/requests")

    assert response.status_code == 200
    assert response.json() == {"total": 0, "requests": []}


@pytest.mark.asyncio
async def test_login_requests_lists_pending_and_hides_done(app: FastAPI) -> None:
    pending = await create_cred_request(
        task_id="task-1",
        title="斗破苍穹",
        fields=[
            _field(
                "username",
                required=True,
                default="admin",
                placeholder="user@example.com",
                description="login.username.description",
            ),
            _field("password", "password", required=True),
        ],
        prefill={"username": "admin"},
    )
    done = await create_cred_request(
        task_id="task-2", title="Other", fields=[], prefill={}
    )
    await cred_broker.complete_request(done.req_id, None)

    async with _client(app) as client:
        response = await client.get("/api/v1/login/requests")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    # Labels / placeholders / descriptions are passed through verbatim: the
    # server does not translate them (labels here are raw t()-style strings).
    assert payload["requests"] == [
        {
            "req_id": pending.req_id,
            "task_id": "task-1",
            "title": "斗破苍穹",
            "fields": [
                {
                    "name": "username",
                    "label": "username.label",
                    "type": "text",
                    "required": True,
                    "default": "admin",
                    "placeholder": "user@example.com",
                    "description": "login.username.description",
                },
                {
                    "name": "password",
                    "label": "password.label",
                    "type": "password",
                    "required": True,
                    "default": "",
                    "placeholder": "",
                    "description": "",
                },
            ],
            "prefill": {"username": "admin"},
            "done": False,
        }
    ]


# ---------------------------------------------------------------------------
# POST /login/requests/{req_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_login_resolves_waiting_prompt(app: FastAPI) -> None:
    task = DownloadTask(title="斗破苍穹", site="qidian", book_id="123456")
    ui = WebLoginUI(task)
    fields = [
        _field("username", required=True),
        _field("password", "password", required=True),
        _field("cookies", "cookie"),
    ]
    prompt_task = asyncio.create_task(
        ui.prompt(fields, {"username": "  cached_user  "})
    )

    async with _client(app) as client:
        payload = await _wait_for_total(client, 1)
        entry = payload["requests"][0]
        assert entry["task_id"] == task.task_id
        assert entry["title"] == "斗破苍穹"
        req_id = entry["req_id"]

        submitted = await client.post(
            f"/api/v1/login/requests/{req_id}",
            json={
                "values": {
                    "password": "  s3cret  ",
                    "cookies": "token=abc; uid=42",
                }
            },
        )
        assert submitted.status_code == 200
        assert submitted.json() == {"ok": True}

        result = await asyncio.wait_for(prompt_task, timeout=5)

    # Submitted values override prefill (and are stripped); the cookie field
    # was parsed exactly like the Web UI used to.
    assert result == {
        "username": "cached_user",
        "password": "s3cret",
        "cookies": {"token": "abc", "uid": "42"},
    }

    # The task reaped the request after consuming the result: a replay is 404.
    async with _client(app) as client:
        replay = await client.post(
            f"/api/v1/login/requests/{req_id}", json={"values": {}}
        )
    assert replay.status_code == 404


@pytest.mark.asyncio
async def test_submit_login_unknown_request_404(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/login/requests/deadbeef", json={"values": {"password": "x"}}
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Login request not found: deadbeef"}


@pytest.mark.asyncio
async def test_submit_login_twice_returns_409(app: FastAPI) -> None:
    """A done-but-not-yet-reaped request answers 409 instead of 404."""
    req = await create_cred_request(task_id="t1", title="Book", fields=[])

    async with _client(app) as client:
        first = await client.post(
            f"/api/v1/login/requests/{req.req_id}",
            json={"values": {"password": "x"}},
        )
        second = await client.post(
            f"/api/v1/login/requests/{req.req_id}",
            json={"values": {"password": "y"}},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json() == {"detail": f"Login request already completed: {req.req_id}"}
    assert await get_req_state(req.req_id) == (True, True)


@pytest.mark.asyncio
async def test_submit_login_empty_body_defaults_to_empty_values(
    app: FastAPI,
) -> None:
    req = await create_cred_request(task_id="t1", title="Book", fields=[])

    async with _client(app) as client:
        response = await client.post(f"/api/v1/login/requests/{req.req_id}", json={})

    assert response.status_code == 200
    assert req.done is True
    assert req.result == {}


@pytest.mark.asyncio
async def test_submit_login_validates_values_type(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/login/requests/whatever", json={"values": "not-a-dict"}
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /login/requests/stream
# ---------------------------------------------------------------------------


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
async def test_login_requests_stream_http_envelope(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint wraps the broker-polling generator in an SSE response.

    ``httpx.ASGITransport`` buffers a response until the ASGI app returns, so
    an endless stream cannot be consumed through it. The real (infinite) loop
    is exercised directly in
    :func:`test_login_request_event_stream_pushes_and_drains`; here a finite
    stand-in verifies the HTTP envelope.
    """
    calls: dict[str, Any] = {}
    frame = api_routes._sse_frame("requests", [{"req_id": "r1"}])

    async def fake_stream(interval: float) -> Any:
        calls["interval"] = interval
        yield frame
        yield frame

    monkeypatch.setattr(api_routes, "_login_request_event_stream", fake_stream)

    async with (
        _client(app) as client,
        client.stream("GET", "/api/v1/login/requests/stream") as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [event for event, _ in events] == ["requests", "requests"]
    assert events[0][1] == [{"req_id": "r1"}]
    assert calls["interval"] == api_routes.LOGIN_STREAM_INTERVAL


@pytest.mark.asyncio
async def test_login_request_event_stream_pushes_and_drains(app: FastAPI) -> None:
    """The real loop pushes pending requests, then one final empty frame."""
    req = await create_cred_request(
        task_id="t1", title="Book", fields=[], prefill={"username": "u"}
    )
    stream = api_routes._login_request_event_stream(0.01)

    first = _parse_sse(await anext(stream))
    assert len(first) == 1
    assert first[0][0] == "requests"
    assert [item["req_id"] for item in first[0][1]] == [req.req_id]
    assert first[0][1][0]["prefill"] == {"username": "u"}

    # Completing the request drains the queue; the next tick pushes one final
    # empty frame so clients can clear their state.
    await cred_broker.complete_request(req.req_id, {"password": "x"})
    second = _parse_sse(await anext(stream))
    await stream.aclose()

    assert len(second) == 1
    assert second[0] == ("requests", [])


@pytest.mark.asyncio
async def test_login_request_event_stream_silent_when_empty() -> None:
    """With nothing pending the loop pushes no event at all."""
    stream = api_routes._login_request_event_stream(0.01)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(stream), timeout=0.5)
    await stream.aclose()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_endpoints_require_token(tmp_path: Path) -> None:
    application = create_app(token="secret", output_dir=tmp_path / "downloads")

    async with _client(application) as client:
        missing = await client.get("/api/v1/login/requests")
        wrong = await client.post(
            "/api/v1/login/requests/abc",
            json={"values": {}},
            headers={"Authorization": "Bearer nope"},
        )
        async with client.stream("GET", "/api/v1/login/requests/stream") as stream:
            stream_body = await stream.aread()
        allowed = await client.get(
            "/api/v1/login/requests", headers={"X-API-Key": "secret"}
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "Invalid or missing API token"}
    assert wrong.status_code == 401
    assert stream.status_code == 401
    assert json.loads(stream_body)["detail"] == "Invalid or missing API token"
    assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# SDK client
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(handler: Handler) -> NovelApiClient:
    return NovelApiClient(
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
    )


def test_client_login_methods() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={"total": 1, "requests": [{"req_id": "r1", "title": "Book"}]},
        )

    with _make_client(handler) as client:
        listing = client.login_requests()
        ok = client.submit_login("r 1", {"password": "x"})

    assert listing["total"] == 1
    assert ok == {"ok": True}
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v1/login/requests"
    assert requests[1].method == "POST"
    assert requests[1].url.raw_path == b"/api/v1/login/requests/r%201"
    assert json.loads(requests[1].content) == {"values": {"password": "x"}}


def test_client_submit_login_defaults_to_empty_values() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as client:
        client.submit_login("r1")

    assert json.loads(requests[0].content) == {"values": {}}


def test_client_submit_login_error_shapes() -> None:
    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Login request not found: r1"})

    with _make_client(handler_404) as client, pytest.raises(ApiError) as not_found:
        client.submit_login("r1", {})
    assert not_found.value.status_code == 404

    def handler_409(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"detail": "Login request already completed: r1"}
        )

    with _make_client(handler_409) as client, pytest.raises(ApiError) as conflict:
        client.submit_login("r1", {})
    assert conflict.value.status_code == 409


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_broker_reset_between_tests() -> None:
    """The autouse broker reset gives every test an empty store."""
    assert cred_broker._CRED_REQS == {}
