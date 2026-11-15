#!/usr/bin/env python3
"""
tests.apps.test_storage_backup_fuzz
-----------------------------------

Seeded, randomized robustness tests for ``storage backup``.

The handwritten matrix in ``test_cli_storage_matrix`` walks a fixed grid of
shapes.  This module instead derives every scenario from a fixed seed, so a
failure is reproducible by rerunning the same parameter.  Each case builds a
multi-database library and then exercises one of the contracts that make a
backup run trustworthy:

* a failure or Ctrl+C in the middle of ``_write_snapshot`` / ``_verify_database``
  leaves no file from that run behind (main file, ``-wal``/``-shm`` sidecars or
  temporary files), never damages an existing backup, and a retry succeeds;
* a pre-existing backup occupying the same-second name is avoided by claiming
  the next free ``.<n>`` suffix, and ``--keep`` pruning only ever removes files
  that this command names, never the fresh set, never a foreign file;
* a post-write verification failure (a deliberately corrupted target or a
  mocked reason) rolls the whole set back;
* retention ``--dry-run`` reports candidates without deleting anything.

Every database lives under a temporary ``raw_data`` root wired into
``storage._resolve_paths``; no network access happens and nothing outside
``tmp_path`` is touched.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from novel_downloader.apps.cli.commands import storage
from novel_downloader.apps.cli.commands.storage import StorageCmd
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

#: A stable timestamp so every case can predict the backup names.
_STAMP = "20260102T030405Z"

#: Old stamps used for pre-existing backups; none matches :data:`_STAMP`.
_OLD_STAMPS = (
    "20240101T120000Z",
    "20250101T000000Z",
    "20250101T000001Z",
    "20250630T235959Z",
    "29991231T235959Z",
)

_STAGES = ("raw", "cleaner")

#: Independent copy of the published backup-name grammar.  Deliberately not
#: imported from the module under test so a change there is caught here.
_BACKUP_NAME_RE = re.compile(
    r"^chapter\.(?P<stage>[^./\\]+)\.(?P<stamp>\d{8}T\d{6}Z)"
    r"(?:\.(?P<seq>\d+))?\.sqlite$"
)

#: Failure modes injected into ``_write_snapshot`` / ``_verify_database``.
_FAILURE_MODES = (
    "write_raise",
    "write_after_publish_raise",
    "write_interrupt",
    "verify_reason",
    "verify_interrupt",
    "write_then_corrupt",
)

_FAILURE_SEEDS = tuple(range(30))
_RETENTION_SEEDS = tuple(range(12))


# ---------------------------------------------------------------------------
# invocation and library helpers
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    StorageCmd.register(subparsers)
    return parser


def _invoke(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


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


@dataclass(frozen=True)
class SourceSpec:
    """One chapter database that a backup run must copy."""

    site: str
    book: str
    stage: str
    chapters: tuple[str, ...]

    def relative(self) -> Path:
        return Path(self.site) / self.book / f"chapter.{self.stage}.sqlite"


@dataclass(frozen=True)
class ExistingBackup:
    """A backup that existed before the run, with its exact bytes."""

    source: SourceSpec
    path: Path
    data: bytes
    stamp: str
    seq: int


def _build_library(rng: random.Random) -> list[SourceSpec]:
    """Return a random 2-4 book library, some books holding two stages."""
    specs: list[SourceSpec] = []
    for index in range(rng.randrange(2, 5)):
        site = rng.choice(("site-a", "site-b"))
        book = f"book-{index:02d}"
        stages = [rng.choice(_STAGES)]
        if rng.random() < 0.4:
            stages.append("cleaner" if stages[0] == "raw" else "raw")
        for stage in stages:
            count = rng.randrange(1, 4)
            chapters = tuple(f"{book}-{stage}-c{n}" for n in range(count))
            specs.append(
                SourceSpec(site=site, book=book, stage=stage, chapters=chapters)
            )
    return specs


def _seed(path: Path, chapter_ids: tuple[str, ...]) -> None:
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


def _materialise(root: Path, specs: list[SourceSpec]) -> None:
    for spec in specs:
        _seed(root / spec.relative(), spec.chapters)


def _ids(path: Path) -> set[str]:
    """Read chapter ids without letting ChapterStorage migrate the file."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT id FROM chapters")}
    finally:
        conn.close()


def _source_bytes(path: Path) -> dict[str, bytes]:
    """Bytes of the files a backup must never rewrite (main + WAL)."""
    result = {str(path): path.read_bytes()}
    wal = Path(f"{path}-wal")
    if wal.is_file():
        result[str(wal)] = wal.read_bytes()
    return result


def _assert_source_unchanged(path: Path, before: dict[str, bytes]) -> None:
    """Every stored byte survives; an empty read-time sidecar is not a write."""
    after = _source_bytes(path)
    for key, expected in before.items():
        assert after.get(key) == expected, key
    for key in set(after) - set(before):
        assert Path(key).name.endswith(("-wal", "-shm")), key


def _spec_by_source(raw_root: Path, specs: list[SourceSpec]) -> dict[Path, SourceSpec]:
    return {(raw_root / spec.relative()).resolve(): spec for spec in specs}


# ---------------------------------------------------------------------------
# output-tree helpers
# ---------------------------------------------------------------------------


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Map every file below ``root`` (relative path) to its bytes."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _backup_match(path: Path) -> re.Match[str] | None:
    return _BACKUP_NAME_RE.match(path.name)


def _files_added_since(out: Path, before: dict[str, bytes]) -> list[Path]:
    """Files that did not exist in the pre-run snapshot."""
    if not out.exists():
        return []
    return sorted(
        path
        for path in out.rglob("*")
        if path.is_file() and str(path.relative_to(out)) not in before
    )


def _leaked_temp_files(out: Path) -> list[Path]:
    return sorted(out.rglob(".*.tmp")) if out.exists() else []


def _existing_backup_path(
    out: Path, spec: SourceSpec, stamp: str, seq: int | None
) -> Path:
    suffix = "" if seq is None else f".{seq}"
    return out / spec.relative().parent / f"chapter.{spec.stage}.{stamp}{suffix}.sqlite"


def _create_existing_backups(
    rng: random.Random,
    out: Path,
    specs: list[SourceSpec],
    *,
    dense_source: SourceSpec | None = None,
    force_same_stamp: tuple[SourceSpec, ...] = (),
) -> list[ExistingBackup]:
    """Create random backups, including same-second names that must be avoided.

    ``dense_source`` gets four distinct old backups plus a same-second copy so
    a ``--keep`` run always has something to prune.  Every source named in
    ``force_same_stamp`` gets a same-second base name, which forces the run to
    claim a ``.<n>`` sibling instead of overwriting it.
    """
    bucket: list[ExistingBackup] = []

    def add(spec: SourceSpec, stamp: str, seq: int | None) -> None:
        path = _existing_backup_path(out, spec, stamp, seq)
        if any(entry.path == path for entry in bucket):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"existing:{spec.site}:{spec.book}:{spec.stage}:{stamp}:{seq}".encode()
        path.write_bytes(data)
        bucket.append(
            ExistingBackup(
                source=spec,
                path=path,
                data=data,
                stamp=stamp,
                seq=0 if seq is None else seq,
            )
        )

    for spec in specs:
        if spec is dense_source:
            for stamp in _OLD_STAMPS[:4]:
                add(spec, stamp, None)
            continue
        for _ in range(rng.randrange(0, 3)):
            add(spec, rng.choice(_OLD_STAMPS), rng.choice([None, 1, 2]))

    for spec in force_same_stamp:
        add(spec, _STAMP, None)
        if rng.random() < 0.5:
            add(spec, _STAMP, 1)
    if dense_source is not None:
        add(dense_source, _STAMP, None)
        if rng.random() < 0.5:
            add(dense_source, _STAMP, 1)

    # At least one same-second collision in every case.
    if not any(entry.stamp == _STAMP for entry in bucket):
        add(rng.choice(specs), _STAMP, None)
    return bucket


def _create_foreign_files(out: Path, specs: list[SourceSpec]) -> list[Path]:
    """Files and a directory that look close to backups but must never be pruned."""
    book_dir = out / specs[0].relative().parent
    book_dir.mkdir(parents=True, exist_ok=True)
    files = [
        book_dir / "notes.txt",
        book_dir / "chapter.raw.20240101T120000Z.sqlite.partial",
        book_dir / "chapter.raw.notatimestamp.sqlite.bak",
    ]
    for path in files:
        path.write_bytes(b"not a backup")
    # A directory whose name matches the backup grammar is still not a file.
    matching_dir = book_dir / "chapter.other.20300101T000000Z.sqlite"
    matching_dir.mkdir(exist_ok=True)
    inside = matching_dir / "inside.txt"
    inside.write_bytes(b"keep")
    return [*files, matching_dir, inside]


def _expected_removed(
    universe: set[Path],
    fresh: set[Path],
    keep: int,
) -> set[Path]:
    """Reference model of the ``--keep`` policy: newest N per book and stage."""
    if keep <= 0:
        return set()
    groups: dict[tuple[Path, str], list[tuple[str, int, Path]]] = {}
    for path in universe:
        match = _backup_match(path)
        assert match is not None, path
        groups.setdefault((path.parent, match.group("stage")), []).append(
            (match.group("stamp"), int(match.group("seq") or 0), path)
        )

    doomed: set[Path] = set()
    for entries in groups.values():
        entries.sort(
            key=lambda entry: (entry[0], entry[1], str(entry[2])), reverse=True
        )
        doomed.update(
            path for _stamp, _seq, path in entries[keep:] if path not in fresh
        )
    return doomed


# ---------------------------------------------------------------------------
# mid-run failure / interrupt fuzz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _FAILURE_SEEDS)
def test_random_failure_or_interrupt_rolls_back_the_run(
    seed: int,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure on any source leaves no partial set and the retry succeeds."""
    rng = random.Random(10_000 + seed)
    specs = _build_library(rng)
    _materialise(raw_root, specs)

    sources = storage._find_chapter_databases(raw_root)
    assert len(sources) >= 2
    spec_by_source = _spec_by_source(raw_root, specs)
    # Never the first source: at least one copy has already been published.
    failing_source = sources[rng.randrange(1, len(sources))]
    failing_spec = spec_by_source[failing_source.resolve()]

    out = tmp_path / "backups"
    _create_existing_backups(
        rng,
        out,
        specs,
        force_same_stamp=(failing_spec,),
    )
    before = _tree_bytes(out)
    sources_before = {
        (raw_root / spec.relative()): _source_bytes(raw_root / spec.relative())
        for spec in specs
    }
    mode = _FAILURE_MODES[seed % len(_FAILURE_MODES)]
    # A failed run must never reach retention, so even a pruning request leaves
    # every pre-existing backup in place.
    keep = rng.choice([0, 1, 2, 3])

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    real_write = storage._write_snapshot
    real_verify = storage._verify_database
    failing_target: Path | None = None

    def tracking_write(source: Path, target: Path):
        nonlocal failing_target
        if Path(source).resolve() == failing_source.resolve():
            failing_target = target
            if mode == "write_raise":
                raise OSError("simulated mid-run write failure")
            if mode == "write_interrupt":
                raise KeyboardInterrupt
        info = real_write(source, target)
        if failing_target == target and mode == "write_after_publish_raise":
            raise RuntimeError("simulated failure after the copy was published")
        if failing_target == target and mode == "write_then_corrupt":
            target.write_bytes(b"corrupted by the fuzz harness")
        return info

    def tracking_verify(target: Path) -> str | None:
        if failing_target is not None and target == failing_target:
            if mode == "verify_reason":
                return "simulated verification failure"
            if mode == "verify_interrupt":
                raise KeyboardInterrupt
        return real_verify(target)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_write_snapshot", tracking_write)
        patch.setattr(storage, "_verify_database", tracking_verify)
        if mode in {"write_interrupt", "verify_interrupt"}:
            with pytest.raises(KeyboardInterrupt):
                _invoke(
                    [
                        "storage",
                        "backup",
                        "--out",
                        str(out),
                        "--keep",
                        str(keep),
                        "--json",
                    ]
                )
        else:
            with pytest.raises(SystemExit) as exc_info:
                _invoke(
                    [
                        "storage",
                        "backup",
                        "--out",
                        str(out),
                        "--keep",
                        str(keep),
                        "--json",
                    ]
                )
            assert exc_info.value.code == 1

    captured = capsys.readouterr()
    if mode in {"verify_reason", "write_then_corrupt"}:
        assert captured.out.count("\n") == 1
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["verification_failed"] is True
        assert payload["count"] == 0
        assert payload["files"] == []
        assert payload["retention"]["removed"] == []
        assert payload["retention"]["candidates"] == []
        error = payload["verification_error"]
        assert Path(error["source"]).resolve() == failing_source.resolve()
        assert error["reason"]
        assert "Backup verification failed" in captured.err
    elif mode in {"write_raise", "write_after_publish_raise"}:
        # Every runtime failure emits the unified ``--json`` line; the human
        # explanation still goes to stderr.
        assert captured.out.count("\n") == 1
        failure = json.loads(captured.out)
        assert failure["ok"] is False
        assert failure["error"] == (
            "io_error" if mode == "write_raise" else "internal_error"
        )
        assert failure["count"] == 0
        assert failure["files"] == []
        assert "Failed to back up" in captured.err
    else:
        assert captured.out == ""
        assert captured.err == ""

    # No file from this run survives -- not the published copy, not the empty
    # reservation, not a sidecar or temporary file -- and every byte that
    # existed before is untouched.
    assert _tree_bytes(out) == before
    assert _files_added_since(out, before) == []
    assert _leaked_temp_files(out) == []
    for source, data in sources_before.items():
        _assert_source_unchanged(source, data)
        assert _ids(source) == set(spec_by_source[source.resolve()].chapters)

    # The same clock now claims the names the failed run released.
    _invoke(["storage", "backup", "--out", str(out), "--keep", "0", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["verification_failed"] is False
    assert payload["count"] == len(specs)
    files = payload["files"]
    assert {Path(entry["source"]).resolve() for entry in files} == set(spec_by_source)
    added = _files_added_since(out, before)
    assert len(added) == len(specs)
    assert all(_STAMP in path.name for path in added)
    for entry in files:
        target = Path(entry["target"])
        spec = spec_by_source[Path(entry["source"]).resolve()]
        assert entry["verified"] is True
        assert _ids(target) == set(spec.chapters)
    # ``--keep 0`` disables pruning, so no pre-existing backup disappears.
    for relative, data in before.items():
        assert (out / relative).read_bytes() == data
    assert _leaked_temp_files(out) == []


def test_corrupt_published_target_is_removed_during_rollback(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: a corrupt target must not survive the rollback.

    ``_source_connection`` used to close the SQLite handle only for
    ``OperationalError``.  A non-database file raises ``DatabaseError``, so
    ``_verify_database`` returned the reason while the handle stayed open; on
    Windows the rollback unlink then failed and the corrupt copy remained.
    """
    db_a = raw_root / "site" / "book-a" / "chapter.raw.sqlite"
    db_b = raw_root / "site" / "book-b" / "chapter.raw.sqlite"
    _seed(db_a, ("a1",))
    _seed(db_b, ("b1",))

    out = tmp_path / "backups"
    old = out / "site" / "book-a" / f"chapter.raw.{_OLD_STAMPS[1]}.sqlite"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"existing backup")

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    real_write = storage._write_snapshot

    def corrupt_second(source: Path, target: Path):
        info = real_write(source, target)
        if Path(source).resolve() == db_b.resolve():
            target.write_bytes(b"not a sqlite database")
        return info

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_write_snapshot", corrupt_second)
        with pytest.raises(SystemExit) as exc_info:
            _invoke(["storage", "backup", "--out", str(out), "--json"])
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["verification_failed"] is True
    failed_target = Path(payload["verification_error"]["target"])
    assert failed_target.parent.name == "book-b"
    assert failed_target.name.startswith(f"chapter.raw.{_STAMP}")
    assert [path.name for path in out.rglob("*") if path.is_file()] == [old.name]
    assert old.read_bytes() == b"existing backup"
    assert _ids(db_a) == {"a1"}
    assert _ids(db_b) == {"b1"}
    assert _leaked_temp_files(out) == []

    # The retry writes a verified set once the injected corruption is gone.
    _invoke(["storage", "backup", "--out", str(out), "--keep", "0", "--json"])
    retry = json.loads(capsys.readouterr().out)
    assert retry["ok"] is True
    assert retry["count"] == 2
    assert all(entry["verified"] is True for entry in retry["files"])


# ---------------------------------------------------------------------------
# same-second collision and --keep retention fuzz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _RETENTION_SEEDS)
def test_random_existing_backups_are_avoided_and_retained(
    seed: int,
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sequence avoidance and pruning follow the documented policy exactly."""
    rng = random.Random(20_000 + seed)
    specs = _build_library(rng)
    _materialise(raw_root, specs)
    spec_by_source = _spec_by_source(raw_root, specs)

    out = tmp_path / "backups"
    existing = _create_existing_backups(rng, out, specs, dense_source=specs[0])
    foreign = _create_foreign_files(out, specs)
    keep = rng.choice([0, 1, 2, 3, 5])

    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)
    _invoke(["storage", "backup", "--out", str(out), "--keep", str(keep), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == len(specs)
    assert payload["verification_failed"] is False
    files = payload["files"]
    assert {Path(entry["source"]).resolve() for entry in files} == set(spec_by_source)

    fresh = {Path(entry["target"]).resolve() for entry in files}
    for entry in files:
        target = Path(entry["target"]).resolve()
        spec = spec_by_source[Path(entry["source"]).resolve()]
        assert entry["verified"] is True
        assert _ids(target) == set(spec.chapters)

        # The fresh copy must occupy the first free suffix for this group:
        # every pre-existing same-second name is left where it was.
        occupied = {
            backup.seq
            for backup in existing
            if backup.source is spec
            and backup.stamp == _STAMP
            and backup.path.parent.resolve() == target.parent
        }
        free = 0
        while free in occupied:
            free += 1
        expected = (
            f"chapter.{spec.stage}.{_STAMP}.sqlite"
            if free == 0
            else f"chapter.{spec.stage}.{_STAMP}.{free}.sqlite"
        )
        assert target.name == expected
        assert target.parent == (out / spec.relative().parent).resolve()

    # A pre-existing same-second name is either untouched byte-for-byte or,
    # when older than the retained window, removed by --keep.  It is never
    # overwritten: the fresh copy claimed a different suffix.
    for backup in existing:
        if backup.stamp == _STAMP and backup.path.exists():
            assert backup.path.read_bytes() == backup.data

    present = {
        path.resolve()
        for path in out.rglob("*")
        if path.is_file() and _backup_match(path) is not None
    }
    removed = {Path(path).resolve() for path in payload["retention"]["removed"]}
    universe = present | removed
    assert universe == {backup.path.resolve() for backup in existing} | fresh

    expected_removed = _expected_removed(universe, fresh, keep)
    assert removed == expected_removed
    assert present == universe - expected_removed
    assert removed <= {backup.path.resolve() for backup in existing}
    assert not fresh & expected_removed

    for backup in existing:
        if backup.path.resolve() in expected_removed:
            assert not backup.path.exists()
        else:
            assert backup.path.read_bytes() == backup.data
    for path in foreign:
        assert path.exists(), f"{path} must never be pruned"
    assert _leaked_temp_files(out) == []


# ---------------------------------------------------------------------------
# retention dry run
# ---------------------------------------------------------------------------


def test_retention_dry_run_deletes_nothing(
    raw_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` reports candidates but never removes an existing backup.

    The new backup is still written: that is the documented contract of
    ``storage backup --dry-run`` (the flag controls retention only).
    """
    rng = random.Random(30_000)
    specs = _build_library(rng)
    _materialise(raw_root, specs)
    spec_by_source = _spec_by_source(raw_root, specs)

    out = tmp_path / "backups"
    existing = _create_existing_backups(rng, out, specs, dense_source=specs[0])
    foreign = _create_foreign_files(out, specs)

    def forbidden_unlink(self: Path, *_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"retention dry run deleted {self}")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    monkeypatch.setattr(storage, "_timestamp", lambda: _STAMP)

    _invoke(
        ["storage", "backup", "--out", str(out), "--keep", "1", "--dry-run", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == len(specs)
    retention = payload["retention"]
    assert retention["dry_run"] is True
    assert retention["removed"] == []
    assert retention["failed"] == []
    assert retention["candidates"]

    fresh = {Path(entry["target"]).resolve() for entry in payload["files"]}
    for entry in payload["files"]:
        spec = spec_by_source[Path(entry["source"]).resolve()]
        assert _ids(Path(entry["target"])) == set(spec.chapters)

    universe = {backup.path.resolve() for backup in existing} | fresh
    expected = _expected_removed(universe, fresh, keep=1)
    assert {Path(path).resolve() for path in retention["candidates"]} == expected
    for backup in existing:
        assert backup.path.read_bytes() == backup.data
    for path in foreign:
        assert path.exists()
    assert _leaked_temp_files(out) == []
