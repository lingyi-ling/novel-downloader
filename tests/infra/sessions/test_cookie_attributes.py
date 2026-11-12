"""Session backends must preserve cookie security attributes on the round-trip."""

from __future__ import annotations

import time
from email.utils import formatdate
from http.cookiejar import Cookie
from http.cookies import Morsel
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.cookie_io import write_cookie_items
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]
FILENAMES = {
    "aiohttp": "aiohttp.cookies",
    "httpx": "httpx.cookies",
    "curl_cffi": "curl_cffi.cookies",
}


@pytest.fixture
def cfg() -> FetcherConfig:
    return FetcherConfig(
        timeout=5,
        headers={"User-Agent": "pytest"},
        trust_env=False,
        verify_ssl=False,
        max_connections=5,
        impersonate="chrome",
    )


async def _session(backend: str, cfg: FetcherConfig) -> Any:
    session = create_session(backend, cfg)
    await session.init()
    return session


def _set_rich_cookie(session: Any, backend: str, expires: int) -> None:
    if backend == "aiohttp":
        morsel: Morsel[str] = Morsel()
        morsel.set("sid", "v", "v")
        morsel["domain"] = "example.com"
        morsel["path"] = "/"
        morsel["secure"] = True
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["expires"] = formatdate(expires, usegmt=True)
        session.session.cookie_jar.update_cookies({"sid": morsel})
        return

    domain = ".example.com"
    session.session.cookies.jar.set_cookie(
        Cookie(
            version=0,
            name="sid",
            value="v",
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=expires,
            discard=False,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None, "SameSite": "Strict", "Max-Age": "3600"},
            rfc2109=False,
        )
    )


def _jar_cookie(session: Any, backend: str) -> Any:
    if backend == "aiohttp":
        jar = session.session.cookie_jar
        return next(cookie for cookie in jar if cookie.key == "sid")
    jar = session.session.cookies.jar
    return next(cookie for cookie in jar if cookie.name == "sid")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_cookie_security_attributes_survive_round_trip(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    expires = int(time.time()) + 3600
    writer = await _session(backend, cfg)
    try:
        _set_rich_cookie(writer, backend, expires)
        assert writer.save_cookies(tmp_path) is True
    finally:
        await writer.close()

    reader = await _session(backend, cfg)
    try:
        assert reader.load_cookies(tmp_path) is True
        cookie = _jar_cookie(reader, backend)
        if backend == "aiohttp":
            assert bool(cookie["secure"]) is True
            assert bool(cookie["httponly"]) is True
            assert cookie["samesite"] == "Strict"
            assert cookie["expires"] != ""
            assert reader.session.cookie_jar._expirations
        else:
            assert cookie.secure is True
            assert getattr(cookie, "_rest", {})["HttpOnly"] is None
            assert getattr(cookie, "_rest", {})["SameSite"] == "Strict"
            assert cookie.expires == expires
            assert cookie.discard is False
    finally:
        await reader.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_session_cookie_stays_a_session_cookie(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    writer = await _session(backend, cfg)
    try:
        writer.update_cookies({"sid": "v"})
        assert writer.save_cookies(tmp_path) is True
    finally:
        await writer.close()

    reader = await _session(backend, cfg)
    try:
        assert reader.load_cookies(tmp_path) is True
        cookie = _jar_cookie(reader, backend)
        if backend == "aiohttp":
            assert cookie["expires"] == ""
            assert not reader.session.cookie_jar._expirations
        else:
            assert cookie.expires is None
            # http.cookiejar's persist/session decision.
            assert cookie.discard is True
    finally:
        await reader.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_expired_cookie_file_entries_are_not_loaded(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    path = tmp_path / FILENAMES[backend]
    write_cookie_items(
        path,
        [
            {
                "name": "dead",
                "value": "v",
                "domain": "example.com",
                "path": "/",
                "expires": time.time() - 60,
            }
        ],
    )

    session = await _session(backend, cfg)
    try:
        assert session.load_cookies(tmp_path) is True
        assert session.get_cookie("dead") is None
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_invalid_same_site_falls_back_without_raising(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    path = tmp_path / FILENAMES[backend]
    write_cookie_items(
        path,
        [
            {
                "name": "sid",
                "value": "v",
                "domain": "example.com",
                "path": "/",
                "same_site": "definitely-not-a-samesite-value",
            }
        ],
    )

    session = await _session(backend, cfg)
    try:
        assert session.load_cookies(tmp_path) is True
        assert session.get_cookie("sid") == "v"
        cookie = _jar_cookie(session, backend)
        if backend == "aiohttp":
            assert cookie["samesite"] == "Lax"
        else:
            assert getattr(cookie, "_rest", {})["SameSite"] == "Lax"
    finally:
        await session.close()
