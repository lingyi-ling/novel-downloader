#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_backup
----------------------------------

Offline tests for ``storage backup`` / ``storage restore``.

Every database lives under a temporary ``raw_data`` directory; the command's
``_resolve_paths`` helper is monkeypatched so the real user data is never
touched. No network access happens.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _invoke(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _seed(path: Path, *chapter_ids: str) -> None:
    """Create a chapter database at ``path`` with the given ids."""
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


def _ids(path: Path) -> set[str]:
    with ChapterStorage(path.parent, path.name) as store:
        return store.existing_ids()


def _newer_schema_db(path: Path) -> None:
    """A structurally valid database written by a future application build."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()


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


def _book_db(root: Path, site: str = "site", book: str = "book") -> Path:
    return root / site / book / "chapter.raw.sqlite"


def _make_backup(out: Path) -> Path:
    """Run ``storage backup`` and return the single produced file."""
    _invoke(["storage", "backup", "--out", str(out)])
    files = sorted(out.rglob("chapter.*.sqlite"))
    assert len(files) == 1
    return files[0]


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def test_backup_and_restore_roundtrip(raw_root: Path, tmp_path: Path) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1", "c2")

    out = tmp_path / "backups"
    backup = _make_backup(out)

    assert backup.parent == out / "site" / "book"
    assert backup.name.startswith("chapter.raw.")
    assert backup.name.endswith(".sqlite")
    assert _ids(backup) == {"c1", "c2"}

    db.unlink()
    _invoke(["storage", "restore", "--from", str(backup)])

    assert db.is_file()
    assert _ids(db) == {"c1", "c2"}


def test_backup_includes_uncheckpointed_wal(raw_root: Path, tmp_path: Path) -> None:
    db = _book_db(raw_root)
    db.parent.mkdir(parents=True)
    store = ChapterStorage(db.parent, db.name)
    store.connect()
    try:
        store.upsert_chapter(
            {
                "id": "wal-only",
                "title": "WAL",
                "content": "still in the write-ahead log",
                "extra": {},
            }
        )
        wal = Path(f"{db}-wal")
        assert wal.is_file() and wal.stat().st_size > 0

        backup = _make_backup(tmp_path / "backups")
    finally:
        store.close()

    # The online backup API sees committed rows that were never checkpointed.
    assert _ids(backup) == {"wal-only"}


def test_backup_refuses_to_overwrite_existing_target(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    existing = out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"keep me")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out)])
    assert exc_info.value.code == 1
    assert existing.read_bytes() == b"keep me"
    assert len(list(out.rglob("*.sqlite"))) == 1


def test_backup_works_with_read_only_source(raw_root: Path, tmp_path: Path) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1")

    try:
        os.chmod(db, stat.S_IREAD)
        backup = _make_backup(tmp_path / "backups")
    finally:
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)

    assert _ids(backup) == {"c1"}


def test_backup_json_is_single_line(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(_book_db(raw_root), "c1")

    _invoke(["storage", "backup", "--out", str(tmp_path / "backups"), "--json"])

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["command"] == "storage backup"
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert len(payload["files"]) == 1
    assert payload["files"][0]["chapters"] == 1
    assert payload["files"][0]["schema_version"] == SCHEMA_VERSION


def test_backup_with_no_databases_is_a_noop(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "backups"

    _invoke(["storage", "backup", "--out", str(out), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["files"] == []
    assert not out.exists()


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_requires_yes_before_replacing_an_existing_database(
    raw_root: Path, tmp_path: Path
) -> None:
    db = _book_db(raw_root)
    _seed(db, "old")

    other = raw_root / "site" / "book2" / "chapter.raw.sqlite"
    _seed(other, "new")
    _invoke(["storage", "backup", "--out", str(tmp_path / "backups")])
    backup = next(
        p
        for p in (tmp_path / "backups").rglob("chapter.*.sqlite")
        if "book2" in p.parts
    )
    # Not used by derivation: --to pins the destination.

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(db),
            ]
        )
    assert exc_info.value.code == 2
    assert _ids(db) == {"old"}
    assert not list(db.parent.glob("*.pre-restore-*.bak"))

    _invoke(
        [
            "storage",
            "restore",
            "--from",
            str(backup),
            "--to",
            str(db),
            "--yes",
        ]
    )

    assert _ids(db) == {"new"}
    saved = list(db.parent.glob("chapter.raw.sqlite.pre-restore-*.bak"))
    assert len(saved) == 1
    assert _ids(saved[0]) == {"old"}


def test_restore_rejects_corrupt_backup_without_touching_target(
    raw_root: Path, tmp_path: Path
) -> None:
    db = _book_db(raw_root)
    _seed(db, "keep")

    corrupt = tmp_path / "backups" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not an sqlite database")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(corrupt), "--yes"])
    assert exc_info.value.code == 1
    assert _ids(db) == {"keep"}
    assert not list(db.parent.glob("*.pre-restore-*.bak"))


def test_restore_rejects_non_chapter_database(raw_root: Path, tmp_path: Path) -> None:
    db = _book_db(raw_root)
    _seed(db, "keep")

    foreign = tmp_path / "backups" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    foreign.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(foreign))
    try:
        conn.execute("CREATE TABLE cookies (value TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(foreign), "--yes"])
    assert exc_info.value.code == 1
    assert _ids(db) == {"keep"}


def test_restore_rejects_newer_schema(raw_root: Path, tmp_path: Path) -> None:
    db = _book_db(raw_root)
    _seed(db, "keep")

    newer = tmp_path / "backups" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    _newer_schema_db(newer)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(newer), "--yes"])
    assert exc_info.value.code == 1
    assert _ids(db) == {"keep"}


def test_restore_failure_leaves_original_database_intact(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _book_db(raw_root)
    _seed(db, "keep")

    replacement = raw_root / "site" / "book2" / "chapter.raw.sqlite"
    _seed(replacement, "new")
    _invoke(["storage", "backup", "--out", str(tmp_path / "backups")])
    backup = next(
        p
        for p in (tmp_path / "backups").rglob("chapter.*.sqlite")
        if "book2" in p.parts
    )

    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        if Path(str(dst)) == db:
            raise OSError("simulated replace failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(storage.os, "replace", failing_replace)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(db),
                "--yes",
            ]
        )
    assert exc_info.value.code == 1
    assert _ids(db) == {"keep"}
    # The automatic pre-restore copy still exists, and no temp file leaked.
    assert list(db.parent.glob("chapter.raw.sqlite.pre-restore-*.bak"))
    assert not list(db.parent.glob(".*.tmp"))


def test_restore_rejects_target_outside_raw_data(
    raw_root: Path, tmp_path: Path
) -> None:
    _seed(_book_db(raw_root), "c1")
    backup = _make_backup(tmp_path / "backups")

    outside = tmp_path / "outside" / "chapter.raw.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(outside),
                "--yes",
            ]
        )
    assert exc_info.value.code == 2
    assert not outside.exists()


def test_restore_rejects_non_database_target_name(
    raw_root: Path, tmp_path: Path
) -> None:
    _seed(_book_db(raw_root), "c1")
    backup = _make_backup(tmp_path / "backups")

    target = raw_root / "site" / "book" / "metadata.json"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(target),
                "--yes",
            ]
        )
    assert exc_info.value.code == 2
    assert not target.exists()


def test_restore_json_is_single_line(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1")
    backup = _make_backup(tmp_path / "backups")

    _invoke(
        [
            "storage",
            "restore",
            "--from",
            str(backup),
            "--to",
            str(db),
            "--yes",
            "--json",
        ]
    )

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["command"] == "storage restore"
    assert payload["ok"] is True
    assert payload["target"] == str(db)
    assert payload["replaced_existing"] is True
    assert payload["pre_restore_backup"]
