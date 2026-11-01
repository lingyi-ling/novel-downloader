#!/usr/bin/env python3
"""Adversarial checks for the failure ledger / ``DownloadSummary`` contract.

``test_download_failures_unit.py`` pins the happy and mildly sad paths.  This
module attacks the parts that decide whether "progress reached 100%" may be
read as "the book is complete":

* the ledger is *run scoped* -- a clean re-run must not inherit yesterday's
  failures (``DownloadLedger.begin_run``);
* failure reasons stay attached to their own chapter under concurrency
  (task-local ``ContextVar``);
* ``total == succeeded + failed`` holds for every filtering / caching /
  storage-failure / strict permutation;
* a broken ledger (read-only disk, full disk) cannot break the download.

Everything runs offline against in-memory fetcher/parser doubles.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.download_ledger import (
    LedgerEntry,
    utc_now_iso,
)
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.mixins import download as download_mod
from novel_downloader.plugins.mixins.download import IncompleteDownloadError
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    ChapterDict,
    ClientConfig,
    FetcherConfig,
    ParserConfig,
)
from tests.plugins.mixins.test_download_failures_unit import (
    BOOK_ID,
    SITE,
    FakeParser,
    FakeUI,
    _assert_consistent,
    _book_dir,
    _book_info,
    _chapter,
    _client,
    _ledger,
    harness,  # noqa: F401 - re-exported pytest fixture
)

# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class _ModeFetcher:
    """Fetcher whose raw pages (or exception) depend on the chapter id."""

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.chapter_calls: list[str] = []
        self.pages: dict[str, list[str]] = {}
        self.boom: frozenset[str] = frozenset()
        self.default_pages = ["<html>chapter</html>"]
        # Set by the test when it wants a *deterministic* interleaving proof.
        self.failure_event: asyncio.Event | None = None

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return ["<html>info</html>"]

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        self.chapter_calls.append(chapter_id)
        # Uneven yielding forces the producers to interleave, which is what
        # would expose a shared (instead of task-local) failure diagnostic.
        for _ in range(int(chapter_id[1:] or 0) % 5):
            await asyncio.sleep(0)
        if chapter_id in self.boom:
            if self.failure_event is not None:
                self.failure_event.set()
            raise RuntimeError(f"network error for {chapter_id}")
        return list(self.pages.get(chapter_id, self.default_pages))

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


class _ModeClient(CommonClient):
    """CommonClient that treats ``<restricted>`` pages as a paywall."""

    silent_none: frozenset[str] = frozenset()
    # When set, a silent-``None`` chapter waits for another producer to publish
    # a failure before it gives up.  That makes the "did chapter A inherit
    # chapter B's diagnosis?" question deterministic instead of racy.
    other_failure: asyncio.Event | None = None

    def _dl_check_restricted(self, raw_pages: list[str]) -> bool:
        restricted = any("<restricted>" in page for page in raw_pages)
        if restricted and self.other_failure is not None:
            self.other_failure.set()
        return restricted

    async def get_chapter(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> ChapterDict | None:
        # Mimics site overrides (e.g. qidian) that fetch, then return ``None``
        # without publishing a reason: the mixin must fall back to
        # ``no_content`` -- and must not pick up a neighbour's reason.
        if chapter_id in self.silent_none:
            if self.other_failure is not None:
                await asyncio.wait_for(self.other_failure.wait(), timeout=5)
                # Let the task that set the event finish publishing its
                # diagnostic before reading our own slot.
                await asyncio.sleep(0)
            return None
        return await super().get_chapter(book_id, chapter_id, **kwargs)


async def _mode_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chapter_ids: list[str],
    *,
    client_cls: type[CommonClient] = _ModeClient,
    **overrides: Any,
) -> tuple[CommonClient, _ModeFetcher, FakeParser]:
    fetcher = _ModeFetcher()
    parser = FakeParser()
    parser.book_info_result = _book_info(chapter_ids)
    parser.chapters = {cid: _chapter(cid) for cid in chapter_ids}

    monkeypatch.setattr(registrar, "get_fetcher", lambda *_a, **_k: fetcher)
    monkeypatch.setattr(registrar, "get_parser", lambda *_a, **_k: parser)

    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "backoff_factor": 0.0,
        "retry_times": 0,
        "cache_chapter": False,
        "storage_batch_size": 1,
        "workers": 4,
    }
    kwargs.update(overrides)
    client = client_cls(SITE, ClientConfig(**kwargs))
    await client.init(FetcherConfig(), ParserConfig())
    return client, fetcher, parser


class _BrokenLedger:
    """Ledger double whose disk is full / read-only for every operation."""

    def __init__(self, base: Path, filename: str = "chapter.failures.jsonl") -> None:
        self.path = base / filename

    def begin_run(self) -> bool:
        raise OSError("No space left on device")

    def append(self, entry: LedgerEntry) -> None:
        raise OSError("No space left on device")


# --------------------------------------------------------------------------- #
# failure attribution under concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_last_failure_diagnostic_is_task_local() -> None:
    """``asyncio.create_task`` copies the context: no cross-task write-back."""

    async def worker(tag: str, yields: int) -> Any:
        download_mod._LAST_FAILURE.set(download_mod._ChapterFailure(tag, 1))
        for _ in range(yields):
            await asyncio.sleep(0)
        return download_mod._LAST_FAILURE.get()

    values = await asyncio.gather(worker("a", 5), worker("b", 0), worker("c", 3))
    assert [v.reason for v in values] == ["a", "b", "c"]

    # A child task inherits the parent's context *at creation* (which is
    # exactly why ``producer`` resets the slot before every fetch) ...
    download_mod._LAST_FAILURE.set(download_mod._ChapterFailure("parent", 1))

    async def child() -> tuple[str, str]:
        inherited = download_mod._LAST_FAILURE.get().reason
        download_mod._LAST_FAILURE.set(download_mod._ChapterFailure("child", 1))
        return inherited, download_mod._LAST_FAILURE.get().reason

    assert await asyncio.create_task(child()) == ("parent", "child")
    # ... and its writes never travel back to the parent.
    assert download_mod._LAST_FAILURE.get().reason == "parent"


@pytest.mark.asyncio
async def test_concurrent_failures_keep_their_own_chapter_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"c{i}" for i in range(1, 21)]
    client, fetcher, _ = await _mode_client(
        tmp_path, monkeypatch, ids, client_cls=_ModeClient, workers=20
    )
    # Three chapters fail, each for a different terminal reason.
    failure_published = asyncio.Event()
    client.silent_none = frozenset({"c3"})  # no diagnostic at all
    client.other_failure = failure_published
    fetcher.failure_event = failure_published
    fetcher.boom = frozenset({"c13"})  # retries exhausted
    fetcher.pages["c7"] = ["<restricted>login required</restricted>"]
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["total"] == 20
    assert summary["failed"] == 3
    assert summary["succeeded"] == 17
    assert summary["skipped"] == 0
    assert ui.progress[-1] == (20, 20)

    by_id = {failure["chapter_id"]: failure for failure in summary["failures"]}
    assert set(by_id) == {"c3", "c7", "c13"}
    assert by_id["c3"]["reason"] == "no_content"
    assert by_id["c7"]["reason"] == "access_restricted"
    assert by_id["c13"]["reason"].startswith("retry_exhausted: RuntimeError")
    assert "c13" in by_id["c13"]["reason"], "a reason must name its own chapter"
    assert all(failure["attempts"] == 1 for failure in summary["failures"])

    ledger_reasons = {e.chapter_id: e.reason for e in _ledger(tmp_path).read_all()}
    assert ledger_reasons == {
        "c3": "no_content",
        "c7": "access_restricted",
        "c13": by_id["c13"]["reason"],
    }

    with ChapterStorage(_book_dir(tmp_path), "chapter.raw.sqlite") as storage:
        stored = set(storage.existing_ids())
    assert len(stored) == 17
    assert not {"c3", "c7", "c13"} & stored


@pytest.mark.asyncio
async def test_storage_error_does_not_steal_another_chapters_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One chapter fails on fetch, two reach storage whose upsert then fails for
    # the batch: every chapter must keep the reason that belongs to it.
    ids = ["c1", "c2", "c3"]
    client, fetcher, _ = await _mode_client(
        tmp_path, monkeypatch, ids, workers=3, storage_batch_size=10
    )
    fetcher.boom = frozenset({"c2"})

    def _boom(batch: list[ChapterDict], need_refetch: bool = False) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ChapterStorage, "upsert_chapters", _boom)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID))

    _assert_consistent(summary)
    by_id = {
        failure["chapter_id"]: failure["reason"] for failure in summary["failures"]
    }
    assert by_id["c2"].startswith("retry_exhausted: RuntimeError")
    assert by_id["c1"].startswith("storage_error")
    assert by_id["c3"].startswith("storage_error")
    assert summary["failed"] == 3
    assert summary["succeeded"] == 0


# --------------------------------------------------------------------------- #
# run scope: fail once, succeed next time
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_run_then_clean_run_leaves_no_ledger(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    chapter_ids = [f"c{i}" for i in range(1, 5)]
    harness.parser.book_info_result = _book_info(chapter_ids)
    harness.parser.chapters = {cid: _chapter(cid) for cid in chapter_ids}
    harness.parser.chapters["c2"] = None

    client1, _ = await _client(tmp_path, harness)
    first = await client1.download_book(BookConfig(book_id=BOOK_ID))

    _assert_consistent(first)
    assert first["failed"] == 1
    assert [e.chapter_id for e in _ledger(tmp_path).read_all()] == ["c2"]

    # Network heals: the second run downloads every chapter.  A successful
    # chapter writes no ledger record, so only ``begin_run`` can retire the
    # stale failure.
    harness.parser.chapters["c2"] = _chapter("c2")
    client2, _ = await _client(tmp_path, harness)
    second = await client2.download_book(BookConfig(book_id=BOOK_ID))

    _assert_consistent(second)
    assert second["total"] == 4
    assert second["succeeded"] == 4
    assert second["failed"] == 0
    assert second["failures"] == []
    assert _ledger(tmp_path).read_all() == []
    assert not _ledger(tmp_path).path.exists()


@pytest.mark.asyncio
async def test_run_that_dies_before_chapters_leaves_an_empty_ledger(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    # Seed what a previous run left behind.
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    _ledger(tmp_path).append(
        LedgerEntry(
            book_id=BOOK_ID,
            chapter_id="c9",
            chapter_title="T c9",
            url="u-c9",
            reason="access_restricted",
            attempts=1,
            failed_at=utc_now_iso(),
            site=SITE,
        )
    )

    harness.parser.book_info_result = None  # metadata cannot be parsed
    client, _ = await _client(tmp_path, harness)

    with pytest.raises(LookupError):
        await client.download_book(BookConfig(book_id=BOOK_ID))

    # The ledger describes the latest *attempt*: it got as far as zero
    # chapters, so it has nothing to report -- and must not present the
    # previous run's c9 as if it were this run's finding.
    assert _ledger(tmp_path).read_all() == []


# --------------------------------------------------------------------------- #
# invariants per path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_range_and_ignore_filters_keep_total_consistent(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    ids = [f"c{i}" for i in range(1, 7)]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = {cid: _chapter(cid) for cid in ids}
    harness.parser.chapters["c3"] = None
    client, fetcher = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(
        BookConfig(
            book_id=BOOK_ID,
            start_id="c2",
            end_id="c5",
            ignore_ids=frozenset({"c4"}),
        ),
        ui=ui,
    )

    _assert_consistent(summary)
    assert summary["total"] == 3  # c2, c3, c5
    assert summary["failed"] == 1
    assert summary["succeeded"] == 2
    assert summary["skipped"] == 0
    assert sorted(fetcher.chapter_calls) == ["c2", "c3", "c5"]
    assert [f["chapter_id"] for f in summary["failures"]] == ["c3"]
    assert [e.chapter_id for e in _ledger(tmp_path).read_all()] == ["c3"]
    assert ui.progress[-1] == (3, 3)


@pytest.mark.asyncio
async def test_empty_plan_returns_zeroes_and_retires_the_old_ledger(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    # A previous run left a failure behind ...
    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    _ledger(tmp_path).append(
        LedgerEntry(
            book_id=BOOK_ID,
            chapter_id="c9",
            chapter_title="T c9",
            url="u-c9",
            reason="access_restricted",
            attempts=1,
            failed_at=utc_now_iso(),
            site=SITE,
        )
    )

    harness.parser.book_info_result = _book_info(["c1", "c2"])
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": _chapter("c2")}
    # ... and this run filters every chapter out, so nothing is attempted.
    client, fetcher = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(
        BookConfig(book_id=BOOK_ID, ignore_ids=frozenset({"c1", "c2"})), ui=ui
    )

    assert summary["total"] == 0
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert summary["skipped"] == 0
    assert summary["failures"] == []
    assert fetcher.chapter_calls == []
    assert ui.progress == [], "a zero-chapter run reports no progress at all"
    assert _ledger(tmp_path).read_all() == []


@pytest.mark.asyncio
async def test_fully_cached_book_is_complete_and_touches_no_ledger(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    ids = ["c1", "c2", "c3"]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = {cid: _chapter(cid) for cid in ids}

    book_dir = _book_dir(tmp_path)
    book_dir.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(book_dir, "chapter.raw.sqlite") as storage:
        for cid in ids:
            storage.upsert_chapter(_chapter(cid), need_refetch=False)

    client, fetcher = await _client(tmp_path, harness, cache_chapter=True)
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["total"] == 3
    assert summary["skipped"] == 3
    assert summary["succeeded"] == 3
    assert summary["failed"] == 0
    assert fetcher.chapter_calls == []
    assert not _ledger(tmp_path).path.exists()
    assert ui.progress[-1] == (3, 3)


@pytest.mark.asyncio
async def test_total_failure_still_fills_the_bar_but_reports_zero_success(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    ids = ["c1", "c2", "c3", "c4"]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = dict.fromkeys(ids)  # every parse fails
    client, _ = await _client(tmp_path, harness)
    ui = FakeUI()

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["total"] == 4
    assert summary["failed"] == 4
    assert summary["succeeded"] == 0
    assert summary["skipped"] == 0
    assert len(summary["failures"]) == 4
    # Progress means "processed", not "usable": the bar closes at 100% and the
    # summary is what stops that from being read as success.
    assert ui.progress[-1] == (4, 4)
    assert len(_ledger(tmp_path).read_all()) == 4


@pytest.mark.asyncio
async def test_whole_batch_storage_failure_marks_every_chapter(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = ["c1", "c2", "c3"]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = {cid: _chapter(cid) for cid in ids}
    client, _ = await _client(tmp_path, harness, storage_batch_size=10)
    ui = FakeUI()

    def _boom(batch: list[ChapterDict], need_refetch: bool = False) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ChapterStorage, "upsert_chapters", _boom)

    summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    _assert_consistent(summary)
    assert summary["total"] == 3
    assert summary["failed"] == 3
    assert summary["succeeded"] == 0
    assert {f["chapter_id"] for f in summary["failures"]} == set(ids)
    assert all(f["reason"].startswith("storage_error") for f in summary["failures"])
    assert ui.progress[-1] == (3, 3)
    assert len(_ledger(tmp_path).read_all()) == 3


@pytest.mark.asyncio
async def test_strict_and_default_modes_report_the_same_numbers(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
) -> None:
    ids = ["c1", "c2", "c3"]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = {cid: _chapter(cid) for cid in ids}
    harness.parser.chapters["c3"] = None
    harness.parser.chapters["c1"] = _chapter("c1", content="")

    lenient_client, _ = await _client(tmp_path / "lenient", harness)
    lenient = await lenient_client.download_book(BookConfig(book_id=BOOK_ID))

    strict_client, _ = await _client(tmp_path / "strict", harness)
    with pytest.raises(IncompleteDownloadError) as excinfo:
        await strict_client.download_book(BookConfig(book_id=BOOK_ID), strict=True)
    strict = excinfo.value.summary

    for summary in (lenient, strict):
        _assert_consistent(summary)
        assert summary["total"] == 3
        assert summary["failed"] == 2
        assert summary["succeeded"] == 1
    assert sorted(f["chapter_id"] for f in strict["failures"]) == ["c1", "c3"]
    assert sorted(f["chapter_id"] for f in lenient["failures"]) == ["c1", "c3"]
    # Both modes leave the same ledger behind: strict only changes the caller's
    # reaction, never the recorded facts.
    assert sorted(e.chapter_id for e in _ledger(tmp_path / "strict").read_all()) == [
        "c1",
        "c3",
    ]


# --------------------------------------------------------------------------- #
# a broken ledger must not break the download
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ledger_disk_failure_degrades_to_a_warning(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ids = ["c1", "c2"]
    harness.parser.book_info_result = _book_info(ids)
    harness.parser.chapters = {"c1": _chapter("c1"), "c2": None}
    monkeypatch.setattr(download_mod, "DownloadLedger", _BrokenLedger)
    client, _ = await _client(tmp_path, harness)
    ui = FakeUI()

    with caplog.at_level(logging.WARNING):
        summary = await client.download_book(BookConfig(book_id=BOOK_ID), ui=ui)

    # The download itself is unaffected: the chapter is still counted, the run
    # still completes, and the CLI still gets a summary/IncompleteDownloadError.
    _assert_consistent(summary)
    assert summary["total"] == 2
    assert summary["failed"] == 1
    assert summary["failures"][0]["chapter_id"] == "c2"
    assert summary["ledger_path"].endswith("chapter.failures.jsonl")
    assert ui.progress[-1] == (2, 2)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Failed to reset download ledger" in m for m in messages)
    assert any("Failed to append to download ledger" in m for m in messages)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
