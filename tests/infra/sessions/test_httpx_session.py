import asyncio
import json

import aiohttp.web
import httpx
import pytest
import pytest_asyncio

from novel_downloader.infra.sessions._httpx import HttpxSession
from novel_downloader.schemas import FetcherConfig


@pytest_asyncio.fixture
async def test_server(aiohttp_server):
    async def handler_ok(request):
        return aiohttp.web.Response(text="hello", status=200)

    async def handler_post(request):
        data = await request.json()
        return aiohttp.web.json_response({"received": data})

    async def handler_set_cookie(request):
        resp = aiohttp.web.Response(text="cookie!")
        resp.set_cookie("token", "abc123")
        return resp

    app = aiohttp.web.Application()
    app.router.add_get("/ok", handler_ok)
    app.router.add_post("/post", handler_post)
    app.router.add_get("/set-cookie", handler_set_cookie)

    server = await aiohttp_server(app)
    return server


@pytest.fixture
def cfg(tmp_path):
    """Default FetcherConfig for httpx backend."""
    return FetcherConfig(
        timeout=5,
        headers={"User-Agent": "pytest"},
        proxy=None,
        proxy_user=None,
        proxy_pass=None,
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
        http2=False,
    )


# ------------------------------------------------
# init + close
# ------------------------------------------------


@pytest.mark.asyncio
async def test_init_and_close(cfg):
    s = HttpxSession(cfg, cookies=None)
    assert s._session is None

    await s.init()
    assert isinstance(s._session, httpx.AsyncClient)

    await s.close()
    assert s._session is None


# ------------------------------------------------
# GET request
# ------------------------------------------------


@pytest.mark.asyncio
async def test_get_request(test_server, cfg):
    s = HttpxSession(cfg, cookies=None)
    await s.init()

    url = test_server.make_url("/ok")
    resp = await s.get(str(url))

    assert resp.status == 200
    assert resp.text == "hello"
    assert resp.ok

    await s.close()


# ------------------------------------------------
# POST JSON
# ------------------------------------------------


@pytest.mark.asyncio
async def test_post_request(test_server, cfg):
    s = HttpxSession(cfg, cookies=None)
    await s.init()

    url = test_server.make_url("/post")
    resp = await s.post(str(url), json={"a": 1})

    assert resp.ok
    assert resp.json() == {"received": {"a": 1}}

    await s.close()


# ------------------------------------------------
# session property error
# ------------------------------------------------


@pytest.mark.asyncio
async def test_session_property_error(cfg):
    s = HttpxSession(cfg, cookies=None)
    with pytest.raises(RuntimeError):
        _ = s.session


# ------------------------------------------------
# cookies save + load
# ------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_save_load(tmp_path, test_server, cfg):
    cookie_dir = tmp_path / "cookies"

    s = HttpxSession(cfg, cookies=None)
    await s.init()

    # request that sets cookie
    url = test_server.make_url("/set-cookie")
    await s.get(str(url))

    # save cookies
    assert s.save_cookies(cookie_dir) is True

    saved = json.loads((cookie_dir / "httpx.cookies").read_text())
    assert saved[0]["name"] == "token"
    assert saved[0]["value"] == "abc123"

    # load into new session
    s2 = HttpxSession(cfg, cookies=None)
    await s2.init()

    assert s2.load_cookies(cookie_dir) is True
    assert s2.get_cookie("token") == "abc123"

    await s.close()
    await s2.close()


# ------------------------------------------------
# cookie update + clear
# ------------------------------------------------


@pytest.mark.asyncio
async def test_cookie_update_and_clear(cfg):
    s = HttpxSession(cfg, cookies=None)
    await s.init()

    s.update_cookies({"k": "v"})
    assert s.get_cookie("k") == "v"

    s.clear_cookie("k")
    assert s.get_cookie("k") is None

    s.update_cookies({"a": "1", "b": "2"})
    s.clear_cookies()

    assert s.get_cookie("a") is None
    assert s.get_cookie("b") is None

    await s.close()


class _DripClient:
    """AsyncClient double whose request never finishes on its own."""

    is_closed = False

    async def get(self, url: str, **kwargs: object) -> object:
        await asyncio.sleep(30)
        raise AssertionError("the outer deadline should have fired")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_request_honours_a_total_deadline(cfg):
    """httpx's own timeout is per operation; a drip feed must still end."""
    cfg.timeout = 0.05
    session = HttpxSession(cfg, cookies=None)
    session._session = _DripClient()  # type: ignore[assignment]

    with pytest.raises(TimeoutError):
        await session.get("https://demo/drip")


@pytest.mark.asyncio
async def test_post_honours_a_total_deadline(cfg):
    cfg.timeout = 0.05
    session = HttpxSession(cfg, cookies=None)

    class _Drip(_DripClient):
        async def post(self, url: str, **kwargs: object) -> object:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    session._session = _Drip()  # type: ignore[assignment]

    with pytest.raises(TimeoutError):
        await session.post("https://demo/drip")
