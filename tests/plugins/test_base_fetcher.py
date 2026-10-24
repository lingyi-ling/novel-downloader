#!/usr/bin/env python3
"""Offline robustness tests for :mod:`novel_downloader.plugins.base.fetcher`.

Every HTTP interaction is served by an in-memory fake session, so these tests
never touch the network.  They pin down the current error-handling contract:
what is retried, what is swallowed, and what is left on disk after a failure.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.base.fetcher as fetcher_mod
from novel_downloader.infra.http_defaults import IMAGE_HEADERS
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.libs.filesystem import font_filename, image_filename
from novel_downloader.plugins.base.fetcher import BaseFetcher, GenericFetcher
from novel_downloader.schemas import FetcherConfig

Handler = Callable[[str, dict[str, Any]], BaseResponse | BaseException]


class FakeSession:
    """Minimal stand-in for ``BaseSession`` used by :class:`BaseFetcher`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.init_calls = 0
        self.close_calls = 0
        self.load_calls = 0
        self.save_calls = 0
        self.updated_cookies: dict[str, str] = {}
        self.load_result = False
        self.save_result = True
        self._headers = {"User-Agent": "test-agent"}

    async def init(self, **kwargs: Any) -> None:
        self.init_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def get(self, url: str, **kwargs: Any) -> BaseResponse:
        self.calls.append((url, kwargs))
        result = self.handler(url, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        self.load_calls += 1
        return self.load_result

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        self.save_calls += 1
        return self.save_result

    def update_cookies(self, cookies: dict[str, str]) -> None:
        self.updated_cookies.update(cookies)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)


class _Fetcher(BaseFetcher):
    site_name = "fake"
    login_status = True

    async def _check_login_status(self) -> bool:
        return type(self).login_status

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return []

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        return []


def _make(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler | None = None,
    *,
    fetcher_cls: type[BaseFetcher] = _Fetcher,
    **cfg_kwargs: Any,
) -> tuple[BaseFetcher, FakeSession]:
    session = FakeSession(handler or (lambda url, kw: BaseResponse(content=b"ok")))
    monkeypatch.setattr(fetcher_mod, "create_session", lambda **kwargs: session)

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fetcher_mod, "async_jitter_sleep", _no_sleep)

    cfg_kwargs.setdefault("max_rps", 0)  # no rate-limiter sleeps
    cfg_kwargs.setdefault("request_interval", 0)
    cfg_kwargs.setdefault("backoff_factor", 0)
    fetcher = fetcher_cls(FetcherConfig(**cfg_kwargs))
    return fetcher, session


def _sequence_handler(
    *responses: BaseResponse | BaseException,
) -> Handler:
    """Return one response per call, repeating the last one forever."""
    queue = list(responses)

    def _handler(url: str, kwargs: dict[str, Any]) -> BaseResponse | BaseException:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item

    return _handler


# --------------------------------------------------------------------------- #
# fetch(): retry boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_returns_decoded_text(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "正文内容".encode()
    fetcher, session = _make(monkeypatch, lambda url, kw: BaseResponse(content=body))

    assert await fetcher.fetch("https://demo/1", encoding="utf-8") == "正文内容"
    assert session.calls[0][1]["encoding"] == "utf-8"


@pytest.mark.asyncio
async def test_fetch_retries_http_errors_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _sequence_handler(
        BaseResponse(content=b"", status=500),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_fetch_raises_connection_error_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(content=b"", status=503)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    with pytest.raises(ConnectionError, match="status 503"):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 3  # retry_times + 1


@pytest.mark.asyncio
async def test_fetch_zero_retries_makes_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(content=b"", status=404)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(ConnectionError):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_propagates_transport_errors_without_inner_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport exceptions are retried by ``DownloadMixin.get_chapter`` only.

    Keeping them out of ``fetch`` avoids multiplying the two retry loops.
    """
    handler = lambda url, kw: OSError("connection reset")  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    with pytest.raises(OSError, match="connection reset"):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_empty_body_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b""))

    assert await fetcher.fetch("https://demo/1") == ""


@pytest.mark.asyncio
async def test_fetch_falls_back_for_misdeclared_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Server claims utf-8 but actually sends GBK; BaseResponse.text falls back.
    body = "中文".encode("gbk")
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=body, encoding="utf-8")
    )

    assert await fetcher.fetch("https://demo/1") == "中文"


# --------------------------------------------------------------------------- #
# fetch_data(): binary downloads never raise
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_data_returns_bytes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b"raw"))

    assert await fetcher.fetch_data("https://demo/a") == b"raw"


@pytest.mark.asyncio
async def test_fetch_data_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"x", status=404)
    )

    assert await fetcher.fetch_data("https://demo/a") is None


@pytest.mark.asyncio
async def test_fetch_data_returns_none_on_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b""))

    assert await fetcher.fetch_data("https://demo/a") is None


@pytest.mark.asyncio
async def test_fetch_data_swallows_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: ConnectionError("boom"))

    assert await fetcher.fetch_data("https://demo/a") is None


# --------------------------------------------------------------------------- #
# media writes: failures leave no partial artefacts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_image_uses_image_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"\x89PNG")
    )

    path = await fetcher.fetch_image("https://img/a.png", tmp_path)

    assert path is not None and path.is_file()
    assert session.calls[0][1]["headers"] == IMAGE_HEADERS


@pytest.mark.asyncio
async def test_fetch_image_skips_existing_file_without_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://img/a.png"
    existing = tmp_path / image_filename(url)
    existing.write_bytes(b"old")

    fetcher, session = _make(monkeypatch)

    assert await fetcher.fetch_image(url, tmp_path) == existing
    assert session.calls == []
    assert existing.read_bytes() == b"old"


@pytest.mark.asyncio
async def test_fetch_image_failure_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"", status=500)
    )
    img_dir = tmp_path / "media"

    assert await fetcher.fetch_image("https://img/a.png", img_dir) is None
    assert not img_dir.exists()


@pytest.mark.asyncio
async def test_fetch_images_continues_after_partial_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    urls = ["https://img/a.png", "https://img/b.png", "https://img/c.png"]
    bodies = {
        urls[0]: BaseResponse(content=b"A"),
        urls[1]: BaseResponse(content=b"", status=404),
        urls[2]: BaseResponse(content=b"C"),
    }
    fetcher, _ = _make(monkeypatch, lambda url, kw: bodies[url])

    await fetcher.fetch_images(tmp_path, urls, concurrent=2)

    saved = {p.name for p in tmp_path.iterdir()}
    assert saved == {image_filename(urls[0]), image_filename(urls[2])}


@pytest.mark.asyncio
async def test_fetch_images_empty_list_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(monkeypatch)
    img_dir = tmp_path / "media"

    await fetcher.fetch_images(img_dir, [])

    assert not img_dir.exists()
    assert session.calls == []


@pytest.mark.asyncio
async def test_fetch_images_all_failed_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"", status=500)
    )
    img_dir = tmp_path / "media"

    await fetcher.fetch_images(img_dir, ["https://img/a.png"])

    assert not img_dir.exists()


@pytest.mark.asyncio
async def test_fetch_fonts_saves_successful_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://fonts/a.woff2"
    fetcher, _ = _make(monkeypatch, lambda u, kw: BaseResponse(content=b"font"))

    await fetcher.fetch_fonts(tmp_path, [url])

    assert (tmp_path / font_filename(url)).read_bytes() == b"font"


@pytest.mark.asyncio
async def test_fetch_media_dispatches_by_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = "https://img/a.png"
    font = "https://fonts/a.woff2"
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b"x"))

    await fetcher.fetch_media(
        tmp_path,
        [
            {"url": image, "type": "image"},
            {"url": font, "type": "font"},
            {"url": "https://other/a.mp3", "type": "audio"},
            {"type": "image"},  # no url
        ],
    )

    assert {p.name for p in tmp_path.iterdir()} == {
        image_filename(image),
        font_filename(font),
    }


@pytest.mark.asyncio
async def test_fetch_media_without_downloadable_entries_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(monkeypatch)
    media_dir = tmp_path / "media"

    await fetcher.fetch_media(media_dir, [{"type": "audio", "url": "https://x/a"}])

    assert not media_dir.exists()
    assert session.calls == []


# --------------------------------------------------------------------------- #
# login / session state
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_without_cookies_fails_without_touching_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    assert await fetcher.login() is False
    assert session.updated_cookies == {}
    assert fetcher.is_logged_in is False


@pytest.mark.asyncio
async def test_login_with_cookies_updates_session_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    assert await fetcher.login(cookies={"sid": "abc"}) is True
    assert session.updated_cookies == {"sid": "abc"}
    assert fetcher.is_logged_in is True


@pytest.mark.asyncio
async def test_login_reports_false_when_status_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RejectingFetcher(_Fetcher):
        login_status = False

    fetcher, _ = _make(monkeypatch, fetcher_cls=_RejectingFetcher)

    assert await fetcher.login(cookies={"sid": "bad"}) is False
    assert fetcher.is_logged_in is False


@pytest.mark.asyncio
async def test_load_state_delegates_to_session_and_reports_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)
    session.load_result = True

    assert await fetcher.load_state() is True
    assert session.load_calls == 1
    assert fetcher.is_logged_in is True


@pytest.mark.asyncio
async def test_save_state_delegates_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)
    session.save_result = False

    assert await fetcher.save_state() is False
    assert session.save_calls == 1


@pytest.mark.asyncio
async def test_context_manager_initialises_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    async with fetcher:
        assert session.init_calls == 1

    assert session.close_calls == 1


# --------------------------------------------------------------------------- #
# GenericFetcher URL/error paths
# --------------------------------------------------------------------------- #
class _Generic(GenericFetcher):
    site_name = "generic"
    BASE_URL = "https://demo.test"
    DEFAULT_BASE_URL = "https://demo.test"
    BOOK_INFO_URL = "{base_url}/book/{book_id}"
    CHAPTER_URL = "{base_url}/chapter/{book_id}/{chapter_id}"


@pytest.mark.asyncio
async def test_generic_fetcher_returns_single_info_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=b"<html>info</html>"),
        fetcher_cls=_Generic,
    )

    assert await fetcher.fetch_book_info("7") == ["<html>info</html>"]
    assert session.calls[0][0] == "https://demo.test/book/7"


@pytest.mark.asyncio
async def test_generic_fetcher_missing_base_url_for_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Paginated(_Generic):
        BASE_URL = ""
        USE_PAGINATED_CHAPTER = True

        @classmethod
        def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
            return f"/{book_id}/{chapter_id}/{idx}"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_Paginated)

    with pytest.raises(RuntimeError, match="BASE_URL is required"):
        await fetcher.fetch_chapter_content("7", "1")


@pytest.mark.asyncio
async def test_generic_fetcher_requires_url_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoTemplates(GenericFetcher):
        site_name = "notemplates"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_NoTemplates)

    with pytest.raises(NotImplementedError, match="BOOK_INFO_URL not set"):
        await fetcher.fetch_book_info("7")
    with pytest.raises(NotImplementedError, match="CHAPTER_URL not set"):
        await fetcher.fetch_chapter_content("7", "1")


@pytest.mark.asyncio
async def test_generic_fetcher_transforms_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Prefixed(_Generic):
        BOOK_ID_REPLACEMENTS = [("-", "/")]
        CHAP_ID_REPLACEMENTS = [("_", "/")]

    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=b"ok"),
        fetcher_cls=_Prefixed,
    )

    await fetcher.fetch_chapter_content("a-b", "c_d")

    assert session.calls[0][0] == "https://demo.test/chapter/a/b/c/d"


@pytest.mark.asyncio
async def test_generic_fetcher_appends_separate_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WithCatalog(_Generic):
        HAS_SEPARATE_CATALOG = True
        BOOK_CATALOG_URL = "{base_url}/catalog/{book_id}"

    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=url.encode()),
        fetcher_cls=_WithCatalog,
    )

    pages = await fetcher.fetch_book_info("7")

    assert pages == [
        "https://demo.test/book/7",
        "https://demo.test/catalog/7",
    ]


@pytest.mark.asyncio
async def test_generic_fetcher_pagination_stops_without_next_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Paginated(_Generic):
        USE_PAGINATED_CHAPTER = True

        @classmethod
        def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
            return f"/chapter/{book_id}/{chapter_id}_{idx}"

    pages = {
        "https://demo.test/chapter/7/1_1": 'second at "/chapter/7/1_2"',
        "https://demo.test/chapter/7/1_2": "last page",
    }
    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=pages[url].encode()),
        fetcher_cls=_Paginated,
    )

    result = await fetcher.fetch_chapter_content("7", "1")

    assert result == [
        'second at "/chapter/7/1_2"',
        "last page",
    ]
    assert len(session.calls) == 2


def test_resolve_base_url_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Variants(GenericFetcher):
        site_name = "variants"
        BASE_URL_MAP = {"traditional": "https://t.example"}
        DEFAULT_BASE_URL = "https://s.example"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_Variants)

    assert fetcher._base_url == "https://s.example"
