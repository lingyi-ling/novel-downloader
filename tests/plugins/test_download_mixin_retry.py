#!/usr/bin/env python3
"""tests.plugins.test_download_mixin_retry
------------------------------------------

Companion suite to ``test_download_mixin_unit`` covering the branches the
first round left open in :mod:`novel_downloader.plugins.mixins.download`:

* ``get_chapter`` retry loop: transient fetch errors, exponential backoff,
  giving up after the last attempt, restricted/empty page short-circuits
* ``_dl_fix_chapter_ids`` when the previous chapter cannot be fetched
* ``get_book_info`` corrupted-cache and total-failure paths
* ``download_book`` cached-chapter skip, storage-worker cleanup on producer
  crash and on storage-worker timeout during cancellation
* ``cache_media`` skipping chapters missing from storage

Fully offline: the registry is monkeypatched with fetcher/parser doubles.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.mixins import download as download_mod
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    ClientConfig,
    FetcherConfig,
    ParserConfig,
)

SITE = "dretry"


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class FlakyFetcher:
    """Fetcher whose chapter/info fetches fail a configurable number of times."""

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.is_logged_in = False
        self.chapter_calls = 0
        self.book_info_calls = 0
        self.fail_chapter_times = 0  # first N chapter fetches raise
        self.fail_book_info_times = 0  # first N info fetches raise
        self.chapter_pages = ["<html>chapter</html>"]
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
        if self.book_info_calls <= self.fail_book_info_times:
            raise RuntimeError("info fetch exploded")
        return ["<html>info</html>"]

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls += 1
        if self.chapter_calls <= self.fail_chapter_times:
            raise RuntimeError(f"transient error #{self.chapter_calls}")
        return list(self.chapter_pages)

    async def fetch_image(self, url: str, img_dir: Path, **kwargs: Any) -> None:
        self.image_calls.append(url)

    async def fetch_images(self, img_dir: Path, urls: list[str], **kwargs: Any) -> None:
        self.images_calls.append(list(urls))

    async def fetch_media(
        self, media_dir: Path, resources: list[Any], **kwargs: Any
    ) -> None:
        self.media_calls.append({"resources": list(resources)})


class StubParser:
    """Parser returning configurable book/chapter results."""

    def __init__(self) -> None:
        self.book_info_result: BookInfoDict | None = _book_info()
        self.chapter_result: ChapterDict | None = _chapter()

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        return self.book_info_result

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        return self.chapter_result


class RecordingUI:
    def __init__(self) -> None:
        self.completed: list[str] = []

    async def on_start(self, book: Any) -> None: ...

    async def on_progress(self, done: int, total: int) -> None: ...

    async def on_complete(self, book: Any) -> None:
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
    fetchers: list[FlakyFetcher] = []
    parser = StubParser()

    def _get_fetcher(site: str, cfg: FetcherConfig) -> FlakyFetcher:
        fetcher = FlakyFetcher(cfg)
        fetchers.append(fetcher)
        return fetcher

    def _get_parser(site: str, cfg: ParserConfig) -> StubParser:
        return parser

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    monkeypatch.setattr(registrar, "get_parser", _get_parser)
    return SimpleNamespace(fetchers=fetchers, parser=parser)


async def _client(
    tmp_path: Path, harness: SimpleNamespace, **overrides: Any
) -> tuple[CommonClient, FlakyFetcher]:
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.5,
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


# --------------------------------------------------------------------------- #
# get_chapter(): retry loop and backoff
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_chapter_retries_transient_errors_until_success(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def _record_sleep(**kwargs: Any) -> None:
        sleeps.append(kwargs["base"])

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _record_sleep)

    client, fetcher = await _client(tmp_path, harness, retry_times=2)
    fetcher.fail_chapter_times = 2

    chap = await client.get_chapter("b1", "c1")

    assert chap is not None
    assert chap["id"] == "c1"
    assert chap["content"] == "body"
    assert fetcher.chapter_calls == 3
    # Exponential backoff: factor 0.5 -> 0.5 after attempt 0, 1.0 after attempt 1.
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_get_chapter_gives_up_after_final_attempt(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(**kwargs: Any) -> None: ...

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _no_sleep)

    client, fetcher = await _client(tmp_path, harness, retry_times=1)
    fetcher.fail_chapter_times = 99

    chap = await client.get_chapter("b1", "c1")

    assert chap is None
    assert fetcher.chapter_calls == 2  # initial attempt + one retry


@pytest.mark.asyncio
async def test_get_chapter_restricted_page_returns_none_without_retry(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fetcher = await _client(tmp_path, harness, retry_times=2)
    monkeypatch.setattr(client, "_dl_check_restricted", lambda pages: True)

    chap = await client.get_chapter("b1", "c1")

    assert chap is None
    # Restricted access is terminal: no retry is spent on it.
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_empty_parse_with_check_empty_returns_none(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.parser.chapter_result = None
    client, fetcher = await _client(tmp_path, harness, retry_times=2)
    monkeypatch.setattr(client, "_dl_check_empty", lambda pages: True)

    chap = await client.get_chapter("b1", "c1")

    assert chap is None
    # _dl_check_empty says "expected empty": no ValueError, no retry.
    assert fetcher.chapter_calls == 1


@pytest.mark.asyncio
async def test_get_chapter_empty_parse_without_check_empty_retries_then_fails(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.parser.chapter_result = None

    async def _no_sleep(**kwargs: Any) -> None: ...

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _no_sleep)

    client, fetcher = await _client(tmp_path, harness, retry_times=1)

    chap = await client.get_chapter("b1", "c1")

    # Empty parse raises ValueError internally and is retried like any
    # other failure until attempts run out.
    assert chap is None
    assert fetcher.chapter_calls == 2


# --------------------------------------------------------------------------- #
# _dl_fix_chapter_ids(): unrepairable previous chapter
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fix_chapter_ids_skips_repair_when_prev_fetch_fails(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.parser.chapter_result = None
    client, fetcher = await _client(tmp_path, harness, cache_chapter=False)
    monkeypatch.setattr(client, "_dl_check_empty", lambda pages: True)

    info = _book_info(
        chapters=[
            {"title": "C1", "url": "u1", "chapterId": "c1"},
            {"title": "C2", "url": "u2"},  # missing chapterId
        ]
    )

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        repaired = await client._dl_fix_chapter_ids("b1", info, storage)

    # The previous chapter could not be fetched: repair is skipped, the
    # missing id stays missing, and the run does not crash.
    assert fetcher.chapter_calls == 1
    assert "chapterId" not in repaired["volumes"][0]["chapters"][1]


# --------------------------------------------------------------------------- #
# get_book_info(): cache and failure boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_book_info_corrupted_cache_falls_back_to_fetch(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness, cache_book_info=True)

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_text("{not valid json", encoding="utf-8")

    info = await client.get_book_info("b1")

    assert info["book_name"] == "Book"
    assert fetcher.book_info_calls == 1


@pytest.mark.asyncio
async def test_get_book_info_fetch_error_raises_lookup_error(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness)
    fetcher.fail_book_info_times = 1

    with pytest.raises(LookupError, match="b1"):
        await client.get_book_info("b1")


@pytest.mark.asyncio
async def test_get_book_info_empty_parse_raises_lookup_error(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = None
    client, fetcher = await _client(tmp_path, harness)

    with pytest.raises(LookupError, match="b1"):
        await client.get_book_info("b1")

    assert fetcher.book_info_calls == 1


# --------------------------------------------------------------------------- #
# download_book(): cached chapters and worker cleanup
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_download_book_skips_chapters_cached_with_refetch_flag_clear(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    # Default cache_chapter=True; the stored chapter is marked as not
    # needing a refetch, so the producer never hits the network.
    client, fetcher = await _client(tmp_path, harness)

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        storage.upsert_chapter(_chapter("c1"), need_refetch=False)

    ui = RecordingUI()
    await client.download_book(BookConfig(book_id="b1"), ui=ui)

    assert fetcher.chapter_calls == 0
    assert ui.completed == ["b1"]


@pytest.mark.asyncio
async def test_download_book_cancels_stuck_storage_worker(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slow the producer down so the download is in flight when cancelled.
    async def _slow_sleep(**kwargs: Any) -> None:
        await asyncio.sleep(0.3)

    async def _wait_for_times_out(coro: Any, timeout: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _slow_sleep)
    monkeypatch.setattr(asyncio, "wait_for", _wait_for_times_out)

    client, _ = await _client(tmp_path, harness, cache_chapter=False)

    task = asyncio.create_task(client.download_book(BookConfig(book_id="b1")))
    await asyncio.sleep(0.05)
    task.cancel()

    # The storage worker ignores the STOP token within the timeout; the
    # mixin must cancel it instead of hanging forever.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_download_book_cleans_up_storage_worker_on_producer_crash(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = await _client(tmp_path, harness, cache_chapter=False)

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("producer exploded")

    monkeypatch.setattr(client, "get_chapter", _explode)

    with pytest.raises(RuntimeError, match="producer exploded"):
        await client.download_book(BookConfig(book_id="b1"))


# --------------------------------------------------------------------------- #
# cache_media(): missing chapters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_media_skips_chapters_missing_from_storage(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client, fetcher = await _client(tmp_path, harness)

    # Metadata lists one chapter but the chapter store is empty.
    _save_info(_book_dir(tmp_path), _book_info())

    await client.cache_media(BookConfig(book_id="b1"))

    assert fetcher.media_calls == []
    assert fetcher.image_calls == []
    assert fetcher.images_calls == []
