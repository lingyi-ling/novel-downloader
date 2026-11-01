"""Backend parity gaps found by the HTTP session audit.

Unlike ``test_backends_consistency.py`` this module deliberately does NOT
patch ``aiohttp.cookiejar.is_ip_address``: the tests below reproduce the
production behaviour of cookie jars on IP-host origins (127.0.0.1 servers),
which is exactly where the three backends used to drift.

Each section covers one audited drift:

* redirect chains: unified limit and unified exception type,
* corrupt ``Content-Encoding`` bodies: unified ``ConnectionError``,
* backend-specific exception translation (including per-request kwargs),
* cookie scope on IP-host origins.

The ``_InjectableGet`` fakes drive the wrapper directly with a backend
exception so the translation table is exercised without needing a live
server that happens to fail.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import aiohttp.web
import httpx
import pytest
import pytest_asyncio
from curl_cffi.requests.exceptions import (
    ChunkedEncodingError as CurlChunkedEncodingError,
)
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
)
from curl_cffi.requests.exceptions import (
    ContentDecodingError as CurlContentDecodingError,
)
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError
from curl_cffi.requests.exceptions import ProxyError as CurlProxyError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from curl_cffi.requests.exceptions import TooManyRedirects as CurlTooManyRedirects

from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.base import TooManyRedirectsError
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]
CHAIN_LENGTH = 40

FILENAMES = {
    "aiohttp": "aiohttp.cookies",
    "httpx": "httpx.cookies",
    "curl_cffi": "curl_cffi.cookies",
}


def _cfg(
    backend: str, *, verify_ssl: bool = False, timeout: float = 5.0
) -> FetcherConfig:
    return FetcherConfig(
        backend=backend,
        timeout=timeout,
        headers={"User-Agent": "pytest-parity"},
        trust_env=False,
        verify_ssl=verify_ssl,
        max_connections=5,
        impersonate="chrome",
    )


@pytest_asyncio.fixture
async def chain_server(aiohttp_server: Any) -> AsyncIterator[Any]:
    """``/c<N>`` redirects to ``/c<N+1>``; ``/c{CHAIN_LENGTH}`` answers 200.

    Requesting ``/c{CHAIN_LENGTH - n}`` follows exactly ``n`` redirects.
    """
    app = aiohttp.web.Application()

    def add_hop(index: int) -> None:
        async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
            raise aiohttp.web.HTTPFound(f"/c{index + 1}")

        app.router.add_get(f"/c{index}", handler)

    for index in range(CHAIN_LENGTH):
        add_hop(index)

    async def final(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="done")

    app.router.add_get(f"/c{CHAIN_LENGTH}", final)
    server = await aiohttp_server(app)
    return server


@pytest_asyncio.fixture
async def cookie_server(aiohttp_server: Any) -> AsyncIterator[Any]:
    """Echo cookies and set one, reachable via 127.0.0.1 and localhost."""
    app = aiohttp.web.Application()

    async def echo(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"cookie": request.headers.get("Cookie", "")})

    async def set_cookie(request: aiohttp.web.Request) -> aiohttp.web.Response:
        response = aiohttp.web.Response(text="set")
        response.set_cookie("token", "abc123", path="/")
        return response

    app.router.add_get("/echo", echo)
    app.router.add_get("/set-cookie", set_cookie)
    server = await aiohttp_server(app)
    return server


@pytest_asyncio.fixture
async def corrupt_gzip_server(aiohttp_server: Any) -> AsyncIterator[Any]:
    """Serve a body claiming to be gzip but containing garbage."""

    async def bad(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body=b"this-is-not-gzip-data",
            headers={"Content-Encoding": "gzip"},
        )

    app = aiohttp.web.Application()
    app.router.add_get("/bad", bad)
    server = await aiohttp_server(app)
    return server


# ---------------------------------------------------------------------------
# Fakes used to drive the wrapper's exception translation directly
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Response stand-in covering aiohttp- and httpx-style attribute names."""

    status = 200
    status_code = 200
    headers = {"content-type": "text/plain"}
    charset = None
    charset_encoding = None
    content = b""

    async def read(self) -> bytes:
        return b""


class _FakeAiohttpGet:
    """Mimic ``aiohttp.ClientSession.get`` (async context manager)."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc

    async def __aenter__(self) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        return _FakeResponse()

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeAiohttpSession:
    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc
        self.get_kwargs: dict[str, Any] | None = None
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> _FakeAiohttpGet:
        self.get_kwargs = kwargs
        return _FakeAiohttpGet(self._exc)

    async def close(self) -> None:
        self.closed = True


class _FakeSyncSession:
    """Mimic ``httpx.AsyncClient.get`` / curl_cffi ``AsyncSession.get``."""

    is_closed = False

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc
        self.get_kwargs: dict[str, Any] | None = None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.get_kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return _FakeResponse()

    async def aclose(self) -> None:
        return None

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Redirect chains: unified limit and unified exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_short_redirect_chain_succeeds(backend: str, chain_server: Any) -> None:
    url = str(chain_server.make_url(f"/c{CHAIN_LENGTH - 5}"))

    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(url)

    assert response.status == 200
    assert response.text == "done"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_long_redirect_chain_raises_unified_error(
    backend: str, chain_server: Any
) -> None:
    """A chain beyond the unified limit raises the facade's own type."""
    url = str(chain_server.make_url(f"/c{CHAIN_LENGTH - 30}"))

    async with create_session(backend, _cfg(backend)) as session:
        with pytest.raises(TooManyRedirectsError) as excinfo:
            await session.get(url)

    # The facade must not leak a backend-specific exception class, and a
    # redirect loop is neither a timeout nor a connection failure.
    assert type(excinfo.value) is TooManyRedirectsError
    assert not isinstance(excinfo.value, (TimeoutError, ConnectionError))


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_redirect_loop_raises_unified_error(
    backend: str, server_any: Any
) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        with pytest.raises(TooManyRedirectsError):
            await session.get(str(server_any.make_url("/loop")))


# ---------------------------------------------------------------------------
# Corrupt Content-Encoding: unified ConnectionError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_corrupt_gzip_raises_builtin_connection_error(
    backend: str, corrupt_gzip_server: Any
) -> None:
    url = str(corrupt_gzip_server.make_url("/bad"))

    async with create_session(backend, _cfg(backend)) as session:
        with pytest.raises(ConnectionError) as excinfo:
            await session.get(url)

    assert type(excinfo.value) is ConnectionError
    assert not isinstance(excinfo.value, TimeoutError)


# ---------------------------------------------------------------------------
# Exception translation table (injected backend exceptions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "exc"),
    [
        ("aiohttp", aiohttp.ServerTimeoutError("server timed out")),
        ("httpx", httpx.ReadTimeout("read timed out")),
        ("curl_cffi", CurlTimeout("curl timed out")),
    ],
)
@pytest.mark.asyncio
async def test_timeout_exceptions_become_builtin_timeout_error(
    backend: str, exc: BaseException
) -> None:
    session = create_session(backend, _cfg(backend))
    await session.init()
    # Release the real backend session before swapping in the fake so the
    # test does not leak an unclosed connection pool.
    await session.close()
    fake = _FakeAiohttpSession(exc) if backend == "aiohttp" else _FakeSyncSession(exc)
    session._session = fake  # type: ignore[assignment]
    try:
        with pytest.raises(TimeoutError) as excinfo:
            await session.get("http://example.invalid/")
    finally:
        await session.close()

    # Normalised to the builtin type, not a backend-specific subclass.
    assert type(excinfo.value) is TimeoutError
    assert "timed out" in str(excinfo.value)


@pytest.mark.parametrize(
    ("backend", "exc"),
    [
        ("aiohttp", aiohttp.ClientHttpProxyError(None, None)),
        ("aiohttp", aiohttp.ClientPayloadError("payload broken")),
        ("httpx", httpx.ProxyError("proxy refused")),
        ("httpx", httpx.DecodingError("bad gzip")),
        ("curl_cffi", CurlProxyError("proxy refused")),
        ("curl_cffi", CurlHTTPError("bad content encoding")),
        ("curl_cffi", CurlContentDecodingError("bad content encoding")),
        ("curl_cffi", CurlChunkedEncodingError("bad chunk")),
        ("curl_cffi", CurlConnectionError("connection reset")),
    ],
)
@pytest.mark.asyncio
async def test_transport_failures_become_builtin_connection_error(
    backend: str, exc: BaseException
) -> None:
    session = create_session(backend, _cfg(backend))
    await session.init()
    # Release the real backend session before swapping in the fake so the
    # test does not leak an unclosed connection pool.
    await session.close()
    fake = _FakeAiohttpSession(exc) if backend == "aiohttp" else _FakeSyncSession(exc)
    session._session = fake  # type: ignore[assignment]
    try:
        with pytest.raises(ConnectionError) as excinfo:
            await session.get("http://example.invalid/")
    finally:
        await session.close()

    assert type(excinfo.value) is ConnectionError


@pytest.mark.parametrize(
    ("backend", "exc"),
    [
        ("aiohttp", aiohttp.TooManyRedirects(None, None)),
        ("httpx", httpx.TooManyRedirects("too many redirects")),
        ("curl_cffi", CurlTooManyRedirects("too many redirects")),
    ],
)
@pytest.mark.asyncio
async def test_too_many_redirects_exceptions_become_unified_error(
    backend: str, exc: BaseException
) -> None:
    session = create_session(backend, _cfg(backend))
    await session.init()
    # Release the real backend session before swapping in the fake so the
    # test does not leak an unclosed connection pool.
    await session.close()
    fake = _FakeAiohttpSession(exc) if backend == "aiohttp" else _FakeSyncSession(exc)
    session._session = fake  # type: ignore[assignment]
    try:
        with pytest.raises(TooManyRedirectsError):
            await session.get("http://example.invalid/")
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Per-request verify handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["aiohttp", "curl_cffi"])
@pytest.mark.asyncio
async def test_per_request_verify_is_forwarded_to_backend(
    backend: str,
) -> None:
    """aiohttp and curl_cffi honour per-request TLS verification."""
    session = create_session(backend, _cfg(backend, verify_ssl=True))
    await session.init()
    # Release the real backend session before swapping in the fake so the
    # test does not leak an unclosed connection pool.
    await session.close()
    fake = _FakeAiohttpSession() if backend == "aiohttp" else _FakeSyncSession()
    session._session = fake  # type: ignore[assignment]
    try:
        await session.get("http://example.invalid/", verify=False)
        assert fake.get_kwargs is not None
        if backend == "aiohttp":
            assert fake.get_kwargs["ssl"] is False
        else:
            assert fake.get_kwargs["verify"] is False

        # No override: nothing is injected into the backend kwargs.
        await session.get("http://example.invalid/")
        assert "ssl" not in fake.get_kwargs
        assert "verify" not in fake.get_kwargs
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_httpx_per_request_verify_conflict_is_loud() -> None:
    """httpx cannot override verification per request; it must not fail silently.

    Previously the flag was silently dropped, so a caller that explicitly
    asked for verification on an unverified client kept downloading over an
    unverified TLS connection without any warning.
    """
    session = create_session("httpx", _cfg("httpx", verify_ssl=True))
    await session.init()
    # Release the real backend session before swapping in the fake so the
    # test does not leak an unclosed connection pool.
    await session.close()
    fake = _FakeSyncSession()
    session._session = fake  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="verify_ssl"):
            await session.get("https://example.invalid/", verify=False)

        # A matching value is a no-op, and None keeps the session default.
        await session.get("https://example.invalid/", verify=True)
        assert "verify" not in fake.get_kwargs
        await session.get("https://example.invalid/")
        assert "verify" not in fake.get_kwargs
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Cookie scope on IP-host origins (no is_ip_address patching here)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_server_cookie_on_ip_host_is_stored_and_sent(
    backend: str, cookie_server: Any
) -> None:
    """A cookie set by an IP-host origin must work like on a named host.

    httpx and curl_cffi always accepted these; aiohttp's default jar silently
    rejected every cookie coming from an IP address, which dropped logins on
    sites served from bare IPs.
    """
    base = str(cookie_server.make_url("/"))

    async with create_session(backend, _cfg(backend)) as session:
        await session.get(base + "set-cookie")
        assert session.get_cookie("token") == "abc123"

        echoed = await session.get(base + "echo")

    assert "token=abc123" in echoed.json()["cookie"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_loaded_domain_scoped_cookie_stays_scoped(
    backend: str, cookie_server: Any, tmp_path: Any
) -> None:
    """A domain-scoped cookie file is sent to its host and only its host."""
    port = cookie_server.make_url("/").port
    (tmp_path / FILENAMES[backend]).write_text(
        json.dumps([{"name": "scoped", "value": "s1", "domain": "127.0.0.1"}]),
        encoding="utf-8",
    )

    async with create_session(backend, _cfg(backend)) as session:
        assert session.load_cookies(tmp_path) is True
        same_host = await session.get(f"http://127.0.0.1:{port}/echo")
        other_host = await session.get(f"http://localhost:{port}/echo")

    assert "scoped=s1" in same_host.json()["cookie"]
    assert "scoped=s1" not in other_host.json()["cookie"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_cookie_round_trip_keeps_ip_host_scope(
    backend: str, cookie_server: Any, tmp_path: Any
) -> None:
    """Saving and reloading an IP-host cookie must not widen its scope."""
    base = str(cookie_server.make_url("/"))
    port = cookie_server.make_url("/").port

    async with create_session(backend, _cfg(backend)) as first:
        await first.get(base + "set-cookie")
        assert first.save_cookies(tmp_path) is True

    async with create_session(backend, _cfg(backend)) as second:
        assert second.load_cookies(tmp_path) is True
        same_host = await second.get(f"http://127.0.0.1:{port}/echo")
        other_host = await second.get(f"http://localhost:{port}/echo")

    assert "token=abc123" in same_host.json()["cookie"]
    assert "token=abc123" not in other_host.json()["cookie"]


# ---------------------------------------------------------------------------
# Timeout type on a live server (regression guard)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def server_any(aiohttp_server: Any) -> AsyncIterator[Any]:
    app = aiohttp.web.Application()

    async def loop_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound("/loop")

    async def slow(request: aiohttp.web.Request) -> aiohttp.web.Response:
        await asyncio.sleep(1.0)
        return aiohttp.web.Response(text="slow")

    app.router.add_get("/loop", loop_handler)
    app.router.add_get("/slow", slow)
    server = await aiohttp_server(app)
    return server


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_live_timeout_is_builtin_timeout_error(
    backend: str, server_any: Any
) -> None:
    url = str(server_any.make_url("/slow"))

    async with create_session(backend, _cfg(backend, timeout=0.2)) as session:
        with pytest.raises(TimeoutError) as excinfo:
            await session.get(url)

    assert type(excinfo.value) is TimeoutError
