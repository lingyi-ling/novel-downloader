#!/usr/bin/env python3
"""Fetcher tests: the app API is preferred, the web reader stays the fallback."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import novel_downloader.plugins.base.fetcher as fetcher_mod
import novel_downloader.plugins.sites.fanqienovel.fetcher as fetcher_module
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.plugins.sites.fanqienovel import appapi
from novel_downloader.plugins.sites.fanqienovel.fetcher import FanqienovelFetcher
from novel_downloader.schemas import FetcherConfig

KEY = b"\x07" * 16
WEB_PREFIX = "https://fanqienovel.com/reader/"

Handler = Callable[[str, dict[str, Any]], BaseResponse]


class FakeSession:
    """In-memory stand-in for ``BaseSession`` (no network in these tests)."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def init(self, **kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> BaseResponse:
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> BaseResponse:
        self.calls.append((url, kwargs))
        return BaseResponse(content=b"")

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        return False

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        return True

    def update_cookies(self, cookies: dict[str, str]) -> None:
        return None

    @property
    def headers(self) -> dict[str, str]:
        return {}


def _encrypt(plain: str) -> str:
    iv = b"\x00" * 16
    body = pad(plain.encode(), 16)
    return base64.b64encode(iv + AES.new(KEY, AES.MODE_CBC, iv).encrypt(body)).decode()


async def _refuse_mint(session: Any, cache_dir: Path) -> None:
    return None


def _device_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "device.json"
    device.write_text(
        json.dumps(
            {
                "device_id": "1026320456490435",
                "install_id": "1026320456494531",
                "content_key": base64.b64encode(KEY).decode(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(appapi.DEVICE_ENV_VAR, str(device))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def _no_device(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(appapi.DEVICE_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def _make(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
) -> tuple[FanqienovelFetcher, FakeSession]:
    session = FakeSession(handler)
    monkeypatch.setattr(fetcher_mod, "create_session", lambda **kwargs: session)

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fetcher_mod, "async_jitter_sleep", _no_sleep)

    fetcher = FanqienovelFetcher(
        FetcherConfig(cache_dir=str(tmp_path / "cache"), max_rps=0, retry_times=0)
    )
    return fetcher, session


def _app_body(html: str, *, crypt_status: int = 0) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "data": {
                "title": "第280章 练题",
                "content": _encrypt(html),
                "crypt_status": crypt_status,
            },
        }
    ).encode()


@pytest.mark.asyncio
async def test_app_api_text_is_returned_when_a_device_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    body = _app_body('<article><p idx="0">第一段</p><p idx="1">第二段</p></article>')
    fetcher, session = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=body)
    )

    pages = await fetcher.fetch_chapter_content(
        "7677527944792689689", "7704667598306165273"
    )

    assert len(pages) == 1
    assert "第一段" in pages[0]
    assert "isChapterLock" in pages[0]
    url, kwargs = session.calls[0]
    assert url.startswith("https://reading.snssdk.com/reading/reader/full/v/?")
    assert "version_code=66.9" in url
    headers = dict(kwargs["headers"])
    assert headers["X-Gorgon"].startswith("8404")
    assert headers["User-Agent"] == "com.dragon.read"


@pytest.mark.asyncio
async def test_web_reader_is_used_when_the_device_has_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    monkeypatch.setattr(fetcher_module, "mint_device", _refuse_mint)
    body = _app_body("", crypt_status=2)
    fetcher, session = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=body)
    )

    await fetcher.fetch_chapter_content("7677527944792689689", "7704667598306165273")

    assert session.calls[0][0].startswith("https://reading.snssdk.com")
    assert session.calls[1][0].startswith(WEB_PREFIX)


@pytest.mark.asyncio
async def test_web_reader_is_used_without_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_device(monkeypatch, tmp_path)
    fetcher, session = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"<html></html>")
    )

    await fetcher.fetch_chapter_content("7677527944792689689", "7704667598306165273")

    assert len(session.calls) == 1
    assert session.calls[0][0].startswith(WEB_PREFIX)


@pytest.mark.asyncio
async def test_non_numeric_chapter_id_skips_the_app_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    fetcher, session = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"<html></html>")
    )

    await fetcher.fetch_chapter_content("7677527944792689689", "chapter-1")

    assert len(session.calls) == 1
    assert session.calls[0][0].startswith(WEB_PREFIX)


@pytest.mark.asyncio
async def test_transport_failure_falls_back_to_the_web_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        if url.startswith("https://reading.snssdk.com"):
            raise ConnectionError("boom")
        return BaseResponse(content=b"<html></html>")

    fetcher, session = _make(tmp_path, monkeypatch, handler)

    await fetcher.fetch_chapter_content("7677527944792689689", "7704667598306165273")

    assert session.calls[-1][0].startswith(WEB_PREFIX)


@pytest.mark.asyncio
async def test_device_is_discovered_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    body = _app_body("<p>正文</p>")
    fetcher, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=body)
    )

    for _ in range(3):
        await fetcher.fetch_chapter_content("1", "7704667598306165273")

    calls: list[Path] = []

    def _spy(cache_dir: Path) -> appapi.FanqieDevice | None:
        calls.append(cache_dir)
        return None

    monkeypatch.setattr(fetcher_module, "discover_device", _spy)
    fetcher._device_checked = False  # white-box: forget the cached lookup
    await fetcher.fetch_chapter_content("1", "7704667598306165273")
    await fetcher.fetch_chapter_content("1", "7704667598306165273")
    assert len(calls) == 1


def test_handles_locked_chapters_follows_device_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_device(monkeypatch, tmp_path)
    without, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"{}")
    )
    assert without.handles_locked_chapters is False

    _device_file(tmp_path, monkeypatch)
    with_device, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"{}")
    )
    assert with_device.handles_locked_chapters is True


@pytest.mark.asyncio
async def test_init_registers_a_device_when_none_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_device(monkeypatch, tmp_path)
    created = appapi.FanqieDevice(
        device_id="555", install_id="666", content_key=b"\x09" * 16
    )
    calls: list[Path] = []

    async def _fake_mint(session: Any, cache_dir: Path) -> appapi.FanqieDevice:
        calls.append(cache_dir)
        return created

    monkeypatch.setattr(fetcher_module, "mint_device", _fake_mint)
    fetcher, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"{}")
    )

    await fetcher.init()

    assert calls == [tmp_path / "cache" / "fanqienovel"]
    assert fetcher._device_cache is created
    assert fetcher.handles_locked_chapters is True


@pytest.mark.asyncio
async def test_init_skips_registration_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_device(monkeypatch, tmp_path)
    monkeypatch.setenv("NOVELDL_FANQIE_AUTO_REGISTER", "0")
    called = False

    async def _fake_mint(session: Any, cache_dir: Path) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(fetcher_module, "mint_device", _fake_mint)
    fetcher, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=b"{}")
    )

    await fetcher.init()

    assert called is False
    assert fetcher.handles_locked_chapters is False


@pytest.mark.asyncio
async def test_key_rejection_triggers_one_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused key must not silently drop the chapter: renew once, then retry."""
    _device_file(tmp_path, monkeypatch)
    renewed = appapi.FanqieDevice(device_id="777", install_id="888", content_key=KEY)
    calls: list[int] = []

    async def _fake_mint(session: Any, cache_dir: Path) -> appapi.FanqieDevice:
        calls.append(1)
        return renewed

    monkeypatch.setattr(fetcher_module, "mint_device", _fake_mint)
    bodies = [
        _app_body("", crypt_status=2),
        _app_body("<p>正文</p>"),
    ]

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        if url.startswith("https://reading.snssdk.com"):
            return BaseResponse(
                content=bodies.pop(0) if bodies else _app_body("<p>x</p>")
            )
        return BaseResponse(content=b"<html></html>")

    fetcher, session = _make(tmp_path, monkeypatch, handler)

    pages = await fetcher.fetch_chapter_content("1", "7704667598306165273")

    assert calls == [1]
    assert "正文" in pages[0]
    assert sum(1 for u, _ in session.calls if u.startswith("https://reading")) == 2


@pytest.mark.asyncio
async def test_key_rejection_gives_up_after_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    calls: list[int] = []

    async def _fake_mint(session: Any, cache_dir: Path) -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(fetcher_module, "mint_device", _fake_mint)
    fetcher, session = _make(
        tmp_path,
        monkeypatch,
        lambda url, kw: BaseResponse(content=_app_body("", crypt_status=2)),
    )

    await fetcher.fetch_chapter_content("1", "7704667598306165273")

    assert calls == [1]
    assert session.calls[-1][0].startswith("https://fanqienovel.com/reader/")


_BOOK_PAGE = (
    "<html><script>window.__INITIAL_STATE__ = "
    '{"page":{"bookName":"B"},"reader":{"chapterData":{}}};</script>'
    '<a data-item="1"></a>' + '"itemId":"111"' + '"itemId":"222"' + "</html>"
)


def _batch_body(ids: list[str]) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "data": {
                cid: {
                    "title": f"章{cid}",
                    "content": _encrypt(f"<p>正文{cid}</p>"),
                    "crypt_status": 0,
                }
                for cid in ids
            },
        }
    ).encode()


@pytest.mark.asyncio
async def test_book_info_records_the_chapter_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)
    fetcher, _ = _make(
        tmp_path, monkeypatch, lambda url, kw: BaseResponse(content=_BOOK_PAGE.encode())
    )

    await fetcher.fetch_book_info("b1")

    assert fetcher._catalog["b1"] == ["111", "222"]


@pytest.mark.asyncio
async def test_consecutive_chapters_share_one_batch_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        if "batch_full" in url:
            return BaseResponse(content=_batch_body(["111", "222"]))
        if url.startswith(WEB_PREFIX) or "page/" in url:
            return BaseResponse(content=_BOOK_PAGE.encode())
        return BaseResponse(content=b"{}")

    fetcher, session = _make(tmp_path, monkeypatch, handler)
    await fetcher.fetch_book_info("b1")
    before = len(session.calls)

    first = await fetcher.fetch_chapter_content("b1", "111")
    second = await fetcher.fetch_chapter_content("b1", "222")

    assert "正文111" in first[0]
    assert "正文222" in second[0]
    batch_calls = [u for u, _ in session.calls[before:] if "batch_full" in u]
    assert len(batch_calls) == 1


@pytest.mark.asyncio
async def test_batch_request_carries_argus_and_ladon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _device_file(tmp_path, monkeypatch)

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        if "batch_full" in url:
            return BaseResponse(content=_batch_body(["111"]))
        return BaseResponse(content=_BOOK_PAGE.encode())

    fetcher, session = _make(tmp_path, monkeypatch, handler)
    await fetcher.fetch_book_info("b1")
    await fetcher.fetch_chapter_content("b1", "111")

    batch = next(kw for u, kw in session.calls if "batch_full" in u)
    headers = dict(batch["headers"])
    assert headers["x-argus"].startswith("8o")
    ladon = base64.b64decode(headers["x-ladon"])
    assert len(ladon) == 36
    assert "X-Gorgon" in headers
