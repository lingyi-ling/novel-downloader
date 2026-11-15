#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_failure_json
----------------------------------------

The ``--json`` failure contract of ``storage backup``.

A runtime failure used to be split in two: a post-write verification failure
printed one machine-readable line, while every earlier failure (not a
database, damaged main file or WAL, exhausted same-second names, I/O error)
reported on stderr only and left stdout empty. This module pins the unified
contract for every class:

* exit code ``1`` and exactly one JSON line on stdout;
* stable ``error`` identifier, non-empty ``message``, affected ``source``;
* ``count``/``bytes`` zero, ``files`` empty, ``verified`` false;
* no file from the failed run survives, and pre-existing backups plus the
  source databases are byte-identical.

Usage errors (exit ``2``) are deliberately excluded and are covered by the
matrix module; they keep an empty stdout.
"""

from __future__ import annotations

import argparse
import json
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

#: A stable timestamp so a test can predict the backup file name.
_STAMP = "20260102T030405Z"

#: Keys every failed-run payload must carry.
_FAILURE_KEYS = {
    "command",
    "ok",
    "error",
    "message",
    "source",
    "timestamp",
    "out",
    "count",
    "bytes",
    "files",
    "verified",
    "verification_failed",
    "retention",
}


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


def _read_json_line(capsys: pytest.CaptureFixture[str]) -> tuple[dict, str]:
    """Return the parsed single JSON line and everything written to stderr."""
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    assert captured.out.endswith("\n")
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    return json.loads(captured.out), captured.err


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


# ---------------------------------------------------------------------------
# database shape builders
# ---------------------------------------------------------------------------


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


def _damaged_wal(path: Path) -> None:
    """Flip bytes inside the second WAL frame, keeping the chain plausible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="failure-json-", dir=path.parent) as tmp:
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
                    ("ch0004", "Chapter 4", "Body four"),
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

    wal = Path(f"{path}-wal")
    data = bytearray(wal.read_bytes())
    assert len(data) > 32 + 24 + 300
    data[32 + 24 + 100 : 32 + 24 + 300] = b"\xff" * 200
    wal.write_bytes(bytes(data))


def _newer_schema(path: Path) -> None:
    _seed(path, "c1")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()


def _fake_backup(
    out: Path,
    stamp: str,
    *,
    book: str = "book",
    seq: int | None = None,
) -> Path:
    suffix = "" if seq is None else f".{seq}"
    path = out / "site" / book / f"chapter.raw.{stamp}{suffix}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"older backup")
    return path


# ---------------------------------------------------------------------------
# shared assertions
# ---------------------------------------------------------------------------


def _assert_no_fresh_artifacts(
    out: Path, old: Path | None, old_bytes: bytes | None
) -> None:
    """The failed run left nothing behind and did not touch old backups."""
    if out.exists():
        assert list(out.rglob(f"chapter.*.{_STAMP}*.sqlite")) == []
        assert list(out.rglob(".*.tmp")) == []
    if old is not None:
        assert old.is_file()
        assert old.read_bytes() == old_bytes


def _assert_failure_payload(
    payload: dict,
    *,
    category: str,
    source: Path | None,
    out_dir: Path,
    verification_failed: bool = False,
) -> None:
    assert set(payload) >= _FAILURE_KEYS
    assert payload["command"] == "storage backup"
    assert payload["ok"] is False
    assert payload["error"] == category
    assert category in storage.BACKUP_ERROR_CATEGORIES
    assert isinstance(payload["message"], str) and payload["message"]
    assert payload["source"] == (str(source) if source is not None else None)
    assert payload["timestamp"] == _STAMP
    assert payload["out"] == str(out_dir.resolve())
    assert payload["count"] == 0
    assert payload["bytes"] == 0
    assert payload["files"] == []
    assert payload["verified"] is False
    assert payload["verification_failed"] is verification_failed
    assert payload["retention"] == {
        "keep": 0,
        "dry_run": False,
        "candidates": [],
        "removed": [],
        "failed": [],
    }


def _run_and_capture_failure(
    capsys: pytest.CaptureFixture[str],
    out: Path,
    db: Path,
    *,
    category: str,
    extra: list[str] | None = None,
    verification_failed: bool = False,
) -> dict:
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--json", *(extra or [])])
    assert exc_info.value.code == 1
    payload, stderr = _read_json_line(capsys)
    assert stderr, "a failed run must still explain itself on stderr"
    _assert_failure_payload(
        payload,
        category=category,
        source=db,
        out_dir=out,
        verification_failed=verification_failed,
    )
    return payload


# ---------------------------------------------------------------------------
# unreadable / damaged sources
# ---------------------------------------------------------------------------


def test_not_a_database_reports_not_database(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _not_database(db)
    before = db.read_bytes()

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    payload = _run_and_capture_failure(capsys, out, db, category="not_database")
    assert "not a database" in payload["message"].lower()

    assert db.read_bytes() == before
    _assert_no_fresh_artifacts(out, old, old_bytes)


def test_corrupt_main_database_reports_integrity_failed(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _corrupt_main(db)
    before = db.read_bytes()

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    payload = _run_and_capture_failure(capsys, out, db, category="integrity_failed")
    assert "integrity" in payload["message"].lower()

    assert db.read_bytes() == before
    _assert_no_fresh_artifacts(out, old, old_bytes)


def test_damaged_wal_reports_wal_damaged(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _damaged_wal(db)
    before = {
        str(db): db.read_bytes(),
        str(db) + "-wal": Path(f"{db}-wal").read_bytes(),
    }

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    payload = _run_and_capture_failure(capsys, out, db, category="wal_damaged")
    assert "write-ahead log" in payload["message"].lower()

    for path_text, expected in before.items():
        assert Path(path_text).read_bytes() == expected
    _assert_no_fresh_artifacts(out, old, old_bytes)


def test_newer_schema_fails_post_write_check_as_schema_unsupported(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The copy is written, then the strict post-write check rejects it."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _newer_schema(db)
    before = db.read_bytes()

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    payload = _run_and_capture_failure(
        capsys, out, db, category="schema_unsupported", verification_failed=True
    )
    error = payload["verification_error"]
    assert error["source"] == str(db)
    assert Path(error["target"]).name == f"chapter.raw.{_STAMP}.sqlite"
    assert "newer" in error["reason"]
    assert "Backup verification failed" in payload["message"]

    assert db.read_bytes() == before
    _assert_no_fresh_artifacts(out, old, old_bytes)


# ---------------------------------------------------------------------------
# naming and I/O failures
# ---------------------------------------------------------------------------


def test_sequence_exhaustion_reports_sequence_exhausted(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    monkeypatch.setattr(storage, "_MAX_BACKUP_SEQUENCE", 1)
    db = _book_db(raw_root)
    _seed(db, "c1")
    before = db.read_bytes()

    out = tmp_path / "backups"
    old = _fake_backup(out, _STAMP)
    second = _fake_backup(out, _STAMP, seq=1)
    old_bytes, second_bytes = old.read_bytes(), second.read_bytes()

    payload = _run_and_capture_failure(capsys, out, db, category="sequence_exhausted")
    assert "refusing to overwrite" in payload["message"].lower()

    assert old.read_bytes() == old_bytes
    assert second.read_bytes() == second_bytes
    assert {path.name for path in (out / "site" / "book").iterdir()} == {
        old.name,
        second.name,
    }
    assert db.read_bytes() == before


def test_write_io_error_reports_io_error(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _seed(db, "c1")
    before = db.read_bytes()

    def explode(_source: Path, _target: Path) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(storage, "_write_snapshot", explode)

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    payload = _run_and_capture_failure(capsys, out, db, category="io_error")
    assert "disk full" in payload["message"]

    assert db.read_bytes() == before
    _assert_no_fresh_artifacts(out, old, old_bytes)


def test_unexpected_exception_reports_internal_error(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    db = _book_db(raw_root)
    _seed(db, "c1")

    def explode(_source: Path, _target: Path) -> None:
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(storage, "_write_snapshot", explode)

    out = tmp_path / "backups"
    payload = _run_and_capture_failure(capsys, out, db, category="internal_error")
    assert "simulated bug" in payload["message"]
    _assert_no_fresh_artifacts(out, None, None)


# ---------------------------------------------------------------------------
# retention failure (new backups are kept)
# ---------------------------------------------------------------------------


def test_retention_failure_reports_retention_failed_and_keeps_backups(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pruning is the one failure where the new set survives; say so in JSON."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    old = _fake_backup(out, "20250101T000000Z")
    old_bytes = old.read_bytes()

    real_unlink = Path.unlink

    def failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == old:
            raise OSError("simulated busy file")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--keep", "1", "--json"])
    assert exc_info.value.code == 1

    payload, stderr = _read_json_line(capsys)
    assert payload["ok"] is False
    assert payload["error"] == "retention_failed"
    assert payload["count"] == 1
    assert payload["files"][0]["verified"] is True
    assert payload["retention"]["failed"] == [
        {"path": str(old), "error": "simulated busy file"}
    ]
    assert "retention" in payload["message"].lower()
    assert stderr

    # The new backup is published, the old one survives, and the source is
    # untouched: retention failure never rolls back a usable set.
    fresh = out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    assert fresh.is_file()
    assert old.read_bytes() == old_bytes
    assert _book_db(raw_root).is_file()


# ---------------------------------------------------------------------------
# configuration resolution
# ---------------------------------------------------------------------------


def test_configuration_failure_still_emits_one_json_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unreadable(**_kwargs: object) -> dict[str, Path]:
        raise SystemExit(storage.EXIT_FAILURE)

    monkeypatch.setattr(storage, "_resolve_paths", unreadable)
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)

    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--json"])
    assert exc_info.value.code == 1

    payload, _stderr = _read_json_line(capsys)
    _assert_failure_payload(payload, category="config_error", source=None, out_dir=out)
    assert "configuration" in payload["message"].lower()
    assert not out.exists()


# ---------------------------------------------------------------------------
# success contract is unchanged
# ---------------------------------------------------------------------------


def test_success_payload_keeps_its_established_shape(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    _invoke(["storage", "backup", "--out", str(out), "--json"])

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)

    assert set(payload) == {
        "command",
        "ok",
        "timestamp",
        "out",
        "count",
        "bytes",
        "files",
        "verified",
        "verification_failed",
        "retention",
    }
    assert payload["ok"] is True
    assert payload["count"] == 1
