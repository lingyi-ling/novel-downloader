"""Session lifecycle, isolation and concurrency hardening tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp.cookiejar
import aiohttp.web
import pytest
import pytest_asyncio

from novel_downloader.infra.sessions import create_session
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]


@pytest.fixture(autouse=True)
def _allow_ip_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiohttp.cookiejar, "is_ip_address", lambda host: False)


@pytest.fixture
def cfg() -> FetcherConfig:
    return FetcherConfig(
        timeout=5,
        headers={"User-Agent": "pytest"},
        trust_env=False,
        verify_ssl=False,
        max_connections=10,
        impersonate="chrome",
    )


@pytest_asyncio.fixture
async def echo_server(aiohttp_server) -> AsyncIterator[Any]:
    async def echo(request: aiohttp.web.Request) -> aiohttp.web.Response:
        await asyncio.sleep(0.01)
        return aiohttp.web.json_response(
            {
                "cookie": request.headers.get("Cookie", ""),
                "q": request.query.get("q", ""),
            }
        )

    async def set_cookie(request: aiohttp.web.Request) -> aiohttp.web.Response:
        response = aiohttp.web.Response(text="ok")
        response.set_cookie(
            request.query.get("name", "token"),
            request.query.get("value", "abc123"),
            path="/",
        )
        return response

    app = aiohttp.web.Application()
    app.router.add_get("/echo", echo)
    app.router.add_get("/set-cookie", set_cookie)
    server = await aiohttp_server(app)
    return server


def _create(backend: str, cfg: FetcherConfig, cookies: dict[str, str] | None = None):
    return create_session(backend, cfg, cookies=cookies)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_close_is_idempotent(backend: str, cfg: FetcherConfig) -> None:
    session = _create(backend, cfg)
    await session.init()

    await session.close()
    await session.close()
    await session.close()

    assert session._session is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_nested_context_managers_keep_outer_session_alive(
    backend: str, cfg: FetcherConfig, echo_server: Any
) -> None:
    """Inner ``async with`` blocks must not close the shared session.

    Helpers wrapped in ``async with session:`` are routinely called from
    code that already holds the same context, so the session is only
    released when the outermost block exits.
    """
    url = str(echo_server.make_url("/echo"))
    session = _create(backend, cfg, cookies={"account": "nested"})

    async with session:
        async with session:
            response = await session.get(url)
        assert response.json()["q"] == ""

        # The inner exit must not have torn down the connection pool.
        response = await session.get(url, params={"q": "still-alive"})
        assert response.json()["q"] == "still-alive"

    assert session._session is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_close_without_init_is_safe(backend: str, cfg: FetcherConfig) -> None:
    session = _create(backend, cfg)
    await session.close()
    await session.close()
    assert session._session is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_concurrent_close_is_safe(backend: str, cfg: FetcherConfig) -> None:
    session = _create(backend, cfg)
    await session.init()
    await asyncio.gather(session.close(), session.close(), session.close())
    assert session._session is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_init_is_idempotent(backend: str, cfg: FetcherConfig) -> None:
    session = _create(backend, cfg)
    await session.init()
    first = session._session
    await session.init()
    assert session._session is first
    await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_reinit_after_close_creates_new_backend_session(
    backend: str, cfg: FetcherConfig
) -> None:
    session = _create(backend, cfg)
    await session.init()
    first = session._session
    await session.close()

    await session.init()
    assert session._session is not None
    assert session._session is not first
    await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_access_after_close_raises(backend: str, cfg: FetcherConfig) -> None:
    session = _create(backend, cfg, cookies={"token": "secret"})
    await session.init()
    await session.close()

    with pytest.raises(RuntimeError):
        _ = session.session  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        await session.get("http://127.0.0.1:1/never")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_input_cookie_mapping_is_copied(backend: str, cfg: FetcherConfig) -> None:
    shared = {"token": "first"}
    session = _create(backend, cfg, cookies=shared)
    shared["token"] = "second"

    await session.init()
    assert session.get_cookie("token") == "first"
    await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_sessions_sharing_input_mapping_are_isolated(
    backend: str, cfg: FetcherConfig
) -> None:
    shared = {"account": "alice"}
    alice = _create(backend, cfg, cookies=shared)
    bob = _create(backend, cfg, cookies=shared)
    await alice.init()
    await bob.init()

    alice.update_cookies({"account": "alice-2"})

    assert alice.get_cookie("account") == "alice-2"
    assert bob.get_cookie("account") == "alice"
    assert shared == {"account": "alice"}

    await alice.close()
    await bob.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_two_sessions_do_not_share_cookies(
    backend: str, cfg: FetcherConfig, echo_server: Any
) -> None:
    url = str(echo_server.make_url("/echo"))

    session_a = _create(backend, cfg, cookies={"account": "alice"})
    session_b = _create(backend, cfg, cookies={"account": "bob"})
    await session_a.init()
    await session_b.init()

    try:
        response_a, response_b = await asyncio.gather(
            session_a.get(url), session_b.get(url)
        )
    finally:
        await session_a.close()
        await session_b.close()

    cookie_a = response_a.json()["cookie"]
    cookie_b = response_b.json()["cookie"]
    assert "account=alice" in cookie_a
    assert "account=bob" not in cookie_a
    assert "account=bob" in cookie_b
    assert "account=alice" not in cookie_b


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_session_reuse_under_concurrency(
    backend: str, cfg: FetcherConfig, echo_server: Any
) -> None:
    session = _create(backend, cfg, cookies={"account": "concurrent"})
    await session.init()
    base = str(echo_server.make_url("/echo"))

    try:
        responses = await asyncio.gather(
            *(session.get(f"{base}?q={index}") for index in range(25))
        )
    finally:
        await session.close()

    assert [response.json()["q"] for response in responses] == [
        str(index) for index in range(25)
    ]
    assert all(
        "account=concurrent" in response.json()["cookie"] for response in responses
    )


@pytest.mark.asyncio
async def test_session_operations_do_not_log_cookie_values(
    cfg: FetcherConfig,
    echo_server: Any,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "DO-NOT-LOG-THIS-COOKIE"
    url = str(echo_server.make_url(f"/set-cookie?name=token&value={secret}"))

    session = _create("aiohttp", cfg)
    await session.init()
    try:
        with caplog.at_level(logging.DEBUG, logger="novel_downloader.infra"):
            await session.get(url)
            assert session.get_cookie("token") == secret
            assert session.save_cookies(tmp_path)
            session.clear_cookies()
            assert session.load_cookies(tmp_path)
    finally:
        await session.close()

    assert secret not in caplog.text
    assert all(record.name.startswith("novel_downloader") for record in caplog.records)
