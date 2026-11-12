#!/usr/bin/env python3
"""
novel_downloader.infra.persistence.chapter_storage
--------------------------------------------------

Storage module for managing novel chapters in an SQLite database.

The store is opened by several stages of a download (raw fetch, corrector,
cleaner, exporter) and, in principle, by more than one process.  The module
therefore treats the file as a shared resource:

* the schema is versioned with ``PRAGMA user_version`` and migrated in place,
  inside a write transaction, only adding columns (never dropping data);
* ``PRAGMA integrity_check`` guards ``connect()`` -- a damaged file is copied
  aside and reported, never silently replaced with an empty database;
* write transactions use ``BEGIN IMMEDIATE`` so two connections queue on the
  write lock instead of deadlocking on commit, and ``WAL`` +
  ``busy_timeout`` keep readers and writers out of each other's way;
* every write updates the in-memory flag cache only after ``COMMIT``, so the
  cache never advertises a chapter that is not on disk.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA_VERSION",
    "ChapterDatabaseInfo",
    "ChapterStorage",
    "ChapterStorageCorruptError",
    "ChapterStorageError",
    "ChapterStorageSchemaError",
    "inspect_chapter_database",
]

import contextlib
import json
import shutil
import sqlite3
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

from novel_downloader.schemas import ChapterDict

#: Bumped whenever the on-disk layout changes.  Older files are migrated in
#: place; newer files are refused instead of being written by older code.
SCHEMA_VERSION: Final[int] = 1

#: How long a connection waits for a competing writer before raising
#: ``database is locked``.
_BUSY_TIMEOUT_MS: Final[int] = 5_000

#: Bound parameters per ``IN (...)`` list.  SQLite builds before 3.32 cap the
#: number of host parameters at 999; chunking keeps long books exportable.
_SQL_VARIABLE_LIMIT: Final[int] = 900

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chapters (
  id           TEXT    NOT NULL PRIMARY KEY,
  title        TEXT    NOT NULL,
  content      TEXT    NOT NULL,
  need_refetch BOOLEAN NOT NULL DEFAULT 0,
  extra        TEXT
)
"""

#: Columns without which a legacy table cannot be repaired automatically.
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("id", "title", "content")

#: Columns that older revisions may lack; added with a conservative default.
_OPTIONAL_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("need_refetch", "BOOLEAN NOT NULL DEFAULT 0"),
    ("extra", "TEXT"),
)

_UPSERT_SQL = """
INSERT INTO chapters (id, title, content, need_refetch, extra)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    title=excluded.title,
    content=excluded.content,
    need_refetch=excluded.need_refetch,
    extra=excluded.extra
"""

#: Fragments SQLite uses when it cannot read the file as a database at all.
_CORRUPTION_MARKERS: Final[tuple[str, ...]] = (
    "not a database",
    "malformed",
    "corrupt",
    "encrypted",
)


class ChapterStorageError(RuntimeError):
    """Base class for storage problems that need a caller-visible diagnosis."""


class ChapterStorageSchemaError(ChapterStorageError):
    """The on-disk schema cannot be used by this version of the code."""


class ChapterStorageCorruptError(ChapterStorageError, sqlite3.DatabaseError):
    """The database file is damaged.

    Also a :class:`sqlite3.DatabaseError`, so existing ``except sqlite3.Error``
    handlers keep working.  The original file is left in place; a copy is
    written next to it (see the exception message) for recovery.
    """


@dataclass(frozen=True)
class ChapterDatabaseInfo:
    """Read-only summary of a chapter database file.

    ``schema_version`` is the ``PRAGMA user_version`` value, ``chapter_count``
    the number of rows currently stored.
    """

    schema_version: int
    chapter_count: int


def inspect_chapter_database(
    conn: sqlite3.Connection,
    *,
    source: str = "chapter database",
    require_schema: bool = True,
    allow_newer: bool = False,
) -> ChapterDatabaseInfo:
    """
    Validate a chapter database through an already-open connection.

    Intended for backup/restore: the helper only reads and therefore works on
    a read-only connection.  With ``require_schema`` (the default) it refuses
    files this build cannot use (newer schema, missing/renamed table, missing
    required columns) and files whose ``PRAGMA integrity_check`` does not
    report ``ok``.  That strict form is what a restore must use: it may never
    replace a healthy database with a file that would only fail later, after
    the old data is gone.

    A backup, by contrast, only has to be a faithful copy.  It passes
    ``require_schema=False, allow_newer=True`` so data written by a newer
    build (or a file with an unexpected layout) can still be preserved; the
    integrity check is always enforced.

    :param conn: Open connection to the candidate database.
    :param source: Human-readable description used in error messages.
    :param require_schema: Require the ``chapters`` table and its required
        columns, and reject a newer schema version.
    :param allow_newer: Tolerate ``user_version`` values above
        :data:`SCHEMA_VERSION` (only meaningful with ``require_schema=False``).
    :raises ChapterStorageCorruptError: if the file fails its integrity check.
    :raises ChapterStorageSchemaError: if the layout cannot be opened by this
        build and ``require_schema`` is true.
    :return: Schema version and row count of the validated database.
    """
    problem = ChapterStorage._integrity_problem(conn)
    if problem is not None:
        raise ChapterStorageCorruptError(
            f"{source} failed its integrity check ({problem})."
        )

    version = ChapterStorage._user_version(conn)
    if version > SCHEMA_VERSION and (require_schema or not allow_newer):
        raise ChapterStorageSchemaError(
            f"{source} uses schema version {version}, newer than this build "
            f"supports ({SCHEMA_VERSION}). Upgrade the application instead of "
            "restoring this file."
        )

    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'chapters'"
    ).fetchone()
    if row is None or str(row[0]) != "table":
        if require_schema:
            raise ChapterStorageSchemaError(
                f"{source} has no 'chapters' table; it is not a chapter database."
            )
        return ChapterDatabaseInfo(schema_version=version, chapter_count=0)

    columns = {str(col[1]) for col in conn.execute("PRAGMA table_info(chapters)")}
    missing = [name for name in _REQUIRED_COLUMNS if name not in columns]
    if missing and require_schema:
        raise ChapterStorageSchemaError(
            f"{source} is missing required column(s) {', '.join(missing)}; "
            "it is not a usable chapter database."
        )

    count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()
    return ChapterDatabaseInfo(
        schema_version=version,
        chapter_count=int(count[0]) if count is not None else 0,
    )


class ChapterStorage:
    """
    Manage storage of chapters in an SQLite database.
    """

    def __init__(self, base_dir: str | Path, filename: str) -> None:
        """
        Initialize storage for a specific book.

        :param base_dir: Directory path where the SQLite file will be stored.
        :param filename: SQLite filename.
        """
        self._db_path = Path(base_dir) / filename
        self._conn: sqlite3.Connection | None = None
        # Cache: chapter id -> need_refetch flag
        self._refetch_flags: dict[str, bool] = {}

    def connect(self) -> None:
        """
        Open the SQLite connection, verify/migrate the schema, warm the cache.

        The file is checked with ``PRAGMA integrity_check`` before anything is
        written.  A damaged file raises :class:`ChapterStorageCorruptError`
        (after being copied aside) and is never turned into a fresh database.

        :raises ChapterStorageCorruptError: if the file is not a valid,
            readable SQLite database.
        :raises ChapterStorageSchemaError: if the file was written by a newer
            schema version, or its layout cannot be migrated.
        :raises sqlite3.OperationalError: if the file cannot be opened (missing
            directory, permissions, read-only location, ...).
        """
        if self._conn is not None:
            return

        conn = sqlite3.connect(
            str(self._db_path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        integrity_problem: str | None = None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            integrity_problem = self._integrity_problem(conn)
            if integrity_problem is None:
                version = self._user_version(conn)
                if version > SCHEMA_VERSION:
                    raise ChapterStorageSchemaError(
                        f"Chapter database '{self._db_path}' uses schema version "
                        f"{version}, newer than this build supports "
                        f"({SCHEMA_VERSION}). Upgrade the application instead of "
                        "writing to this file."
                    )
                # WAL lets an exporter read while a processor writes.  Set it
                # only after the file proved healthy and not newer than us.
                self._enable_wal(conn)
                conn.execute("PRAGMA synchronous = NORMAL")
                self._migrate(conn, version)
                self._load_existing_keys(conn)
        except ChapterStorageSchemaError:
            self._safe_close(conn)
            raise
        except sqlite3.DatabaseError as exc:
            self._safe_close(conn)
            if _is_corruption_error(exc):
                raise self._corrupt_error(str(exc)) from exc
            raise
        except BaseException:
            self._safe_close(conn)
            raise

        if integrity_problem is not None:
            self._safe_close(conn)
            raise self._corrupt_error(integrity_problem)

        self._conn = conn

    def exists(self, chap_id: str) -> bool:
        """
        Return True if the chapter id is known (present in cache/DB).

        :param chap_id: Chapter identifier.
        """
        return chap_id in self._refetch_flags

    def need_refetch(self, chap_id: str) -> bool:
        """
        Return True if the chapter is known to require refetch; if the id is
        unknown, default to True (refetch) to be conservative.

        :param chap_id: Chapter identifier.
        """
        return self._refetch_flags.get(chap_id, True)

    def existing_ids(self) -> set[str]:
        """
        All chapter IDs currently present in this store.
        """
        return set(self._refetch_flags.keys())

    def clean_ids(self) -> set[str]:
        """
        Chapter IDs present in this store that DO NOT need refetch.
        """
        return {cid for cid, need in self._refetch_flags.items() if need is False}

    def dirty_ids(self) -> set[str]:
        """
        Chapter IDs present in this store that DO need refetch.
        """
        return {cid for cid, need in self._refetch_flags.items() if need is True}

    def upsert_chapter(self, data: ChapterDict, need_refetch: bool = False) -> None:
        """
        Insert or update a single chapter.

        The row and the in-memory flag become visible together: a chapter is
        reported as stored only after ``COMMIT`` succeeded.

        :param data: ChapterDict containing `id`, `title`, `content`, `extra`.
        :param need_refetch: Whether this chapter should be marked to refetch.
        :raises TypeError: if ``id``/``title``/``content`` is not a string, or
            ``extra`` is not JSON-serializable.
        :raises ValueError: if ``id`` is empty.
        """
        record = self._encode_chapter(data, need_refetch)
        with self._transaction(self.conn) as conn:
            conn.execute(_UPSERT_SQL, record)
        self._refetch_flags[record[0]] = need_refetch

    def upsert_chapters(
        self, data: list[ChapterDict], need_refetch: bool = False
    ) -> None:
        """
        Insert or update multiple chapters in a single transaction.

        All records are encoded and validated before the transaction opens, so
        a bad row cannot leave earlier rows (or the flag cache) half-written.
        If the same id appears more than once, the last occurrence wins.

        :param data: List of ChapterDicts.
        :param need_refetch: Whether these chapters should be marked to refetch.
        :raises TypeError: if a row violates the :class:`ChapterDict` contract.
        :raises ValueError: if a chapter id is empty.
        """
        if not data:
            return

        records = [self._encode_chapter(chapter, need_refetch) for chapter in data]

        with self._transaction(self.conn) as conn:
            conn.executemany(_UPSERT_SQL, records)

        # Publish the flags only after the rows are durably written.
        for record in records:
            self._refetch_flags[record[0]] = need_refetch

    def get_chapter(self, chap_id: str) -> ChapterDict | None:
        """
        Retrieve a single chapter by id.

        :param chap_id: Chapter identifier.
        :return: A ChapterDict if found, else None.
        """
        cur = self.conn.execute(
            "SELECT id, title, content, extra FROM chapters WHERE id = ?",
            (chap_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        return ChapterDict(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            extra=self._load_dict(row["extra"]),
        )

    def get_chapters(self, chap_ids: list[str]) -> dict[str, ChapterDict | None]:
        """
        Retrieve multiple chapters by their ids.

        The returned mapping keeps the caller's order (first occurrence for
        duplicate ids) and contains ``None`` for every requested id that is
        missing, which is how exporters detect gaps.  Ids are looked up in
        chunks so books with more than 999 chapters work on old SQLite builds.

        :param chap_ids: List of chapter identifiers.
        :return: A dict mapping chap_id to ChapterDict (or None if not found).
        """
        if not chap_ids:
            return {}

        result: dict[str, ChapterDict | None] = dict.fromkeys(chap_ids)
        unique_ids = list(dict.fromkeys(chap_ids))
        for start in range(0, len(unique_ids), _SQL_VARIABLE_LIMIT):
            chunk = unique_ids[start : start + _SQL_VARIABLE_LIMIT]
            placeholders = ",".join("?" for _ in chunk)
            query = f"""
                SELECT id, title, content, extra
                  FROM chapters
                 WHERE id IN ({placeholders})
            """
            rows = self.conn.execute(query, tuple(chunk)).fetchall()
            for row in rows:
                result[row["id"]] = ChapterDict(
                    id=row["id"],
                    title=row["title"],
                    content=row["content"],
                    extra=self._load_dict(row["extra"]),
                )
        return result

    def delete_chapter(self, chap_id: str) -> bool:
        """
        Delete a single chapter if it exists.

        :param chap_id: Chapter identifier.
        :return: True if a row was deleted, False otherwise.
        """
        with self._transaction(self.conn) as conn:
            cur = conn.execute(
                "DELETE FROM chapters WHERE id = ?",
                (chap_id,),
            )
            deleted = (cur.rowcount or 0) > 0

        # Only after COMMIT: the cache must not forget a row that is still on
        # disk because the delete transaction rolled back.
        self._refetch_flags.pop(chap_id, None)

        return deleted

    def delete_chapters(self, chap_ids: list[str]) -> int:
        """
        Delete multiple chapters if they exist (single transaction).

        :param chap_ids: List of chapter identifiers.
        :return: Number of rows deleted.
        """
        if not chap_ids:
            return 0

        unique_ids = list(dict.fromkeys(chap_ids))
        deleted = 0
        with self._transaction(self.conn) as conn:
            for start in range(0, len(unique_ids), _SQL_VARIABLE_LIMIT):
                chunk = unique_ids[start : start + _SQL_VARIABLE_LIMIT]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM chapters WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
                deleted += cur.rowcount or 0

        for cid in unique_ids:
            self._refetch_flags.pop(cid, None)

        return deleted

    def vacuum(self) -> None:
        """
        Rebuild the SQLite file to reclaim disk space.
        """
        self.conn.execute("VACUUM")

    def close(self) -> None:
        """
        Close the database connection and clear in-memory caches.
        """
        if self._conn is None:
            return

        with contextlib.suppress(Exception):
            self._conn.close()

        self._conn = None
        self._refetch_flags.clear()

    @property
    def conn(self) -> sqlite3.Connection:
        """
        Return the active SQLite connection.

        :raises RuntimeError: if connect() has not been called.
        """
        if self._conn is None:
            raise RuntimeError(
                "Database connection is not established. Call connect() first."
            )
        return self._conn

    # ------------------------------------------------------------------
    # schema / recovery helpers
    # ------------------------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection, version: int) -> None:
        """
        Bring an existing file up to :data:`SCHEMA_VERSION`, in one transaction.

        Only additive changes are performed (``ALTER TABLE ... ADD COLUMN``);
        unknown extra columns are left untouched.  Re-reading the layout under
        the write lock makes concurrent first-opens safe: the loser of the race
        sees the already-migrated schema instead of a duplicate-column error.

        :param conn: Connection being initialized (not yet published).
        :param version: ``PRAGMA user_version`` read before the transaction.
        :raises ChapterStorageSchemaError: if the file cannot be migrated.
        """
        with self._transaction(conn) as tx:
            row = tx.execute(
                "SELECT type FROM sqlite_master WHERE name = 'chapters'"
            ).fetchone()

            if row is None:
                if version > 0:
                    raise ChapterStorageSchemaError(
                        f"Chapter database '{self._db_path}' declares schema "
                        f"version {version} but has no 'chapters' table. "
                        "Refusing to recreate it (that would hide data loss); "
                        "inspect the file or move it aside."
                    )
                tx.execute(_CREATE_TABLE_SQL)
                tx.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return

            if str(row["type"]) != "table":
                raise ChapterStorageSchemaError(
                    f"Chapter database '{self._db_path}' has a "
                    f"'{row['type']}' named 'chapters', not a table."
                )

            info = {
                str(col["name"]): col
                for col in tx.execute("PRAGMA table_info(chapters)")
            }
            missing = [name for name in _REQUIRED_COLUMNS if name not in info]
            if missing:
                raise ChapterStorageSchemaError(
                    f"Chapter database '{self._db_path}' is missing required "
                    f"column(s) {', '.join(missing)}; it cannot be migrated "
                    "without guessing at the lost structure."
                )

            for name, declaration in _OPTIONAL_COLUMNS:
                if name not in info:
                    tx.execute(
                        f'ALTER TABLE chapters ADD COLUMN "{name}" {declaration}'
                    )

            if version != SCHEMA_VERSION:
                tx.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _enable_wal(conn: sqlite3.Connection) -> None:
        """
        Switch the file to WAL mode, waiting out concurrent openers.

        Unlike ordinary statements, ``PRAGMA journal_mode`` needs a brief
        exclusive lock and reports ``database is locked`` *without* consulting
        the busy handler.  Several processes opening the same new file (e.g. a
        download and an export started together) would otherwise fail on this
        single line, so retry it until ``busy_timeout`` is exhausted.

        :param conn: Open connection.
        """
        deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
        delay = 0.01
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL").fetchone()
                return
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    @staticmethod
    def _integrity_problem(conn: sqlite3.Connection) -> str | None:
        """
        Run ``PRAGMA integrity_check`` and return a short problem summary.

        :param conn: Open connection.
        :return: ``None`` when healthy, otherwise the first error lines
            (truncated) for the exception message.
        """
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        problems: list[str] = []
        size = 0
        for row in rows:
            text = str(row[0])
            if text.strip().lower() == "ok":
                continue
            problems.append(text)
            size += len(text)
            if size >= 800 or len(problems) >= 3:
                break
        if not problems:
            return None
        return "; ".join(problems)[:800]

    @staticmethod
    def _user_version(conn: sqlite3.Connection) -> int:
        """
        Read ``PRAGMA user_version`` as an integer.

        :param conn: Open connection.
        """
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def _corrupt_error(self, detail: str) -> ChapterStorageCorruptError:
        """
        Back the damaged file up and build an actionable exception.

        The original file is never moved or overwritten, so a user (or a later
        release) can still attempt recovery.  A failed backup does not mask the
        corruption: the error message says so and stays diagnosable.

        :param detail: Short description of the integrity/parse failure.
        """
        backup = self._backup_corrupt()
        if backup is not None:
            note = f"a backup copy was saved to '{backup}'"
        else:
            note = (
                "an automatic backup could not be written; "
                "the original was left untouched"
            )
        return ChapterStorageCorruptError(
            f"Chapter database '{self._db_path}' is corrupt or unreadable "
            f"({detail}). {note}. The file was NOT recreated -- move it aside "
            "after recovering what you can to start a fresh download."
        )

    def _backup_corrupt(self) -> Path | None:
        """
        Copy the database (and its WAL sidecars) next to itself.

        :return: Path of the backup, or ``None`` if copying failed.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self._db_path.with_name(f"{self._db_path.name}.corrupt-{stamp}")
        target = base
        counter = 1
        while target.exists() and counter < 100:
            target = base.with_name(f"{base.name}-{counter}")
            counter += 1

        try:
            shutil.copy2(self._db_path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self._db_path}{suffix}")
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(f"{target}{suffix}"))
        except OSError:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)
            return None
        return target

    @staticmethod
    def _safe_close(conn: sqlite3.Connection) -> None:
        """Close ``conn`` best-effort while unwinding from ``connect()``."""
        with contextlib.suppress(Exception):
            conn.close()

    # ------------------------------------------------------------------
    # transaction / cache helpers
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """
        Run a block in an immediate write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so a competing
        writer waits on ``busy_timeout`` instead of failing at ``COMMIT`` (the
        classic deferred-transaction upgrade deadlock).  Any failure -- the
        block's or ``COMMIT``'s -- rolls the whole transaction back before the
        exception escapes, so a failed call never leaves half-written rows.

        :param conn: Connection to use (``connect()`` passes one that is not
            published on the instance yet).
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise

    def _load_existing_keys(self, conn: sqlite3.Connection) -> None:
        """
        Populate the in-memory cache from the database.

        :param conn: Connection being initialized.
        :raises ChapterStorageSchemaError: if a row has a non-text id, which
            would silently poison the cache and de-duplication.
        """
        cur = conn.execute("SELECT id, need_refetch FROM chapters")
        flags: dict[str, bool] = {}
        for row in cur.fetchall():
            chap_id = row["id"]
            if not isinstance(chap_id, str):
                raise ChapterStorageSchemaError(
                    f"Chapter database '{self._db_path}' contains a row whose "
                    f"id is {type(chap_id).__name__}, not text."
                )
            flags[chap_id] = self._as_bool(row["need_refetch"], default=True)
        self._refetch_flags = flags

    @staticmethod
    def _encode_chapter(
        chapter: ChapterDict, need_refetch: bool
    ) -> tuple[str, str, str, int, str]:
        """
        Validate and serialize one chapter for the row tuple.

        ``content == ""`` is kept: a media-only or processor-produced chapter
        legitimately has an empty body, and silently dropping it would look
        like a missing chapter.  ``None`` is a caller bug and is rejected.

        :param chapter: Source chapter.
        :param need_refetch: Flag value for the row.
        :raises TypeError: on a non-text ``id``/``title``/``content`` or a
            non-JSON-serializable ``extra``.
        :raises ValueError: on an empty id.
        """
        chap_id = chapter["id"]
        title = chapter["title"]
        content = chapter["content"]

        if not isinstance(chap_id, str):
            raise TypeError(f"chapter id must be str, got {type(chap_id).__name__}")
        if not chap_id:
            raise ValueError("chapter id must not be empty")
        if not isinstance(title, str):
            raise TypeError(
                f"chapter {chap_id!r} title must be str, got {type(title).__name__}"
            )
        if not isinstance(content, str):
            raise TypeError(
                f"chapter {chap_id!r} content must be str, got {type(content).__name__}"
            )

        extra_json = json.dumps(chapter["extra"], ensure_ascii=False)
        return (chap_id, title, content, int(need_refetch), extra_json)

    @staticmethod
    def _as_bool(value: Any, *, default: bool = True) -> bool:
        """
        Coerce a legacy ``need_refetch`` value to a real boolean.

        SQLite is dynamically typed, so a hand-edited or old file can hold
        text (where ``bool("0")`` would wrongly mean *True*) or ``NULL``.  An
        unknown flag is treated as "refetch" -- the conservative choice for
        chapter completeness.

        :param value: Raw column value.
        :param default: Result for ``NULL``/unparsable values.
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, float):
            return value != 0
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "t", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "f", "no", "n", "off", ""}:
                return False
            try:
                return float(text) != 0
            except ValueError:
                return default
        return default

    @staticmethod
    def _load_dict(data: str | None) -> dict[str, Any]:
        """
        Parse a JSON string into a dict, returning {} on error/empty.

        Valid JSON that is not a mapping (a list, a number, ...) also yields
        ``{}``: the ``extra`` column is contracted to hold objects.

        :param data: Raw JSON text, possibly ``NULL`` in a legacy row.
        """
        try:
            result = json.loads(data if data is not None else "")
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    def __enter__(self) -> Self:
        """
        Enter context manager, automatically connecting to the database.
        """
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """
        Exit context manager, closing the database connection.
        """
        self.close()

    def __repr__(self) -> str:
        return f"<ChapterStorage path='{self._db_path}'>"


def _is_corruption_error(exc: sqlite3.DatabaseError) -> bool:
    """
    Return True when SQLite reports a damaged/foreign file.

    Transient failures (``database is locked``, ``disk I/O error``,
    ``unable to open database file``) must propagate as-is: mislabelling them
    as corruption would copy a healthy database aside and produce a
    misleading diagnosis.

    :param exc: Error raised while opening/checking the file.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)
