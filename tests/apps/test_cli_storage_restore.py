#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_restore
-----------------------------------

Offline tests for ``storage restore``: single-file and directory sources,
dry-run, the ``--force`` gate, path containment and symlink refusal, source
validation, grouped rollback on failure and Ctrl+C, and the ``--json``
contract.

Every database lives under a temporary ``raw_data`` root wired into
``storage._resolve_paths``; no network access happens.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.infra.persistence.chapter_storage import (
    SCHEMA_VERSION,
    ChapterStorage,
)

#: A stable timestamp so a test can predict backup file names.
_STAMP = "20260102T030405Z"

#: JSON keys every ``storage restore --json`` run must publish.
_REQUIRED_KEYS = {
    "ok",
    "restored",
    "files",
    "skipped",
    "dry_run",
    "error",
    "message",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _invoke(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    """Read exactly one JSON line from stdout."""
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out
    return json.loads(out)


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


def _ids(path: Path) -> set[str]:
    with ChapterStorage(path.parent, path.name) as store:
        return store.existing_ids()


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


@pytest.fixture()
def backups(raw_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A backup set produced by the real ``storage backup`` command."""
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _seed(raw_root / "site" / "book" / "chapter.raw.sqlite", "c1", "c2")
    _seed(raw_root / "site" / "book" / "chapter.cleaner.sqlite", "c1")
    out = tmp_path / "backups"
    _invoke(["storage", "backup", "--out", str(out)])
    return out


def _book_db(root: Path, *, site: str = "site", book: str = "book") -> Path:
    return root / site / book / "chapter.raw.sqlite"


# ---------------------------------------------------------------------------
# audit: the command exists and exposes the documented surface
# ---------------------------------------------------------------------------


def test_restore_subcommand_is_registered() -> None:
    """``storage restore`` is registered alongside backup/verify."""
    parser = _parser()
    args = parser.parse_args(
        ["storage", "restore", "--from", "x", "--to", "y", "--force", "--json"]
    )
    assert args.func is StorageCmd.run or callable(args.func)
    assert args.source == "x"
    assert args.target == "y"
    assert args.force is True
    assert args.as_json is True
    assert args.verify is True
    assert args.dry_run is False


# ---------------------------------------------------------------------------
# single file restore
# ---------------------------------------------------------------------------


def test_restore_single_file_roundtrip(raw_root: Path, backups: Path) -> None:
    db = _book_db(raw_root)
    db.unlink()

    _invoke(["storage", "restore", "--from", str(next(backups.rglob("*.raw.*")))])

    assert db.is_file()
    assert _ids(db) == {"c1", "c2"}


def test_restore_single_file_keeps_legacy_json_fields(
    raw_root: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original single-file keys survive so old consumers keep working."""
    backup = next(backups.rglob("*.raw.*"))
    _invoke(["storage", "restore", "--from", str(backup), "--force", "--json"])
    payload = _payload(capsys)

    assert payload.keys() >= _REQUIRED_KEYS
    assert payload["ok"] is True
    assert payload["restored"] == 1
    assert payload["dry_run"] is False
    assert payload["error"] is None
    assert payload["message"] is None
    assert payload["skipped"] == 0
    # Legacy single-file fields.
    for key in (
        "command",
        "from",
        "target",
        "replaced_existing",
        "pre_restore_backup",
        "schema_version",
        "chapters",
        "bytes",
        "sha256",
    ):
        assert key in payload, key
    assert payload["command"] == "storage restore"
    assert payload["replaced_existing"] is True
    assert payload["pre_restore_backup"]
    assert len(payload["files"]) == 1  # type: ignore[arg-type]


def test_restore_directory_restores_every_stage(raw_root: Path, backups: Path) -> None:
    for name in ("chapter.raw.sqlite", "chapter.cleaner.sqlite"):
        (raw_root / "site" / "book" / name).unlink()

    _invoke(["storage", "restore", "--from", str(backups)])

    assert _ids(raw_root / "site" / "book" / "chapter.raw.sqlite") == {"c1", "c2"}
    assert _ids(raw_root / "site" / "book" / "chapter.cleaner.sqlite") == {"c1"}


def test_restore_directory_skips_older_copies_of_a_stage(
    raw_root: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the newest copy of each ``<book, stage>`` group is installed."""
    db = _book_db(raw_root)
    _seed(db, "c1", "c2", "c3")
    monkeypatch.setattr(storage, "_timestamp", lambda: "20260103T000000Z")
    _invoke(["storage", "backup", "--out", str(backups)])
    for name in ("chapter.raw.sqlite", "chapter.cleaner.sqlite"):
        (raw_root / "site" / "book" / name).unlink()

    _invoke(["storage", "restore", "--from", str(backups)])

    # The newer backup holds c3; the older one must not overwrite it.
    assert _ids(db) == {"c1", "c2", "c3"}
    # Only one ``.bak`` per target: the older copies were never installed.
    assert not list(db.parent.glob("*.pre-restore-*.bak"))


def test_restore_directory_reports_skipped_count(
    raw_root: Path,
    backups: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _book_db(raw_root)
    _seed(db, "c1", "c2", "c3")
    monkeypatch.setattr(storage, "_timestamp", lambda: "20260103T000000Z")
    _invoke(["storage", "backup", "--out", str(backups)])
    for name in ("chapter.raw.sqlite", "chapter.cleaner.sqlite"):
        (raw_root / "site" / "book" / name).unlink()

    _invoke(["storage", "restore", "--from", str(backups), "--json"])
    payload = _payload(capsys)

    assert payload["ok"] is True
    assert payload["skipped"] == 2  # raw + cleaner each had one older copy
    assert payload["restored"] == 2


def test_restore_directory_to_an_outside_root_is_rejected(
    raw_root: Path, backups: Path
) -> None:
    """A set restore may not write outside ``raw_data_dir``."""
    other = raw_root.parent / "other-raw"
    other.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backups), "--to", str(other)])
    assert exc_info.value.code == 2
    assert not list(other.iterdir())


def test_restore_directory_to_single_file_rejects_a_set(
    raw_root: Path, backups: Path
) -> None:
    target = raw_root / "site" / "book" / "chapter.raw.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backups),
                "--to",
                str(target),
                "--force",
            ]
        )
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    raw_root: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    before = db.read_bytes()

    _invoke(["storage", "restore", "--from", str(backups), "--dry-run", "--json"])
    payload = _payload(capsys)

    assert payload["dry_run"] is True
    assert payload["ok"] is True
    assert payload["restored"] == 0
    assert len(payload["files"]) == 2  # type: ignore[arg-type]
    assert db.read_bytes() == before
    assert not list(raw_root.rglob("*.pre-restore-*"))
    assert not list(raw_root.rglob(".*.tmp"))


def test_dry_run_without_force_still_plans(raw_root: Path, backups: Path) -> None:
    """A preview of an overwrite does not require ``--force``."""
    _invoke(["storage", "restore", "--from", str(backups), "--dry-run"])


def test_dry_run_still_rejects_a_corrupt_source(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "b" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a database")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(corrupt), "--dry-run", "--json"])
    assert exc_info.value.code == 1
    assert _payload(capsys)["error"] == "not_database"


# ---------------------------------------------------------------------------
# --force gate
# ---------------------------------------------------------------------------


def test_restore_refuses_to_overwrite_without_force(
    raw_root: Path, backups: Path
) -> None:
    db = _book_db(raw_root)
    backup = next(backups.rglob("*.raw.*"))

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backup)])
    assert exc_info.value.code == 2
    assert _ids(db) == {"c1", "c2"}
    assert not list(db.parent.glob("*.pre-restore-*"))


def test_force_replaces_and_preserves_the_original(
    raw_root: Path, backups: Path
) -> None:
    db = _book_db(raw_root)
    backup = next(backups.rglob("*.raw.*"))

    _invoke(["storage", "restore", "--from", str(backup), "--force"])

    saved = list(db.parent.glob("chapter.raw.sqlite.pre-restore-*.bak"))
    assert len(saved) == 1
    assert _ids(saved[0]) == {"c1", "c2"}


def test_yes_is_an_alias_for_force(raw_root: Path, backups: Path) -> None:
    db = _book_db(raw_root)
    backup = next(backups.rglob("*.raw.*"))

    _invoke(["storage", "restore", "--from", str(backup), "--yes"])

    assert list(db.parent.glob("*.pre-restore-*.bak"))


# ---------------------------------------------------------------------------
# path containment and symlinks
# ---------------------------------------------------------------------------


def test_restore_rejects_target_outside_raw_data(
    raw_root: Path, backups: Path, tmp_path: Path
) -> None:
    backup = next(backups.rglob("*.raw.*"))
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
                "--force",
            ]
        )
    assert exc_info.value.code == 2
    assert not outside.exists()


def test_restore_rejects_traversal_target(raw_root: Path, backups: Path) -> None:
    backup = next(backups.rglob("*.raw.*"))
    escaping = raw_root / "site" / ".." / ".." / "escaped" / "chapter.raw.sqlite"

    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(escaping),
                "--force",
            ]
        )
    assert exc_info.value.code == 2


def test_restore_rejects_a_symlinked_book_directory(
    raw_root: Path, backups: Path, tmp_path: Path
) -> None:
    """A link planted below ``raw_data`` must not redirect the write."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    book = raw_root / "site" / "linked"
    (raw_root / "site").mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, book, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating directory symlinks")

    backup = next(backups.rglob("*.raw.*"))
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(book / "chapter.raw.sqlite"),
                "--force",
            ]
        )
    assert exc_info.value.code == 2
    assert not list(outside.iterdir())


def test_restore_rejects_a_symlinked_target_file(
    raw_root: Path, backups: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "real.sqlite"
    _seed(outside, "outside")
    book = raw_root / "site" / "book"
    book.mkdir(parents=True, exist_ok=True)
    target = book / "chapter.raw.sqlite"
    if target.exists():
        target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating file symlinks")

    backup = next(backups.rglob("*.raw.*"))
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backup), "--force"])
    assert exc_info.value.code == 2
    assert _ids(outside) == {"outside"}


def test_restore_rejects_a_symlinked_target_without_os_privileges(
    raw_root: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symlink guard is wired into the plan, not only into the OS.

    Creating a real link needs privileges on Windows, so this case simulates
    the check on the target path instead: the refusal must happen before any
    write, even when the link could not be created on the test machine.
    """
    target = raw_root / "site" / "book" / "chapter.raw.sqlite"
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self == target:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    backup = next(backups.rglob("*.raw.*"))
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backup), "--force"])
    assert exc_info.value.code == 2
    assert _ids(target) == {"c1", "c2"}


def test_restore_refuses_a_source_inside_the_target(
    raw_root: Path, backups: Path
) -> None:
    """Restoring a backup directory onto itself must not recurse into it."""
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backups),
                "--to",
                str(backups),
                "--force",
            ]
        )
    assert exc_info.value.code == 2


def test_restore_refuses_a_file_onto_itself(raw_root: Path, backups: Path) -> None:
    backup = next(backups.rglob("*.raw.*"))
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            [
                "storage",
                "restore",
                "--from",
                str(backup),
                "--to",
                str(backup),
                "--force",
            ]
        )
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# source validation
# ---------------------------------------------------------------------------


def test_restore_rejects_a_corrupt_backup(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _book_db(raw_root)
    _seed(db, "keep")

    corrupt = tmp_path / "b" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not an sqlite database")

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(corrupt), "--json"])
    assert exc_info.value.code == 1
    payload = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"] == "not_database"
    assert _ids(db) == {"keep"}


def test_restore_rejects_a_newer_schema(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    newer = tmp_path / "b" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    newer.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(newer))
    try:
        conn.execute(
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(newer), "--json"])
    assert exc_info.value.code == 1
    assert _payload(capsys)["error"] == "schema_unsupported"


def test_no_verify_accepts_a_newer_schema(raw_root: Path, tmp_path: Path) -> None:
    """``--no-verify`` relaxes only the schema gate, not the integrity check."""
    newer = tmp_path / "b" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    newer.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(newer))
    try:
        conn.execute(
            "CREATE TABLE chapters (id TEXT PRIMARY KEY, title TEXT, content TEXT)"
        )
        conn.execute("INSERT INTO chapters VALUES ('x', 'T', 'B')")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    _invoke(["storage", "restore", "--from", str(newer), "--no-verify"])

    assert _book_db(raw_root).is_file()


def test_restore_rejects_a_foreign_database(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = tmp_path / "b" / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    foreign.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(foreign))
    try:
        conn.execute("CREATE TABLE cookies (value TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(foreign), "--json"])
    assert exc_info.value.code == 1
    assert _payload(capsys)["error"] == "schema_unsupported"


def test_restore_missing_source_is_a_json_failure(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(tmp_path / "nope"), "--json"])
    assert exc_info.value.code == 1
    payload = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"] == "source_missing"


def test_restore_empty_backup_directory(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(empty), "--json"])
    assert exc_info.value.code == 1
    assert _payload(capsys)["error"] == "no_backups"


# ---------------------------------------------------------------------------
# grouped rollback
# ---------------------------------------------------------------------------


def test_failure_mid_set_rolls_every_target_back(
    raw_root: Path,
    backups: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on the second target restores the first one too."""
    db = _book_db(raw_root)
    cleaner = raw_root / "site" / "book" / "chapter.cleaner.sqlite"
    original = db.read_bytes()

    real_write = storage._write_snapshot
    calls = {"n": 0}

    def failing_write(source: Path, target: Path) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated failure on the second target")
        return real_write(source, target)

    monkeypatch.setattr(storage, "_write_snapshot", failing_write)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backups), "--force"])
    assert exc_info.value.code == 1

    # Both targets are byte-identical to their pre-restore state.
    assert db.read_bytes() == original
    assert cleaner.is_file()
    assert not list(raw_root.rglob("*.pre-restore-*"))


def test_failure_before_any_write_leaves_targets_untouched(
    raw_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt file anywhere in the set aborts before the first write."""
    db = _book_db(raw_root)
    _seed(db, "c1")
    original = db.read_bytes()

    out = tmp_path / "backups"
    out.mkdir()
    corrupt = out / "site" / "book" / f"chapter.cleaner.{_STAMP}.sqlite"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"garbage")
    good = out / "site" / "book" / f"chapter.raw.{_STAMP}.sqlite"
    storage._write_snapshot(db, good)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(out), "--force"])
    assert exc_info.value.code == 1
    assert db.read_bytes() == original
    assert not list(raw_root.rglob("*.pre-restore-*"))


def test_keyboard_interrupt_rolls_the_group_back(
    raw_root: Path,
    backups: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C during the second target restores the first and re-raises."""
    db = _book_db(raw_root)
    original = db.read_bytes()

    real_write = storage._write_snapshot
    calls = {"n": 0}

    def interrupting_write(source: Path, target: Path) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_write(source, target)

    monkeypatch.setattr(storage, "_write_snapshot", interrupting_write)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "restore", "--from", str(backups), "--force"])

    assert db.read_bytes() == original
    assert not list(raw_root.rglob("*.pre-restore-*"))


def test_keyboard_interrupt_during_preserve_rolls_back(
    raw_root: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt while saving the old database also cleans up."""
    db = _book_db(raw_root)
    original = db.read_bytes()

    def interrupting_preserve(target: Path, stamp: str) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_preserve_existing", interrupting_preserve)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "restore", "--from", str(backups), "--force"])

    assert db.read_bytes() == original


# ---------------------------------------------------------------------------
# JSON contract
# ---------------------------------------------------------------------------


def test_json_failure_has_the_stable_shape(
    raw_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _invoke(["storage", "restore", "--from", str(tmp_path / "nope"), "--json"])

    payload = _payload(capsys)
    assert payload.keys() >= _REQUIRED_KEYS
    assert payload["ok"] is False
    assert payload["error"] in storage.RESTORE_ERROR_CATEGORIES
    assert isinstance(payload["message"], str) and payload["message"]
    assert payload["restored"] == 0
    assert payload["files"] == []


def test_usage_errors_do_not_emit_json(
    raw_root: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--force`` refusal is a usage error: exit 2 and empty stdout."""
    backup = next(backups.rglob("*.raw.*"))
    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "restore", "--from", str(backup), "--json"])
    assert exc_info.value.code == 2
    assert capsys.readouterr().out == ""


def test_json_dry_run_is_one_line(
    raw_root: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _invoke(["storage", "restore", "--from", str(backups), "--dry-run", "--json"])
    payload = _payload(capsys)
    assert payload["dry_run"] is True
    assert payload["restored"] == 0
    assert payload["error"] is None
