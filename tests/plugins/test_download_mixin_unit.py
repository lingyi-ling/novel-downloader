#!/usr/bin/env python3
"""tests.plugins.test_download_mixin_unit
-----------------------------------------

Targeted tests for :mod:`novel_downloader.plugins.mixins.download`.

The base-client suite covers the plain happy path; these tests exercise the
remaining branches of ``DownloadMixin``: the empty download plan, UI progress
reporting, storage upsert failures, task cancellation, the metadata cache
hit/expiry paths, ``_dl_fix_chapter_ids`` repair logic, image caching and
``cache_media``.

Everything runs offline: the registry is monkeypatched with fetcher/parser
doubles, following the conventions of ``tests/plugins/test_base_client.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.mixins.download import STOP, DownloadMixin
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    ClientConfig,
    FetcherConfig,
    ParserConfig,
)

SITE = "demo"


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class FakeFetcher:
    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.is_logged_in = False
        self.book_info_calls = 0
        self.chapter_calls = 0
        self.book_info_pages = ["<html>info</html>"]
        self.chapter_pages = ["<html>chapter</html>"]
        self.book_info_errors: list[BaseException] = []
        self.media_calls: list[dict[str, Any]] = []
        self.image_calls: list[str] = []
        self.images_calls: list[list[str]] = []

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def login(self, **kwargs: Any) -> bool:
        return True

    async def load_state(self) -> bool:
        return True

    async def save_state(self) -> bool:
        return True

    @property
    def login_fields(self) -> list[Any]:
        return []

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        self.book_info_calls += 1
        if self.book_info_errors:
            raise self.book_info_errors.pop(0)
        return list(self.book_info_pages)

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls += 1
        return list(self.chapter_pages)

    async def fetch_image(
        self,
        url: str,
        img_dir: Path,
        *,
        name: str | None = None,
        on_exist: str = "skip",
    ) -> Path | None:
        self.image_calls.append(url)
        return None

    async def fetch_images(
        self,
        img_dir: Path,
        urls: list[str],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None:
        self.images_calls.append(list(urls))

    async def fetch_media(
        self,
        media_dir: Path,
        resources: list[Any],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None:
        self.media_calls.append({"resources": list(resources), "on_exist": on_exist})


class FakeParser:
    def __init__(self) -> None:
        self.book_info_result: BookInfoDict | None = _book_info()
        self.chapter_result: ChapterDict | None = _chapter()
        self.chapter_results: dict[str, ChapterDict] = {}

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        import copy

        return copy.deepcopy(self.book_info_result)

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        import copy

        if chapter_id in self.chapter_results:
            return copy.deepcopy(self.chapter_results[chapter_id])
        return copy.deepcopy(self.chapter_result)


class FakeUI:
    """Records DownloadUI callbacks."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.progress: list[tuple[int, int]] = []
        self.completed: list[str] = []

    async def on_start(self, book: Any) -> None:
        self.started.append(book.book_id)

    async def on_progress(self, done: int, total: int) -> None:
        self.progress.append((done, total))

    async def on_complete(self, book: Any, *, failed: int = 0) -> None:
        self.completed.append(book.book_id)


def _book_info(chapters: list[dict[str, Any]] | None = None) -> BookInfoDict:
    return {
        "book_name": "Book",
        "author": "Author",
        "cover_url": "",
        "update_time": "",
        "serial_status": "",
        "summary": "",
        "tags": [],
        "volumes": [
            {
                "volume_name": "V",
                "chapters": chapters
                if chapters is not None
                else [{"title": "C1", "url": "u1", "chapterId": "c1"}],
            }
        ],
        "extra": {},
    }


def _chapter(chapter_id: str = "c1") -> ChapterDict:
    return {"id": chapter_id, "title": "title", "content": "body", "extra": {}}


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    fetchers: list[FakeFetcher] = []
    parser = FakeParser()

    def _get_fetcher(site: str, cfg: FetcherConfig) -> FakeFetcher:
        fetcher = FakeFetcher(cfg)
        fetchers.append(fetcher)
        return fetcher

    def _get_parser(site: str, cfg: ParserConfig) -> FakeParser:
        return parser

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    monkeypatch.setattr(registrar, "get_parser", _get_parser)
    return SimpleNamespace(fetchers=fetchers, parser=parser)


async def _client(
    tmp_path: Path, harness: SimpleNamespace, **overrides: Any
) -> tuple[CommonClient, FakeFetcher]:
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.0,
        "retry_times": 0,
    }
    kwargs.update(overrides)
    client = CommonClient(SITE, ClientConfig(**kwargs))
    await client.init(FetcherConfig(), ParserConfig())
    return client, harness.fetchers[-1]


def _book_dir(tmp_path: Path, book_id: str = "b1") -> Path:
    return tmp_path / "raw" / SITE / book_id


def _save_info(book_dir: Path, info: dict[str, Any]) -> None:
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "book_info.raw.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )


def _load_info(book_dir: Path) -> dict[str, Any]:
    return json.loads((book_dir / "book_info.raw.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# StopToken
# --------------------------------------------------------------------------- #


def test_stop_token_repr_is_stop() -> None:
    assert repr(STOP) == "STOP"


# --------------------------------------------------------------------------- #
# download_book()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_download_book_empty_plan_reports_start_only(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(chapters=[])
    client, fetcher = await _client(tmp_path, harness)
    ui = FakeUI()

    await client.download_book(BookConfig(book_id="b1"), ui=ui)

    assert ui.started == ["b1"]
    assert ui.completed == []  # nothing downloaded -> no completion callback
    assert ui.progress == []
    assert fetcher.chapter_calls == 0


@pytest.mark.asyncio
async def test_download_book_reports_progress_and_completion(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(
        tmp_path, harness, storage_batch_size=1, cache_chapter=False
    )
    ui = FakeUI()

    await client.download_book(BookConfig(book_id="b1"), ui=ui)

    assert ui.started == ["b1"]
    assert ui.completed == ["b1"]
    assert ui.progress[-1] == (1, 1)
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_download_book_survives_storage_upsert_failure(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.plugins.mixins import download as download_mod

    class _FailingStorage:
        calls = 0

        def __init__(self, base: Path, filename: str = "chapter.raw.sqlite") -> None:
            self.base = base

        def __enter__(self) -> _FailingStorage:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def need_refetch(self, cid: str) -> bool:
            return False

        def upsert_chapters(
            self, batch: list[ChapterDict], need_refetch: bool = False
        ) -> None:
            type(self).calls += 1
            raise RuntimeError("database is locked")

        def upsert_chapter(self, data: ChapterDict) -> None:
            raise RuntimeError("database is locked")

        def get_chapter(self, cid: str) -> ChapterDict | None:
            return None

        def get_chapters(self, ids: list[str]) -> dict[str, ChapterDict | None]:
            return dict.fromkeys(ids)

        def exists(self, cid: str) -> bool:
            return False

    monkeypatch.setattr(download_mod, "ChapterStorage", _FailingStorage)
    client, _ = await _client(tmp_path, harness, cache_chapter=False)
    ui = FakeUI()

    # The batch failure is logged and swallowed; the run still completes.
    await client.download_book(BookConfig(book_id="b1"), ui=ui)

    assert _FailingStorage.calls >= 1
    assert ui.completed == ["b1"]


@pytest.mark.asyncio
async def test_download_book_cancellation_propagates_and_flushes(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.plugins.mixins import download as download_mod

    async def _slow_sleep(**kwargs: Any) -> None:
        await asyncio.sleep(0.2)

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _slow_sleep)
    client, _ = await _client(tmp_path, harness, cache_chapter=False)

    task = asyncio.create_task(client.download_book(BookConfig(book_id="b1")))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Metadata was fetched and stored before the cancellation.
    assert (_book_dir(tmp_path) / "book_info.raw.json").is_file()


# --------------------------------------------------------------------------- #
# download_book(): cover/volume image caching
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_download_book_caches_cover_and_volume_covers(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    info = _book_info()
    info["cover_url"] = "https://img.example/cover.jpg"
    info["volumes"][0]["volume_cover"] = "https://img.example/v1.jpg"
    harness.parser.book_info_result = info
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    await client.download_book(BookConfig(book_id="b1"))

    assert fetcher.image_calls == ["https://img.example/cover.jpg"]
    assert fetcher.images_calls == [["https://img.example/v1.jpg"]]


# --------------------------------------------------------------------------- #
# _dl_fix_chapter_ids(): chapterId repair
# --------------------------------------------------------------------------- #


def _broken_info() -> BookInfoDict:
    return _book_info(
        chapters=[
            {"title": "C1", "url": "u1", "chapterId": "c1"},
            {"title": "C2", "url": "u2"},  # missing chapterId
        ]
    )


@pytest.mark.asyncio
async def test_fix_chapter_ids_repairs_from_stored_previous_chapter(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

    harness.parser.book_info_result = _broken_info()
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    # Seed storage with the previous chapter carrying next_cid.
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    stored = _chapter("c1")
    stored["extra"] = {"next_cid": "c2"}
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        storage.upsert_chapter(stored)

        repaired = await client._dl_fix_chapter_ids("b1", _broken_info(), storage)

    # Repaired from local storage: no chapter had to be fetched.
    assert fetcher.chapter_calls == 0
    assert repaired["volumes"][0]["chapters"][1]["chapterId"] == "c2"
    # The repaired metadata is persisted for later runs.
    assert _load_info(book_dir)["volumes"][0]["chapters"][1]["chapterId"] == "c2"


@pytest.mark.asyncio
async def test_fix_chapter_ids_refetches_previous_when_storage_empty(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

    fetched = _chapter("c1")
    fetched["extra"] = {"next_cid": "c2"}
    harness.parser.chapter_results["c1"] = fetched
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        repaired = await client._dl_fix_chapter_ids("b1", _broken_info(), storage)

        # Storage was empty: the previous chapter was fetched to discover
        # next_cid, and the fetched chapter was cached for the next repair.
        assert fetcher.chapter_calls == 1
        assert storage.exists("c1")

    assert repaired["volumes"][0]["chapters"][1]["chapterId"] == "c2"


@pytest.mark.asyncio
async def test_fix_chapter_ids_without_next_cid_keeps_chapter_unrepaired(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

    # The previous chapter parses but exposes no next_cid.
    fetched = _chapter("c1")
    fetched["extra"] = {}
    harness.parser.chapter_results["c1"] = fetched
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        repaired = await client._dl_fix_chapter_ids("b1", _broken_info(), storage)

    assert "chapterId" not in repaired["volumes"][0]["chapters"][1]


@pytest.mark.asyncio
async def test_fix_chapter_ids_leaves_first_chapter_without_id_alone(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

    info = _book_info(
        chapters=[{"title": "C0", "url": "u0"}, {"title": "C1", "url": "u1"}]
    )
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        repaired = await client._dl_fix_chapter_ids("b1", info, storage)

    # No previous id to infer from: no repair, no fetch for the repair path.
    assert fetcher.chapter_calls == 0
    assert "chapterId" not in repaired["volumes"][0]["chapters"][0]


# --------------------------------------------------------------------------- #
# cache_media()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_media_downloads_resources_and_respects_force_update(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    chapter = _chapter("c1")
    chapter["extra"] = {
        "resources": [{"url": "https://img.example/a.png", "type": "image"}]
    }
    harness.parser.chapter_result = chapter
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)

    await client.download_book(BookConfig(book_id="b1"))
    fetcher.media_calls.clear()

    await client.cache_media(BookConfig(book_id="b1"))
    await client.cache_media(BookConfig(book_id="b1"), force_update=True)

    assert [call["on_exist"] for call in fetcher.media_calls] == ["skip", "overwrite"]
    assert fetcher.media_calls[0]["resources"] == chapter["extra"]["resources"]


@pytest.mark.asyncio
async def test_cache_media_without_plan_does_nothing(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(chapters=[])
    client, fetcher = await _client(tmp_path, harness)

    # cache_media reads metadata from disk, not from the parser.
    _save_info(_book_dir(tmp_path), _book_info(chapters=[]))
    await client.cache_media(BookConfig(book_id="b1"))

    assert fetcher.media_calls == []
    assert fetcher.image_calls == []


# --------------------------------------------------------------------------- #
# get_book_info(): metadata cache
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_book_info_returns_fresh_cached_metadata(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness, cache_book_info=True)
    cached = _book_info()
    cached["book_name"] = "Cached"
    cached["last_checked"] = time.time()
    _save_info(_book_dir(tmp_path), cached)

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Cached"
    assert fetcher.book_info_calls == 0


@pytest.mark.asyncio
async def test_get_book_info_refetches_expired_cached_metadata(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness, cache_book_info=True)
    cached = _book_info()
    cached["book_name"] = "Stale"
    cached["last_checked"] = 0.0  # far older than ONE_DAY
    _save_info(_book_dir(tmp_path), cached)

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Book"  # freshly parsed value
    assert fetcher.book_info_calls == 1
    assert _load_info(_book_dir(tmp_path))["book_name"] == "Book"


@pytest.mark.asyncio
async def test_get_book_info_skips_cache_when_disabled(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness, cache_book_info=False)
    cached = _book_info()
    cached["book_name"] = "Ignored"
    cached["last_checked"] = time.time()
    _save_info(_book_dir(tmp_path), cached)

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Book"
    assert fetcher.book_info_calls == 1


# --------------------------------------------------------------------------- #
# module sanity: DownloadMixin is composed into CommonClient
# --------------------------------------------------------------------------- #


def test_common_client_composes_download_mixin() -> None:
    assert issubclass(CommonClient, DownloadMixin)
