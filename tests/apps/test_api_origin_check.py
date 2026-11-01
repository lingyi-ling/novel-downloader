#!/usr/bin/env python3
"""
tests.apps.test_api_origin_check
--------------------------------

Offline tests for the browser-origin gate on the local REST API
(:class:`novel_downloader.apps.api.middleware.OriginCheckMiddleware`).

Threat model: by default the API binds to ``127.0.0.1`` without a token. That
stops *remote* clients, but not *browser* ones — a website the user visits can
still fire cross-origin requests at ``http://127.0.0.1:<port>`` (browsers
always attach ``Origin`` to cross-origin POSTs, and a DNS-rebinding page can
even read the answers). The gate answers every request whose ``Origin`` is not
a loopback origin with ``403``:

* non-browser clients (CLI, SDK, curl) send no ``Origin`` and pass unchanged;
* loopback origins (``http://127.0.0.1:any``, ``http://localhost``, ``[::1]``)
  pass, so locally served pages keep working;
* look-alike hosts (``127.0.0.1.evil.com``) and ``Origin: null`` are rejected;
* ``--cors`` keeps its documented meaning: the gate is not installed, CORS
  itself governs what browsers may do.

All requests go through ``httpx.ASGITransport`` against the real ``create_app``
stack; no server is started and no network access happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from novel_downloader.apps.api import create_app

_HEALTH = "/api/v1/health"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application state/config outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> httpx.AsyncClient:
    """Default app: localhost bind, no token, no CORS."""
    application = create_app(output_dir=tmp_path / "downloads")
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


async def _get(
    tmp_path: Path, *, cors: bool = False, token: str | None = None, **headers: str
) -> httpx.Response:
    """One request through a freshly built app (helper for CORS/token cases)."""
    application: FastAPI = create_app(
        output_dir=tmp_path / "downloads", cors=cors, token=token
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        return await http_client.get(_HEALTH, headers=headers)


@pytest.mark.asyncio
async def test_request_without_origin_passes(client: httpx.AsyncClient) -> None:
    """Non-browser clients (CLI, SDK, curl) never send ``Origin``."""
    response = await client.get(_HEALTH)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8000",
        "http://127.0.0.1",
        "http://localhost:5500",
        "https://localhost",
        "http://[::1]:8000",
    ],
)
async def test_loopback_origins_pass(client: httpx.AsyncClient, origin: str) -> None:
    """Locally served pages (any loopback host, any port) keep working."""
    response = await client.get(_HEALTH, headers={"Origin": origin})
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "https://music.163.com",
        "http://127.0.0.1.evil.com",
        "http://localhost.evil.com",
        "http://evil.com@127.0.0.1",  # userinfo cannot smuggle a local host
        "null",  # sandboxed iframes / file:// pages
        "",
    ],
)
async def test_non_local_origins_are_rejected(
    client: httpx.AsyncClient, origin: str
) -> None:
    """A website must not be able to drive the tokenless local API."""
    response = await client.get(_HEALTH, headers={"Origin": origin})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()
    # Error responses must never be cacheable.
    assert "no-store" in response.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_rejection_applies_to_downloads_mount(
    client: httpx.AsyncClient,
) -> None:
    """The gate also covers the ``/downloads`` static mount (file reads)."""
    response = await client.get(
        "/downloads/whatever.txt", headers={"Origin": "http://evil.com"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_gate_runs_before_token_check(tmp_path: Path) -> None:
    """Layering: a forged origin is rejected even when a token is configured."""
    response = await _get(
        tmp_path, token="secret-token", **{"Origin": "http://evil.com"}
    )
    assert response.status_code == 403  # not 401: origin gate sits outside auth


@pytest.mark.asyncio
async def test_cors_mode_disables_the_gate(tmp_path: Path) -> None:
    """``--cors`` is the explicit opt-in for cross-origin browser access."""
    response = await _get(tmp_path, cors=True, **{"Origin": "http://evil.com"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


@pytest.mark.asyncio
async def test_gate_leaves_websocket_scopes_alone(tmp_path: Path) -> None:
    """The middleware must pass through non-HTTP ASGI scopes untouched."""
    from novel_downloader.apps.api.middleware import OriginCheckMiddleware

    seen: dict[str, Any] = {}

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen["scope"] = scope

    gate = OriginCheckMiddleware(inner)
    await gate({"type": "websocket", "path": "/ws"}, None, None)  # type: ignore[arg-type]
    assert seen["scope"]["type"] == "websocket"
