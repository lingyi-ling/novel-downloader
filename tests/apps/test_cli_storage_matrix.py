#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_matrix
----------------------------------

Table-driven boundary matrix for ``storage backup`` / ``storage verify``.

The individual test modules cover the happy paths and the known failure modes
one scenario at a time.  This module drives the same commands over a grid of
database shapes and option combinations so the *contract* -- exit code, JSON
line count, rollback semantics and source byte invariance -- is checked
uniformly.  Every database lives under a temporary ``raw_data`` root wired into
``storage._resolve_paths``; no network access happens.

Two invariants are asserted for every shape:

* the source database (main file plus any pre-existing ``-wal``) is
  byte-identical afterwards, for every command, successful or not;
* a failed ``storage backup`` leaves no file from that run behind, so a retry
  can never mistake a partial set for a complete one.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.infra.persistence.chapter_storage import (
    SCHEMA_VERSION,
    ChapterStorage,
)

#: A stable timestamp so a test can predict the backup file name.
_STAMP = "20260102T030405Z"

#: Marker only present in the WAL of the legacy fixture.
_WAL_SENTINEL = "WAL-SENTINEL-7f3a"


# ---------------------------------------------------------------------------
# invocation helpers
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _invoke(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _output_line(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    assert captured.out.endswith("\n")
    return captured.out


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(_output_line(capsys))


@pytest.fixture()
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary raw-data root wired into ``storage._resolve_paths``."""
    root = tmp_path / "raw"
    root.mkdir()
    paths = {
        "raw": root,
        "cache": tmp_path / "cache",
        "downloads": tmp_path / "downloads",
        "logs": tmp_path / "logs",
    }
    monkeypatch.setattr(storage, "_resolve_paths", lambda **_kwargs: paths)
    yield root


def _book_db(root: Path, site: str = "site", book: str = "book") -> Path:
    return root / site / book / "chapter.raw.sqlite"


# ---------------------------------------------------------------------------
# database shape builders
# ---------------------------------------------------------------------------


def _seed(path: Path, *chapter_ids: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(path.parent, path.name) as store:
        store.upsert_chapters(
            [
                {
                    "id": chap_id,
                    "title": f"Title {chap_id}",
                    "content": f"Body {chap_id}",
                    "extra": {},
                }
                for chap_id in chapter_ids
            ]
        )


def _write_sqlite(
    path: Path,
    statements: list[str],
    *,
    version: int = 0,
    journal: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if journal is not None:
            conn.execute(f"PRAGMA journal_mode = {journal}")
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _legacy_wal(path: Path) -> None:
    """A pre-schema version file whose schema and rows stay in the WAL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="legacy-matrix-", dir=path.parent) as tmp:
        staging = Path(tmp) / path.name
        conn = sqlite3.connect(str(staging), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.execute("PRAGMA user_version = 0")
            conn.execute(
                "CREATE TABLE chapters ("
                "id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO chapters VALUES (?, ?, ?)",
                [
                    ("ch0001", "Chapter 1", "Body one"),
                    ("ch0002", "Chapter 2", "Body two"),
                    ("ch0003", "Chapter 3", "Body three"),
                    ("ch0004", "Chapter 4", f"Body four {_WAL_SENTINEL}"),
                ],
            )
            import shutil

            shutil.copy2(staging, path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{staging}{suffix}")
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(f"{path}{suffix}"))
        finally:
            conn.close()


def _damage_wal(path: Path) -> None:
    """Flip bytes inside the second WAL frame, keeping the chain plausible."""
    _legacy_wal(path)
    wal = Path(f"{path}-wal")
    data = bytearray(wal.read_bytes())
    assert len(data) > 32 + 24 + 300
    data[32 + 24 + 100 : 32 + 24 + 300] = b"\xff" * 200
    wal.write_bytes(bytes(data))


def _torn_wal_tail(path: Path) -> None:
    """A crash-style torn tail is safe: SQLite discards it, no corruption."""
    _legacy_wal(path)
    wal = Path(f"{path}-wal")
    data = wal.read_bytes()
    assert len(data) > 100
    wal.write_bytes(data[:-100])


def _empty_wal_sidecar(path: Path) -> None:
    _seed(path, "c1")
    Path(f"{path}-wal").write_bytes(b"")


def _zero_byte(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _empty_schema(path: Path) -> None:
    _write_sqlite(
        path,
        ["CREATE TABLE scratch (x INTEGER)", "DROP TABLE scratch"],
        journal="DELETE",
    )


def _missing_table(path: Path, version: int) -> None:
    _write_sqlite(
        path,
        ["CREATE TABLE unrelated (id INTEGER)"],
        version=version,
        journal="DELETE",
    )


def _missing_column(path: Path) -> None:
    _write_sqlite(
        path,
        ["CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT)"],
        version=SCHEMA_VERSION,
        journal="DELETE",
    )


def _future_table(path: Path) -> None:
    _write_sqlite(
        path,
        ["CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"],
        version=SCHEMA_VERSION + 1,
        journal="DELETE",
    )


def _chapters_view(path: Path) -> None:
    _write_sqlite(
        path,
        [
            "CREATE TABLE src (id TEXT, title TEXT, content TEXT)",
            "CREATE VIEW chapters AS SELECT id, title, content FROM src",
        ],
        version=1,
        journal="DELETE",
    )


def _negative_version(path: Path) -> None:
    _write_sqlite(
        path,
        ["CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"],
        version=-1,
        journal="DELETE",
    )


def _not_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not an sqlite database\n" * 64)


def _corrupt_main(path: Path) -> None:
    """Valid header, damaged leaf page: SQLite opens it but integrity fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO chapters VALUES (?, ?, ?)",
            [(f"c{index}", "t" * 50, "x" * 5000) for index in range(5)],
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    data = bytearray(path.read_bytes())
    page_size = 4096
    leaves = [
        index
        for index in range(1, len(data) // page_size)
        if data[index * page_size] == 0x0D  # leaf table b-tree page
    ]
    assert leaves, "fixture must contain a leaf table page"
    start = leaves[0] * page_size + 100
    data[start : start + 300] = b"\xff" * 300
    path.write_bytes(bytes(data))


# ---------------------------------------------------------------------------
# shape matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShapeCase:
    name: str
    build: Callable[[Path], None]
    verify_ok: bool
    verify_reason: str | None
    backup_ok: bool
    backup_no_verify_ok: bool
    #: A damaged source is rejected while the snapshot is being built, before
    #: any per-file entry exists; that generic path reports on stderr only.
    backup_emits_json: bool = True


SHAPE_CASES: tuple[ShapeCase, ...] = (
    ShapeCase("healthy", lambda p: _seed(p, "c1", "c2"), True, None, True, True),
    ShapeCase("legacy_wal", _legacy_wal, True, None, True, True),
    ShapeCase("torn_wal_tail", _torn_wal_tail, True, None, True, True),
    ShapeCase("empty_wal_sidecar", _empty_wal_sidecar, True, None, True, True),
    ShapeCase("negative_user_version", _negative_version, True, None, True, True),
    ShapeCase("zero_byte", _zero_byte, False, "no 'chapters' table", False, True),
    ShapeCase("empty_schema", _empty_schema, False, "no 'chapters' table", False, True),
    ShapeCase(
        "missing_table_v1",
        lambda p: _missing_table(p, 1),
        False,
        "no 'chapters' table",
        False,
        True,
    ),
    ShapeCase(
        "missing_table_v0",
        lambda p: _missing_table(p, 0),
        False,
        "no 'chapters' table",
        False,
        True,
    ),
    ShapeCase(
        "missing_column",
        _missing_column,
        False,
        "missing required column",
        False,
        True,
    ),
    ShapeCase("future_schema", _future_table, False, "newer", False, True),
    ShapeCase(
        "future_schema_no_table",
        lambda p: _missing_table(p, SCHEMA_VERSION + 1),
        False,
        "newer",
        False,
        True,
    ),
    ShapeCase(
        "chapters_is_a_view",
        _chapters_view,
        False,
        "no 'chapters' table",
        False,
        True,
    ),
    ShapeCase(
        "not_a_database",
        _not_database,
        False,
        "not a database",
        False,
        False,
        backup_emits_json=False,
    ),
    ShapeCase(
        "corrupt_main",
        _corrupt_main,
        False,
        "integrity",
        False,
        False,
        backup_emits_json=False,
    ),
    ShapeCase(
        "damaged_wal",
        _damage_wal,
        False,
        "write-ahead log",
        False,
        False,
        backup_emits_json=False,
    ),
)


def _source_bytes(db: Path) -> dict[str, bytes]:
    """Bytes of the files a command must never rewrite (main + WAL)."""
    files = {str(db): db.read_bytes()}
    wal = Path(f"{db}-wal")
    if wal.is_file():
        files[str(wal)] = wal.read_bytes()
    return files


def _assert_source_unchanged(db: Path, before: dict[str, bytes]) -> None:
    """Every file that existed before the run still holds the same bytes.

    A read-only SQLite open of a WAL-mode database can create an empty
    ``-wal``/``-shm`` pair; that artifact is not part of the database's
    stored bytes and is intentionally not compared here.
    """
    for path_text, expected in before.items():
        path = Path(path_text)
        assert path.is_file(), path
        assert path.read_bytes() == expected, path


def _fresh_backups(out: Path) -> list[Path]:
    """Files written by a run whose clock is pinned to :data:`_STAMP`."""
    if not out.exists():
        return []
    return sorted(out.rglob(f"chapter.*.{_STAMP}*.sqlite"))


def _leaked_temp_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob(".*.tmp"))


@pytest.mark.parametrize("case", SHAPE_CASES, ids=[case.name for case in SHAPE_CASES])
def test_verify_shape_matrix(
    case: ShapeCase,
    raw_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``storage verify --json`` classifies every shape the same way."""
    db = _book_db(raw_root)
    case.build(db)
    before = _source_bytes(db)

    if case.verify_ok:
        _invoke(["storage", "verify", "--json"])
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.count("\n") == 1
        payload = json.loads(captured.out)
        assert payload == {
            "ok": True,
            "total": 1,
            "healthy": 1,
            "corrupt": 0,
            "errors": [],
        }
    else:
        with pytest.raises(SystemExit) as exc_info:
            _invoke(["storage", "verify", "--json"])
        assert exc_info.value.code == 1
        payload = _payload(capsys)
        assert payload["ok"] is False
        assert payload["total"] == 1
        assert payload["healthy"] == 0
        assert payload["corrupt"] == 1
        assert len(payload["errors"]) == 1
        reason = payload["errors"][0]["reason"]
        assert isinstance(reason, str)
        assert case.verify_reason is not None
        assert case.verify_reason.lower() in reason.lower()

    _assert_source_unchanged(db, before)
    assert not list(raw_root.rglob("*.corrupt-*"))
    assert not list(raw_root.rglob("*.pre-restore-*"))


@pytest.mark.parametrize("case", SHAPE_CASES, ids=[case.name for case in SHAPE_CASES])
def test_backup_default_shape_matrix(
    case: ShapeCase,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default backup verifies each copy and rolls the whole set back on failure."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    case.build(db)
    before = _source_bytes(db)

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    if case.backup_ok:
        _invoke(["storage", "backup", "--out", str(out), "--json"])
        payload = _payload(capsys)
        assert payload["ok"] is True
        assert payload["count"] == 1
        assert payload["verified"] is True
        assert payload["verification_failed"] is False
        assert len(_fresh_backups(out)) == 1
    else:
        with pytest.raises(SystemExit) as exc_info:
            _invoke(["storage", "backup", "--out", str(out), "--json"])
        assert exc_info.value.code == 1
        if case.backup_emits_json:
            payload = _payload(capsys)
            assert payload["ok"] is False
            assert payload["count"] == 0
            assert payload["files"] == []
            assert payload["verified"] is False
            assert payload["verification_failed"] is True
            assert payload["retention"] == {
                "keep": 0,
                "dry_run": False,
                "candidates": [],
                "removed": [],
                "failed": [],
            }
        else:
            # Generic snapshot failures are reported on stderr only; this is
            # the current contract and is asserted here so it cannot change
            # silently.
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err != ""
        assert _fresh_backups(out) == []

    # The pre-existing backup is never touched by a failed run, and the source
    # database plus its WAL are byte-identical.
    assert old.read_bytes() == old_bytes
    _assert_source_unchanged(db, before)
    assert _leaked_temp_files(out) == []
    assert not list(raw_root.rglob("*.corrupt-*"))


@pytest.mark.parametrize("case", SHAPE_CASES, ids=[case.name for case in SHAPE_CASES])
def test_backup_no_verify_shape_matrix(
    case: ShapeCase,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--no-verify`` preserves what this build cannot open, but not damage."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    case.build(db)
    before = _source_bytes(db)

    out = tmp_path / "backups"
    if case.backup_no_verify_ok:
        _invoke(["storage", "backup", "--out", str(out), "--no-verify", "--json"])
        payload = _payload(capsys)
        assert payload["ok"] is True
        assert payload["count"] == 1
        assert payload["verified"] is False
        assert payload["verification_failed"] is False
        assert payload["files"][0]["verified"] is False
        assert len(_fresh_backups(out)) == 1
    else:
        with pytest.raises(SystemExit) as exc_info:
            _invoke(["storage", "backup", "--out", str(out), "--no-verify", "--json"])
        assert exc_info.value.code == 1
        assert _fresh_backups(out) == []
        captured = capsys.readouterr()
        if case.backup_emits_json:
            assert captured.out.count("\n") == 1
            assert json.loads(captured.out)["ok"] is False
        else:
            assert captured.out == ""
            assert captured.err != ""

    _assert_source_unchanged(db, before)
    assert _leaked_temp_files(out) == []


# ---------------------------------------------------------------------------
# read-only source and sidecar handling
# ---------------------------------------------------------------------------


def test_backup_read_only_source_leaves_bytes_untouched(
    raw_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _seed(db, "c1", "c2")
    wal = Path(f"{db}-wal")
    wal_bytes = wal.read_bytes() if wal.is_file() else None
    before = _source_bytes(db)

    try:
        os.chmod(db, stat.S_IREAD)
        _invoke(["storage", "backup", "--out", str(tmp_path / "backups")])
        _invoke(["storage", "verify", "--json"])
    finally:
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)

    _assert_source_unchanged(db, before)
    if wal_bytes is not None:
        assert Path(f"{db}-wal").read_bytes() == wal_bytes


def test_verify_does_not_create_backup_or_restore_artifacts(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1")

    _invoke(["storage", "verify", "--json"])
    _payload(capsys)

    assert not list(raw_root.rglob("*.corrupt-*"))
    assert not list(raw_root.rglob("*.pre-restore-*"))
    assert not list(raw_root.rglob("*.tmp"))


# ---------------------------------------------------------------------------
# backup destination boundaries
# ---------------------------------------------------------------------------


def test_backup_out_pointing_at_a_file_is_a_usage_error(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1")
    before = _source_bytes(db)
    target = tmp_path / "not-a-directory"
    target.write_bytes(b"keep")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(target), "--json"])
    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""
    assert target.read_bytes() == b"keep"
    _assert_source_unchanged(db, before)


def test_backup_out_inside_raw_data_json(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = raw_root / "backups"
    _invoke(["storage", "backup", "--out", str(out), "--json"])
    assert _payload(capsys)["count"] == 1

    _invoke(["storage", "backup", "--out", str(out), "--json"])
    assert _payload(capsys)["count"] == 1
    assert len(_fresh_backups(out)) == 2


# ---------------------------------------------------------------------------
# same-second naming and target collisions
# ---------------------------------------------------------------------------


def _fake_backup(
    out: Path,
    stamp: str,
    *,
    book: str = "book",
    stage: str = "raw",
    seq: int | None = None,
) -> Path:
    suffix = "" if seq is None else f".{seq}"
    path = out / "site" / book / f"chapter.{stage}.{stamp}{suffix}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"older backup")
    return path


def test_backup_skips_a_directory_occupying_the_base_name(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    book_dir = out / "site" / "book"
    occupied = book_dir / f"chapter.raw.{_STAMP}.sqlite"
    occupied.mkdir(parents=True)

    _invoke(["storage", "backup", "--out", str(out), "--json"])
    payload = _payload(capsys)

    assert payload["files"][0]["target"] == str(
        occupied.with_name(f"chapter.raw.{_STAMP}.1.sqlite")
    )
    assert occupied.is_dir()
    assert not any(occupied.iterdir())


def test_backup_sequence_exhaustion_rolls_back_and_exits_one(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every accepted same-second name taken: fail without leaving the set."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    monkeypatch.setattr(storage, "_MAX_BACKUP_SEQUENCE", 1)
    db = _book_db(raw_root)
    _seed(db, "c1")
    before = _source_bytes(db)

    out = tmp_path / "backups"
    book_dir = out / "site" / "book"
    base = _fake_backup(out, _STAMP)
    first = _fake_backup(out, _STAMP, seq=1)
    base_bytes, first_bytes = base.read_bytes(), first.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--json"])
    assert exc_info.value.code == 1

    assert base.read_bytes() == base_bytes
    assert first.read_bytes() == first_bytes
    assert {path.name for path in book_dir.iterdir()} == {base.name, first.name}
    _assert_source_unchanged(db, before)
    # Generic I/O failures currently report on stderr only.
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# retention (--keep 0/2), dry-run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeepCase:
    name: str
    keep: int
    stamps: tuple[str, ...]
    expected_removed: tuple[str, ...]


KEEP_CASES: tuple[KeepCase, ...] = (
    KeepCase("keep_0_disables", 0, ("20250101T000000Z", "20250101T000001Z"), ()),
    KeepCase(
        "keep_1_keeps_fresh",
        1,
        ("20250101T000000Z", "20250101T000001Z"),
        ("20250101T000000Z", "20250101T000001Z"),
    ),
    KeepCase(
        "keep_2_evicts_oldest",
        2,
        ("20250101T000000Z", "20250101T000001Z"),
        ("20250101T000000Z",),
    ),
    KeepCase(
        "keep_2_with_three_old",
        2,
        ("20250101T000000Z", "20250101T000001Z", "20250101T000002Z"),
        ("20250101T000000Z", "20250101T000001Z"),
    ),
    KeepCase("keep_above_count", 5, ("20250101T000000Z", "20250101T000001Z"), ()),
)


@pytest.mark.parametrize("case", KEEP_CASES, ids=[case.name for case in KEEP_CASES])
def test_keep_boundary_matrix(
    case: KeepCase,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--keep N`` keeps the fresh copy plus the newest ``N-1`` old ones."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    olds = [_fake_backup(out, stamp) for stamp in case.stamps]
    expected = {
        str(out / "site" / "book" / f"chapter.raw.{stamp}.sqlite")
        for stamp in case.expected_removed
    }

    _invoke(
        ["storage", "backup", "--out", str(out), "--keep", str(case.keep), "--json"]
    )
    payload = _payload(capsys)

    assert payload["ok"] is True
    assert payload["retention"]["keep"] == case.keep
    assert payload["retention"]["dry_run"] is False
    assert set(payload["retention"]["removed"]) == expected
    assert payload["retention"]["failed"] == []

    fresh = out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    assert fresh.is_file()
    for path in olds:
        assert path.exists() is (str(path) not in expected)
    assert fresh.exists()


@pytest.mark.parametrize(
    "case",
    [case for case in KEEP_CASES if case.keep > 0],
    ids=[case.name for case in KEEP_CASES if case.keep > 0],
)
def test_keep_dry_run_matrix_reports_without_deleting(
    case: KeepCase,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    olds = [_fake_backup(out, stamp) for stamp in case.stamps]
    expected = {
        str(out / "site" / "book" / f"chapter.raw.{stamp}.sqlite")
        for stamp in case.expected_removed
    }

    _invoke(
        [
            "storage",
            "backup",
            "--out",
            str(out),
            "--keep",
            str(case.keep),
            "--dry-run",
            "--json",
        ]
    )
    payload = _payload(capsys)

    assert payload["ok"] is True
    assert payload["retention"]["dry_run"] is True
    assert payload["retention"]["removed"] == []
    assert set(payload["retention"]["candidates"]) == expected
    for path in olds:
        assert path.exists()
    assert (out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite").exists()


def test_keep_two_orders_same_second_copies_numerically(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``.10`` is newer than ``.9``, so ``--keep 2`` evicts ``.9``, not ``.10``."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    ninth = _fake_backup(out, "20250101T000000Z", seq=9)
    tenth = _fake_backup(out, "20250101T000000Z", seq=10)

    _invoke(["storage", "backup", "--out", str(out), "--keep", "2", "--json"])
    payload = _payload(capsys)

    assert payload["retention"]["removed"] == [str(ninth)]
    assert not ninth.exists()
    assert tenth.exists()
    assert (out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite").exists()


# ---------------------------------------------------------------------------
# interruption
# ---------------------------------------------------------------------------


def test_backup_interrupt_during_snapshot_rolls_back_the_partial_set(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C while a later snapshot runs must not leave a partial set."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root, book="book-a"), "a1")
    _seed(_book_db(raw_root, book="book-b"), "b1")

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z", book="book-a")
    old_bytes = old.read_bytes()

    real_write = storage._write_snapshot
    calls: list[Path] = []

    def interrupt_second(source: Path, target: Path):
        calls.append(target)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_write(source, target)

    monkeypatch.setattr(storage, "_write_snapshot", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "backup", "--out", str(out), "--json"])

    assert len(calls) == 2
    assert _fresh_backups(out) == []
    assert old.read_bytes() == old_bytes
    assert _leaked_temp_files(out) == []


def test_backup_interrupt_during_verification_rolls_back_the_partial_set(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C while the second copy is re-read must remove both copies."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db_a = _book_db(raw_root, book="book-a")
    db_b = _book_db(raw_root, book="book-b")
    _seed(db_a, "a1")
    _seed(db_b, "b1")
    before_a = _source_bytes(db_a)
    before_b = _source_bytes(db_b)

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z", book="book-a")

    real_verify = storage._verify_database
    calls: list[Path] = []

    def interrupt_second(path: Path):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_verify(path)

    monkeypatch.setattr(storage, "_verify_database", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "backup", "--out", str(out), "--json"])

    assert len(calls) == 2
    assert _fresh_backups(out) == []
    assert old.exists()
    _assert_source_unchanged(db_a, before_a)
    _assert_source_unchanged(db_b, before_b)
    assert _leaked_temp_files(out) == []


def test_backup_interrupt_with_keep_does_not_prune(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial set is not a successful run, so retention must never run."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root, book="book-a"), "a1")
    _seed(_book_db(raw_root, book="book-b"), "b1")

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z", book="book-a")

    real_write = storage._write_snapshot
    calls: list[Path] = []

    def interrupt_second(source: Path, target: Path):
        calls.append(target)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_write(source, target)

    monkeypatch.setattr(storage, "_write_snapshot", interrupt_second)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "backup", "--out", str(out), "--keep", "1"])

    assert old.exists()
    assert _fresh_backups(out) == []
