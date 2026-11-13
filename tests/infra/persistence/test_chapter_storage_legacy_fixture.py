"""Migration tests against a real pre-schema-version chapter database.

The fixture is written the way an older release left files on disk: a
``chapters`` table with only ``id``/``title``/``content``, ``user_version``
still 0, WAL mode with committed frames that were never checkpointed, and a
table without a unique constraint (which such a file may legally use to hold
duplicate chapter ids).  One row has an empty body, and ``ch0003`` is a real
gap rather than an empty row.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from novel_downloader.infra.persistence.chapter_storage import (
    SCHEMA_VERSION,
    ChapterStorage,
    ChapterStorageCorruptError,
)
from novel_downloader.schemas import ChapterDict

#: Present only in the WAL, so a checkpoint/discard is observable.
_WAL_SENTINEL = "WAL-SENTINEL-7f3a"

_LEGACY_COLUMNS = "id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL"

_LEGACY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ch0001", "Chapter 1", "Body one"),
    ("ch0002", "Chapter 2", "Body two"),
    ("ch0002", "Chapter 2 (stale copy)", ""),
    ("ch0004", "Chapter 4", f"Body four {_WAL_SENTINEL}"),
)

_TEXT_FLAG_COLUMNS = (
    "id TEXT NOT NULL PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, "
    "need_refetch TEXT, extra TEXT"
)

_TEXT_FLAG_ROWS: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("clean", "Clean", "body", "0", '{"i": 1}'),
    ("dirty", "Dirty", "body", "1", None),
    ("null", "Null", "body", None, None),
    ("false", "False", "body", "false", None),
)


def _build_legacy_db(
    path: Path,
    *,
    rows: Sequence[Sequence[object]] = _LEGACY_ROWS,
    columns: str = _LEGACY_COLUMNS,
    wal: bool = True,
) -> None:
    """Write a real legacy file, leaving committed rows uncheckpointed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)

    staging_dir = Path(tempfile.mkdtemp(prefix="legacy-db-", dir=path.parent))
    staging = staging_dir / path.name
    conn = sqlite3.connect(str(staging), isolation_level=None)
    try:
        conn.execute(f"PRAGMA journal_mode = {'WAL' if wal else 'DELETE'}")
        if wal:
            # Keep every committed frame in the sidecar, like a file copied
            # while the application still had it open.
            conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("PRAGMA user_version = 0")
        conn.execute(f"CREATE TABLE chapters ({columns})")
        placeholders = ", ".join("?" for _ in rows[0])
        conn.executemany(
            f"INSERT INTO chapters VALUES ({placeholders})",
            [tuple(row) for row in rows],
        )
        shutil.copy2(staging, path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{staging}{suffix}")
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(f"{path}{suffix}"))
    finally:
        conn.close()
        shutil.rmtree(staging_dir, ignore_errors=True)


def _chapter(chap_id: str, *, content: str) -> ChapterDict:
    return ChapterDict(
        id=chap_id,
        title=f"Title {chap_id}",
        content=content,
        extra={},
    )


def _columns(path: Path) -> list[str]:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return [str(row[1]) for row in conn.execute("PRAGMA table_info(chapters)")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the fixture itself
# ---------------------------------------------------------------------------


def test_fixture_is_a_real_legacy_wal_database(tmp_path: Path) -> None:
    db = tmp_path / "chapter.raw.sqlite"
    _build_legacy_db(db)

    wal = Path(f"{db}-wal")
    assert wal.is_file()
    assert wal.stat().st_size > 32
    # The sentinel row was never checkpointed: it must not be in the main file.
    assert _WAL_SENTINEL.encode() not in db.read_bytes()
    assert _WAL_SENTINEL.encode() in wal.read_bytes()
    assert _columns(db) == ["id", "title", "content"]

    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        rows = conn.execute("SELECT id, title, content FROM chapters").fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    assert [row[0] for row in rows].count("ch0002") == 2
    assert any(row[2] == "" for row in rows)


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_legacy_migration_is_idempotent_and_keeps_every_row(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    _build_legacy_db(db)

    for _ in range(3):
        with ChapterStorage(tmp_path, db.name) as store:
            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
            assert _columns(db) == [
                "id",
                "title",
                "content",
                "need_refetch",
                "extra",
            ]
            # Duplicate ids are real rows; migration must not deduplicate them.
            count = store.conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
            assert count == 4
            assert store.existing_ids() == {"ch0001", "ch0002", "ch0004"}
            # A pre-flag row was a completed chapter, so it stays clean.
            assert store.clean_ids() == {"ch0001", "ch0002", "ch0004"}
            assert store.dirty_ids() == set()

            first = store.get_chapter("ch0001")
            assert first is not None
            assert first["content"] == "Body one"

            # Data that only existed in the WAL survives migration and reopen.
            wal_row = store.get_chapter("ch0004")
            assert wal_row is not None
            assert _WAL_SENTINEL in wal_row["content"]

            stale = store.conn.execute(
                "SELECT content FROM chapters WHERE id = 'ch0002' ORDER BY content"
            ).fetchall()
            assert [row[0] for row in stale] == ["", "Body two"]

            flag, extra = store.conn.execute(
                "SELECT need_refetch, extra FROM chapters WHERE id = 'ch0001'"
            ).fetchone()
            assert int(flag) == 0
            assert extra is None


def test_legacy_migration_preserves_request_order_and_reports_gaps(
    tmp_path: Path,
) -> None:
    db = tmp_path / "order.sqlite"
    _build_legacy_db(db)

    with ChapterStorage(tmp_path, db.name) as store:
        result = store.get_chapters(["ch0004", "ch0003", "ch0001", "ch0004", "ch0002"])

        assert list(result) == ["ch0004", "ch0003", "ch0001", "ch0002"]
        assert result["ch0003"] is None
        found = result["ch0004"]
        assert found is not None
        assert _WAL_SENTINEL in found["content"]
        duplicate = result["ch0002"]
        assert duplicate is not None
        assert duplicate["content"] in {"Body two", ""}
        assert "ch0003" not in store.existing_ids()


def test_legacy_text_zero_flag_is_clean_and_nulls_are_conservative(
    tmp_path: Path,
) -> None:
    db = tmp_path / "text-flags.sqlite"
    _build_legacy_db(
        db,
        columns=_TEXT_FLAG_COLUMNS,
        rows=_TEXT_FLAG_ROWS,
        wal=False,
    )

    with ChapterStorage(tmp_path, db.name) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # ``bool("0")`` is True: the old TEXT flag must be parsed, not cast.
        assert store.clean_ids() == {"clean", "false"}
        assert store.dirty_ids() == {"dirty", "null"}

        raw = dict(store.conn.execute("SELECT id, need_refetch FROM chapters"))
        assert raw["clean"] == "0"
        assert raw["dirty"] == "1"

        clean = store.get_chapter("clean")
        assert clean is not None
        assert clean["extra"] == {"i": 1}
        dirty = store.get_chapter("dirty")
        assert dirty is not None
        assert dirty["extra"] == {}


def test_legacy_duplicate_rows_become_writable_after_migration(
    tmp_path: Path,
) -> None:
    db = tmp_path / "writable.sqlite"
    _build_legacy_db(db)

    with ChapterStorage(tmp_path, db.name) as store:
        # A plain insert works even though the table has no unique id.
        store.upsert_chapter(_chapter("ch0003", content="refetched gap"))
        # An upsert of a duplicated id collapses the stale copies.
        store.upsert_chapter(_chapter("ch0002", content="Body two again"))
        store.upsert_chapters([_chapter("ch0005", content="five")])

        assert (
            store.conn.execute(
                "SELECT COUNT(*) FROM chapters WHERE id = 'ch0002'"
            ).fetchone()[0]
            == 1
        )
        updated = store.get_chapter("ch0002")
        assert updated is not None
        assert updated["content"] == "Body two again"
        assert store.get_chapter("ch0003") is not None
        assert store.existing_ids() == {
            "ch0001",
            "ch0002",
            "ch0003",
            "ch0004",
            "ch0005",
        }
        assert store.need_refetch("ch0002") is False


# ---------------------------------------------------------------------------
# corruption
# ---------------------------------------------------------------------------


def test_corrupt_legacy_file_is_copied_aside_and_never_replaced(
    tmp_path: Path,
) -> None:
    db = tmp_path / "corrupt.sqlite"
    _build_legacy_db(db, wal=False)

    raw = bytearray(db.read_bytes())
    assert len(raw) > 100
    # Damage the file header: SQLite refuses to read it at all.
    raw[0:16] = b"NOT-A-SQLITE-DB!"
    db.write_bytes(bytes(raw))
    damaged = db.read_bytes()

    with pytest.raises(ChapterStorageCorruptError) as excinfo:
        ChapterStorage(tmp_path, db.name).connect()

    assert "corrupt" in str(excinfo.value).lower()
    assert db.read_bytes() == damaged
    copies = list(tmp_path.glob("corrupt.sqlite.corrupt-*"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == damaged

    # A second attempt still refuses instead of creating a fresh database.
    with pytest.raises(ChapterStorageCorruptError):
        ChapterStorage(tmp_path, db.name).connect()
    assert db.read_bytes() == damaged


def test_torn_wal_tail_is_crash_recovery_not_corruption(tmp_path: Path) -> None:
    """A crash can leave a partial last frame; that must still be openable."""
    db = tmp_path / "torn.sqlite"
    _build_legacy_db(db)
    wal = Path(f"{db}-wal")
    data = wal.read_bytes()
    assert len(data) > 132
    # Cut inside the first frame, exactly like an interrupted append.
    wal.write_bytes(data[: 32 + 100])

    with ChapterStorage(tmp_path, db.name) as store:
        # SQLite discards the incomplete tail; the store is usable and empty
        # (the schema lived only in the abandoned WAL) instead of rejected.
        assert store.existing_ids() == set()
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    assert not list(tmp_path.glob("torn.sqlite.corrupt-*"))


def test_corrupt_legacy_wal_is_detected_before_sqlite_discards_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "wal-damage.sqlite"
    _build_legacy_db(db)
    wal = Path(f"{db}-wal")
    data = bytearray(wal.read_bytes())
    assert len(data) > 32 + 24 + 300
    # Break a committed frame's payload while leaving the later frames intact.
    data[32 + 24 + 100 : 32 + 24 + 300] = b"\xff" * 200
    wal.write_bytes(bytes(data))
    damaged_wal = wal.read_bytes()
    main_before = db.read_bytes()

    with pytest.raises(ChapterStorageCorruptError) as excinfo:
        ChapterStorage(tmp_path, db.name).connect()

    message = str(excinfo.value).lower()
    assert "wal" in message
    assert "checksum" in message
    # Nothing was reset or checkpointed: both original files are untouched.
    assert wal.read_bytes() == damaged_wal
    assert db.read_bytes() == main_before

    copies = sorted(tmp_path.glob("wal-damage.sqlite.corrupt-*-wal"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == damaged_wal
    main_copies = [
        path
        for path in tmp_path.glob("wal-damage.sqlite.corrupt-*")
        if not path.name.endswith(("-wal", "-shm"))
    ]
    assert len(main_copies) == 1
    assert main_copies[0].read_bytes() == main_before
