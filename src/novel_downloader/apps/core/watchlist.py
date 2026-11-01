#!/usr/bin/env python3
"""
novel_downloader.apps.core.watchlist
------------------------------------

Persistent list of books the user wants to follow ("追更").

A watch entry records the source site, the book id, a display title, the last
chapter observed on the site and the time the entry was last updated::

    {"site": "b520", "book_id": "8_8187", "title": "...",
     "last_chapter": "...", "updated_at": "2026-01-01T00:00:00+00:00"}

Storage
-------

The list lives in ``<app_home>/watchlist.json`` where ``app_home`` follows
:func:`novel_downloader.infra.paths.app_home` (the platform data directory,
overridable with ``NOVEL_HOME``).

Writes are atomic: the new document is written to a temporary file in the same
directory, flushed and then moved into place with :func:`os.replace`, so a
reader never observes a half-written file and a crash leaves the previous
version intact.

Concurrency
-----------

Every read-modify-write mutation (``add``, ``remove``, ``update_latest``) holds
a per-store :class:`threading.Lock`, so several threads may share one store
instance — the desktop calls it from worker threads (follow add/remove and the
follow check). Writes themselves stay atomic: the new document is written to a
temporary file in the same directory, flushed and then moved into place with
:func:`os.replace`, so a reader never observes a half-written file.

Threads within one process are therefore safe. Two *processes* writing
simultaneously are serialized by the filesystem only at the ``os.replace``
step, so the last writer wins; neither can corrupt the file. This is the same
trade-off used by
:class:`novel_downloader.infra.persistence.state.StateManager`, but with an
atomic write added.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_CHECK_CONCURRENCY",
    "DEFAULT_CHECK_TIMEOUT",
    "PER_SITE_LIMIT",
    "CheckResult",
    "WatchEntry",
    "WatchlistStore",
    "check_all",
    "check_entry",
    "default_watchlist_path",
]

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from novel_downloader.infra.paths import app_home
from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)

#: Filename used inside the application home directory.
WATCHLIST_FILENAME = "watchlist.json"

#: Schema version written to the JSON document.
_SCHEMA_VERSION = 1

#: Results requested per site while looking for a followed book's latest
#: chapter; shared by the CLI ``follow check`` and the desktop follow check.
PER_SITE_LIMIT = 10

#: Default per-search timeout (seconds) for the follow check; matches the
#: ``search`` command.
DEFAULT_CHECK_TIMEOUT = 5.0

#: Default number of followed books checked at the same time.
DEFAULT_CHECK_CONCURRENCY = 4


def default_watchlist_path() -> Path:
    """
    Return the default path of the watchlist file.

    Resolved on every call so ``NOVEL_HOME`` changes (tests, portable installs)
    take effect without re-importing the module.
    """
    return app_home() / WATCHLIST_FILENAME


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WatchEntry:
    """One followed book."""

    site: str
    book_id: str
    title: str = ""
    last_chapter: str = ""
    updated_at: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Identity of the entry within a watchlist."""
        return (self.site, self.book_id)

    def to_dict(self) -> dict[str, str]:
        """Return the JSON representation of this entry."""
        return {
            "site": self.site,
            "book_id": self.book_id,
            "title": self.title,
            "last_chapter": self.last_chapter,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> WatchEntry | None:
        """
        Build an entry from decoded JSON, returning ``None`` when unusable.

        Only ``site`` and ``book_id`` are mandatory; every other field is
        coerced to a string and defaults to an empty string. Entries with an
        empty identity are dropped so a hand-edited file cannot poison the
        list.
        """
        if not isinstance(data, dict):
            return None

        site = str(data.get("site") or "").strip()
        book_id = str(data.get("book_id") or "").strip()
        if not site or not book_id:
            return None

        return cls(
            site=site,
            book_id=book_id,
            title=str(data.get("title") or "").strip(),
            last_chapter=str(data.get("last_chapter") or "").strip(),
            updated_at=str(data.get("updated_at") or "").strip(),
        )


@dataclass(frozen=True)
class CheckResult:
    """Outcome of checking one followed book for a new chapter."""

    #: The entry as it was when the check started.
    entry: WatchEntry
    #: Newest chapter observed on the site (``""`` when nothing matched).
    latest: str = ""
    #: Title of the search result the chapter came from.
    matched_title: str = ""
    #: Error message when the search itself failed.
    error: str | None = None
    #: Whether :meth:`WatchlistStore.update_latest` persisted ``latest``.
    changed: bool = False

    @property
    def found(self) -> bool:
        """Whether a latest chapter was found (failed checks never do)."""
        return self.error is None and bool(self.latest)


class WatchlistStore:
    """
    File-backed CRUD store for followed books.

    Every public mutation reads the current file, applies the change and
    atomically writes the result back while holding a per-store
    :class:`threading.Lock`, so one store instance may be shared by several
    threads (the desktop does) without losing updates. A corrupt or unreadable
    file is treated as an empty list (and logged), so a broken file never
    prevents the CLI from running; the first successful write replaces it.

    :param path: Watchlist file to use; defaults to
        :func:`default_watchlist_path`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_watchlist_path()
        #: Serializes read-modify-write cycles across threads.
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The file backing this store."""
        return self._path

    # ---------- queries ----------

    def entries(self) -> list[WatchEntry]:
        """Return every followed book in file order."""
        return self._read()

    def get(self, site: str, book_id: str) -> WatchEntry | None:
        """Return one entry, or ``None`` when it is not followed."""
        key = (site.strip(), str(book_id).strip())
        for entry in self._read():
            if entry.key == key:
                return entry
        return None

    # ---------- mutations ----------

    def add(
        self,
        site: str,
        book_id: str,
        *,
        title: str | None = None,
        last_chapter: str | None = None,
    ) -> tuple[WatchEntry, bool]:
        """
        Add a book or refresh an existing entry.

        An existing entry keeps its title/last chapter unless a non-empty
        replacement is supplied, so ``follow add`` never erases data collected
        by ``follow check``.

        :param site: Source site key.
        :param book_id: Site-specific book id.
        :param title: Optional display title.
        :param last_chapter: Optional current last chapter.
        :return: ``(entry, created)`` where ``created`` is ``False`` when the
            book was already followed.
        """
        norm_site = site.strip()
        norm_id = str(book_id).strip()
        if not norm_site or not norm_id:
            raise ValueError("site and book_id must not be empty")

        with self._lock:
            entries = self._read()
            for index, entry in enumerate(entries):
                if entry.key != (norm_site, norm_id):
                    continue

                new_title = title.strip() if title and title.strip() else entry.title
                new_chapter = (
                    last_chapter.strip()
                    if last_chapter and last_chapter.strip()
                    else entry.last_chapter
                )
                updated = replace(
                    entry,
                    title=new_title,
                    last_chapter=new_chapter,
                    updated_at=_now_iso(),
                )
                entries[index] = updated
                self._write(entries)
                return updated, False

            created = WatchEntry(
                site=norm_site,
                book_id=norm_id,
                title=title.strip() if title else "",
                last_chapter=last_chapter.strip() if last_chapter else "",
                updated_at=_now_iso(),
            )
            entries.append(created)
            self._write(entries)
            return created, True

    def remove(self, site: str, book_id: str) -> bool:
        """
        Remove a book from the list.

        :return: ``True`` when an entry was removed, ``False`` when it was not
            followed (idempotent).
        """
        key = (site.strip(), str(book_id).strip())
        with self._lock:
            entries = self._read()
            kept = [entry for entry in entries if entry.key != key]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    def update_latest(
        self,
        site: str,
        book_id: str,
        last_chapter: str,
        *,
        title: str | None = None,
    ) -> bool:
        """
        Record a newly observed last chapter.

        The entry is only rewritten (and ``updated_at`` refreshed) when the
        chapter actually changed; a missing entry is a no-op. Safe to call from
        several threads sharing this store.

        :param site: Source site key.
        :param book_id: Site-specific book id.
        :param last_chapter: Latest chapter observed on the site.
        :param title: Optional title to fill in when the entry has none.
        :return: ``True`` when the stored chapter changed.
        """
        key = (site.strip(), str(book_id).strip())
        chapter = last_chapter.strip()
        with self._lock:
            entries = self._read()
            for index, entry in enumerate(entries):
                if entry.key != key:
                    continue
                if not chapter or chapter == entry.last_chapter:
                    return False

                new_title = entry.title
                if not new_title and title and title.strip():
                    new_title = title.strip()
                entries[index] = replace(
                    entry,
                    last_chapter=chapter,
                    title=new_title,
                    updated_at=_now_iso(),
                )
                self._write(entries)
                return True
            return False

    # ---------- persistence ----------

    def _read(self) -> list[WatchEntry]:
        """Load entries from disk, tolerating a missing or corrupt file."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Could not read watchlist %s: %s", self._path, exc)
            return []

        try:
            document = json.loads(text)
        except (ValueError, TypeError) as exc:
            logger.warning("Ignoring corrupt watchlist %s: %s", self._path, exc)
            return []

        if isinstance(document, dict):
            raw_entries = document.get("books")
            if not isinstance(raw_entries, list):
                logger.warning(
                    "Ignoring watchlist %s: 'books' is not a list", self._path
                )
                return []
        elif isinstance(document, list):
            # Accept a bare list written by older/hand-made files.
            raw_entries = document
        else:
            logger.warning("Ignoring watchlist %s: unsupported document", self._path)
            return []

        entries: list[WatchEntry] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_entries:
            entry = WatchEntry.from_dict(raw)
            if entry is None or entry.key in seen:
                continue
            seen.add(entry.key)
            entries.append(entry)
        return entries

    def _write(self, entries: list[WatchEntry]) -> None:
        """Atomically replace the watchlist file with ``entries``."""
        payload = {
            "version": _SCHEMA_VERSION,
            "books": [entry.to_dict() for entry in entries],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            # Never leave a temporary file behind, and keep the old file as-is.
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise


# ---------------------------------------------------------------------------
# Update check ("追更检查"), shared by the CLI and the desktop client
# ---------------------------------------------------------------------------


def _get_search_module() -> ModuleType:
    """
    Return the search module (patch point for tests).

    Imported lazily so importing this module (and everything that imports it,
    like the API routes) does not pull in the aiohttp/plugin stack.
    """
    from novel_downloader.plugins import search as search_module

    return search_module


def _match_result(
    entry: WatchEntry, results: list[SearchResult]
) -> SearchResult | None:
    """
    Pick the search result that belongs to ``entry``.

    A result from the same site with the exact same book id wins; otherwise a
    result whose normalized title equals the followed title is accepted (a site
    may expose the same book under a different id).
    """
    for result in results:
        if result.get("site") != entry.site:
            continue
        if str(result.get("book_id", "")) == entry.book_id:
            return result

    if not entry.title:
        return None

    wanted = normalize_text(entry.title)
    if not wanted:
        return None
    for result in results:
        if result.get("site") != entry.site:
            continue
        if normalize_text(result.get("title", "")) == wanted:
            return result
    return None


async def check_entry(
    entry: WatchEntry, timeout: float = DEFAULT_CHECK_TIMEOUT
) -> CheckResult:
    """
    Search one site and return the newest chapter for ``entry``.

    Network and plugin failures are reported as
    :attr:`CheckResult.error` instead of raising, so one broken book never
    interrupts a whole sweep.
    """
    search_module = _get_search_module()
    keyword = entry.title or entry.book_id
    try:
        results = await search_module.search(
            keyword=keyword,
            sites=[entry.site],
            limit=None,
            per_site_limit=PER_SITE_LIMIT,
            timeout=timeout,
            sort="quality",
        )
    except Exception as exc:  # network/plugin failures are per-book, not fatal
        # Keep the type name next to the message: some network errors (bare
        # ``TimeoutError`` et al.) stringify to an empty string.
        return CheckResult(entry=entry, error=f"{type(exc).__name__}: {exc}")

    matched = _match_result(entry, results)
    if matched is None:
        return CheckResult(entry=entry)
    return CheckResult(
        entry=entry,
        latest=str(matched.get("latest_chapter") or "").strip(),
        matched_title=str(matched.get("title") or "").strip(),
    )


async def _record_latest(
    store: WatchlistStore, entry: WatchEntry, result: CheckResult
) -> CheckResult:
    """
    Persist a found chapter, turning a storage failure into an error result.

    Unchanged chapters and not-found results pass through untouched. The
    write itself runs in a worker thread (the store is thread-safe): the
    atomic write includes an ``fsync``, which on a slow disk must neither
    block the event loop nor withhold a search slot from the remaining books.
    """
    if not result.found or result.latest == entry.last_chapter:
        return result
    try:
        changed = await asyncio.to_thread(
            store.update_latest,
            entry.site,
            entry.book_id,
            result.latest,
            title=result.matched_title,
        )
    except Exception as exc:  # disk failures are per-book, not fatal
        logger.warning("Could not update %s/%s: %s", entry.site, entry.book_id, exc)
        return CheckResult(entry=entry, error=f"{type(exc).__name__}: {exc}")
    return replace(result, changed=changed)


async def check_all(
    store: WatchlistStore,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    max_concurrency: int = DEFAULT_CHECK_CONCURRENCY,
    progress_cb: Callable[[CheckResult], None] | None = None,
) -> list[CheckResult]:
    """
    Check every followed book in ``store`` for a new chapter.

    Each book is searched independently with at most ``max_concurrency``
    searches in flight (an :class:`asyncio.Semaphore` bounds them); a failing
    or slow book never affects the others. Every new chapter found is
    persisted immediately through the thread-safe store, so the results and
    the file agree even when the sweep is interrupted.

    :param store: Watchlist to read the entries from and persist updates into.
    :param timeout: Per-book search timeout in seconds.
    :param max_concurrency: Maximum number of books searched at the same time
        (values below 1 are treated as 1).
    :param progress_cb: Optional observer called once per finished check, in
        completion order (not list order), on the event loop thread. A raising
        callback is logged and ignored so it cannot abort the sweep.
    :return: One :class:`CheckResult` per entry, in the store's entry order
        regardless of the order the checks completed in.
    """
    entries = store.entries()
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
    slots: list[CheckResult | None] = [None] * len(entries)

    async def worker(index: int, entry: WatchEntry) -> None:
        async with semaphore:
            result = await check_entry(entry, timeout)
        # Persist (and report) outside the semaphore and off the event loop:
        # a slow disk write must neither withhold a search slot from the
        # remaining books nor block the loop serving other requests.
        result = await _record_latest(store, entry, result)
        slots[index] = result
        if progress_cb is not None:
            try:
                progress_cb(result)
            except Exception:  # a broken observer must not abort the sweep
                logger.exception("follow-check progress callback failed")

    await asyncio.gather(*(worker(index, entry) for index, entry in enumerate(entries)))
    return [slot for slot in slots if slot is not None]
