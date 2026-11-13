#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_verify
----------------------------------

Offline tests for ``storage verify``.

Every database lives under a temporary ``raw_data`` directory; the command's
``_resolve_paths`` helper is monkeypatched so the real user data is never
touched. No network access happens, and the command must not modify the files
it inspects (the tests compare bytes before and after).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.infra.persistence.chapter_storage import (
    SCHEMA_VERSION,
    ChapterStorage,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _invoke(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _seed(path: Path, *chapter_ids: str) -> None:
    """Create a healthy chapter database at ``path``."""
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


def _write_sqlite(path: Path, statements: list[str], *, version: int = 0) -> None:
    """Create a database from raw SQL without using ``ChapterStorage``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _book_db(root: Path, site: str = "site", book: str = "book") -> Path:
    return root / site / book / "chapter.raw.sqlite"


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out
    # The JSON contract is one line: no embedded newlines besides the final one.
    assert out.endswith("\n")
    assert "\n" not in out.strip()
    return json.loads(out)


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
    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    yield root


#: Present only in the WAL of the legacy fixture.
_WAL_SENTINEL = "WAL-SENTINEL-7f3a"

_LEGACY_SCHEMA = (
    "CREATE TABLE chapters ("
    "id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL)"
)

_LEGACY_ROWS = (
    ("ch0001", "Chapter 1", "Body one"),
    ("ch0002", "Chapter 2", "Body two"),
    ("ch0003", "Chapter 3", "Body three"),
    ("ch0004", "Chapter 4", f"Body four {_WAL_SENTINEL}"),
)


def _make_legacy_wal_db(path: Path) -> None:
    """Write a file whose schema and rows stay in a multi-frame WAL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="legacy-cli-", dir=path.parent) as tmp:
        staging = Path(tmp) / path.name
        conn = sqlite3.connect(str(staging), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.execute("PRAGMA user_version = 0")
            conn.execute(_LEGACY_SCHEMA)
            conn.executemany("INSERT INTO chapters VALUES (?, ?, ?)", _LEGACY_ROWS)
            shutil.copy2(staging, path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{staging}{suffix}")
                if sidecar.is_file():
                    shutil.copy2(sidecar, Path(f"{path}{suffix}"))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# healthy databases
# ---------------------------------------------------------------------------


def test_verify_healthy_database_is_ok_and_left_untouched(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1", "c2")
    before = db.read_bytes()
    wal = Path(f"{db}-wal")
    wal_before = wal.read_bytes() if wal.is_file() else None

    _invoke(["storage", "verify"])

    captured = capsys.readouterr()
    # The console may fold a long path; the trailing file name stays intact.
    assert "chapter.raw.sqlite: ok" in captured.err
    assert storage._redact_path(db).startswith("~")
    assert db.read_bytes() == before
    if wal_before is not None:
        assert wal.read_bytes() == wal_before
    assert not list(raw_root.rglob("*.corrupt-*"))


def test_verify_accepts_healthy_database_whose_data_is_in_the_wal(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A legacy WAL file still verifies after a read-only open."""
    db = _book_db(raw_root)
    _make_legacy_wal_db(db)
    assert Path(f"{db}-wal").stat().st_size > 32
    main_before = db.read_bytes()

    _invoke(["storage", "verify", "--json"])

    assert _payload(capsys) == {
        "ok": True,
        "total": 1,
        "healthy": 1,
        "corrupt": 0,
        "errors": [],
    }
    assert db.read_bytes() == main_before


def test_verify_multiple_databases_are_all_checked(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")
    _seed(_book_db(raw_root, "site-b", "book-2"), "c2")

    _invoke(["storage", "verify", "--json"])

    payload = _payload(capsys)
    assert payload == {
        "ok": True,
        "total": 2,
        "healthy": 2,
        "corrupt": 0,
        "errors": [],
    }


def test_verify_reports_nothing_when_no_databases_exist(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _invoke(["storage", "verify"])

    assert "No chapter databases found" in capsys.readouterr().err

    _invoke(["storage", "verify", "--json"])
    assert _payload(capsys) == {
        "ok": True,
        "total": 0,
        "healthy": 0,
        "corrupt": 0,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# structural problems
# ---------------------------------------------------------------------------


def test_verify_reports_missing_chapters_table(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _write_sqlite(db, ["CREATE TABLE unrelated (id INTEGER)"], version=1)
    before = db.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    assert "chapters" in capsys.readouterr().err.lower()
    assert db.read_bytes() == before


def test_verify_rejects_newer_schema_version(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _write_sqlite(
        db,
        [
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)",
        ],
        version=SCHEMA_VERSION + 1,
    )
    before = db.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err.lower()
    assert "newer" in err
    assert str(SCHEMA_VERSION + 1) in err
    assert db.read_bytes() == before


def test_verify_reports_missing_required_column(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _write_sqlite(
        db,
        ["CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT)"],
        version=SCHEMA_VERSION,
    )
    before = db.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    assert "content" in capsys.readouterr().err.lower()
    assert db.read_bytes() == before


def test_verify_reports_non_database_file(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    db.parent.mkdir(parents=True)
    original = b"this is not an sqlite database\n" * 64
    db.write_bytes(original)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    assert "not a database" in capsys.readouterr().err.lower()
    assert db.read_bytes() == original


# ---------------------------------------------------------------------------
# WAL damage
# ---------------------------------------------------------------------------


def test_verify_reports_damaged_wal_without_touching_files(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _make_legacy_wal_db(db)
    wal = Path(f"{db}-wal")
    data = bytearray(wal.read_bytes())
    assert len(data) > 32 + 24 + 300
    data[32 + 24 + 100 : 32 + 24 + 300] = b"\xff" * 200
    wal.write_bytes(bytes(data))
    damaged_wal = wal.read_bytes()
    main_before = db.read_bytes()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err.lower()
    assert "write-ahead log" in err

    # A damaged file is only reported: no repair, no copy, no rename.
    assert wal.read_bytes() == damaged_wal
    assert db.read_bytes() == main_before
    assert not list(raw_root.rglob("*.corrupt-*"))
    assert not list(raw_root.rglob("*.pre-restore-*"))


# ---------------------------------------------------------------------------
# permission failures
# ---------------------------------------------------------------------------


def test_verify_reports_permission_failure(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1")
    before = db.read_bytes()

    def deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage.sqlite3, "connect", deny)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify"])

    assert exc_info.value.code == 1
    assert "permission denied" in capsys.readouterr().err.lower()
    assert db.read_bytes() == before


# ---------------------------------------------------------------------------
# JSON report and exit codes
# ---------------------------------------------------------------------------


def test_verify_json_reports_corrupt_database_and_exits_one(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy = _book_db(raw_root, "site-a", "book-1")
    _seed(healthy, "c1")
    corrupt = _book_db(raw_root, "site-b", "book-2")
    _write_sqlite(corrupt, ["CREATE TABLE unrelated (id INTEGER)"], version=1)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify", "--json"])

    assert exc_info.value.code == 1
    payload = _payload(capsys)
    assert payload["ok"] is False
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["corrupt"] == 1
    errors = payload["errors"]
    assert isinstance(errors, list) and len(errors) == 1
    entry = errors[0]
    assert entry["path"] == storage._redact_path(corrupt)
    assert entry["reason"]
    assert storage._redact_path(healthy) not in str(errors)


def test_verify_json_exits_zero_when_everything_is_healthy(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_book_db(raw_root), "c1")

    _invoke(["storage", "verify", "--json"])

    payload = _payload(capsys)
    assert payload["ok"] is True
    assert payload["corrupt"] == 0
    assert payload["errors"] == []


# ---------------------------------------------------------------------------
# path redaction and symlinks
# ---------------------------------------------------------------------------


def test_verify_redacts_home_directory_in_json_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    root = home / "raw"
    root.mkdir(parents=True)
    paths = {
        "raw": root,
        "cache": tmp_path / "cache",
        "downloads": tmp_path / "downloads",
        "logs": tmp_path / "logs",
    }
    monkeypatch.setattr(storage, "_resolve_paths", lambda: paths)
    monkeypatch.setattr(storage, "_home_dir", lambda: str(home))
    db = _book_db(root)
    _write_sqlite(db, ["CREATE TABLE unrelated (id INTEGER)"], version=1)

    with pytest.raises(SystemExit):
        _invoke(["storage", "verify", "--json"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    path = payload["errors"][0]["path"]
    assert path.startswith("~")
    assert str(home) not in out
    assert str(home) not in payload["errors"][0]["reason"]


def test_verify_ignores_symlinked_databases(
    raw_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "outside" / "site" / "book" / "chapter.raw.sqlite"
    _seed(target, "c1")
    link = _book_db(raw_root)
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    _invoke(["storage", "verify", "--json"])

    assert _payload(capsys)["total"] == 0
    assert link.is_symlink()
