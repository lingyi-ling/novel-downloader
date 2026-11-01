#!/usr/bin/env python3
"""Failure bookkeeping for :meth:`DownloadMixin.download_book`.

The happy path is covered by ``tests/plugins/test_download_mixin_unit.py``.
These tests pin the completeness contract introduced with the failure ledger:

* a chapter that cannot be fetched is recorded (ledger + summary), not skipped
  silently;
* progress still reaches ``total`` so the progress bar never stalls;
* ``strict=True`` raises :class:`IncompleteDownloadError` with the summary;
* the returned :class:`DownloadSummary` satisfies
  ``total == succeeded + failed``.

Everything runs offline against in-memory fetcher/parser doubles.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.download_ledger import DownloadLedger
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.mixins import download as download_mod
from novel_downloader.plugins.mixins.download import IncompleteDownloadError
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
BOOK_ID = "b1"


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class FakeFetcher:
    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.chapter_calls: list[str] = []
        self.book_info_pages = ["<html>info</html>"]
        self.chapter_pages = ["<html>chapter</html>"]

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return list(self.book_info_pages)

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls.append(chapter_id)
        return list(self.chapter_pages)

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


class FakeParser:
    def __init__(self) -> None:
        self.book_info_result: BookInfoDict | None = None
        self.chapters: dict[str, ChapterDict | None] = {}

    def parse_book_info(
        self, raw_pages: list[str], **kwargs: Any
    ) -> BookInfoDict | None:
        return copy.deepcopy(self.book_info_result)

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> ChapterDict | None:
        return copy.deepcopy(self.chapters.get(chapter_id))


class FakeUI:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.progress: list[tuple[int, int]] = []
        self.completed: list[str] = []

    async def on_start(self, book: BookConfig) -> None:
        self.started.append(book.book_id)

    async def on_progress(self, done: int, total: int) -> None:
        self.progress.append((done, total))

    async def on_complete(self, book: BookConfig, *, failed: int = 0) -> None:
        self.completed.append(book.book_id)


def _chapter(chapter_id: str, *, content: str = "body") -> ChapterDict:
    return {
        "id": chapter_id,
        "title": f"T {chapter_id}",
        "content": content,
        "extra": {},
    }


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
        "cache_chapter": False,
        "storage_batch_size": 1,
    }
    kwargs.update(overrides)
    client = CommonClient(SITE, ClientConfig(**kwargs))
    await client.init(FetcherConfig(), ParserConfig())
    return client, harness.fetchers[-1]


def _book_dir(tmp_path: Path) -> Path:
    return tmp_path / "raw" / SITE / BOOK_ID


def _ledger(tmp_path: Path) -> DownloadLedger:
    return DownloadLedger(_book_dir(tmp_path))


def _assert_consistent(summary: Any) -> None:
    assert summary["total"] == summary["succeeded"] + summary["failed"]
    assert summary["failed"] == len(summary["failures"])
    assert summary["skipped"] <= summary["succeeded"]


# --------------------------------------------------------------------------- #
# partial failure
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_chapter_is_ledgered_and_summarised(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(["c1", "c2", "c3"])
    harness.parser.chapters = {
        "c1": _chapter("c1"),
        "c2": None,  # parser rejects this chapter -> retries exhausted
        "c3": _chapter("c3"),
    }
    client, _ = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["total"] == 3
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 0
    assert summary["book_id"] == BOOK_ID
    assert summary["site"] == SITE

    (failure,) = summary["failures"]
    assert failure["chapter_id"] == "c2"
    assert failure["chapter_title"] == "T c2"
    assert failure["url"] == "u-c2"
    assert failure["reason"].startswith("retry_exhausted")
    assert failure["attempts"] == 1
    assert failure["failed_at"].endswith("Z")

    # Progress semantics: failures still advance the bar to 100%.
    assert ui.progress[-1] == (3, 3)
    assert ui.started == [BOOK_ID]
    assert ui.completed == [BOOK_ID]

    # Only the two usable chapters reached storage.
    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        assert sorted(storage.existing_ids()) == ["c1", "c3"]

    # The ledger is the durable record of the missing chapter.
    ledger = _ledger(tmp_path)
    assert summary["ledger_path"] == str(ledger.path)
    entries = ledger.read_all()
    assert [e.chapter_id for e in entries] == ["c2"]
    assert entries[0].site == SITE
    assert entries[0].book_id == BOOK_ID
    assert entries[0].attempts == 1


@pytest.mark.asyncio
async def test_strict_mode_raises_incomplete_download_error(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(["c1", "c2"])
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": None}
    client, _ = await _client(tmp_path, harness)

    with pytest.raises(IncompleteDownloadError) as excinfo:
        await client.download_book(BookConfig(book_id=BOOK_ID), strict=True)

    summary = excinfo.value.summary
    _assert_consistent(summary)
    assert summary["failed"] == 1
    assert summary["failures"][0]["chapter_id"] == "c2"
    assert BOOK_ID in str(excinfo.value)
    # The failure was persisted before the error was raised.
    assert [e.chapter_id for e in _ledger(tmp_path).read_all()] == ["c2"]


# --------------------------------------------------------------------------- #
# complete downloads stay quiet
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_complete_download_reports_no_failures_and_no_ledger(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(["c1", "c2"])
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": _chapter("c2")}
    client, _ = await _client(tmp_path, harness)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    _assert_consistent(summary)
    assert summary["total"] == 2
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    assert summary["failures"] == []
    # No append -> no ledger file at all (backwards compatible).
    assert not _ledger(tmp_path).path.exists()


@pytest.mark.asyncio
async def test_empty_plan_returns_zeroed_summary(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info([])
    client, fetcher = await _client(tmp_path, harness)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["total"] == 0
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert summary["skipped"] == 0
    assert summary["failures"] == []
    assert fetcher.chapter_calls == []


# --------------------------------------------------------------------------- #
# content-level failure signal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_chapter_content_counts_as_failure(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(["c1", "c2"])
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": _chapter("c2", content="")}
    client, _ = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["failed"] == 1
    assert summary["failures"][0]["chapter_id"] == "c2"
    assert summary["failures"][0]["reason"] == "empty_content"
    assert ui.progress[-1] == (2, 2)
    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        assert sorted(storage.existing_ids()) == ["c1"]


@pytest.mark.asyncio
async def test_image_only_chapter_is_not_a_failure(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    image_chapter = _chapter("c1", content="\n  ")
    image_chapter["extra"] = {
        "resources": [{"url": "https://img.test/a.png", "type": "image"}]
    }
    harness.parser.book_info_result = _book_info(["c1"])
    harness.parser.chapters = {"c1": image_chapter}
    client, _ = await _client(tmp_path, harness)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    assert summary["failed"] == 0
    assert summary["succeeded"] == 1
    assert not _ledger(tmp_path).path.exists()


# --------------------------------------------------------------------------- #
# cached and unstorable chapters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cached_chapter_counts_as_skipped_and_still_complete(
    tmp_path: Path, harness: SimpleNamespace
) -> None:
    harness.parser.book_info_result = _book_info(["c1", "c2"])
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": _chapter("c2")}

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        storage.upsert_chapter(_chapter("c1"), need_refetch=False)

    client, fetcher = await _client(tmp_path, harness, cache_chapter=True)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    _assert_consistent(summary)
    assert summary["skipped"] == 1
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    # The cached chapter was never fetched again.
    assert fetcher.chapter_calls == ["c2"]


class _FailingStorage:
    calls = 0

    def __init__(self, base: Path, filename: str = "chapter.raw.sqlite") -> None:
        self.base = base

    def __enter__(self) -> _FailingStorage:
        return self

    def __exit__(self, *exc: Any) -> None: ...

    def need_refetch(self, cid: str) -> bool:
        return True

    def upsert_chapters(
        self, batch: list[ChapterDict], need_refetch: bool = False
    ) -> None:
        type(self).calls += 1
        raise RuntimeError("database is locked")

    def upsert_chapter(self, data: ChapterDict) -> None: ...

    def get_chapter(self, cid: str) -> ChapterDict | None:
        return None

    def get_chapters(self, ids: list[str]) -> dict[str, ChapterDict | None]:
        return dict.fromkeys(ids)

    def exists(self, cid: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_storage_failure_marks_chapters_as_failed(
    tmp_path: Path,
    harness: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.parser.book_info_result = _book_info(["c1"])
    harness.parser.chapters = {"c1": _chapter("c1")}
    monkeypatch.setattr(download_mod, "ChapterStorage", _FailingStorage)
    client, _ = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["failed"] == 1
    assert summary["failures"][0]["reason"].startswith("storage_error")
    # A fetched-but-unstored chapter must not look like success, but progress
    # still completes.
    assert ui.progress[-1] == (1, 1)
    assert [e.chapter_id for e in _ledger(tmp_path).read_all()] == ["c1"]
