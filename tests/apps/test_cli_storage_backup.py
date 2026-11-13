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
import shutil
import sqlite3
import stat
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


#: Present only in the WAL of the legacy fixture.
_WAL_SENTINEL = "WAL-SENTINEL-7f3a"

_LEGACY_SCHEMA = (
    "CREATE TABLE chapters ("
    "id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL)"
)

_LEGACY_ROWS = (
    ("ch0001", "Chapter 1", "Body one"),
    ("ch0002", "Chapter 2", "Body two"),
    ("ch0002", "Chapter 2 (stale copy)", ""),
    ("ch0004", "Chapter 4", f"Body four {_WAL_SENTINEL}"),
)


def _make_legacy_db(path: Path) -> None:
    """Write a pre-schema-version file whose committed rows stay in the WAL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="legacy-cli-", dir=path.parent))
    staging = staging_dir / path.name
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
        shutil.rmtree(staging_dir, ignore_errors=True)


def _raw_rows(path: Path) -> list[tuple[str, str, str]]:
    """Read a chapter file without opening it through ChapterStorage."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute("SELECT id, title, content FROM chapters")
        ]
    finally:
        conn.close()


def _raw_version(path: Path) -> int:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])
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


def _fake_backup(
    out: Path,
    stamp: str,
    *,
    site: str = "site",
    book: str = "book",
    stage: str = "raw",
) -> Path:
    """Create a file with the exact name (and layout) ``storage backup`` uses."""
    path = out / site / book / f"chapter.{stage}.{stamp}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"older backup")
    return path


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.is_file())


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
# backup retention (--keep)
# ---------------------------------------------------------------------------


def test_keep_prunes_all_but_the_newest_n(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    oldest = _fake_backup(out, "20250101T000000Z")
    second = _fake_backup(out, "20250101T000001Z")
    third = _fake_backup(out, "20250101T000002Z")

    _invoke(["storage", "backup", "--out", str(out), "--keep", "2", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["retention"]["keep"] == 2
    assert payload["retention"]["dry_run"] is False
    assert sorted(payload["retention"]["removed"]) == sorted([str(oldest), str(second)])
    assert payload["retention"]["failed"] == []

    # The fresh backup and the second-newest old one survive.
    assert _names(out / "site" / "book") == [
        "chapter.raw.20250101T000002Z.sqlite",
        f"chapter.raw.{_STAMP}.sqlite",
    ]
    assert not oldest.exists()
    assert not second.exists()
    assert third.exists()
    assert _ids(out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite") == {"c1"}


def test_keep_is_applied_per_book(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A global newest-N rule would evict one book entirely; per book must not."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root, site="site", book="book-a"), "a1")
    _seed(_book_db(raw_root, site="site", book="book-b"), "b1")

    out = tmp_path / "backups"
    a_old = [
        _fake_backup(out, "20240101T000000Z", book="book-a"),
        _fake_backup(out, "20240101T000001Z", book="book-a"),
    ]
    b_old = [
        _fake_backup(out, "20241201T000000Z", book="book-b"),
        _fake_backup(out, "20241201T000001Z", book="book-b"),
    ]

    _invoke(["storage", "backup", "--out", str(out), "--keep", "1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["retention"]["removed"]) == sorted(
        str(path) for path in [*a_old, *b_old]
    )
    for book in ("book-a", "book-b"):
        assert _names(out / "site" / book) == [f"chapter.raw.{_STAMP}.sqlite"]
    assert _ids(out / "site" / "book-a" / f"chapter.raw.{_STAMP}.sqlite") == {"a1"}
    assert _ids(out / "site" / "book-b" / f"chapter.raw.{_STAMP}.sqlite") == {"b1"}


def test_keep_counts_each_stage_separately(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A book's ``raw`` backups must not evict its ``cleaner`` backups."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    for stamp in ("20250101T000000Z", "20250101T000001Z"):
        _fake_backup(out, stamp, stage="raw")
    cleaner_oldest = _fake_backup(out, "20230101T000000Z", stage="cleaner")
    cleaner_newest = _fake_backup(out, "20230101T000001Z", stage="cleaner")

    _invoke(["storage", "backup", "--out", str(out), "--keep", "1"])

    assert _names(out / "site" / "book") == [
        "chapter.cleaner.20230101T000001Z.sqlite",
        f"chapter.raw.{_STAMP}.sqlite",
    ]
    assert not cleaner_oldest.exists()
    assert cleaner_newest.exists()


def test_keep_leaves_non_backup_entries_alone(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    book_dir = out / "site" / "book"
    old = _fake_backup(out, "20250101T000000Z")

    foreign = [
        book_dir / "notes.txt",
        book_dir / "chapter.raw.sqlite",
        book_dir / "chapter.raw.20250101T000000Z.sqlite.partial",
        book_dir / "chapter.raw.notatimestamp.sqlite",
        book_dir / "chapter.other.20250101T000000Z.sqlite.bak",
    ]
    for path in foreign:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a backup")

    # A directory whose name matches the pattern is still not a backup file.
    matching_dir = book_dir / "chapter.cleaner.20250101T000000Z.sqlite"
    matching_dir.mkdir()
    (matching_dir / "inside.txt").write_bytes(b"keep")

    # A symlink is never followed or removed, even with a matching name.
    link = book_dir / "chapter.cleaner.20240101T000000Z.sqlite"
    link_target = book_dir / "link-target.txt"
    link_target.write_bytes(b"keep")
    try:
        link.symlink_to(link_target)
    except (OSError, NotImplementedError):
        link = None  # type: ignore[assignment]

    _invoke(["storage", "backup", "--out", str(out), "--keep", "1"])

    assert not old.exists()
    for path in foreign:
        assert path.exists(), f"{path} should not be touched"
    assert matching_dir.is_dir()
    assert (matching_dir / "inside.txt").exists()
    if link is not None:
        assert link.is_symlink()
        assert link_target.exists()


def test_keep_dry_run_reports_without_deleting(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    old = [
        _fake_backup(out, stamp) for stamp in ("20250101T000000Z", "20250101T000001Z")
    ]

    _invoke(
        ["storage", "backup", "--out", str(out), "--keep", "1", "--dry-run", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    retention = payload["retention"]
    assert payload["ok"] is True
    assert retention["dry_run"] is True
    assert retention["removed"] == []
    assert sorted(retention["candidates"]) == sorted(str(path) for path in old)
    for path in old:
        assert path.exists()
    assert (out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite").exists()


def test_keep_deletion_failure_continues_and_exits_1(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    failing = _fake_backup(out, "20250101T000000Z")
    removable = _fake_backup(out, "20250101T000001Z")

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == failing.name:
            raise OSError("simulated unlink failure")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--keep", "1"])
    assert exc_info.value.code == 1

    # The failure did not abort the pass, and the fresh backup is intact.
    assert failing.exists()
    assert not removable.exists()
    assert _names(out / "site" / "book") == [
        failing.name,
        f"chapter.raw.{_STAMP}.sqlite",
    ]


def test_keep_never_deletes_the_fresh_backup(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A future-dated file must not push the just-written backup out."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    future = _fake_backup(out, "99991231T235959Z")
    old = _fake_backup(out, "20250101T000000Z")

    _invoke(["storage", "backup", "--out", str(out), "--keep", "1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    fresh = out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    assert payload["retention"]["removed"] == [str(old)]
    assert fresh.exists()
    assert future.exists()
    assert not old.exists()
    assert _ids(fresh) == {"c1"}


@pytest.mark.parametrize("keep_args", [[], ["--keep", "0"]])
def test_keep_zero_disables_cleanup(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    keep_args: list[str],
) -> None:
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    olds = [
        _fake_backup(out, stamp) for stamp in ("20250101T000000Z", "20250101T000001Z")
    ]

    _invoke(["storage", "backup", "--out", str(out), *keep_args, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["retention"] == {
        "keep": 0,
        "dry_run": False,
        "candidates": [],
        "removed": [],
        "failed": [],
    }
    for path in olds:
        assert path.exists()
    assert (out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite").exists()


def test_negative_keep_is_a_usage_error(raw_root: Path, tmp_path: Path) -> None:
    _seed(_book_db(raw_root), "c1")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            ["storage", "backup", "--out", str(tmp_path / "backups"), "--keep", "-1"]
        )
    assert exc_info.value.code == 2


def test_dry_run_without_keep_is_a_usage_error(raw_root: Path, tmp_path: Path) -> None:
    """``--dry-run`` must not silently turn into an ordinary backup run."""
    _seed(_book_db(raw_root), "c1")

    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out), "--dry-run"])
    assert exc_info.value.code == 2
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


def test_restore_accepts_a_relative_source_path(
    raw_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--from ./backups/...`` must work like an absolute path.

    The read-only URI used to open the source requires an absolute path;
    passing the user's relative spelling straight through raised ``ValueError``
    and escaped as a traceback instead of restoring the database.
    """
    db = _book_db(raw_root)
    _seed(db, "c1", "c2")
    backup = _make_backup(tmp_path / "backups")
    db.unlink()

    monkeypatch.chdir(tmp_path)
    _invoke(["storage", "restore", "--from", str(backup.relative_to(tmp_path))])

    assert db.is_file()
    assert _ids(db) == {"c1", "c2"}


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


# ---------------------------------------------------------------------------
# legacy (pre-schema-version) databases
# ---------------------------------------------------------------------------


def test_backup_and_restore_roundtrip_of_legacy_wal_database(
    raw_root: Path, tmp_path: Path
) -> None:
    """A pre-v1.31 file keeps its rows, schema and WAL-only data across a trip."""
    db = _book_db(raw_root)
    _make_legacy_db(db)
    wal = Path(f"{db}-wal")
    assert wal.is_file() and wal.stat().st_size > 32

    original = _raw_rows(db)
    assert len(original) == 4
    assert any(content == "" for _, _, content in original)
    assert any(_WAL_SENTINEL in content for _, _, content in original)

    backup = _make_backup(tmp_path / "backups")

    # The snapshot contains the committed WAL frames and does not silently
    # upgrade the file: a restore is a faithful copy, migration happens later.
    assert _raw_rows(backup) == original
    assert _raw_version(backup) == 0
    conn = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(chapters)")]
    finally:
        conn.close()
    assert columns == ["id", "title", "content"]

    _invoke(["storage", "restore", "--from", str(backup), "--to", str(db), "--yes"])

    assert _raw_rows(db) == original
    assert _raw_version(db) == 0
    preserved = list(db.parent.glob("chapter.raw.sqlite.pre-restore-*.bak"))
    assert len(preserved) == 1
    assert _raw_rows(preserved[0]) == original

    # The restored legacy file becomes the current schema on first use, with
    # every row (including duplicate ids and the WAL-only chapter) intact.
    with ChapterStorage(db.parent, db.name) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.existing_ids() == {"ch0001", "ch0002", "ch0004"}
        restored = store.get_chapter("ch0004")
        assert restored is not None
        assert _WAL_SENTINEL in restored["content"]
        assert (
            store.conn.execute(
                "SELECT COUNT(*) FROM chapters WHERE id = 'ch0002'"
            ).fetchone()[0]
            == 2
        )

        # The migrated file is writable again despite the duplicate ids.
        store.upsert_chapter(
            {
                "id": "ch0002",
                "title": "Chapter 2",
                "content": "Body two again",
                "extra": {},
            }
        )
        assert (
            store.conn.execute(
                "SELECT COUNT(*) FROM chapters WHERE id = 'ch0002'"
            ).fetchone()[0]
            == 1
        )


def test_backup_refuses_corrupt_legacy_database_without_touching_it(
    raw_root: Path, tmp_path: Path
) -> None:
    db = _book_db(raw_root)
    db.parent.mkdir(parents=True)
    original = b"this is not an sqlite database\n" * 64
    db.write_bytes(original)

    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out)])
    assert exc_info.value.code == 1
    assert db.read_bytes() == original
    assert not out.exists() or not list(out.rglob("*.sqlite"))


def test_backup_refuses_damaged_wal_instead_of_writing_an_empty_snapshot(
    raw_root: Path, tmp_path: Path
) -> None:
    db = _book_db(raw_root)
    _make_legacy_db(db)
    wal = Path(f"{db}-wal")
    data = bytearray(wal.read_bytes())
    assert len(data) > 32 + 24 + 300
    data[32 + 24 + 100 : 32 + 24 + 300] = b"\xff" * 200
    wal.write_bytes(bytes(data))
    damaged_wal = wal.read_bytes()
    main_before = db.read_bytes()

    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "backup", "--out", str(out)])
    assert exc_info.value.code == 1

    # The online backup API would see only the main file (schema included in
    # the WAL) and publish an empty, table-less snapshot.  Nothing may change.
    assert wal.read_bytes() == damaged_wal
    assert db.read_bytes() == main_before
    assert not out.exists() or not list(out.rglob("*.sqlite"))
