#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.infra.persistence.download_ledger`."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from novel_downloader.infra.persistence.download_ledger import (
    FAILURES_FILENAME,
    DownloadLedger,
    LedgerEntry,
    reason_code,
    utc_now_iso,
)


def _entry(
    chapter_id: str = "c1",
    *,
    reason: str = "retry_exhausted: TimeoutError: read timeout",
    attempts: int = 2,
) -> LedgerEntry:
    return LedgerEntry(
        book_id="b1",
        chapter_id=chapter_id,
        chapter_title=f"T {chapter_id}",
        url=f"https://example.test/{chapter_id}",
        reason=reason,
        attempts=attempts,
        failed_at=utc_now_iso(),
        site="demo",
    )


def test_path_is_next_to_chapter_storage(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path / "b1")

    assert ledger.path == tmp_path / "b1" / FAILURES_FILENAME
    assert ledger.path.name == "chapter.failures.jsonl"
    # Constructing the ledger must not create anything.
    assert not (tmp_path / "b1").exists()


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)
    entry = _entry()

    ledger.append(entry)

    assert ledger.path.is_file()
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["chapter_id"] == "c1"
    assert payload["book_id"] == "b1"
    assert payload["site"] == "demo"

    assert ledger.read_all() == [entry]


def test_read_all_dedupes_by_chapter_id_keeping_the_last(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)
    ledger.append(_entry("c1", reason="empty_parse", attempts=1))
    ledger.append(_entry("c2", reason="access_restricted", attempts=1))
    latest = _entry("c1", reason="retry_exhausted: OSError: gone", attempts=3)
    ledger.append(latest)

    entries = ledger.read_all()

    assert [e.chapter_id for e in entries] == ["c1", "c2"]
    assert entries[0] == latest
    assert entries[0].attempts == 3


def test_read_all_missing_file_is_empty(tmp_path: Path) -> None:
    assert DownloadLedger(tmp_path).read_all() == []


def test_read_all_skips_corrupt_lines_with_a_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = DownloadLedger(tmp_path)
    ledger.append(_entry("c1"))
    with ledger.path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("{not json}\n")
        fh.write("\n")
        fh.write(json.dumps({"book_id": "b1"}) + "\n")  # no chapter_id
        fh.write(json.dumps(["nope"]) + "\n")  # top level is a list
        fh.write("null\n")  # top level is None
    ledger.append(_entry("c3"))

    with caplog.at_level(logging.WARNING):
        entries = ledger.read_all()

    assert [e.chapter_id for e in entries] == ["c1", "c3"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4


def test_read_all_survives_nul_bytes_prose_and_huge_lines(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = DownloadLedger(tmp_path)
    ledger.append(_entry("c1"))
    with ledger.path.open("ab") as fh:
        # NUL bytes: valid UTF-8, invalid JSON.
        fh.write(b'{"chapter_id": "c9"}\x00\x00\n')
        # Hand-written Chinese prose (no JSON at all).
        fh.write("这一行是我手写的中文，不是 JSON。\n".encode())
        # A one-megabyte junk line.
        fh.write(b"x" * (1024 * 1024) + b"\n")
        # A line nested past the decoder's recursion limit (RecursionError,
        # which is not a ValueError and used to escape ``read_all``).
        fh.write(b"[" * 100_000 + b"\n")
    ledger.append(_entry("c2"))

    with caplog.at_level(logging.WARNING):
        entries = ledger.read_all()

    assert [e.chapter_id for e in entries] == ["c1", "c2"]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 4


def test_summary_counts_and_groups_by_reason_code(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)
    ledger.append(_entry("c1", reason="retry_exhausted: TimeoutError: a"))
    ledger.append(_entry("c2", reason="retry_exhausted: TimeoutError: b"))
    ledger.append(_entry("c3", reason="access_restricted"))
    # Duplicate chapter: the latest reason wins, the total does not grow.
    ledger.append(_entry("c1", reason="empty_content"))

    summary = ledger.summary()

    assert summary["total"] == 3
    assert summary["by_reason"] == {
        "empty_content": 1,
        "retry_exhausted": 1,
        "access_restricted": 1,
    }


def test_summary_of_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert DownloadLedger(tmp_path).summary() == {"total": 0, "by_reason": {}}


def test_clear_removes_the_file(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)
    ledger.append(_entry())

    assert ledger.clear() is True
    assert not ledger.path.exists()
    assert ledger.clear() is False


def test_concurrent_appends_never_interleave_lines(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)

    def worker(index: int) -> None:
        for n in range(20):
            ledger.append(_entry(f"w{index}-{n}", reason="x: y"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 80
    for line in lines:
        assert json.loads(line)["reason"] == "x: y"


def test_100_threads_on_two_instances_write_whole_lines(tmp_path: Path) -> None:
    # Two instances model the mixin plus a caller that built its own ledger for
    # the same book: the lock is per path, not per object, so neither races.
    ledgers = [DownloadLedger(tmp_path), DownloadLedger(tmp_path)]

    def worker(index: int) -> None:
        ledger = ledgers[index % 2]
        for n in range(100):
            ledger.append(
                _entry(f"w{index}-{n}", reason="retry_exhausted: TimeoutError: t")
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = ledgers[0].path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10_000
    seen: set[str] = set()
    for line in lines:
        payload = json.loads(line)  # every line is whole JSON
        assert payload["reason"] == "retry_exhausted: TimeoutError: t"
        seen.add(payload["chapter_id"])
    assert len(seen) == 10_000
    assert len(ledgers[0].read_all()) == 10_000


def test_reason_code_only_groups_snake_case_prefixes() -> None:
    assert reason_code("retry_exhausted: TimeoutError: read") == "retry_exhausted"
    assert reason_code("empty_parse") == "empty_parse"
    # Not a snake_case code -> the whole reason is kept.
    assert reason_code("HTTP 503: upstream") == "HTTP 503: upstream"


def test_utc_now_iso_shape() -> None:
    stamp = utc_now_iso()

    assert stamp.endswith("Z")
    assert len(stamp) == 20
    assert stamp[4] == "-" and stamp[7] == "-" and stamp[10] == "T"


def test_from_dict_rejects_unusable_records() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        LedgerEntry.from_dict(["nope"])
    with pytest.raises(ValueError, match="not a JSON object"):
        LedgerEntry.from_dict(None)
    with pytest.raises(ValueError, match="no chapter_id"):
        LedgerEntry.from_dict({"book_id": "b1"})
    with pytest.raises(ValueError, match="no chapter_id"):
        LedgerEntry.from_dict({"chapter_id": ""})


def test_from_dict_reports_optional_fields() -> None:
    entry = LedgerEntry.from_dict({"chapter_id": "c1"})

    assert entry.book_id == ""
    assert entry.chapter_title == ""
    assert entry.url == ""
    assert entry.reason == "unknown"
    assert entry.attempts == 1
    assert entry.site == ""


def test_from_dict_repairs_a_defective_attempts_field() -> None:
    # A garbled counter must not delete the chapter from the ledger: the
    # chapter is missing either way, so the record is kept (and repaired).
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": "3"}).attempts == 3
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": " 3 "}).attempts == 3
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": "many"}).attempts == 1
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": 2.0}).attempts == 2
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": 2.5}).attempts == 1
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": None}).attempts == 1
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": -5}).attempts == 0
    assert LedgerEntry.from_dict({"chapter_id": "c1", "attempts": True}).attempts == 1


# --------------------------------------------------------------------------- #
# run scope: the ledger is a diagnostic for the run that just finished
# --------------------------------------------------------------------------- #


def test_begin_run_discards_previous_run_records(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)
    for cid in ("c3", "c7", "c13"):
        ledger.append(_entry(cid))
    assert ledger.summary()["total"] == 3

    assert ledger.begin_run() is True

    assert not ledger.path.exists()
    assert ledger.read_all() == []
    assert ledger.summary() == {"total": 0, "by_reason": {}}
    # Starting a run when there is nothing to discard is a no-op.
    assert ledger.begin_run() is False


def test_failed_run_then_clean_run_leaves_no_ledger(tmp_path: Path) -> None:
    ledger = DownloadLedger(tmp_path)

    # Run 1: 5 of 100 chapters fail.  Only failures are written.
    for cid in (f"c{i}" for i in (3, 7, 13, 21, 55)):
        ledger.append(_entry(cid, reason="access_restricted"))
    assert [e.chapter_id for e in ledger.read_all()] == [
        "c3",
        "c7",
        "c13",
        "c21",
        "c55",
    ]

    # Run 2 starts, then every chapter downloads: nothing is appended.
    ledger.begin_run()

    assert ledger.read_all() == [], "a successful run must not inherit failures"
    assert ledger.summary()["total"] == 0
    assert not ledger.path.exists(), "missing file == previous run was complete"
