#!/usr/bin/env python3
"""
tests.apps.test_storage_case_collisions
---------------------------------------

``storage backup`` on filesystems that fold case (the Windows and macOS
defaults).

The command mirrors the ``<site>/<book>`` layout and names its output
``chapter.<stage>.<UTC>[.<n>].sqlite``.  On a case-insensitive volume two
spellings of one name are one file, so every place that compares, groups or
claims a path must stop treating the spellings as distinct.  These tests cover
the four such places:

* :func:`storage._reserve_backup_target` -- a case variant already occupying a
  name must push the claim to the next ``.<n>`` sequence instead of
  overwriting it;
* :func:`storage._run_backup` -- source books whose directories differ only by
  case must not overwrite each other's backup;
* the ``--out`` destination -- a case variant of the raw-data root must not be
  mistaken for a directory inside it;
* :func:`storage._plan_retention` -- ``--keep`` must not count one file twice
  nor delete the copy the current run just published.

Extension matching (``.sqlite`` / ``.SQLITE``) is deliberately strict: only
the exact spelling this command itself writes is recognised by discovery,
restore and retention, so a foreign case variant is never deleted.  The tests
pin that one behaviour across all three consumers.

Platform note: ``pathlib`` folds case in path comparisons on Windows but not on
POSIX, and ``os.path.normcase`` is the identity function on POSIX even though
the default macOS volume is case-insensitive.  ``storage`` therefore cannot
rely on either alone.  :func:`test_case_folding_contract_of_path_primitives`
records the primitive behaviour, and the retention protect set is folded
explicitly (see :func:`test_keep_protects_the_fresh_backup_by_spelling`).
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

#: The timestamp the current run publishes under.
_STAMP = "20260102T030405Z"

#: A clock that went backwards: older than the backups a keep run must prune.
_SKEWED_STAMP = "20230101T000000Z"

#: Two pre-existing backups, oldest first.
_OLD_STAMP = "20250101T000000Z"
_OLD_NEXT_STAMP = "20250101T000001Z"


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


def _case_insensitive(directory: Path) -> bool:
    """True when ``directory``'s volume folds case in file lookups."""
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return os.path.lexists(directory / "caseprobe")
    finally:
        probe.rmdir()


def _backup_files(out: Path) -> list[Path]:
    """Every file ``storage backup`` itself names below ``out``."""
    return sorted(
        path
        for path in out.rglob("*")
        if path.is_file() and storage._BACKUP_NAME_RE.match(path.name)
    )


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    assert captured.out.endswith("\n")
    return json.loads(captured.out)


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


# ---------------------------------------------------------------------------
# claiming a backup name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_upper", [False, True], ids=["lower", "upper"])
def test_reserve_backup_target_steps_over_a_case_variant(
    request_upper: bool, tmp_path: Path
) -> None:
    """A name differing only by case already owns the base path.

    On a case-insensitive volume the existing file is the same inode, so the
    claim must move to ``.<n>`` rather than truncate it.  On a case-sensitive
    volume the two spellings are genuinely different files and the base name is
    free; either way the reserved path must never be the file holding the
    previous backup's bytes.
    """
    book_dir = tmp_path / "site" / "book"
    book_dir.mkdir(parents=True)
    stamp = _STAMP
    if request_upper:
        existing_name = f"chapter.raw.{stamp}.sqlite"
        requested = f"CHAPTER.RAW.{stamp}.SQLITE"
    else:
        existing_name = f"CHAPTER.RAW.{stamp}.SQLITE"
        requested = f"chapter.raw.{stamp}.sqlite"
    existing = book_dir / existing_name
    existing.write_bytes(b"keep me")

    reserved = storage._reserve_backup_target(book_dir / requested)

    assert not os.path.samefile(reserved, existing)
    assert reserved.is_file()
    assert reserved.stat().st_size == 0  # an empty claim, not the old bytes
    assert existing.read_bytes() == b"keep me"

    expected = Path(requested)
    if _case_insensitive(book_dir):
        expected = expected.with_name(f"{expected.stem}.1{expected.suffix}")
    assert reserved.name == expected.name


def test_reserve_backup_target_continues_past_variant_and_sequence(
    tmp_path: Path,
) -> None:
    """``.<n>`` skipping counts a case variant as an occupied name."""
    book_dir = tmp_path / "site" / "book"
    book_dir.mkdir(parents=True)
    (book_dir / f"CHAPTER.RAW.{_STAMP}.SQLITE").write_bytes(b"base")
    (book_dir / f"chapter.raw.{_STAMP}.1.sqlite").write_bytes(b"first")

    reserved = storage._reserve_backup_target(book_dir / f"chapter.raw.{_STAMP}.sqlite")

    for occupied in (
        book_dir / f"CHAPTER.RAW.{_STAMP}.SQLITE",
        book_dir / f"chapter.raw.{_STAMP}.1.sqlite",
    ):
        assert not os.path.samefile(reserved, occupied)
    if _case_insensitive(book_dir):
        assert reserved.name == f"chapter.raw.{_STAMP}.2.sqlite"
    else:
        # Distinct files: the base name was free all along.
        assert reserved.name == f"chapter.raw.{_STAMP}.sqlite"
        assert not os.path.samefile(reserved, book_dir / f"CHAPTER.RAW.{_STAMP}.SQLITE")


def test_uppercase_source_alias_is_discovered_once(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``chapter.raw.sqlite`` and ``CHAPTER.RAW.SQLITE`` are one database.

    Discovery must not hand the same physical file to the planner twice (which
    would publish a spurious ``.1`` copy), and the strict name regex keeps the
    alias out of the source set on a case-sensitive volume as well.
    """
    db = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed(db, "c1")

    assert storage._find_chapter_databases(raw_root) == [db]

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    out = tmp_path / "backups"
    storage._run_backup(raw_root, out, as_json=True)

    payload = _payload(capsys)
    assert payload["count"] == 1
    assert len(_backup_files(out)) == 1
    assert _ids(_backup_files(out)[0]) == {"c1"}


# ---------------------------------------------------------------------------
# source trees that differ only by case
# ---------------------------------------------------------------------------


def test_case_variant_books_get_distinct_targets(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``SITE/Book`` and ``site/book`` never publish over each other.

    On a case-sensitive source tree the two directories are distinct and this
    is a plain two-book backup.  On a case-insensitive volume they are one
    directory, so the simulated discovery returns the same physical database
    under both spellings; the targets then differ only by case and the claim
    must sequence the second one instead of replacing the first.
    """
    upper = raw_root / "SITE" / "Book" / "chapter.raw.sqlite"
    _seed(upper, "upper")
    lower = raw_root / "site" / "book" / "chapter.raw.sqlite"
    case_insensitive = _case_insensitive(raw_root)
    if not case_insensitive:
        _seed(lower, "lower")

    def discovery(root: Path, *, skip: Path | None = None) -> list[Path]:
        return [upper, lower]

    monkeypatch.setattr(storage, "_find_chapter_databases", discovery)
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    out = tmp_path / "backups"
    storage._run_backup(raw_root, out, as_json=True)

    payload = _payload(capsys)
    assert payload["ok"] is True
    assert payload["count"] == 2

    targets = [Path(entry["target"]) for entry in payload["files"]]
    assert len(targets) == 2
    assert not os.path.samefile(targets[0], targets[1])
    for target in targets:
        assert target.is_file()
        assert storage._BACKUP_NAME_RE.match(target.name) is not None
    ids = [_ids(target) for target in targets]
    expected = [{"upper"}, {"upper"}] if case_insensitive else [{"upper"}, {"lower"}]
    assert ids == expected


@pytest.mark.skipif(
    os.name == "nt", reason="NTFS folds case; case-variant dirs are one dir"
)
def test_case_variant_source_directories_on_case_sensitive_fs(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With real case-sensitive directories both books are backed up intact."""
    if _case_insensitive(raw_root):  # pragma: no cover - macOS default volume
        pytest.skip("volume folds case; covered by the simulated discovery test")

    upper = raw_root / "SITE" / "Book" / "chapter.raw.sqlite"
    lower = raw_root / "site" / "book" / "chapter.raw.sqlite"
    _seed(upper, "upper")
    _seed(lower, "lower")

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    out = tmp_path / "backups"
    storage._run_backup(raw_root, out, as_json=True)
    payload = _payload(capsys)

    assert payload["count"] == 2
    by_parent = {Path(entry["target"]).parent: entry for entry in payload["files"]}
    assert set(by_parent) == {out / "SITE" / "Book", out / "site" / "book"}
    assert _ids(Path(by_parent[out / "SITE" / "Book"]["target"])) == {"upper"}
    assert _ids(Path(by_parent[out / "site" / "book"]["target"])) == {"lower"}


# ---------------------------------------------------------------------------
# --out paths that differ only by case
# ---------------------------------------------------------------------------


def test_out_case_variant_of_source_root_is_not_treated_as_nested(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--out RAW`` beside ``raw`` must not be mistaken for a subdirectory.

    The destination equals the raw-data root on a case-insensitive volume and
    is an unrelated directory on a case-sensitive one; in neither case may the
    source database be skipped as "inside the backup destination".
    """
    _seed(raw_root / "site" / "book" / "chapter.raw.sqlite", "c1")
    out = tmp_path / raw_root.name.upper()  # tmp_path/RAW

    captured: dict[str, Path | None] = {}
    real = storage._find_chapter_databases

    def spy(root: Path, *, skip: Path | None = None) -> list[Path]:
        captured["skip"] = skip
        return real(root, skip=skip)

    monkeypatch.setattr(storage, "_find_chapter_databases", spy)
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    storage._run_backup(raw_root, out, as_json=True)

    payload = _payload(capsys)
    assert captured["skip"] is None
    assert payload["count"] == 1
    assert _ids(_backup_files(out)[0]) == {"c1"}

    # A second run must not pick the first run's timestamped copy as a source.
    storage._run_backup(raw_root, out, as_json=True)
    assert _payload(capsys)["count"] == 1


# ---------------------------------------------------------------------------
# extension spelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "matches"),
    [
        ("chapter.raw.sqlite", True),
        ("CHAPTER.RAW.SQLITE", False),
        ("chapter.raw.SQLITE", False),
        ("Chapter.Raw.Sqlite", False),
    ],
)
def test_chapter_database_name_matching_is_exactly_lowercase(
    name: str, matches: bool
) -> None:
    assert (storage._CHAPTER_DB_RE.match(name) is not None) is matches


@pytest.mark.parametrize(
    ("name", "matches"),
    [
        (f"chapter.raw.{_STAMP}.sqlite", True),
        (f"chapter.raw.{_STAMP}.1.sqlite", True),
        (f"CHAPTER.RAW.{_STAMP}.SQLITE", False),
        (f"chapter.raw.{_STAMP}.SQLITE", False),
        (f"chapter.raw.{_STAMP}.1.SQLITE", False),
    ],
)
def test_backup_name_matching_is_exactly_lowercase(name: str, matches: bool) -> None:
    assert (storage._BACKUP_NAME_RE.match(name) is not None) is matches


def test_uppercase_extensions_are_ignored_by_every_consumer(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Discovery, restore and retention agree: only the written spelling counts.

    Recognising the upper-case variant in one place and not another would let
    a restore miss a file retention happily deletes.  The strict spelling is
    the conservative choice: a foreign case variant is never deleted.
    """
    _seed(raw_root / "site" / "book" / "chapter.raw.sqlite", "c1")
    _seed(raw_root / "SITE" / "BOOK" / "CHAPTER.CLEANER.SQLITE", "uppercase")

    assert storage._find_chapter_databases(raw_root) == [
        raw_root / "site" / "book" / "chapter.raw.sqlite"
    ]

    backup_src = tmp_path / "backup_src" / "site" / "book"
    backup_src.mkdir(parents=True)
    (backup_src / f"CHAPTER.RAW.{_OLD_STAMP}.SQLITE").write_bytes(b"foreign")
    assert storage._find_backup_sources(tmp_path / "backup_src") == []

    out = tmp_path / "backups"
    book_dir = out / "site" / "book"
    book_dir.mkdir(parents=True)
    foreign = book_dir / f"CHAPTER.RAW.{_SKEWED_STAMP}.SQLITE"
    foreign.write_bytes(b"foreign")
    old = book_dir / f"chapter.raw.{_OLD_STAMP}.sqlite"
    old.write_bytes(b"old")

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    storage._run_backup(raw_root, out, as_json=True, keep=1)
    payload = _payload(capsys)

    assert payload["retention"]["removed"] == [str(old.resolve())]
    assert foreign.is_file()


# ---------------------------------------------------------------------------
# --keep retention under case folding
# ---------------------------------------------------------------------------


def test_keep_protects_the_fresh_backup_by_spelling(tmp_path: Path) -> None:
    """A fresh backup is never pruned because its path is spelled differently.

    ``_run_backup`` protects the path it just wrote; on a case-insensitive
    volume whose ``resolve()`` preserves the caller's spelling (macOS) the walk
    reports the directory under the stored spelling instead.  The protect check
    must fold case, and the skewed clock puts the fresh copy outside the kept
    window so the protection is what saves it.
    """
    book_dir = tmp_path / "backups" / "SITE" / "BOOK"
    book_dir.mkdir(parents=True)
    fresh = book_dir / f"chapter.raw.{_SKEWED_STAMP}.sqlite"
    fresh.write_bytes(b"fresh")
    old = book_dir / f"chapter.raw.{_OLD_STAMP}.sqlite"
    old.write_bytes(b"old")

    failures: list[tuple[Path, str]] = []
    protect = {Path(str(fresh).upper())}
    doomed = storage._plan_retention(
        tmp_path / "backups", 1, protect=protect, failures=failures
    )

    assert doomed == []
    assert failures == []


def test_keep_neither_double_counts_nor_prunes_across_case(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rotation over a differently-spelled destination keeps the fresh copy.

    The run publishes into ``<out>/site/book`` while the destination already
    holds ``<out>/SITE/BOOK``.  On a case-insensitive volume both name one
    directory: the fresh copy (older than the history, by design of the skewed
    clock) must survive and exactly the one expiry candidate is removed.  On a
    case-sensitive volume they are two directories and each group is pruned on
    its own.
    """
    _seed(raw_root / "site" / "book" / "chapter.raw.sqlite", "c1")
    out = tmp_path / "backups"
    stored = out / "SITE" / "BOOK"
    stored.mkdir(parents=True)
    older = stored / f"chapter.raw.{_OLD_STAMP}.sqlite"
    older.write_bytes(b"old-1")
    newer = stored / f"chapter.raw.{_OLD_NEXT_STAMP}.sqlite"
    newer.write_bytes(b"old-2")

    monkeypatch.setattr(storage, "_timestamp", lambda: _SKEWED_STAMP)
    storage._run_backup(raw_root, out, as_json=True, keep=1)
    payload = _payload(capsys)

    fresh = out / "site" / "book" / f"chapter.raw.{_SKEWED_STAMP}.sqlite"
    assert fresh.is_file()
    assert _ids(fresh) == {"c1"}
    assert [Path(path) for path in payload["retention"]["removed"]] == [older]
    assert newer.is_file()
    assert not older.exists()
    expected_files = 2 if _case_insensitive(out) else 3
    assert len(_backup_files(out)) == expected_files


# ---------------------------------------------------------------------------
# the primitives the implementation relies on
# ---------------------------------------------------------------------------


def test_case_folding_contract_of_path_primitives(tmp_path: Path) -> None:
    """Record what ``pathlib``/``normcase``/``resolve`` fold on this platform.

    Windows paths compare case-insensitively and ``resolve`` canonicalises the
    stored spelling of existing components.  POSIX paths compare
    case-sensitively and ``os.path.normcase`` is the identity function even
    when the volume folds case (the default macOS filesystem), which is why
    ``storage`` folds the retention protect set itself instead of relying on
    ``normcase``.
    """
    probe = tmp_path / "Book"
    probe.mkdir()
    variant = tmp_path / "book"

    if _case_insensitive(tmp_path):
        assert variant.exists()
        assert os.path.samefile(probe, variant)
    else:
        assert not variant.exists()

    if os.name == "nt":
        assert Path("C:/A/B") == Path("c:/a/b")
        assert Path("C:/A/B/chapter.raw.sqlite").is_relative_to(Path("c:/a/b"))
        assert Path(str(probe).upper()).resolve() == probe.resolve()
    else:
        assert Path("/A/B") != Path("/a/b")
        assert not Path("/A/B/x").is_relative_to(Path("/a/b"))
        assert os.path.normcase("/A/B") == "/A/B"
