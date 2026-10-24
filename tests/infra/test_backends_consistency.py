"""Cross-backend behavioural consistency tests for the HTTP session facade.

Every test runs the same assertion against ``aiohttp``, ``httpx`` and
``curl_cffi`` backed by a local ``aiohttp`` server.  The facade unifies the
differences that can be unified (redirect defaults, timeout/connection
exception types, response encoding fallback) so this suite fails if those
contracts drift apart again.

Anything that cannot be unified without changing a backend's semantics is
documented in the final report instead of being asserted here.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import aiohttp.cookiejar
import aiohttp.web
import pytest
import pytest_asyncio

from novel_downloader.infra.sessions import BaseSession, create_session
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    """Parametrize over installed backends; skip optional ones gracefully."""
    if request.param == "curl_cffi":
        pytest.importorskip("curl_cffi")
    return str(request.param)


@pytest.fixture(autouse=True)
def _allow_ip_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """aiohttp refuses cookies for IP hosts unless the jar is "unsafe"."""
    monkeypatch.setattr(aiohttp.cookiejar, "is_ip_address", lambda host: False)


def _cfg(backend: str, *, timeout: float = 5.0) -> FetcherConfig:
    return FetcherConfig(
        backend=backend,
        timeout=timeout,
        headers={"User-Agent": "pytest-consistency"},
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
        # Only used by curl_cffi; other backends ignore it.
        impersonate="chrome",
    )


@pytest_asyncio.fixture
async def server(aiohttp_server: Any) -> AsyncIterator[Any]:
    """Local server exercising text, JSON, forms, redirects and cookies."""

    async def ok(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="hello", headers={"X-Test": "v1"})

    async def json_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"answer": 42})

    async def echo_query(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"q": request.query.get("q", "")})

    async def echo_headers(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"headers": dict(request.headers)})

    async def echo_cookies(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"cookie": request.headers.get("Cookie", "")})

    async def echo_json(request: aiohttp.web.Request) -> aiohttp.web.Response:
        payload = await request.json()
        return aiohttp.web.json_response(
            {"json": payload, "content_type": request.content_type}
        )

    async def echo_form(request: aiohttp.web.Request) -> aiohttp.web.Response:
        form = await request.post()
        return aiohttp.web.json_response(
            {"form": dict(form), "content_type": request.content_type}
        )

    async def redirect(request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound("/target")

    async def target(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="target")

    async def slow(request: aiohttp.web.Request) -> aiohttp.web.Response:
        await asyncio.sleep(0.5)
        return aiohttp.web.Response(text="slow")

    async def set_cookie(request: aiohttp.web.Request) -> aiohttp.web.Response:
        response = aiohttp.web.Response(text="set")
        response.set_cookie("token", "abc123", path="/")
        return response

    async def missing(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="missing", status=404)

    async def gbk_body(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body="中文测试".encode("gbk"), content_type="text/plain"
        )

    async def gbk_declared(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            body="中文测试".encode("gbk"),
            content_type="text/plain",
            charset="gbk",
        )

    app = aiohttp.web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/json", json_handler)
    app.router.add_get("/echo-query", echo_query)
    app.router.add_get("/echo-headers", echo_headers)
    app.router.add_get("/echo-cookies", echo_cookies)
    app.router.add_post("/echo-json", echo_json)
    app.router.add_post("/echo-form", echo_form)
    app.router.add_route("*", "/redirect", redirect)
    app.router.add_route("*", "/target", target)
    app.router.add_get("/slow", slow)
    app.router.add_get("/set-cookie", set_cookie)
    app.router.add_get("/missing", missing)
    app.router.add_get("/gbk", gbk_body)
    app.router.add_get("/gbk-declared", gbk_declared)

    return await aiohttp_server(app)


def _url(server: Any, path: str) -> str:
    return str(server.make_url(path))


def _closed_port() -> int:
    """Return a TCP port that is currently closed on the loopback interface."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_session(session: Any) -> BaseSession:
    assert isinstance(session, BaseSession)
    return session


# ---------------------------------------------------------------------------
# Basic GET / POST semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_headers_and_text(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/ok"))

    assert response.status == 200
    assert response.ok is True
    assert response.text == "hello"
    assert response.headers["X-Test"] == "v1"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_get_json(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/json"))

    assert response.status == 200
    assert response.json() == {"answer": 42}


@pytest.mark.asyncio
async def test_get_params(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/echo-query"), params={"q": "中文"})

    assert response.json()["q"] == "中文"


@pytest.mark.asyncio
async def test_get_custom_headers(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(
            _url(server, "/echo-headers"), headers={"X-Custom": "yes"}
        )

    sent = {key.lower(): value for key, value in response.json()["headers"].items()}
    assert sent["x-custom"] == "yes"
    assert sent["user-agent"] == "pytest-consistency"


@pytest.mark.asyncio
async def test_post_json(backend: str, server: Any) -> None:
    payload = {"a": 1, "b": [2, 3]}

    async with create_session(backend, _cfg(backend)) as session:
        response = await session.post(_url(server, "/echo-json"), json=payload)

    body = response.json()
    assert body["json"] == payload
    assert body["content_type"].startswith("application/json")


@pytest.mark.asyncio
async def test_post_form(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.post(
            _url(server, "/echo-form"), data={"a": "1", "b": "two"}
        )

    body = response.json()
    assert body["form"] == {"a": "1", "b": "two"}
    assert body["content_type"].startswith("application/x-www-form-urlencoded")


@pytest.mark.asyncio
async def test_error_status_is_returned_not_raised(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/missing"))

    assert response.status == 404
    assert response.ok is False
    assert response.text == "missing"


# ---------------------------------------------------------------------------
# Redirect policy (unified default: follow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_is_followed_by_default(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/redirect"))

    assert response.status == 200
    assert response.text == "target"


@pytest.mark.asyncio
async def test_redirect_explicit_true(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/redirect"), allow_redirects=True)

    assert response.status == 200
    assert response.text == "target"


@pytest.mark.asyncio
async def test_redirect_explicit_false(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/redirect"), allow_redirects=False)

    assert response.status == 302
    assert response.headers["location"] == "/target"
    assert response.text != "target"


@pytest.mark.asyncio
async def test_post_redirect_is_followed_by_default(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.post(_url(server, "/redirect"), json={"x": 1})

    assert response.status == 200
    assert response.text == "target"


# ---------------------------------------------------------------------------
# Transport failure types (unified: builtin TimeoutError / ConnectionError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_builtin_timeout_error(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend, timeout=0.2)) as session:
        with pytest.raises(TimeoutError) as excinfo:
            await session.get(_url(server, "/slow"))

    # The backend-specific class must not leak through the facade.
    assert not isinstance(excinfo.value, ConnectionError)


@pytest.mark.asyncio
async def test_connection_error_raises_builtin_connection_error(backend: str) -> None:
    url = f"http://127.0.0.1:{_closed_port()}/"
    async with create_session(backend, _cfg(backend, timeout=3.0)) as session:
        with pytest.raises(ConnectionError):
            await session.get(url)


# ---------------------------------------------------------------------------
# Cookies: server-set, manual, save/load round-trip, and sending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_cookie_is_stored_and_sent(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        await session.get(_url(server, "/set-cookie"))
        assert session.get_cookie("token") == "abc123"

        echoed = await session.get(_url(server, "/echo-cookies"))

    assert "token=abc123" in echoed.json()["cookie"]


@pytest.mark.asyncio
async def test_constructor_cookie_is_sent(backend: str, server: Any) -> None:
    cookie = {"pre": "loaded"}

    async with create_session(backend, _cfg(backend), cookies=cookie) as session:
        echoed = await session.get(_url(server, "/echo-cookies"))

    assert "pre=loaded" in echoed.json()["cookie"]


@pytest.mark.asyncio
async def test_updated_cookie_is_sent(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        session.update_cookies({"manual": "m1"})
        echoed = await session.get(_url(server, "/echo-cookies"))

    assert "manual=m1" in echoed.json()["cookie"]


@pytest.mark.asyncio
async def test_get_cookie_is_deterministic_with_duplicate_names(
    backend: str, server: Any
) -> None:
    """The same name on two domains resolves to the first jar entry everywhere."""
    async with create_session(backend, _cfg(backend)) as session:
        session.update_cookies({"token": "manual"})
        await session.get(_url(server, "/set-cookie"))

        # ``update_cookies`` stores a domain-less cookie; the server adds a
        # host-scoped cookie with the same name.
        assert session.get_cookie("token") == "manual"


@pytest.mark.asyncio
async def test_cookie_save_load_roundtrip(
    backend: str, server: Any, tmp_path: Any
) -> None:
    cfg = _cfg(backend)

    async with create_session(backend, cfg) as first:
        await first.get(_url(server, "/set-cookie"))
        assert first.get_cookie("token") == "abc123"
        assert first.save_cookies(tmp_path, "cookies.json") is True

    saved = json.loads((tmp_path / "cookies.json").read_text(encoding="utf-8"))
    assert isinstance(saved, list)
    assert any(
        item.get("name") == "token" and item.get("value") == "abc123" for item in saved
    )

    async with create_session(backend, cfg) as second:
        assert second.load_cookies(tmp_path, "cookies.json") is True
        assert second.get_cookie("token") == "abc123"
        echoed = await second.get(_url(server, "/echo-cookies"))

    assert "token=abc123" in echoed.json()["cookie"]


@pytest.mark.asyncio
async def test_clear_cookie(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        await session.get(_url(server, "/set-cookie"))
        session.clear_cookie("token")
        assert session.get_cookie("token") is None

        echoed = await session.get(_url(server, "/echo-cookies"))

    assert "token=abc123" not in echoed.json()["cookie"]


# ---------------------------------------------------------------------------
# Response encoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_charset_wins(backend: str, server: Any) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/gbk-declared"), encoding="utf-8")

    assert response.encoding == "gbk"
    assert response.text == "中文测试"


@pytest.mark.asyncio
async def test_caller_encoding_used_when_charset_absent(
    backend: str, server: Any
) -> None:
    async with create_session(backend, _cfg(backend)) as session:
        response = await session.get(_url(server, "/gbk"), encoding="gbk")

    assert response.encoding == "gbk"
    assert response.text == "中文测试"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent_and_releases_session(backend: str) -> None:
    session = _assert_session(create_session(backend, _cfg(backend)))
    await session.init()
    assert session._session is not None

    await session.close()
    await session.close()
    await session.close()

    assert session._session is None
    with pytest.raises(RuntimeError):
        _ = session.session  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        await session.get("http://127.0.0.1:1/never")


@pytest.mark.asyncio
async def test_close_before_init_is_safe(backend: str) -> None:
    session = _assert_session(create_session(backend, _cfg(backend)))

    await session.close()
    await session.close()

    assert session._session is None
