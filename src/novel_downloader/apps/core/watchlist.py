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

The store performs a read-modify-write cycle without locking. It is designed
for a single process at a time (the CLI and each desktop/API process). Two
processes writing simultaneously are serialized by the filesystem only at the
``os.replace`` step, so the last writer wins; neither can corrupt the file.
This is the same trade-off used by
:class:`novel_downloader.infra.persistence.state.StateManager`, but with an
atomic write added.
"""

from __future__ import annotations

__all__ = [
    "WatchEntry",
    "WatchlistStore",
    "default_watchlist_path",
]

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novel_downloader.infra.paths import app_home

logger = logging.getLogger(__name__)

#: Filename used inside the application home directory.
WATCHLIST_FILENAME = "watchlist.json"

#: Schema version written to the JSON document.
_SCHEMA_VERSION = 1


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


class WatchlistStore:
    """
    File-backed CRUD store for followed books.

    Every public operation reads the current file, applies the change and
    atomically writes the result back. A corrupt or unreadable file is treated
    as an empty list (and logged), so a broken file never prevents the CLI from
    running; the first successful write replaces it.

    :param path: Watchlist file to use; defaults to
        :func:`default_watchlist_path`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_watchlist_path()

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
        chapter actually changed; a missing entry is a no-op.

        :param site: Source site key.
        :param book_id: Site-specific book id.
        :param last_chapter: Latest chapter observed on the site.
        :param title: Optional title to fill in when the entry has none.
        :return: ``True`` when the stored chapter changed.
        """
        key = (site.strip(), str(book_id).strip())
        chapter = last_chapter.strip()
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
