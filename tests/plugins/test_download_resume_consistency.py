#!/usr/bin/env python3
"""Crash-consistency tests for ``DownloadMixin.download_book``.

The download pipeline is interrupted at the two places where a crash can
leave chapter storage and the failure ledger out of step:

1. after a chapter (or batch) has been committed to ``chapter.raw.sqlite``
   but before the run finished -- including while the failure ledger is being
   written (a ledger write error must never turn a missing chapter into a
   success);
2. between runs -- the next run must reuse what is on disk, refetch only what
   is missing, and never count a chapter twice.

Everything runs offline against in-memory fetcher/parser doubles, following
``tests/plugins/test_download_drain.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.download_ledger import (
    DownloadLedger,
    LedgerEntry,
)
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

SITE = "dresume"
BOOK_ID = "b1"
CHAPTER_IDS = [f"c{i}" for i in range(1, 7)]


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class InterruptibleFetcher:
    """Fetches every chapter except the ids listed in ``fail_ids``/``crash_on``."""

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.chapter_calls: list[str] = []
        self.fail_ids: set[str] = set()
        self.crash_on: str | None = None

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return ["<html>info</html>"]

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls.append(chapter_id)
        if chapter_id == self.crash_on:
            raise asyncio.CancelledError
        if chapter_id in self.fail_ids:
            raise RuntimeError(f"chapter {chapter_id} unavailable")
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
        #: Per-chapter ``extra`` payloads (next_cid, encryption flags, ...).
        self.chapter_extra: dict[str, dict[str, Any]] = {}

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        return self.book_info_result

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        chap = _chapter(chapter_id)
        chap["extra"] = dict(self.chapter_extra.get(chapter_id, {}))
        return chap


class RefetchClient(CommonClient):
    """A client whose chapters need a refetch (qidian/qqbook-style encryption)."""

    def _dl_check_refetch(self, chap: ChapterDict) -> bool:
        return bool(chap["extra"].get("needs_it"))


class CrashUI:
    """Reports progress normally until ``crash_at``, then aborts the run."""

    def __init__(self, crash_at: int) -> None:
        self.crash_at = crash_at
        self.triggered = False
        self.progress: list[tuple[int, int]] = []

    async def on_start(self, book: Any) -> None: ...

    async def on_progress(self, done: int, total: int) -> None:
        self.progress.append((done, total))
        if done >= self.crash_at and not self.triggered:
            self.triggered = True
            raise asyncio.CancelledError

    async def on_complete(self, book: Any, *, failed: int = 0) -> None: ...


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


def _book_info_with_missing_id() -> BookInfoDict:
    """Book metadata with one chapter whose ``chapterId`` has to be repaired."""
    info = _book_info([])
    info["volumes"] = [
        {
            "volume_name": "V",
            "chapters": [
                {"title": "T c1", "url": "u-c1", "chapterId": "c1"},
                {"title": "T c2", "url": "u-c2"},
                {"title": "T c3", "url": "u-c3", "chapterId": "c3"},
            ],
        }
    ]
    return info


def _chapter(chapter_id: str) -> ChapterDict:
    return {
        "id": chapter_id,
        "title": f"T {chapter_id}",
        "content": f"body of {chapter_id}",
        "extra": {},
    }


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    parser = ChapterParser(CHAPTER_IDS)
    fetchers: list[InterruptibleFetcher] = []

    def _get_fetcher(site: str, cfg: FetcherConfig) -> InterruptibleFetcher:
        fetcher = InterruptibleFetcher(cfg)
        fetchers.append(fetcher)
        return fetcher

    monkeypatch.setattr(registrar, "get_fetcher", _get_fetcher)
    monkeypatch.setattr(registrar, "get_parser", lambda *_a, **_k: parser)

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _no_sleep)
    return SimpleNamespace(fetchers=fetchers, parser=parser)


async def _make_client(
    tmp_path: Path,
    harness: SimpleNamespace,
    *,
    client_cls: type[CommonClient] = CommonClient,
    **overrides: Any,
) -> tuple[CommonClient, InterruptibleFetcher]:
    """Build a client rooted in ``tmp_path`` (a fresh one == a "restart")."""
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.0,
        "retry_times": 0,
        "workers": 1,
        "storage_batch_size": 1,
    }
    kwargs.update(overrides)
    client = client_cls(SITE, ClientConfig(**kwargs))
    await client.init(FetcherConfig(), ParserConfig())
    fetcher = harness.fetchers[-1]
    return client, fetcher


def _book_dir(tmp_path: Path) -> Path:
    return tmp_path / "raw" / SITE / BOOK_ID


def _stored_ids(tmp_path: Path) -> list[str]:
    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        return sorted(storage.existing_ids())


# --------------------------------------------------------------------------- #
# interrupted run -> resume
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_interrupted_run_resumes_without_refetching_stored_chapters(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """Crash after two committed chapters: the restart fetches exactly the rest.

    This is the "chapter written, run never finished, ledger never written"
    case: the committed chapters must be reused (no duplicate download, no
    lost chapter) and the second run's counters must add up.
    """
    client, first_fetcher = await _make_client(tmp_path, harness)
    assert isinstance(first_fetcher, InterruptibleFetcher)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            client.download_book(BookConfig(book_id=BOOK_ID), ui=CrashUI(2)),
            timeout=10,
        )

    assert _stored_ids(tmp_path) == ["c1", "c2"]

    # --- restart ---
    client2, second_fetcher = await _make_client(tmp_path, harness, cache_chapter=True)
    summary = await client2.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["succeeded"] == len(CHAPTER_IDS)
    assert summary["failed"] == 0
    assert summary["skipped"] == 2
    assert second_fetcher.chapter_calls == ["c3", "c4", "c5", "c6"]
    assert _stored_ids(tmp_path) == sorted(CHAPTER_IDS)
    assert DownloadLedger(_book_dir(tmp_path)).read_all() == []


@pytest.mark.asyncio
async def test_a_run_that_stored_everything_before_aborting_is_not_redone(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """All chapters committed, the run aborts before returning: nothing to do."""
    client, _ = await _make_client(tmp_path, harness, workers=3)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            client.download_book(
                BookConfig(book_id=BOOK_ID), ui=CrashUI(len(CHAPTER_IDS))
            ),
            timeout=10,
        )

    assert _stored_ids(tmp_path) == sorted(CHAPTER_IDS)

    client2, fetcher = await _make_client(
        tmp_path, harness, workers=3, cache_chapter=True
    )
    summary = await client2.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["succeeded"] == len(CHAPTER_IDS)
    assert summary["failed"] == 0
    assert summary["skipped"] == len(CHAPTER_IDS)
    assert fetcher.chapter_calls == []
    assert DownloadLedger(_book_dir(tmp_path)).read_all() == []


# --------------------------------------------------------------------------- #
# failure ledger vs. chapter storage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_ledger_write_error_never_turns_a_gap_into_a_success(
    tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fetcher = await _make_client(tmp_path, harness)
    assert isinstance(fetcher, InterruptibleFetcher)
    fetcher.fail_ids = {"c3"}

    def _cannot_write(self: DownloadLedger, entry: LedgerEntry) -> None:
        raise OSError("ledger volume is read-only")

    monkeypatch.setattr(DownloadLedger, "append", _cannot_write)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["failed"] == 1
    assert summary["succeeded"] == len(CHAPTER_IDS) - 1
    assert summary["succeeded"] + summary["failed"] == summary["total"]
    assert [failure["chapter_id"] for failure in summary["failures"]] == ["c3"]
    # The gap really is absent from storage; the ledger just could not record it.
    assert _stored_ids(tmp_path) == sorted(set(CHAPTER_IDS) - {"c3"})
    assert not DownloadLedger(_book_dir(tmp_path)).path.exists()


@pytest.mark.asyncio
async def test_a_stale_failure_for_a_stored_chapter_is_cleared_not_refetched(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """Yesterday's failure for a chapter that is now on disk must not survive."""
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        storage.upsert_chapter(_chapter("c1"), need_refetch=False)

    ledger = DownloadLedger(book_dir)
    ledger.append(
        LedgerEntry(
            book_id=BOOK_ID,
            chapter_id="c1",
            chapter_title="T c1",
            url="u-c1",
            reason="retry_exhausted: RuntimeError: yesterday",
            attempts=3,
            failed_at="2026-01-01T00:00:00Z",
            site=SITE,
        )
    )

    client, fetcher = await _make_client(tmp_path, harness, cache_chapter=True)
    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert "c1" not in fetcher.chapter_calls
    assert summary["skipped"] == 1
    assert summary["succeeded"] == len(CHAPTER_IDS)
    assert summary["failed"] == 0
    assert ledger.read_all() == []
    assert _stored_ids(tmp_path) == sorted(CHAPTER_IDS)


@pytest.mark.asyncio
async def test_a_chapter_stored_during_id_repair_keeps_its_refetch_flag(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """A repair-time chapter must not be marked clean when the site needs refetch.

    ``_dl_fix_chapter_ids`` fetches the previous chapter only to discover the
    next id; if it stored that chapter without consulting ``_dl_check_refetch``
    the download phase would skip an encrypted chapter and the exporter would
    treat its raw body as final.
    """
    harness.parser.book_info_result = _book_info_with_missing_id()
    harness.parser.chapter_extra = {
        "c1": {"next_cid": "c2", "needs_it": True},
        "c2": {"needs_it": True},
        "c3": {"needs_it": True},
    }

    client, fetcher = await _make_client(
        tmp_path, harness, client_cls=RefetchClient, cache_chapter=True
    )
    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    # Once by the id repair, once by the download phase: the repaired copy
    # must not be treated as final.
    assert fetcher.chapter_calls.count("c1") == 2
    assert summary["total"] == 3
    assert summary["succeeded"] == 3
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        assert storage.need_refetch("c1") is True


@pytest.mark.asyncio
async def test_a_chapter_needing_refetch_is_refetched_and_counted_once(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    """``need_refetch`` chapters are intentionally re-fetched, not duplicated."""
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        storage.upsert_chapter(_chapter("c1"), need_refetch=True)

    client, fetcher = await _make_client(tmp_path, harness, cache_chapter=True)
    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert "c1" in fetcher.chapter_calls
    assert summary["total"] == len(CHAPTER_IDS)
    assert summary["succeeded"] == len(CHAPTER_IDS)
    assert summary["failed"] == 0
    assert _stored_ids(tmp_path) == sorted(CHAPTER_IDS)
