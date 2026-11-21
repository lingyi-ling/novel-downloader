#!/usr/bin/env python3
"""
tests.apps.test_storage_concurrent_backup
-----------------------------------------

Cross-process tests for ``storage backup`` writing into one ``--out``.

The command serializes its claim -> publish -> verify -> prune sequence with a
per-destination lock. These tests start real child processes (a thread-level
test cannot exercise ``msvcrt.locking``/``flock``) and assert that every source
ends up with exactly one complete backup, that ``--keep`` keeps its promise,
and that leftovers from a killed run are swept without touching real data.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE

CLI_MODULE = "novel_downloader.apps.cli.main"

#: ``chapter.<stage>.<UTC>[.<n>].sqlite`` as produced by ``storage backup``.
_BACKUP_RE = re.compile(r"^(chapter\.[a-z0-9_]+\.\d{8}T\d{6}Z(?:\.\d+)?\.sqlite)$")


def _child_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["NOVEL_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NOVEL_LANG"] = "en_US"
    return env


def _seed(path: Path, chapters: tuple[str, ...] = ("c1", "c2")) -> None:
    """Create a chapter database at ``path`` with a couple of rows."""
    from novel_downloader.infra.persistence.chapter_storage import ChapterStorage

    path.parent.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(path.parent, path.name) as store:
        store.upsert_chapters(
            [
                {"id": cid, "title": f"T {cid}", "content": f"body {cid}", "extra": {}}
                for cid in chapters
            ]
        )


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    (work / "settings.toml").write_bytes(DEFAULT_CONFIG_FILE.read_bytes())
    return work, home


def _start(work: Path, home: Path, out: Path, *extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            CLI_MODULE,
            "storage",
            "backup",
            "--out",
            str(out),
            "--json",
            *extra,
        ],
        cwd=str(work),
        env=_child_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )


def _finish(proc: subprocess.Popen[str]) -> str:
    stdout, stderr = proc.communicate(timeout=240)
    assert proc.returncode == 0, stderr
    assert stdout.count("\n") == 1, stdout
    return stdout


def _backup_paths(out: Path) -> list[Path]:
    return sorted(
        path for path in out.rglob("chapter.*.sqlite") if _BACKUP_RE.match(path.name)
    )


def _groups(paths: list[Path], out: Path) -> dict[tuple[str, str, str], list[Path]]:
    """Group backups by ``(site, book, stage)`` using their relative location."""
    grouped: dict[tuple[str, str, str], list[Path]] = {}
    for path in paths:
        rel = path.relative_to(out)
        site, book = rel.parts[0], rel.parts[1]
        stage = path.name.split(".")[1]
        grouped.setdefault((site, book, stage), []).append(path)
    return grouped


def _row_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("select count(*) from chapters").fetchone()[0])


def test_two_processes_sharing_out_keep_one_copy_per_source(tmp_path: Path) -> None:
    """Parallel runs publish complete sets, and ``--keep 1`` converges to one."""
    work, home = _workspace(tmp_path)
    raw = work / "raw_data"
    out = tmp_path / "backups"
    out.mkdir()
    _seed(raw / "siteA" / "book1" / "chapter.raw.sqlite")
    _seed(raw / "siteA" / "book2" / "chapter.raw.sqlite", ("x",))
    _seed(raw / "siteB" / "book3" / "chapter.raw.sqlite")

    procs = [_start(work, home, out, "--keep", "1") for _ in range(3)]
    for proc in procs:
        _finish(proc)

    paths = _backup_paths(out)
    groups = _groups(paths, out)
    assert set(groups) == {
        ("siteA", "book1", "raw"),
        ("siteA", "book2", "raw"),
        ("siteB", "book3", "raw"),
    }
    # Three runs into the same UTC second cannot overwrite each other: either
    # the second-resolution names land in differenct seconds, or the sequence
    # suffix claimed with O_EXCL keeps them apart.
    for group in groups.values():
        assert group, "a source lost its backup"
    # ``--keep 1`` is the last phase of every run, so exactly one survives.
    assert all(len(group) == 1 for group in groups.values()), groups

    assert not list(out.glob("*.tmp"))
    assert not list(out.glob(".*.tmp"))
    assert not (out / ".storage-backup.lock").exists()
    for path in paths:
        assert _row_count(path) > 0


def test_parallel_runs_without_keep_publish_every_source(tmp_path: Path) -> None:
    """Without retention, each run may add copies but never a partial file."""
    work, home = _workspace(tmp_path)
    raw = work / "raw_data"
    out = tmp_path / "backups"
    out.mkdir()
    _seed(raw / "site" / "book" / "chapter.raw.sqlite")

    procs = [_start(work, home, out) for _ in range(4)]
    for proc in procs:
        _finish(proc)

    paths = _backup_paths(out)
    assert len(paths) >= 1
    for path in paths:
        assert _row_count(path) == 2
    assert not list(out.glob("*.tmp"))


def test_stale_artifacts_from_a_killed_run_are_swept(tmp_path: Path) -> None:
    """An empty name claim and a half-written ``.tmp`` copy are garbage."""
    work, home = _workspace(tmp_path)
    raw = work / "raw_data"
    out = tmp_path / "backups"
    out.mkdir()
    _seed(raw / "site" / "book" / "chapter.raw.sqlite")

    book_dir = out / "site" / "book"
    book_dir.mkdir(parents=True)
    stale_claim = book_dir / "chapter.raw.20200101T000000Z.sqlite"
    stale_claim.write_bytes(b"")
    stale_tmp = book_dir / ".chapter.raw.20200101T000000Z.sqlite.deadbeef.tmp"
    stale_tmp.write_bytes(b"partial")
    real_backup = book_dir / "chapter.raw.20200101T000000Z.1.sqlite"
    shutil.copyfile(raw / "site" / "book" / "chapter.raw.sqlite", real_backup)
    unrelated = out / "notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    _finish(_start(work, home, out))

    assert not stale_claim.exists()
    assert not stale_tmp.exists()
    assert real_backup.exists(), "a backup with content must never be swept"
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_second_run_does_not_reuse_the_first_run_files(tmp_path: Path) -> None:
    """Same-second runs claim ``.1``/``.2`` instead of overwriting."""
    work, home = _workspace(tmp_path)
    raw = work / "raw_data"
    out = tmp_path / "backups"
    out.mkdir()
    _seed(raw / "site" / "book" / "chapter.raw.sqlite")

    _finish(_start(work, home, out))
    first = _backup_paths(out)
    _finish(_start(work, home, out))
    second = _backup_paths(out)

    assert set(first) <= set(second)
    assert len(second) >= 2
    for path in second:
        assert _row_count(path) == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
