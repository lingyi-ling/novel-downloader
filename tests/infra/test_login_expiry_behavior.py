"""Lock the session-layer behaviour for 301/401/500 responses.

The session classes do not perform login-expiry detection or re-login; those
decisions live in the site fetchers (``_check_login_status``).  These tests
make sure responses are surfaced unchanged and that cookies are not cleared
implicitly when a request fails with 401.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp.web
import pytest
import pytest_asyncio

from novel_downloader.infra.sessions import create_session
from novel_downloader.schemas import FetcherConfig


@pytest.fixture
def cfg() -> FetcherConfig:
    return FetcherConfig(
        timeout=5,
        headers={"User-Agent": "pytest"},
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
    )


@pytest_asyncio.fixture
async def status_server(aiohttp_server) -> AsyncIterator[Any]:
    counts = {"unauthorized": 0, "redirect": 0, "error": 0}

    async def unauthorized(request: aiohttp.web.Request) -> aiohttp.web.Response:
        counts["unauthorized"] += 1
        return aiohttp.web.Response(text="login required", status=401)

    async def redirect(request: aiohttp.web.Request) -> aiohttp.web.Response:
        counts["redirect"] += 1
        return aiohttp.web.Response(
            headers={"Location": "/ok"}, status=302, text="moved"
        )

    async def error(request: aiohttp.web.Request) -> aiohttp.web.Response:
        counts["error"] += 1
        return aiohttp.web.Response(text="boom", status=500)

    async def ok(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="hello", status=200)

    app = aiohttp.web.Application()
    app.router.add_get("/unauthorized", unauthorized)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/error", error)
    app.router.add_get("/ok", ok)
    server = await aiohttp_server(app)
    server.counts = counts
    return server


@pytest.mark.asyncio
async def test_401_is_returned_without_retry_or_cookie_change(
    cfg: FetcherConfig, status_server: Any
) -> None:
    session = create_session("aiohttp", cfg, cookies={"token": "keep-me"})
    await session.init()
    try:
        response = await session.get(str(status_server.make_url("/unauthorized")))
        assert response.status == 401
        assert response.ok is False
        assert response.text == "login required"
        assert status_server.counts["unauthorized"] == 1
        # No implicit logout / cookie clearing.
        assert session.get_cookie("token") == "keep-me"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_500_is_returned_without_session_level_retry(
    cfg: FetcherConfig, status_server: Any
) -> None:
    session = create_session("aiohttp", cfg)
    await session.init()
    try:
        response = await session.get(str(status_server.make_url("/error")))
        assert response.status == 500
        assert response.ok is False
        assert status_server.counts["error"] == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_redirect_is_surfaced_when_disabled(
    cfg: FetcherConfig, status_server: Any
) -> None:
    session = create_session("aiohttp", cfg)
    await session.init()
    try:
        response = await session.get(
            str(status_server.make_url("/redirect")), allow_redirects=False
        )
        assert response.status == 302
        assert response.headers["location"] == "/ok"
        assert response.ok is True  # 3xx remains a "non-error" for this helper
        assert status_server.counts["redirect"] == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_redirect_is_followed_by_default(
    cfg: FetcherConfig, status_server: Any
) -> None:
    session = create_session("aiohttp", cfg)
    await session.init()
    try:
        response = await session.get(str(status_server.make_url("/redirect")))
        assert response.status == 200
        assert response.text == "hello"
    finally:
        await session.close()
