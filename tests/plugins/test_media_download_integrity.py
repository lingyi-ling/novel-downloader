#!/usr/bin/env python3
"""tests.plugins.test_media_download_integrity
---------------------------------------------

Integrity of the media (image/font) download path:

* protocol-relative (``//host/x``) and root-relative URLs are resolved before
  the request; bare URLs keep the documented ``.jpg`` fallback suffix
* a 200 response that is not media (login page, JSON error, oversized body) is
  never written to disk as an image
* the same URL is downloaded once, including when two chapter workers ask for
  it concurrently
* an explicit ``name=`` cannot escape the media directory
* a write error (disk full / permission denied) is a skipped image, not an
  exception that aborts the book download
* cancelling a batch leaves neither partial files nor temporary files behind
* cover/volume-image failures are isolated from the chapter pipeline

Everything runs offline against the in-memory session double from
``tests.plugins.test_base_fetcher``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import novel_downloader.plugins.base.fetcher as fetcher_mod
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.libs.filesystem import image_filename
from novel_downloader.plugins.mixins.download import DownloadMixin
from tests.plugins.test_base_fetcher import _Fetcher, _make

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
HTML = b"<!DOCTYPE html><html><body>login required</body></html>"
JPEG_HEADERS = {"Content-Type": "image/jpeg"}


class _MediaFetcher(_Fetcher):
    """Fetcher double with a site origin so relative URLs can be resolved."""

    DEFAULT_BASE_URL = "https://example.test/"


# --------------------------------------------------------------------------- #
# URL normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_protocol_relative_url_is_requested_over_https(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        return BaseResponse(content=PNG, headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler)

    path = await fetcher.fetch_image("//cdn.example.com/a.png", tmp_path)

    assert calls == ["https://cdn.example.com/a.png"]
    assert path == tmp_path / image_filename("https://cdn.example.com/a.png")
    assert path is not None and path.read_bytes() == PNG


@pytest.mark.asyncio
async def test_root_relative_url_is_resolved_against_the_site_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        return BaseResponse(content=PNG, headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler, fetcher_cls=_MediaFetcher)

    path = await fetcher.fetch_image("/uploads/a.png", tmp_path)

    assert calls == ["https://example.test/uploads/a.png"]
    # The stored resource keeps the relative URL; the local name must be the
    # one derived from it (that is what exporters look up).
    assert path == tmp_path / image_filename("/uploads/a.png")


@pytest.mark.asyncio
async def test_extensionless_url_keeps_the_default_jpg_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=PNG))

    path = await fetcher.fetch_image("https://cdn.example.com/image?id=7", tmp_path)

    assert path is not None and path.suffix == ".jpg"


# --------------------------------------------------------------------------- #
# content validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_html_error_page_is_never_saved_as_an_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A hot-link protection / expired URL page can be served with 200 and an
    # image content-type; the body still gives it away.
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=HTML, headers=JPEG_HEADERS)
    )

    assert await fetcher.fetch_image("https://cdn.example.com/a.jpg", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_non_media_content_type_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(
            content=PNG, headers={"Content-Type": "text/html; charset=utf-8"}
        ),
    )

    assert await fetcher.fetch_image("https://cdn.example.com/a.jpg", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_octet_stream_payload_is_still_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(
            content=PNG, headers={"Content-Type": "application/octet-stream"}
        ),
    )

    path = await fetcher.fetch_image("https://cdn.example.com/a.bin", tmp_path)

    assert path is not None and path.read_bytes() == PNG


@pytest.mark.asyncio
async def test_oversized_media_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_MediaFetcher, "MAX_MEDIA_BYTES", 8)
    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=PNG, headers=JPEG_HEADERS),
        fetcher_cls=_MediaFetcher,
    )

    assert await fetcher.fetch_image("https://cdn.example.com/a.jpg", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_font_download_rejects_an_html_error_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=HTML))

    await fetcher.fetch_fonts(tmp_path, ["https://cdn.example.com/a.woff2"])

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# duplicate / concurrent downloads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_duplicate_urls_in_one_batch_are_downloaded_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        return BaseResponse(content=PNG, headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler)
    url = "https://cdn.example.com/a.jpg"

    await fetcher.fetch_images(tmp_path, [url, url, "https://cdn.example.com/b.jpg"])

    assert calls == [url, "https://cdn.example.com/b.jpg"]


@pytest.mark.asyncio
async def test_concurrent_requests_for_the_same_image_share_one_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        started.set()
        await release.wait()
        return BaseResponse(content=PNG, headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler)
    url = "https://cdn.example.com/a.jpg"

    first = asyncio.create_task(fetcher.fetch_image(url, tmp_path))
    await started.wait()
    second = asyncio.create_task(fetcher.fetch_image(url, tmp_path))
    # Let the second task reach the per-file guard before the first finishes.
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()

    paths = await asyncio.gather(first, second)

    assert calls == [url]
    assert paths[0] == paths[1] is not None
    assert paths[0].read_bytes() == PNG


@pytest.mark.asyncio
async def test_overwrite_mode_refetches_an_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        return BaseResponse(content=b"new", headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler)
    url = "https://cdn.example.com/a.jpg"
    existing = tmp_path / image_filename(url)
    existing.write_bytes(b"old")

    path = await fetcher.fetch_image(url, tmp_path, on_exist="overwrite")

    assert path == existing
    assert existing.read_bytes() == b"new"
    assert calls == [url]


@pytest.mark.asyncio
async def test_zero_byte_cached_image_is_refetched_in_skip_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        calls.append(url)
        return BaseResponse(content=PNG, headers=JPEG_HEADERS)

    fetcher, _ = _make(monkeypatch, handler)
    url = "https://cdn.example.com/a.jpg"
    cached = tmp_path / image_filename(url)
    cached.write_bytes(b"")

    path = await fetcher.fetch_image(url, tmp_path)

    assert path == cached
    assert cached.read_bytes() == PNG
    assert calls == [url]


@pytest.mark.asyncio
async def test_invalid_url_is_skipped_instead_of_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: ValueError("URL is not absolute or has no host")
    )

    assert await fetcher.fetch_image("https://cdn.example.com/a.jpg", tmp_path) is None


@pytest.mark.asyncio
async def test_malformed_url_is_skipped_before_any_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``urlparse`` itself rejects an unbalanced IPv6 literal.
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=PNG))

    assert await fetcher.fetch_image("https://[bad", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# filesystem safety
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explicit_name_cannot_escape_the_media_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media_dir = tmp_path / "media"
    outside = tmp_path / "escape.jpg"
    outside.write_bytes(b"do not touch")
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=PNG))

    path = await fetcher.fetch_image(
        "https://cdn.example.com/a.jpg", media_dir, name="../../escape"
    )

    assert path is not None
    assert path.parent == media_dir
    assert outside.read_bytes() == b"do not touch"


@pytest.mark.asyncio
async def test_write_failure_is_reported_as_a_skipped_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kwargs: Any) -> None:
        raise PermissionError("disk full")

    monkeypatch.setattr(fetcher_mod, "write_file", boom)
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=PNG))

    assert await fetcher.fetch_image("https://cdn.example.com/a.jpg", tmp_path) is None
    # The batch helper must not raise either.
    await fetcher.fetch_images(tmp_path, ["https://cdn.example.com/a.jpg"])


@pytest.mark.asyncio
async def test_cancelling_a_batch_leaves_no_partial_or_temp_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    async def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        started.set()
        await asyncio.Event().wait()  # never completes
        raise AssertionError("unreachable")

    fetcher, _ = _make(monkeypatch, handler)
    media_dir = tmp_path / "media"

    task = asyncio.create_task(
        fetcher.fetch_images(media_dir, ["https://cdn.example.com/a.jpg"])
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not media_dir.exists() or list(media_dir.iterdir()) == []


# --------------------------------------------------------------------------- #
# download.py: metadata images are best-effort
# --------------------------------------------------------------------------- #


class _BrokenImageFetcher:
    """Fetcher whose cover/volume-image fetches fail with an OSError."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_image(self, url: str, img_dir: Path, **kwargs: Any) -> None:
        self.calls.append(url)
        raise PermissionError("media directory is not writable")

    async def fetch_images(self, img_dir: Path, urls: list[str], **kwargs: Any) -> None:
        self.calls.extend(urls)
        raise OSError("no space left on device")


@pytest.mark.asyncio
async def test_cover_image_failure_does_not_abort_the_download(
    tmp_path: Path,
) -> None:
    fetcher = _BrokenImageFetcher()
    client = SimpleNamespace(fetcher=fetcher, _raw_data_dir=tmp_path, _site="test")
    book_info = {
        "cover_url": "https://cdn.example.com/cover.jpg",
        "volumes": [
            {
                "volume_name": "V",
                "volume_cover": "https://cdn.example.com/v1.jpg",
                "chapters": [],
            }
        ],
    }

    await DownloadMixin._dl_cache_info_images(client, "b1", book_info)  # type: ignore[arg-type]

    assert fetcher.calls == [
        "https://cdn.example.com/cover.jpg",
        "https://cdn.example.com/v1.jpg",
    ]
