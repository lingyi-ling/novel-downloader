"""Hardening tests for the SQLite chapter store.

These tests pin down the guarantees the download / processing / export
pipelines rely on:

* an old database is migrated in place, idempotently, without losing rows,
* a corrupted database is backed up and reported, never silently replaced,
* a write either lands completely or not at all (including its flag cache),
* concurrent connections/processes cannot corrupt the file or lose chapters,
* reads keep the caller's chapter order and scale past SQLite's variable limit.
"""

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from novel_downloader.infra.persistence import chapter_storage
from novel_downloader.infra.persistence.chapter_storage import (
    SCHEMA_VERSION,
    ChapterStorage,
    ChapterStorageCorruptError,
    ChapterStorageSchemaError,
)
from novel_downloader.schemas import ChapterDict

_OLD_SCHEMA = """
CREATE TABLE chapters (
  id    TEXT NOT NULL PRIMARY KEY,
  title TEXT NOT NULL,
  content TEXT NOT NULL
);
"""


def _chapter(cid: str, *, title: str | None = None, content: str | None = None):
    return ChapterDict(
        id=cid,
        title=title if title is not None else f"Title {cid}",
        content=content if content is not None else f"Content {cid}",
        extra={"cid": cid},
    )


def _open(path: Path | str) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _columns(path: Path) -> set[str]:
    conn = _open(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
    finally:
        conn.close()


def _table_exists(path: Path) -> bool:
    conn = _open(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chapters'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _make_legacy(
    path: Path, *, with_flag: bool = False, flag_value: object = 0
) -> None:
    conn = _open(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        if with_flag:
            conn.execute(
                """
                CREATE TABLE chapters (
                  id           TEXT NOT NULL PRIMARY KEY,
                  title        TEXT NOT NULL,
                  content      TEXT NOT NULL,
                  need_refetch BOOLEAN,
                  extra        TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO chapters (id, title, content, need_refetch, extra)"
                " VALUES ('legacy', 'Old title', 'Old content', ?, '{\"v\": 1}')",
                (flag_value,),
            )
        else:
            conn.executescript(_OLD_SCHEMA)
            conn.execute(
                "INSERT INTO chapters (id, title, content)"
                " VALUES ('legacy', 'Old title', 'Old content')"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# schema creation and migration
# ---------------------------------------------------------------------------


def test_new_database_records_schema_version(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "new.db") as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(chapters)")}
    assert {"id", "title", "content", "need_refetch", "extra"} <= columns


def test_empty_zero_byte_file_is_initialized(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    db.write_bytes(b"")

    with ChapterStorage(tmp_path, "empty.db") as store:
        store.upsert_chapter(_chapter("c1"))
        assert store.get_chapter("c1") is not None

    assert db.stat().st_size > 0


def test_legacy_database_missing_columns_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _make_legacy(db)

    with ChapterStorage(tmp_path, "legacy.db") as store:
        assert store.exists("legacy") is True
        # A pre-flag row was a completed chapter; it stays clean.
        assert store.need_refetch("legacy") is False
        row = store.get_chapter("legacy")
        assert row is not None
        assert row["title"] == "Old title"
        assert row["content"] == "Old content"
        store.upsert_chapter(_chapter("c2"))
        assert store.get_chapter("c2") is not None

    assert {"need_refetch", "extra"} <= _columns(db)
    conn = _open(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    _make_legacy(db)

    for _ in range(3):
        with ChapterStorage(tmp_path, "idem.db") as store:
            assert store.get_chapter("legacy") is not None

    conn = _open(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        count = conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_extra_legacy_columns_are_preserved(tmp_path: Path) -> None:
    db = tmp_path / "extra.db"
    conn = _open(db)
    try:
        conn.execute(
            """
            CREATE TABLE chapters (
              id           TEXT NOT NULL PRIMARY KEY,
              title        TEXT NOT NULL,
              content      TEXT NOT NULL,
              need_refetch BOOLEAN NOT NULL DEFAULT 0,
              extra        TEXT,
              legacy_note  TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO chapters (id, title, content, extra, legacy_note)"
            " VALUES ('c1', 't', 'c', '{}', 'keep me')"
        )
    finally:
        conn.close()

    with ChapterStorage(tmp_path, "extra.db") as store:
        store.upsert_chapter(_chapter("c1", title="new title"))
        note = store.conn.execute(
            "SELECT legacy_note FROM chapters WHERE id = 'c1'"
        ).fetchone()[0]

    assert note == "keep me"


def test_unmigratable_schema_is_rejected_without_partial_changes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "partial.db"
    conn = _open(db)
    try:
        # Missing title/content: guessing those would silently lose data.
        conn.execute("CREATE TABLE chapters (id TEXT NOT NULL PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO chapters (id, note) VALUES ('c1', 'keep')")
    finally:
        conn.close()

    with pytest.raises(ChapterStorageSchemaError):
        ChapterStorage(tmp_path, "partial.db").connect()

    # No optional column was added and the version was not bumped: the failed
    # migration rolled back as a unit.
    assert _columns(db) == {"id", "note"}
    conn = _open(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def test_transaction_rolls_back_schema_changes(tmp_path: Path) -> None:
    db = tmp_path / "rollback.db"
    _make_legacy(db)
    store = ChapterStorage(tmp_path, "rollback.db")
    conn = _open(db)
    try:
        with pytest.raises(RuntimeError), store._transaction(conn):
            conn.execute("ALTER TABLE chapters ADD COLUMN probe TEXT")
            raise RuntimeError("simulated failure after ALTER")
    finally:
        conn.close()

    assert "probe" not in _columns(db)


def test_newer_user_version_is_rejected_without_touching_the_file(
    tmp_path: Path,
) -> None:
    db = tmp_path / "future.db"
    with ChapterStorage(tmp_path, "future.db") as store:
        store.upsert_chapter(_chapter("c1"))

    conn = _open(db)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    finally:
        conn.close()

    before = _sha256(db)
    with pytest.raises(ChapterStorageSchemaError) as excinfo:
        ChapterStorage(tmp_path, "future.db").connect()

    assert str(SCHEMA_VERSION + 1) in str(excinfo.value)
    assert _sha256(db) == before


def test_versioned_database_without_table_is_not_silently_recreated(
    tmp_path: Path,
) -> None:
    db = tmp_path / "versioned.db"
    conn = _open(db)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()

    with pytest.raises(ChapterStorageSchemaError):
        ChapterStorage(tmp_path, "versioned.db").connect()

    assert not _table_exists(db)


def test_concurrent_first_connect_migrates_once(tmp_path: Path) -> None:
    """Two processes opening a legacy DB at once must not race on ALTER TABLE."""
    db = tmp_path / "race.db"
    _make_legacy(db)

    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            with ChapterStorage(tmp_path, "race.db") as store:
                assert store.get_chapter("legacy") is not None
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert {"need_refetch", "extra"} <= _columns(db)
    assert _table_exists(db)


# ---------------------------------------------------------------------------
# corruption detection and recovery
# ---------------------------------------------------------------------------


def test_non_database_file_is_backed_up_and_reported(tmp_path: Path) -> None:
    db = tmp_path / "broken.db"
    original = b"this is definitely not an sqlite database\n" * 64
    db.write_bytes(original)

    with pytest.raises(ChapterStorageCorruptError) as excinfo:
        ChapterStorage(tmp_path, "broken.db").connect()

    message = str(excinfo.value)
    assert "broken.db" in message
    assert "backup" in message.lower()
    assert isinstance(excinfo.value, sqlite3.DatabaseError)

    # The original is left in place, byte for byte, for manual recovery.
    assert db.read_bytes() == original
    backups = list(tmp_path.glob("broken.db.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original

    # A second open must fail again instead of creating a fresh empty table.
    with pytest.raises(ChapterStorageCorruptError):
        ChapterStorage(tmp_path, "broken.db").connect()
    assert db.read_bytes() == original


def test_corrupt_btree_page_is_detected_by_integrity_check(tmp_path: Path) -> None:
    db = tmp_path / "pages.db"
    conn = _open(db)
    try:
        conn.execute("PRAGMA page_size = 512")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            """
            CREATE TABLE chapters (
              id           TEXT NOT NULL PRIMARY KEY,
              title        TEXT NOT NULL,
              content      TEXT NOT NULL,
              need_refetch BOOLEAN NOT NULL DEFAULT 0,
              extra        TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO chapters VALUES (?, ?, ?, 0, '{}')",
            [(f"c{i}", "t" * 100, "x" * 300) for i in range(200)],
        )
    finally:
        conn.close()

    raw = bytearray(db.read_bytes())
    assert len(raw) > 1024
    raw[512:1024] = b"\xff" * 512
    db.write_bytes(bytes(raw))
    damaged = db.read_bytes()

    with pytest.raises(ChapterStorageCorruptError) as excinfo:
        ChapterStorage(tmp_path, "pages.db").connect()

    assert "pages.db" in str(excinfo.value)
    assert db.read_bytes() == damaged
    assert list(tmp_path.glob("pages.db.corrupt-*"))


def test_corruption_backup_failure_still_reports_diagnosable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "broken.db"
    original = b"not a database"
    db.write_bytes(original)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(chapter_storage.shutil, "copy2", _boom)

    with pytest.raises(ChapterStorageCorruptError) as excinfo:
        ChapterStorage(tmp_path, "broken.db").connect()

    message = str(excinfo.value)
    assert "backup" in message.lower()
    assert db.read_bytes() == original


def test_unopenable_path_propagates_and_does_not_poison_the_instance(
    tmp_path: Path,
) -> None:
    # A directory can never be opened as a database file.
    (tmp_path / "blocked.db").mkdir()
    store = ChapterStorage(tmp_path, "blocked.db")

    with pytest.raises(sqlite3.OperationalError):
        store.connect()
    assert store._conn is None

    # A later, correct path on the same object still works (no stale handle).
    store._db_path = tmp_path / "ok.db"  # type: ignore[attr-defined]
    store.connect()
    store.upsert_chapter(_chapter("c1"))
    assert store.get_chapter("c1") is not None
    store.close()


def test_missing_parent_directory_is_reported_not_created(tmp_path: Path) -> None:
    store = ChapterStorage(tmp_path / "does-not-exist", "book.db")
    with pytest.raises(sqlite3.OperationalError):
        store.connect()
    assert store._conn is None


def test_read_only_database_failure_is_clean_and_diagnosable(
    tmp_path: Path,
) -> None:
    """A write-protected file must never be modified or half-updated.

    Depending on the journal mode the file is already in, opening a read-only
    database can succeed (SQLite supports read-only WAL readers).  Either way
    writes must raise, leave the bytes untouched and keep the flag cache empty.
    """
    db = tmp_path / "readonly.db"
    with ChapterStorage(tmp_path, "readonly.db") as store:
        store.upsert_chapter(_chapter("c1"))

    os.chmod(db, stat.S_IREAD)
    try:
        if os.access(db, os.W_OK):  # e.g. root, or a filesystem without modes
            pytest.skip("filesystem does not enforce read-only files")
        before = _sha256(db)
        store = ChapterStorage(tmp_path, "readonly.db")
        try:
            store.connect()
        except sqlite3.OperationalError:
            assert store._conn is None
        else:
            assert store.get_chapter("c1") is not None
            with pytest.raises(sqlite3.OperationalError):
                store.upsert_chapter(_chapter("c2"))
            assert store.existing_ids() == {"c1"}
            store.close()
        assert _sha256(db) == before
    finally:
        os.chmod(db, stat.S_IWRITE)


# ---------------------------------------------------------------------------
# write semantics
# ---------------------------------------------------------------------------


def test_duplicate_ids_in_one_batch_last_write_wins(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "dup.db") as store:
        store.upsert_chapters(
            [
                _chapter("dup", content="first"),
                _chapter("dup", content="second"),
            ]
        )
        got = store.get_chapter("dup")
        count = store.conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE id = 'dup'"
        ).fetchone()[0]

    assert got is not None
    assert got["content"] == "second"
    assert count == 1


def test_none_content_is_rejected_before_any_write(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "none.db") as store:
        good = _chapter("c1")
        bad = ChapterDict(
            id="c2",
            title="t",
            content=None,  # type: ignore[typeddict-item]
            extra={},
        )
        with pytest.raises(TypeError):
            store.upsert_chapters([good, bad])

        assert store.existing_ids() == set()
        assert store.get_chapter("c1") is None
        assert store.get_chapter("c2") is None


def test_none_title_and_empty_id_are_rejected(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "bad.db") as store:
        with pytest.raises(TypeError):
            store.upsert_chapter(
                ChapterDict(
                    id="c1",
                    title=None,  # type: ignore[typeddict-item]
                    content="c",
                    extra={},
                )
            )
        with pytest.raises(ValueError):
            store.upsert_chapter(_chapter(""))
        assert store.existing_ids() == set()


def test_empty_content_round_trips(tmp_path: Path) -> None:
    """An empty body is a real value for media-only / processed chapters."""
    with ChapterStorage(tmp_path, "empty-content.db") as store:
        store.upsert_chapter(_chapter("c1", content=""))
        got = store.get_chapter("c1")
        assert got is not None
        assert got["content"] == ""
        assert store.need_refetch("c1") is False


def test_overlong_title_is_stored_without_truncation(tmp_path: Path) -> None:
    title = "长" * 100_000
    with ChapterStorage(tmp_path, "long.db") as store:
        store.upsert_chapter(_chapter("c1", title=title))
        got = store.get_chapter("c1")
    assert got is not None
    assert got["title"] == title
    assert len(got["title"]) == 100_000


def test_commit_failure_rolls_back_rows_and_flags(tmp_path: Path) -> None:
    class CommitFails:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args: object) -> object:
            if sql.strip().upper().startswith("COMMIT"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    with ChapterStorage(tmp_path, "commit.db") as store:
        real = store._conn
        assert real is not None
        store._conn = CommitFails(real)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.upsert_chapter(_chapter("c1"))
            with pytest.raises(sqlite3.OperationalError):
                store.upsert_chapters([_chapter("c2"), _chapter("c3")])
        finally:
            store._conn = real

        assert store.existing_ids() == set()
        assert store.get_chapter("c1") is None
        assert store.get_chapter("c2") is None
        assert store.get_chapter("c3") is None


def test_delete_failure_keeps_row_and_cache(tmp_path: Path) -> None:
    class DeleteFails:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args: object) -> object:
            if sql.strip().upper().startswith("DELETE"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    with ChapterStorage(tmp_path, "delete.db") as store:
        store.upsert_chapters([_chapter("c1"), _chapter("c2")])
        real = store._conn
        assert real is not None
        store._conn = DeleteFails(real)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.delete_chapter("c1")
            with pytest.raises(sqlite3.OperationalError):
                store.delete_chapters(["c1", "c2"])
        finally:
            store._conn = real

        assert store.get_chapter("c1") is not None
        assert store.existing_ids() == {"c1", "c2"}

        assert store.delete_chapter("missing") is False
        assert store.delete_chapters(["c1", "c2"]) == 2
        assert store.existing_ids() == set()


def test_vacuum_keeps_data_readable(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "vacuum.db") as store:
        store.upsert_chapters([_chapter(f"c{i}") for i in range(20)])
        store.delete_chapters([f"c{i}" for i in range(10)])
        store.vacuum()
        assert store.existing_ids() == {f"c{i}" for i in range(10, 20)}
        assert store.get_chapter("c15") is not None


# ---------------------------------------------------------------------------
# concurrency and journal settings
# ---------------------------------------------------------------------------


def test_wal_and_busy_timeout_are_enabled(tmp_path: Path) -> None:
    with ChapterStorage(tmp_path, "pragmas.db") as store:
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert timeout >= 1000


def test_second_writer_gets_locked_error_and_recovers(tmp_path: Path) -> None:
    first = ChapterStorage(tmp_path, "lock.db")
    second = ChapterStorage(tmp_path, "lock.db")
    first.connect()
    second.connect()
    try:
        first.upsert_chapter(_chapter("c1"))
        second.conn.execute("PRAGMA busy_timeout = 50")
        first.conn.execute("BEGIN IMMEDIATE")
        first.conn.execute(
            "UPDATE chapters SET content = 'uncommitted' WHERE id = 'c1'"
        )

        with pytest.raises(sqlite3.OperationalError):
            second.upsert_chapter(_chapter("c2"))
        assert second.existing_ids() == set()

        first.conn.execute("ROLLBACK")
        second.upsert_chapter(_chapter("c2"))
        assert second.get_chapter("c2") is not None
        # The rolled back update never became visible.
        assert first.get_chapter("c1")["content"] == "Content c1"  # type: ignore[index]
    finally:
        first.close()
        second.close()


def test_reader_sees_committed_snapshot_while_writer_is_active(
    tmp_path: Path,
) -> None:
    writer = ChapterStorage(tmp_path, "snapshot.db")
    writer.connect()
    reader = ChapterStorage(tmp_path, "snapshot.db")
    reader.connect()
    try:
        writer.upsert_chapter(_chapter("c1", content="old"))
        writer.conn.execute("BEGIN IMMEDIATE")
        writer.conn.execute("UPDATE chapters SET content = 'new' WHERE id = 'c1'")

        old = reader.get_chapter("c1")
        assert old is not None
        assert old["content"] == "old"

        writer.conn.execute("COMMIT")
        new = reader.get_chapter("c1")
        assert new is not None
        assert new["content"] == "new"
    finally:
        writer.close()
        reader.close()


def test_two_connections_writing_in_parallel_lose_nothing(tmp_path: Path) -> None:
    count = 30
    errors: list[BaseException] = []

    def worker(prefix: str) -> None:
        try:
            with ChapterStorage(tmp_path, "parallel.db") as store:
                for i in range(count):
                    store.upsert_chapter(_chapter(f"{prefix}{i}"))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("a",)),
        threading.Thread(target=worker, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    with ChapterStorage(tmp_path, "parallel.db") as store:
        expected = {f"a{i}" for i in range(count)} | {f"b{i}" for i in range(count)}
        assert store.existing_ids() == expected
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------------------------------
# reads / exports
# ---------------------------------------------------------------------------


def test_get_chapters_preserves_request_order_and_reports_gaps(
    tmp_path: Path,
) -> None:
    with ChapterStorage(tmp_path, "order.db") as store:
        store.upsert_chapters([_chapter("c1"), _chapter("c2"), _chapter("c3")])
        result = store.get_chapters(["c3", "missing", "c1", "c3"])

    assert list(result) == ["c3", "missing", "c1"]
    assert result["c3"] is not None
    assert result["missing"] is None
    assert result["c1"] is not None


def test_get_chapters_handles_more_ids_than_sqlite_variables(tmp_path: Path) -> None:
    ids = [f"c{i:04d}" for i in range(1200)]
    with ChapterStorage(tmp_path, "many.db") as store:
        store.upsert_chapters([_chapter(cid) for cid in ids])
        result = store.get_chapters(ids)
        assert len(result) == len(ids)
        assert all(result[cid] is not None for cid in ids)

        assert store.delete_chapters(ids) == len(ids)
        assert store.existing_ids() == set()


def test_need_refetch_nullable_legacy_values_are_conservative(
    tmp_path: Path,
) -> None:
    db = tmp_path / "nullflag.db"
    _make_legacy(db, with_flag=True, flag_value=None)

    with ChapterStorage(tmp_path, "nullflag.db") as store:
        assert store.exists("legacy") is True
        # Unknown is not the same as clean: force a refetch.
        assert store.need_refetch("legacy") is True
        assert store.dirty_ids() == {"legacy"}
        assert store.clean_ids() == set()


def test_text_bool_legacy_values_are_coerced(tmp_path: Path) -> None:
    db = tmp_path / "textflag.db"
    _make_legacy(db, with_flag=True, flag_value="0")

    with ChapterStorage(tmp_path, "textflag.db") as store:
        # bool("0") is True -- that bug would keep an unprocessed chapter clean.
        assert store.need_refetch("legacy") is False
        assert store.clean_ids() == {"legacy"}


def test_extra_json_round_trips_and_broken_extra_is_empty(
    tmp_path: Path,
) -> None:
    with ChapterStorage(tmp_path, "extra.db") as store:
        store.upsert_chapter(_chapter("c1"))
        store.conn.execute("UPDATE chapters SET extra = 'not json' WHERE id = 'c1'")
        got = store.get_chapter("c1")
        nulled = store.conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE extra IS NULL"
        ).fetchone()[0]

    assert got is not None
    assert got["extra"] == {}
    assert nulled == 0


def test_new_storage_after_delete_and_recreate(tmp_path: Path) -> None:
    """A deleted book file is a fresh store, not a half-migrated one."""
    db = tmp_path / "book.db"
    with ChapterStorage(tmp_path, "book.db") as store:
        store.upsert_chapter(_chapter("old"))

    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"book.db{suffix}").unlink(missing_ok=True)

    with ChapterStorage(tmp_path, "book.db") as store:
        assert store.existing_ids() == set()
        store.upsert_chapter(_chapter("new"))
        assert store.existing_ids() == {"new"}
    assert db.stat().st_size > 0
