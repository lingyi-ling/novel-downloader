"""Cookie persistence hardening: path isolation, scope, atomic writes."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp.cookiejar
import aiohttp.web
import pytest
import pytest_asyncio

from novel_downloader.infra.cookies import CookieStore
from novel_downloader.infra.sessions import create_session
from novel_downloader.schemas import FetcherConfig

BACKENDS = ["aiohttp", "httpx", "curl_cffi"]
FILENAMES = {
    "aiohttp": "aiohttp.cookies",
    "httpx": "httpx.cookies",
    "curl_cffi": "curl_cffi.cookies",
}
ALLOWED_KEYS = {"name", "value", "domain", "path"}


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
        return aiohttp.web.json_response({"cookie": request.headers.get("Cookie", "")})

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
    return await aiohttp_server(app)


async def _initialized(backend: str, cfg: FetcherConfig, cookies=None):
    session = create_session(backend, cfg, cookies=cookies)
    await session.init()
    return session


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_save_and_load_require_initialized_session(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    session = create_session(backend, cfg)
    assert session.save_cookies(tmp_path) is False
    assert session.load_cookies(tmp_path) is False


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_missing_file_returns_false(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    session = await _initialized(backend, cfg)
    try:
        assert session.load_cookies(tmp_path / "missing") is False
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        ".",
        "..",
        "../evil.cookies",
        "..\\evil.cookies",
        "sub/evil",
        "sub\\evil",
        "a\x00b",
    ],
)
async def test_path_traversal_filenames_are_rejected(
    backend: str, cfg: FetcherConfig, tmp_path: Path, filename: str
) -> None:
    session = await _initialized(backend, cfg)
    try:
        with pytest.raises(ValueError, match="Invalid cookie filename"):
            session.save_cookies(tmp_path, filename)
        with pytest.raises(ValueError, match="Invalid cookie filename"):
            session.load_cookies(tmp_path, filename)
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_save_creates_directories_and_returns_false_on_os_error(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    session = await _initialized(backend, cfg)
    try:
        nested = tmp_path / "one" / "two"
        assert session.save_cookies(nested) is True
        assert (nested / FILENAMES[backend]).is_file()

        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        assert session.save_cookies(blocker) is False
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_save_overwrites_and_leaves_no_temp_files(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    session = await _initialized(backend, cfg)
    try:
        session.update_cookies({"token": "first"})
        assert session.save_cookies(tmp_path) is True
        session.update_cookies({"token": "second"})
        assert session.save_cookies(tmp_path) is True
    finally:
        await session.close()

    filename = FILENAMES[backend]
    data = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    assert [entry["name"] for entry in data] == ["token"]
    assert [entry["value"] for entry in data] == ["second"]
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_saved_file_contains_only_cookie_fields(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    session = await _initialized(backend, cfg)
    try:
        session.update_cookies({"token": "secret"})
        assert session.save_cookies(tmp_path) is True
    finally:
        await session.close()

    raw = (tmp_path / FILENAMES[backend]).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data
    for entry in data:
        assert set(entry) <= ALLOWED_KEYS
        assert entry["name"] == "token"
        assert entry["value"] == "secret"
    assert "password" not in raw.lower()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_legacy_name_value_format_still_loads(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    legacy = tmp_path / FILENAMES[backend]
    legacy.write_text(
        json.dumps([{"name": "legacy", "value": "yes"}]), encoding="utf-8"
    )

    session = await _initialized(backend, cfg)
    try:
        assert session.load_cookies(tmp_path) is True
        assert session.get_cookie("legacy") == "yes"
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        '{"name": "token", "value": "x"}',
        "null",
        '[1, "x", [], {"value": "missing-name"}]',
        '[{"name": "bad name", "value": "x"}]',
    ],
)
async def test_malformed_cookie_files_do_not_raise(
    backend: str, cfg: FetcherConfig, tmp_path: Path, payload: str
) -> None:
    (tmp_path / FILENAMES[backend]).write_text(payload, encoding="utf-8")
    session = await _initialized(backend, cfg)
    try:
        session.load_cookies(tmp_path)  # return value is irrelevant, must not raise
        assert session.get_cookie("bad name") is None
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_domain_and_path_round_trip(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    (tmp_path / FILENAMES[backend]).write_text(
        json.dumps(
            [{"name": "scoped", "value": "1", "domain": "example.com", "path": "/x"}]
        ),
        encoding="utf-8",
    )
    session = await _initialized(backend, cfg)
    try:
        assert session.load_cookies(tmp_path) is True
        assert session.get_cookie("scoped") == "1"
        if backend == "aiohttp":
            jar = session.session.cookie_jar  # type: ignore[attr-defined]
            loaded = [(cookie.key, cookie["domain"], cookie["path"]) for cookie in jar]
        else:
            jar = session.session.cookies.jar  # type: ignore[attr-defined]
            loaded = [(cookie.name, cookie.domain, cookie.path) for cookie in jar]
        assert ("scoped", "example.com", "/x") in loaded
    finally:
        await session.close()


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_site_directories_are_isolated(
    backend: str, cfg: FetcherConfig, tmp_path: Path
) -> None:
    site_a = tmp_path / "site_a"
    site_b = tmp_path / "site_b"

    writer = await _initialized(backend, cfg)
    try:
        writer.update_cookies({"token": "site-a-only"})
        assert writer.save_cookies(site_a) is True
    finally:
        await writer.close()

    same_site = await _initialized(backend, cfg)
    other_site = await _initialized(backend, cfg)
    try:
        assert same_site.load_cookies(site_a) is True
        assert same_site.get_cookie("token") == "site-a-only"

        assert other_site.load_cookies(site_b) is False
        assert other_site.get_cookie("token") is None
    finally:
        await same_site.close()
        await other_site.close()

    assert CookieStore(site_a).get("token") == "site-a-only"
    assert CookieStore(site_b).get("token") == ""


@pytest.mark.asyncio
async def test_aiohttp_saved_scope_does_not_leak_to_other_host(
    cfg: FetcherConfig, tmp_path: Path, echo_server: Any
) -> None:
    session = await _initialized("aiohttp", cfg)
    try:
        set_url = str(
            echo_server.make_url("/set-cookie?name=token&value=scoped-secret")
        )
        await session.get(set_url)
        assert session.save_cookies(tmp_path) is True
    finally:
        await session.close()

    saved = json.loads((tmp_path / "aiohttp.cookies").read_text(encoding="utf-8"))
    assert saved[0]["domain"] == "127.0.0.1"

    restored = await _initialized("aiohttp", cfg)
    try:
        assert restored.load_cookies(tmp_path) is True
        same_host = await restored.get(str(echo_server.make_url("/echo")))
        other_host = await restored.get(
            f"http://localhost:{echo_server.make_url('/').port}/echo"
        )
    finally:
        await restored.close()

    assert "token=scoped-secret" in same_host.json()["cookie"]
    assert "token=scoped-secret" not in other_host.json()["cookie"]


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Windows has no POSIX permission bits"
)
@pytest.mark.asyncio
async def test_saved_file_is_owner_only(cfg: FetcherConfig, tmp_path: Path) -> None:
    session = await _initialized("aiohttp", cfg)
    try:
        session.update_cookies({"token": "secret"})
        assert session.save_cookies(tmp_path) is True
    finally:
        await session.close()

    mode = (tmp_path / "aiohttp.cookies").stat().st_mode & 0o777
    assert mode == 0o600
