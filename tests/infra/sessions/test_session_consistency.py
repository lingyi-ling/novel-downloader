"""Cross-backend consistency tests for the three HTTP session implementations.

Every test here runs against all of aiohttp, httpx and curl_cffi; anything
that only works on one of them is an inconsistency between the public
session contract and the backends it wraps.

No test uses the network: the target and the proxy are local aiohttp
servers started on ephemeral ports.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
from typing import Any

import aiohttp
import aiohttp.web
import httpx
import pytest
import pytest_asyncio
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
)

import novel_downloader.infra.sessions._curl_cffi as curl_backend
from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.base import TooManyRedirectsError
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]

PROXY_USER = "user"
PROXY_PASS = "pw"
PROXY_AUTH_HEADER = (
    "Basic " + base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()
)


def make_cfg(**overrides: Any) -> FetcherConfig:
    """Config that works for all three backends."""
    values: dict[str, Any] = {
        "timeout": 5.0,
        "headers": {"User-Agent": "pytest"},
        "proxy": None,
        "proxy_user": None,
        "proxy_pass": None,
        "trust_env": False,
        "verify_ssl": False,
        "max_connections": 5,
        "http2": False,
        "impersonate": "chrome",
        "request_interval": 0.0,
    }
    values.update(overrides)
    return FetcherConfig(**values)


# --------------------------------------------------------------------------- #
# Local servers
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def target_server(aiohttp_server: Any) -> Any:
    """A direct target that also records the connections it served."""
    state: dict[str, Any] = {"transports": [], "hits": 0}

    async def handler_ok(request: aiohttp.web.Request) -> aiohttp.web.Response:
        state["hits"] += 1
        state["transports"].append(id(request.transport))
        return aiohttp.web.Response(text="direct", status=200)

    async def handler_slow(request: aiohttp.web.Request) -> aiohttp.web.Response:
        await asyncio.sleep(1.0)
        return aiohttp.web.Response(text="slow", status=200)

    async def handler_503(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            text="busy", status=503, headers={"Retry-After": "7"}
        )

    async def handler_loop(request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound("/loop")

    async def handler_large(request: aiohttp.web.Request) -> aiohttp.web.Response:
        # 1 KiB: larger than the test limit, small enough to stay trivial.
        return aiohttp.web.Response(body=b"x" * 1024, status=200)

    app = aiohttp.web.Application()
    app.router.add_get("/ok", handler_ok)
    app.router.add_get("/slow", handler_slow)
    app.router.add_get("/503", handler_503)
    app.router.add_get("/loop", handler_loop)
    app.router.add_get("/large", handler_large)
    server = await aiohttp_server(app)
    server.state = state  # type: ignore[attr-defined]
    return server


@pytest_asyncio.fixture
async def proxy_server(aiohttp_server: Any) -> dict[str, Any]:
    """A tiny HTTP proxy that can require ``Proxy-Authorization``."""
    state: dict[str, Any] = {"required_auth": None, "seen": []}

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        header = request.headers.get("Proxy-Authorization")
        state["seen"].append(header)
        required = state["required_auth"]
        if required is not None and header != required:
            return aiohttp.web.Response(status=407, text="proxy auth required")
        return aiohttp.web.Response(text="proxied", status=200)

    app = aiohttp.web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    state["url"] = str(server.make_url("/")).rstrip("/")
    return state


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Proxy: explicit configuration
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_proxy_auth_from_config_is_sent(
    backend: str, proxy_server: dict[str, Any]
) -> None:
    proxy_server["required_auth"] = PROXY_AUTH_HEADER
    session = create_session(
        backend,
        make_cfg(
            proxy=proxy_server["url"],
            proxy_user=PROXY_USER,
            proxy_pass=PROXY_PASS,
        ),
    )
    await session.init()
    try:
        resp = await session.get("http://target.invalid/x")
        assert resp.status == 200
        assert resp.text == "proxied"
        assert proxy_server["seen"] == [PROXY_AUTH_HEADER]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_proxy_credentials_inside_the_url_are_sent(
    backend: str, proxy_server: dict[str, Any]
) -> None:
    """``http://user:pass@host:port`` must authenticate on every backend."""
    proxy_server["required_auth"] = PROXY_AUTH_HEADER
    port = proxy_server["url"].rsplit(":", 1)[1]
    session = create_session(
        backend,
        make_cfg(proxy=f"http://{PROXY_USER}:{PROXY_PASS}@127.0.0.1:{port}"),
    )
    await session.init()
    try:
        resp = await session.get("http://target.invalid/x")
        assert resp.status == 200
        assert proxy_server["seen"] == [PROXY_AUTH_HEADER]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_user_only_proxy_credentials_are_sent(
    backend: str, proxy_server: dict[str, Any]
) -> None:
    """An empty password is a valid credential, not "no credentials".

    The old code required both ``proxy_user`` and ``proxy_pass`` to be
    truthy, so this configuration silently sent no ``Proxy-Authorization``.
    """
    expected = "Basic " + base64.b64encode(b"user:").decode()
    proxy_server["required_auth"] = expected
    session = create_session(
        backend,
        make_cfg(proxy=proxy_server["url"], proxy_user="user", proxy_pass=""),
    )
    await session.init()
    try:
        resp = await session.get("http://target.invalid/x")
        assert resp.status == 200
        assert proxy_server["seen"] == [expected]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_blank_proxy_is_ignored(backend: str, target_server: Any) -> None:
    """A whitespace-only proxy setting must not reach a backend.

    httpx raised ``Unknown scheme for proxy URL URL('%20%20%20')`` and curl
    failed with ``Unsupported proxy syntax`` for this value, while aiohttp
    silently connected directly.
    """
    session = create_session(backend, make_cfg(proxy="   "))
    await session.init()
    try:
        resp = await session.get(str(target_server.make_url("/ok")))
        assert resp.text == "direct"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_unsupported_proxy_scheme_fails_loudly(backend: str) -> None:
    """Every backend rejects an unusable scheme at init, without secrets."""
    session = create_session(
        backend, make_cfg(proxy="ftp://user:topsecret@127.0.0.1:1")
    )
    with pytest.raises(ValueError) as excinfo:
        await session.init()

    message = str(excinfo.value)
    assert backend in message
    assert "ftp" in message
    assert "topsecret" not in message


@pytest.mark.asyncio
async def test_aiohttp_rejects_socks_proxies() -> None:
    """aiohttp would otherwise speak HTTP to the SOCKS port until timeout."""
    session = create_session("aiohttp", make_cfg(proxy="socks5://127.0.0.1:1080"))
    with pytest.raises(ValueError) as excinfo:
        await session.init()

    message = str(excinfo.value)
    assert "socks5" in message
    assert "curl_cffi" in message


@pytest.mark.asyncio
async def test_httpx_socks_proxy_needs_socksio() -> None:
    if importlib.util.find_spec("socksio") is not None:
        pytest.skip("socksio is installed; httpx supports SOCKS here")
    session = create_session("httpx", make_cfg(proxy="socks5://127.0.0.1:1080"))
    with pytest.raises(RuntimeError, match="socksio"):
        await session.init()


@pytest.mark.asyncio
async def test_curl_cffi_accepts_socks_proxies() -> None:
    session = create_session("curl_cffi", make_cfg(proxy="socks5://127.0.0.1:1080"))
    await session.init()
    await session.close()


# --------------------------------------------------------------------------- #
# Proxy: environment
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_env_all_proxy_is_honoured(
    backend: str, proxy_server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """aiohttp ignored ``ALL_PROXY`` (both cases) and went direct."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", proxy_server["url"])
    session = create_session(backend, make_cfg(trust_env=True))
    await session.init()
    try:
        resp = await session.get("http://target.invalid/x")
        assert resp.text == "proxied"
        assert len(proxy_server["seen"]) == 1
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_env_uppercase_http_proxy_is_honoured(
    backend: str, proxy_server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", proxy_server["url"])
    session = create_session(backend, make_cfg(trust_env=True))
    await session.init()
    try:
        resp = await session.get("http://target.invalid/x")
        assert resp.text == "proxied"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_env_no_proxy_bypasses_the_proxy(
    backend: str,
    proxy_server: dict[str, Any],
    target_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", proxy_server["url"])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    session = create_session(backend, make_cfg(trust_env=True))
    await session.init()
    try:
        resp = await session.get(str(target_server.make_url("/ok")))
        assert resp.text == "direct"
        assert proxy_server["seen"] == []
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_env_proxy_is_ignored_without_trust_env(
    backend: str, proxy_server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", proxy_server["url"])
    session = create_session(backend, make_cfg(trust_env=False, timeout=2))
    await session.init()
    try:
        # The target is unreachable when the request is direct; what matters
        # is that the environment proxy is never contacted.  (curl_cffi
        # ignored trust_env entirely and proxied through it.)
        with contextlib.suppress(ConnectionError, TimeoutError):
            await session.get("http://target.invalid/x")
        assert proxy_server["seen"] == []
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# Timeouts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("timeout", [0, -1, -0.5])
def test_non_positive_timeout_is_rejected(backend: str, timeout: float) -> None:
    """0 was "no deadline" on aiohttp/curl and "fail now" on httpx."""
    with pytest.raises(ValueError, match="timeout"):
        create_session(backend, make_cfg(timeout=timeout))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_none_timeout_disables_the_deadline(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg(timeout=None))
    await session.init()
    try:
        resp = await session.get(str(target_server.make_url("/ok")))
        assert resp.text == "direct"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_slow_response_times_out(backend: str, target_server: Any) -> None:
    session = create_session(backend, make_cfg(timeout=0.1))
    await session.init()
    try:
        with pytest.raises(TimeoutError):
            await session.get(str(target_server.make_url("/slow")))
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# Responses and redirects
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_non_2xx_is_returned_with_headers(
    backend: str, target_server: Any
) -> None:
    """Sessions return the response; the fetcher raises HttpStatusError.

    What all three backends must agree on is the data that classification
    needs: the status and a case-insensitively readable ``Retry-After``.
    """
    session = create_session(backend, make_cfg())
    await session.init()
    try:
        resp = await session.get(str(target_server.make_url("/503")))
        assert resp.status == 503
        assert resp.ok is False
        assert resp.headers.get("Retry-After") == "7"
        assert resp.headers.get("retry-after") == "7"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_redirect_loop_raises_the_unified_error(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    try:
        with pytest.raises(TooManyRedirectsError):
            await session.get(str(target_server.make_url("/loop")))
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# Connection lifetime
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_sequential_requests_reuse_a_connection(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    try:
        url = str(target_server.make_url("/ok"))
        await session.get(url)
        await session.get(url)
        transports = target_server.state["transports"]
        assert len(transports) == 2
        assert transports[0] == transports[1]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_reinit_after_close(backend: str, target_server: Any) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    await session.close()
    assert session._session is None

    await session.init()
    try:
        resp = await session.get(str(target_server.make_url("/ok")))
        assert resp.text == "direct"
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_close_is_idempotent_and_concurrent(backend: str) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    await asyncio.gather(session.close(), session.close(), session.close())
    assert session._session is None
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_use_after_close_raises_runtime_error(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    await session.close()

    with pytest.raises(RuntimeError, match="not initialized"):
        await session.get(str(target_server.make_url("/ok")))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_concurrent_init_creates_one_backend(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ``async with`` blocks must not build two connection pools."""
    created: list[int] = []

    if backend == "aiohttp":
        real_aiohttp_session = aiohttp.ClientSession

        def aiohttp_factory(*args: Any, **kwargs: Any) -> Any:
            created.append(1)
            return real_aiohttp_session(*args, **kwargs)

        monkeypatch.setattr(aiohttp, "ClientSession", aiohttp_factory)
    elif backend == "httpx":
        real_httpx_client = httpx.AsyncClient

        def httpx_factory(*args: Any, **kwargs: Any) -> Any:
            created.append(1)
            return real_httpx_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", httpx_factory)
    else:
        real_curl_session = curl_backend.AsyncSession

        def curl_factory(*args: Any, **kwargs: Any) -> Any:
            created.append(1)
            return real_curl_session(*args, **kwargs)

        monkeypatch.setattr(curl_backend, "AsyncSession", curl_factory)

    session = create_session(backend, make_cfg())
    await asyncio.gather(*(session.init() for _ in range(3)))
    assert len(created) == 1
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_cancelled_close_still_releases_the_backend(backend: str) -> None:
    """Cancelling the caller must not abort the shielded backend shutdown."""

    class _SlowClose:
        closed = False
        is_closed = False

        async def close(self) -> None:
            await asyncio.sleep(0.05)
            self.closed = True
            self.is_closed = True

        async def aclose(self) -> None:
            await self.close()

    session = create_session(backend, make_cfg())
    fake = _SlowClose()
    session._session = fake

    task = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    for _ in range(100):
        if fake.closed:
            break
        await asyncio.sleep(0.01)
    assert fake.closed, "the backend close was lost with the cancelled caller"
    assert session._session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_cancelled_request_does_not_poison_the_pool(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    try:
        task = asyncio.create_task(session.get(str(target_server.make_url("/slow"))))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        resp = await session.get(str(target_server.make_url("/ok")))
        assert resp.text == "direct"
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #
class _RaisingClient:
    """Client double for httpx/curl_cffi: ``get`` raises immediately."""

    is_closed = False
    closed = False

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def get(self, url: str, **kwargs: Any) -> Any:
        raise self.exc

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _RaisingResponse:
    """Async context manager that fails on entry, like aiohttp's response."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __aenter__(self) -> Any:
        raise self.exc

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _AiohttpRaisingClient:
    """aiohttp double: ``get`` returns an async context manager."""

    closed = False

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def get(self, url: str, **kwargs: Any) -> _RaisingResponse:
        return _RaisingResponse(self.exc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "error"),
    [
        ("aiohttp", aiohttp.ClientConnectionError("boom")),
        ("httpx", httpx.ConnectError("boom")),
        ("curl_cffi", CurlConnectionError("boom")),
    ],
)
async def test_proxy_password_never_reaches_the_exception(
    backend: str, error: BaseException
) -> None:
    """libcurl echoes the proxy URL; the facade must mask the password."""
    proxy_url = "http://user:topsecret@proxy.invalid:3128"
    session = create_session(backend, make_cfg(proxy=proxy_url))

    # Simulate the message libcurl produces, which includes the URL it was
    # given.  The facade must not have handed it a credential-bearing URL in
    # the first place, and the translator masks it if a library re-adds it.
    error.args = (f"cannot connect through {proxy_url}",)
    session._session = (
        _AiohttpRaisingClient(error) if backend == "aiohttp" else _RaisingClient(error)
    )

    with pytest.raises(ConnectionError) as excinfo:
        await session.get("https://target.invalid/")

    message = str(excinfo.value)
    assert "topsecret" not in message
    assert "***" in message


# --------------------------------------------------------------------------- #
# Response size caps
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_max_bytes_rejects_an_oversized_body(
    backend: str, target_server: Any
) -> None:
    """The cap must be enforced while reading, not after buffering the body.

    Media downloads are attacker-influenced (URLs come from site HTML); a
    response far above the 64 MiB limit must be refused without first holding
    it in memory.
    """
    from novel_downloader.infra.sessions.response import ResponseTooLargeError

    session = create_session(backend, make_cfg())
    await session.init()
    try:
        url = str(target_server.make_url("/large"))
        with pytest.raises(ResponseTooLargeError):
            await session.get(url, max_bytes=16)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_max_bytes_allows_a_body_within_the_limit(
    backend: str, target_server: Any
) -> None:
    session = create_session(backend, make_cfg())
    await session.init()
    try:
        url = str(target_server.make_url("/large"))
        response = await session.get(url, max_bytes=2048)
    finally:
        await session.close()

    assert response.content == b"x" * 1024
