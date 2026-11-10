#!/usr/bin/env python3
"""tests.plugins.test_download_drain
------------------------------------

Stopping semantics of ``DownloadMixin.download_book``'s storage pipeline.

The bounded save queue used to be terminated with ``put_nowait(STOP)``: when
the queue was full the code cancelled the storage worker instead, discarding up
to ``maxsize`` already-downloaded chapters.  ``succeeded`` was derived as
``total - failed``, so those chapters were reported as successes even though
they never reached the database, and the failure ledger stayed empty.

These tests pin the replacement contract:

* the worker is stopped by *draining* the queue, so a full queue at stop time
  (normal completion or cancellation) still ends up on disk;
* chapters that really cannot be saved are ledgered as failures and never
  counted as successes;
* ``asyncio.CancelledError`` is not swallowed by the failure bookkeeping.

Everything runs offline against in-memory fetcher/parser doubles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.download_ledger import DownloadLedger
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

SITE = "ddrain"
BOOK_ID = "b1"
#: One more chapter than the save queue can hold: every producer finishes and
#: the queue is still full when the stop signal arrives.
QUEUE_CAPACITY = 10
CHAPTER_IDS = [f"c{i}" for i in range(1, QUEUE_CAPACITY + 2)]
#: Two more than the queue can hold together with the one item the worker
#: already took: one producer is still blocked in ``save_q.put`` at cancel
#: time, i.e. it owns a fetched chapter that has not been handed off yet.
CANCEL_IDS = [f"c{i}" for i in range(1, QUEUE_CAPACITY + 3)]


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class InstantFetcher:
    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.chapter_calls: list[str] = []

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return ["<html>info</html>"]

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls.append(chapter_id)
        return ["<html>chapter</html>"]

    async def fetch_image(
        self,
        url: str,
        img_dir: Path,
        *,
        name: str | None = None,
        on_exist: str = "skip",
    ) -> Path | None:
        return None

    async def fetch_images(
        self,
        img_dir: Path,
        urls: list[str],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None: ...

    async def fetch_media(
        self,
        media_dir: Path,
        resources: list[Any],
        *,
        on_exist: str = "skip",
        concurrent: int = 5,
    ) -> None: ...


class ChapterParser:
    def __init__(self, chapter_ids: list[str]) -> None:
        self.book_info_result = _book_info(chapter_ids)

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        return self.book_info_result

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        return _chapter(chapter_id)


class GatedUI:
    """Progress callbacks stop at ``gate`` until the test lets them through."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.first_progress = asyncio.Event()
        self.progress: list[tuple[int, int]] = []
        self.completed: list[str] = []

    async def on_start(self, book: Any) -> None: ...

    async def on_progress(self, done: int, total: int) -> None:
        self.first_progress.set()
        await self.gate.wait()
        self.progress.append((done, total))

    async def on_complete(self, book: Any, *, failed: int = 0) -> None:
        self.completed.append(book.book_id)


class BrokenRefetchClient(CommonClient):
    """A site hook that fails before the chapter reaches a storage batch."""

    def _dl_check_refetch(self, chap: ChapterDict) -> bool:
        raise RuntimeError("refetch hook exploded")


def _book_info(chapter_ids: list[str]) -> BookInfoDict:
    return {
        "book_name": "Book",
        "author": "Author",
        "cover_url": "",
        "update_time": "",
        "summary": "",
        "extra": {},
        "volumes": [
            {
                "volume_name": "V",
                "chapters": [
                    {"title": f"T {cid}", "url": f"u-{cid}", "chapterId": cid}
                    for cid in chapter_ids
                ],
            }
        ],
    }


def _chapter(chapter_id: str) -> ChapterDict:
    return {
        "id": chapter_id,
        "title": f"T {chapter_id}",
        "content": "body",
        "extra": {},
    }


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    parser = ChapterParser(CHAPTER_IDS)

    def _get_fetcher(site: str, cfg: FetcherConfig) -> InstantFetcher:
        return InstantFetcher(cfg)

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    monkeypatch.setattr(registrar, "get_parser", lambda *_a, **_k: parser)

    async def _no_sleep(**kwargs: Any) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _no_sleep)
    return SimpleNamespace(parser=parser)


async def _client(
    tmp_path: Path,
    *,
    client_cls: type[CommonClient] = CommonClient,
    **overrides: Any,
) -> CommonClient:
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.0,
        "retry_times": 0,
        "cache_chapter": False,
        "storage_batch_size": 1,
        "workers": len(CHAPTER_IDS),
    }
    kwargs.update(overrides)
    client = client_cls(SITE, ClientConfig(**kwargs))
    await client.init(FetcherConfig(), ParserConfig())
    return client


def _book_dir(tmp_path: Path) -> Path:
    return tmp_path / "raw" / SITE / BOOK_ID


def _stored_ids(tmp_path: Path) -> list[str]:
    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        return sorted(storage.existing_ids())


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# a full queue at stop time must be drained, not discarded
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stop_drains_a_full_save_queue(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    client = await _client(tmp_path)
    fetcher = client.fetcher
    assert isinstance(fetcher, InstantFetcher)
    ui = GatedUI()

    task = asyncio.create_task(client.download_book(BookConfig(book_id=BOOK_ID), ui=ui))

    # The worker parks inside the first progress callback (batch_size=1), so
    # every producer finishes while the 10-slot queue stays full.
    await asyncio.wait_for(ui.first_progress.wait(), timeout=5)
    await _wait_until(lambda: len(fetcher.chapter_calls) == len(CHAPTER_IDS))
    # Give the coordinator time to reach its stop signal while the worker is
    # still parked.  The old code cancelled the worker here and lost the queue.
    await asyncio.sleep(0.3)
    ui.gate.set()

    summary = await asyncio.wait_for(task, timeout=10)

    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["failed"] == 0
    assert summary["succeeded"] == len(CHAPTER_IDS)
    assert _stored_ids(tmp_path) == sorted(CHAPTER_IDS)
    assert DownloadLedger(_book_dir(tmp_path)).read_all() == []


@pytest.mark.asyncio
async def test_cancellation_drains_a_full_save_queue_before_exiting(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(CANCEL_IDS)
    client = await _client(tmp_path, workers=len(CANCEL_IDS))
    fetcher = client.fetcher
    assert isinstance(fetcher, InstantFetcher)
    ui = GatedUI()

    task = asyncio.create_task(client.download_book(BookConfig(book_id=BOOK_ID), ui=ui))
    await asyncio.wait_for(ui.first_progress.wait(), timeout=5)
    # Every chapter was fetched; the last producer is blocked handing one over
    # because the queue is full.  Cancelling now must not lose that chapter.
    await _wait_until(lambda: len(fetcher.chapter_calls) == len(CANCEL_IDS))
    await asyncio.sleep(0.2)

    task.cancel()
    # The cancellation handler must drain rather than cancel the worker:
    # release the parked progress callback only after the cancel.
    await asyncio.sleep(0.2)
    ui.gate.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    # Everything that was downloaded before the cancel is on disk, and no
    # chapter was falsely ledgered as failed.
    assert _stored_ids(tmp_path) == sorted(CANCEL_IDS)
    assert DownloadLedger(_book_dir(tmp_path)).read_all() == []


# --------------------------------------------------------------------------- #
# unsaved chapters are failures, never inferred successes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_worker_failure_ledgers_every_dropped_chapter(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """After an unrecoverable storage error the worker keeps draining.

    Every remaining chapter must be reported instead of vanishing between
    ``failed`` and ``succeeded`` (the old code silently ``continue``d).
    """
    client = await _client(tmp_path, client_cls=BrokenRefetchClient)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["succeeded"] == 0
    assert summary["failed"] == len(CHAPTER_IDS)
    assert sorted(f["chapter_id"] for f in summary["failures"]) == sorted(CHAPTER_IDS)
    assert all(f["reason"].startswith("storage_error") for f in summary["failures"])
    assert sorted(
        e.chapter_id for e in DownloadLedger(_book_dir(tmp_path)).read_all()
    ) == sorted(CHAPTER_IDS)
    assert _stored_ids(tmp_path) == []


@pytest.mark.asyncio
async def test_chapter_cannot_be_counted_as_success_without_being_stored(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that fails is a failure for each member, once."""

    def _boom(batch: list[ChapterDict], need_refetch: bool = False) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ChapterStorage, "upsert_chapters", _boom)
    client = await _client(tmp_path, storage_batch_size=10)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["succeeded"] == 0
    assert summary["failed"] == len(CHAPTER_IDS)
    assert len(summary["failures"]) == len(CHAPTER_IDS)
    entries = DownloadLedger(_book_dir(tmp_path)).read_all()
    assert len(entries) == len(CHAPTER_IDS)
    assert len({e.chapter_id for e in entries}) == len(CHAPTER_IDS)


# --------------------------------------------------------------------------- #
# CancelledError must travel through the failure handlers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_chapter_propagates_cancellation(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _client(tmp_path)
    assert isinstance(client.fetcher, InstantFetcher)

    async def _cancelled(*args: Any, **kwargs: Any) -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(client.fetcher, "fetch_chapter_content", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await client.get_chapter(BOOK_ID, "c1")

    # Cancellation is not a chapter diagnosis: nothing was recorded as failed.
    assert download_mod._LAST_FAILURE.get() is None


@pytest.mark.asyncio
async def test_download_propagates_cancellation_from_a_producer(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _client(tmp_path)
    assert isinstance(client.fetcher, InstantFetcher)

    async def _cancelled(*args: Any, **kwargs: Any) -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(client.fetcher, "fetch_chapter_content", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            client.download_book(BookConfig(book_id=BOOK_ID)), timeout=10
        )

    # The storage worker was cleaned up (no pending task at loop shutdown).
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert pending == []
