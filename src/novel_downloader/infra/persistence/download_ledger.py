#!/usr/bin/env python3
"""
novel_downloader.infra.persistence.download_ledger
--------------------------------------------------

A per-run ledger for chapters that could not be downloaded.

Every failed chapter is written as one JSON object per line (JSON Lines) to
``chapter.failures.jsonl`` next to ``chapter.raw.sqlite``.  The file describes
**the most recent run only**: :meth:`DownloadLedger.begin_run` -- called once
at the start of every download by
:meth:`~novel_downloader.plugins.mixins.download.DownloadMixin.download_book`
-- removes whatever a previous run left behind, and a run that downloads every
chapter leaves no file at all.

That scope is deliberate.  A successful chapter writes no record, so an
append-only history could never answer "is this book complete *now*?": the five
failures of last night would still be reported after a flawless re-run, and any
consumer of :meth:`DownloadLedger.read_all` would keep claiming chapters are
missing that are on disk.  A run-scoped file has exactly one meaning -- read it
after a run and it lists the chapters that run could not fetch.  Callers that
want a history must archive the file between runs (CI artifact, backup, ...);
the live file is not a log.

:meth:`DownloadLedger.read_all` still de-duplicates by ``chapter_id`` (last
record wins) so a chapter recorded twice inside one run is counted once.

The ledger never raises for bad content: a corrupt line -- invalid JSON, NUL
bytes, hand-written prose, absurdly long or deeply nested values -- is skipped
with a warning so one broken record cannot hide the rest of the book.  Writes
are serialized with a lock shared by every ledger instance aimed at the same
path, so concurrent tasks (including the ``asyncio.to_thread`` workers used by
the parsers) cannot interleave halves of a line.
"""

from __future__ import annotations

__all__ = [
    "FAILURES_FILENAME",
    "DownloadLedger",
    "LedgerEntry",
    "LedgerSummary",
    "reason_code",
    "utc_now_iso",
]

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

#: Filename used for the failure ledger inside a book's raw data directory.
FAILURES_FILENAME = "chapter.failures.jsonl"

# Reason codes are ``code`` or ``code: detail``; only a bare snake_case token
# is treated as a groupable code by :func:`reason_code`.
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# One lock per ledger path.  Two ``DownloadLedger`` objects built for the same
# book (the mixin builds one, a caller may build another) must not race on the
# same file, so the lock cannot live on the instance.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """
    Return the process-wide write lock for ``path``, creating it once.

    :param path: Ledger file path.
    :return: Shared :class:`threading.Lock` for that path.
    """
    try:
        key = os.path.normcase(str(path.resolve()))
    except OSError:  # pragma: no cover - ``resolve`` is effectively total
        key = os.path.normcase(str(path.absolute()))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def utc_now_iso() -> str:
    """
    Return the current UTC time as an ISO-8601 string ending in ``Z``.

    :return: e.g. ``2026-11-01T08:30:00Z``.
    """
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def reason_code(reason: str) -> str:
    """
    Return the groupable code of a failure reason.

    Reasons are recorded as ``code`` or ``code: detail`` (for example
    ``retry_exhausted: TimeoutError: read timeout``).  When the part before
    the first ``": "`` looks like a snake_case code it is returned, otherwise
    the whole reason is kept so nothing is silently mis-grouped.

    :param reason: Failure reason as stored in the ledger.
    :return: Stable grouping key for :meth:`DownloadLedger.summary`.
    """
    code, sep, _detail = reason.partition(": ")
    if sep and _CODE_RE.match(code):
        return code
    return reason


class LedgerSummary(TypedDict):
    """Aggregate view over the (de-duplicated) ledger entries."""

    total: int
    by_reason: dict[str, int]


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """
    One failed-chapter record.

    :ivar book_id: Book identifier the chapter belongs to.
    :ivar chapter_id: Chapter identifier as used by the site.
    :ivar chapter_title: Human readable chapter title, when known.
    :ivar url: Chapter URL, when known.
    :ivar reason: Why the chapter failed (``code`` or ``code: detail``).
    :ivar attempts: Number of fetch attempts made before giving up.
    :ivar failed_at: UTC timestamp, ISO-8601, e.g. ``2026-11-01T08:30:00Z``.
    :ivar site: Source site key.
    """

    book_id: str
    chapter_id: str
    chapter_title: str
    url: str
    reason: str
    attempts: int
    failed_at: str
    site: str

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a JSON-serializable mapping."""
        return {
            "book_id": self.book_id,
            "chapter_id": self.chapter_id,
            "chapter_title": self.chapter_title,
            "url": self.url,
            "reason": self.reason,
            "attempts": self.attempts,
            "failed_at": self.failed_at,
            "site": self.site,
        }

    @classmethod
    def from_dict(cls, data: object) -> LedgerEntry:
        """
        Build an entry from a decoded JSON value.

        Only two defects make a record unusable, because only those two block
        identifying *which* chapter is missing: a non-object top level and a
        missing/empty ``chapter_id``.  Everything else is repaired instead of
        rejected -- missing strings become ``""``, a missing ``reason`` becomes
        ``"unknown"`` and a missing/garbled ``attempts`` becomes ``1``.  Losing
        a whole failure record over a cosmetic field defect would under-report
        missing chapters, the one thing this ledger exists to prevent.

        :param data: Decoded JSON object.
        :raises ValueError: if ``data`` is not an object or has no
            ``chapter_id``.
        """
        if not isinstance(data, dict):
            raise ValueError("ledger entry is not a JSON object")

        chapter_id = _as_str(data.get("chapter_id"))
        if not chapter_id:
            raise ValueError("ledger entry has no chapter_id")

        return cls(
            book_id=_as_str(data.get("book_id")),
            chapter_id=chapter_id,
            chapter_title=_as_str(data.get("chapter_title")),
            url=_as_str(data.get("url")),
            reason=_as_str(data.get("reason")) or "unknown",
            attempts=_as_attempts(data.get("attempts", 1)),
            failed_at=_as_str(data.get("failed_at")),
            site=_as_str(data.get("site")),
        )


def _as_str(value: object) -> str:
    """Coerce a decoded JSON value to ``str`` (missing values become ``""``)."""
    return value if isinstance(value, str) else ""


def _as_attempts(value: object) -> int:
    """
    Coerce a decoded JSON ``attempts`` value to a non-negative integer.

    Accepts ints, integral floats and integer-looking strings (``"3"``,
    ``" 3 "``).  Booleans, ``None`` and anything unparsable fall back to ``1``
    so a fixed-width scratch field can never hide a failed chapter.

    :param value: Raw ``attempts`` value from the decoded JSON object.
    :return: Attempt count, never negative.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if value.is_integer() else 1
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 1
    return 1


class DownloadLedger:
    """
    Per-run JSON Lines ledger of failed chapters for a single book.

    The target directory usually is ``<raw_data_dir>/<site>/<book_id>``, i.e.
    the same directory as ``chapter.raw.sqlite``.  The file is created on the
    first :meth:`append` call; constructing the ledger touches nothing.  Call
    :meth:`begin_run` at the start of a run to drop the previous run's records.
    """

    def __init__(self, base_dir: str | Path, filename: str = FAILURES_FILENAME):
        """
        :param base_dir: Directory holding the book's raw data.
        :param filename: Ledger filename; defaults to
            :data:`FAILURES_FILENAME`.
        """
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / filename
        # Process-wide per-path lock, shared with any other ledger instance
        # writing the same file.  Only synchronous work happens while the lock
        # is held, so it is safe inside a single event loop (it also covers
        # callers that run in worker threads).
        self._lock = _lock_for(self._path)

    @property
    def path(self) -> Path:
        """Path of the ledger file (under :attr:`base_dir`)."""
        return self._path

    def append(self, entry: LedgerEntry) -> None:
        """
        Append one record as a single JSON line.

        :param entry: Record to persist.
        :raises OSError: if the file cannot be written.
        """
        line = json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()

    def read_all(self) -> list[LedgerEntry]:
        """
        Read every valid record, de-duplicated by ``chapter_id``.

        After a completed run this is exactly that run's failures, because
        :meth:`begin_run` discarded the previous run's records.  Within one run
        the last record for a chapter wins.  Corrupt or incomplete lines are
        skipped with a warning instead of failing the whole read; that includes
        lines that are not JSON at all (NUL bytes, hand-edited prose) and
        values nested so deeply that the decoder hits the recursion limit.

        :return: One entry per chapter, in first-seen order.
        """
        if not self._path.is_file():
            return []

        latest: dict[str, LedgerEntry] = {}
        # ``errors="replace"`` keeps a single bad byte sequence from aborting
        # the scan; the affected line then fails to parse and is skipped.
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = LedgerEntry.from_dict(json.loads(stripped))
                except (ValueError, TypeError, RecursionError) as exc:
                    logger.warning(
                        "Skipping corrupt ledger line %s:%d (%s)",
                        self._path,
                        lineno,
                        exc,
                    )
                    continue
                latest[entry.chapter_id] = entry
        return list(latest.values())

    def summary(self) -> LedgerSummary:
        """
        Summarize the de-duplicated ledger.

        :return: ``{"total": <entries>, "by_reason": {<code>: <count>, ...}}``.
        """
        entries = self.read_all()
        by_reason: dict[str, int] = {}
        for entry in entries:
            code = reason_code(entry.reason)
            by_reason[code] = by_reason.get(code, 0) + 1
        return LedgerSummary(total=len(entries), by_reason=by_reason)

    def begin_run(self) -> bool:
        """
        Drop the records a previous run left behind, starting a fresh ledger.

        Call this once, before the first chapter of a run is attempted, so the
        file keeps its per-run meaning.  Without it, a chapter that failed last
        night would still be listed after a flawless re-run (a successful
        chapter writes no record to overwrite the old failure), and every
        reader of the ledger would report the book as incomplete.

        The removal is deliberately *not* a rotation: the ledger file exists if
        and only if the run that just finished had at least one failure, which
        makes "missing file == complete run" true.  Consumers that want history
        should archive the file themselves.

        :return: ``True`` if a previous ledger file was removed, ``False`` if
            there was nothing to discard.
        """
        return self.clear()

    def clear(self) -> bool:
        """
        Remove the ledger file.

        :return: ``True`` if a file was removed, ``False`` if there was none.
        """
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                return False
        return True

    def __repr__(self) -> str:
        return f"<DownloadLedger path='{self._path}'>"
