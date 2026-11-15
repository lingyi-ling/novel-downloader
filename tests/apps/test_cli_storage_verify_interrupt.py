#!/usr/bin/env python3
"""
tests.apps.test_cli_storage_verify_interrupt
--------------------------------------------

Interrupt and ``--json`` contract matrix for ``storage verify``.

``docs/guide/cli-examples.md`` (and the English edition) promises:

* a Ctrl+C during the walk prints a partial summary on stderr, then the CLI's
  usual ``Aborted.`` and exit code 1 -- never a traceback;
* ``--json`` still emits exactly one line on stdout, with ``ok: false``,
  ``interrupted: true`` and counters that cover only completed checks;
* the walk is strictly read-only: source bytes never change.

Every stage of ``_run_verify`` is exercised here by raising ``KeyboardInterrupt``
in-process (monkeypatched or injected through ``sys.settrace``); no real signal
is sent to the test process. The stages are: before discovery starts, during
discovery, at a database open, during the integrity check, during the
per-database progress line, and before/while the JSON report is serialized.

A partial report must stay internally consistent: ``corrupt`` always equals
``len(errors)``, and a database whose check did not return is never counted.

No network access happens and every database lives under a temporary
``raw_data`` root wired into ``storage._resolve_paths``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli import main as cli_main_module
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
    """Create a healthy chapter database (``ChapterStorage`` keeps it in WAL)."""
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
    """Create a database from raw SQL without ``ChapterStorage``.

    The connection uses SQLite's default DELETE journal mode, so a read-only
    open must not create any sidecar at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _write_healthy_delete_journal_db(path: Path) -> None:
    _write_sqlite(
        path,
        [
            "CREATE TABLE chapters ("
            "id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL)"
        ],
        version=SCHEMA_VERSION,
    )


def _write_foreign_db(path: Path) -> None:
    """A readable SQLite file that is not a chapter database."""
    _write_sqlite(path, ["CREATE TABLE unrelated (id INTEGER)"], version=SCHEMA_VERSION)


def _captured_json(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], str, str]:
    """Return ``(payload, stdout, stderr)`` for a one-line JSON report."""
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, captured.out
    assert captured.out.endswith("\n")
    return json.loads(captured.out), captured.out, captured.err


def _files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


def _capture(root: Path, databases: list[Path]) -> tuple[dict[Path, bytes], set[Path]]:
    """Snapshot the database bytes and every file under ``root``."""
    return {database: database.read_bytes() for database in databases}, _files_under(
        root
    )


def _assert_read_only_aftermath(
    root: Path,
    databases: list[Path],
    *,
    before: dict[Path, bytes],
    files_before: set[Path],
    allow_sidecars: bool = True,
) -> None:
    """No byte changed, no artifact appeared, and nothing was removed.

    A WAL-mode database opened read-only makes SQLite create an empty ``-wal``
    and a ``-shm``; that is intrinsic and allowed here as long as the WAL stays
    empty (a non-empty one would mean ``verify`` wrote to the database). A
    DELETE-journal database may not gain any file at all.
    """
    for database in databases:
        assert database.read_bytes() == before[database]

    current = _files_under(root)
    assert files_before <= current, files_before - current
    added = current - files_before
    if allow_sidecars:
        allowed = {
            Path(f"{database}{suffix}")
            for database in databases
            for suffix in ("-wal", "-shm")
        }
        assert added <= allowed, added - allowed
        for path in added:
            if path.name.endswith("-wal"):
                assert path.stat().st_size == 0
    else:
        assert added == set(), added

    for path in added:
        name = path.name
        assert ".tmp" not in name
        assert ".bak" not in name
        assert ".corrupt-" not in name
    assert not list(root.rglob("noveldl-snapshot-*"))


def _interrupt_on_call(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    attribute: str,
    nth: int,
) -> list[object]:
    """Patch ``target.attribute`` so the ``nth`` call raises ``KeyboardInterrupt``."""
    real = getattr(target, attribute)
    calls: list[object] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        calls.append(args[0] if args else kwargs.get("path"))
        if len(calls) == nth:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(target, attribute, wrapper)
    return calls


# ---------------------------------------------------------------------------
# mid-walk interrupt: counters and read-only guarantee
# ---------------------------------------------------------------------------


def test_verify_json_interrupt_mid_walk_reports_only_completed_checks(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 3rd of four databases is interrupted: 2 checks are reported."""
    databases = [
        _book_db(raw_root, "site-a", "book-1"),
        _book_db(raw_root, "site-b", "book-1"),
        _book_db(raw_root, "site-c", "book-1"),
        _book_db(raw_root, "site-d", "book-1"),
    ]
    _seed(databases[0], "c1")
    _write_foreign_db(databases[1])
    _seed(databases[2], "c3")
    _seed(databases[3], "c4")
    before, files_before = _capture(raw_root, databases)

    real_verify = storage._verify_database
    calls: list[Path] = []

    def interrupt_third(path: Path) -> storage._VerifyProblem | None:
        calls.append(path)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return real_verify(path)

    monkeypatch.setattr(storage, "_verify_database", interrupt_third)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    # The walk stops at the interrupted database: the fourth is never opened.
    assert calls == databases[:3]

    payload, _out, err = _captured_json(capsys)
    # ``--json`` keeps stderr silent even when the walk is interrupted.
    assert err == ""
    assert payload["ok"] is False
    assert payload["total"] == 4
    assert payload["healthy"] == 1
    assert payload["corrupt"] == 1
    assert payload["interrupted"] is True

    errors = payload["errors"]
    assert isinstance(errors, list) and len(errors) == 1
    assert errors[0]["path"] == storage._redact_path(databases[1])
    assert "chapters" in errors[0]["reason"].lower()

    # The interrupted and untouched databases are not counted; the counter
    # invariant holds for the partial report.
    assert payload["healthy"] + payload["corrupt"] == 2
    assert payload["corrupt"] == len(errors)

    _assert_read_only_aftermath(
        raw_root, databases, before=before, files_before=files_before
    )


def test_verify_human_interrupt_mid_walk_summarizes_without_json(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human mode prints the partial summary on stderr and no JSON at all."""
    first = _book_db(raw_root, "site-a", "book-1")
    corrupt = _book_db(raw_root, "site-b", "book-1")
    interrupted = _book_db(raw_root, "site-c", "book-1")
    untouched = _book_db(raw_root, "site-d", "book-1")
    _seed(first, "c1")
    _write_foreign_db(corrupt)
    _seed(interrupted, "c3")
    _seed(untouched, "c4")
    databases = [first, corrupt, interrupted, untouched]
    before, files_before = _capture(raw_root, databases)

    real_verify = storage._verify_database
    calls: list[Path] = []

    def interrupt_third(path: Path) -> storage._VerifyProblem | None:
        calls.append(path)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return real_verify(path)

    monkeypatch.setattr(storage, "_verify_database", interrupt_third)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        storage.t(
            "Interrupted after checking {checked} of {total} database(s): "
            "{healthy} healthy, {corrupt} corrupt."
        ).format(checked=2, total=4, healthy=1, corrupt=1)
        in captured.err
    )
    # The interrupted database was announced before it was opened, but only
    # the two completed checks produced a result line.
    assert "Checking 3/4" in captured.err
    assert captured.err.count("chapter.raw.sqlite: ok") == 1
    assert captured.err.count("chapter.raw.sqlite: corrupt:") == 1
    assert "No chapter databases found" not in captured.err

    _assert_read_only_aftermath(
        raw_root,
        databases,
        before=before,
        files_before=files_before,
    )


# ---------------------------------------------------------------------------
# interrupt stages inside the walk
# ---------------------------------------------------------------------------


def test_verify_json_interrupt_during_database_open(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C reaches the loop while the second database is being opened."""
    first = _book_db(raw_root, "site-a", "book-1")
    second = _book_db(raw_root, "site-b", "book-1")
    _seed(first, "c1")
    _seed(second, "c2")
    before, files_before = _capture(raw_root, [first, second])

    calls = _interrupt_on_call(monkeypatch, storage.sqlite3, "connect", 2)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    assert len(calls) == 2
    payload, _out, err = _captured_json(capsys)
    assert err == ""
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["corrupt"] == 0
    assert payload["errors"] == []
    assert payload["interrupted"] is True
    assert payload["ok"] is False

    _assert_read_only_aftermath(
        raw_root, [first, second], before=before, files_before=files_before
    )


def test_verify_json_interrupt_during_integrity_check(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C inside the integrity check must close the connection."""
    first = _book_db(raw_root, "site-a", "book-1")
    second = _book_db(raw_root, "site-b", "book-1")
    _seed(first, "c1")
    _seed(second, "c2")
    before, files_before = _capture(raw_root, [first, second])

    calls = _interrupt_on_call(monkeypatch, storage, "inspect_chapter_database", 2)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    assert len(calls) == 2
    payload, _out, err = _captured_json(capsys)
    assert err == ""
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["corrupt"] == 0
    assert payload["errors"] == []
    assert payload["interrupted"] is True

    _assert_read_only_aftermath(
        raw_root, [first, second], before=before, files_before=files_before
    )
    # The read-only context manager closes the connection on the way out; a
    # leaked handle would keep the file locked on Windows.
    second.unlink()
    assert not second.exists()


def test_verify_json_interrupt_during_discovery(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt before the first open still emits the one JSON line."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")

    def interrupted(_root: Path, **_kwargs: object) -> list[Path]:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_find_chapter_databases", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    payload, _out, err = _captured_json(capsys)
    assert err == ""
    assert payload == {
        "ok": False,
        "total": 0,
        "healthy": 0,
        "corrupt": 0,
        "errors": [],
        "interrupted": True,
    }


def test_verify_human_interrupt_during_discovery_is_not_an_empty_library(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovery interrupt must not report "nothing to verify"."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")

    def interrupted(_root: Path, **_kwargs: object) -> list[Path]:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_find_chapter_databases", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Scanning for chapter databases..." in captured.err
    assert (
        storage.t(
            "Interrupted after checking {checked} of {total} database(s): "
            "{healthy} healthy, {corrupt} corrupt."
        ).format(checked=0, total=0, healthy=0, corrupt=0)
        in captured.err
    )
    assert "No chapter databases found" not in captured.err


# ---------------------------------------------------------------------------
# interrupt around the JSON report itself
# ---------------------------------------------------------------------------


def test_verify_json_interrupt_before_serialization_prints_nothing(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C before the report is built leaves stdout empty, never half-JSON."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")
    _seed(_book_db(raw_root, "site-b", "book-1"), "c2")

    def interrupted(*_args: object, **_kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.json, "dumps", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _executable_lines() -> list[int]:
    """Line numbers of ``_run_verify`` that carry bytecode."""
    return sorted(
        {
            lineno
            for _start, _end, lineno in storage._run_verify.__code__.co_lines()
            if lineno is not None
        }
    )


def _run_with_interrupt_at_line(root: Path, lineno: int) -> bool:
    """Run ``_run_verify`` raising ``KeyboardInterrupt`` at ``lineno``.

    :return: Whether the line was reached and the interrupt was injected.
    """
    code = storage._run_verify.__code__
    state = {"fired": False}
    previous = sys.gettrace()

    def trace(frame: Any, event: str, arg: Any) -> Any:
        if getattr(frame, "f_code", None) is not code:
            return None
        return local

    def local(frame: Any, event: str, arg: Any) -> Any:
        if (
            event == "line"
            and getattr(frame, "f_lineno", None) == lineno
            and not state["fired"]
        ):
            state["fired"] = True
            raise KeyboardInterrupt
        return local

    sys.settrace(trace)
    try:
        storage._run_verify(root, as_json=True)
    except KeyboardInterrupt:
        pass
    finally:
        sys.settrace(previous)
    return state["fired"]


def test_verify_json_interrupt_at_every_stage_keeps_report_consistent(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Interrupt at each executable line: report is absent or consistent."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")
    _seed(_book_db(raw_root, "site-b", "book-1"), "c2")

    reported = 0
    suppressed = 0
    for lineno in _executable_lines():
        fired = _run_with_interrupt_at_line(raw_root, lineno)
        captured = capsys.readouterr()
        if not fired:
            # The line was not reached in ``--json`` mode (an interactive-only
            # branch); the run completed with its normal report.
            assert captured.out.count("\n") == 1
            continue

        assert captured.err == ""
        if not captured.out:
            suppressed += 1
            continue

        assert captured.out.count("\n") == 1
        payload = json.loads(captured.out)
        assert payload["corrupt"] == len(payload["errors"])
        assert payload["healthy"] + payload["corrupt"] <= payload["total"]
        if payload.get("interrupted") is True:
            assert payload["ok"] is False
            reported += 1
        else:
            # The interrupt landed after the complete, healthy report had
            # already been written; that report must stay a clean success.
            assert "interrupted" not in payload
            assert payload["ok"] is True
            assert payload["corrupt"] == 0

    # Both halves of the contract are exercised: interrupts that land before
    # the report is built print nothing, later ones still print one line.
    assert suppressed > 0
    assert reported > 0


# ---------------------------------------------------------------------------
# CLI boundary: "Aborted." and exit code 1, never a traceback
# ---------------------------------------------------------------------------


def test_cli_storage_verify_interrupt_exits_one_without_traceback(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli_main`` turns the mid-walk interrupt into ``Aborted.`` + exit 1."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")
    _seed(_book_db(raw_root, "site-b", "book-1"), "c2")

    def interrupted(_path: Path) -> storage._VerifyProblem | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_verify_database", interrupted)
    monkeypatch.setattr(sys, "argv", ["novel-cli", "storage", "verify", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        cli_main_module.cli_main()

    assert exc_info.value.code == 1
    payload, out, err = _captured_json(capsys)
    assert payload["interrupted"] is True
    assert payload["ok"] is False
    assert payload["healthy"] == 0
    assert "Aborted." in err
    assert "Traceback" not in err
    assert out.count("\n") == 1


def test_cli_storage_verify_interrupt_before_scan_exits_one_without_traceback(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C during configuration resolution also ends as exit 1."""

    def interrupted(**_kwargs: object) -> dict[str, Path]:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_resolve_paths", interrupted)
    monkeypatch.setattr(sys, "argv", ["novel-cli", "storage", "verify", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        cli_main_module.cli_main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Aborted." in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("attribute", ["dumps", "emit"])
def test_cli_storage_verify_interrupt_at_report_write_exits_one(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
) -> None:
    """An interrupt while serializing/writing the report is still exit 1."""
    _seed(_book_db(raw_root, "site-a", "book-1"), "c1")
    target = storage.json if attribute == "dumps" else storage.ui

    def interrupted(*_args: object, **_kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(target, attribute, interrupted)
    monkeypatch.setattr(sys, "argv", ["novel-cli", "storage", "verify", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        cli_main_module.cli_main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # No partial line: the payload is a single write, so an interrupted write
    # either lands whole or not at all.
    assert captured.out == ""
    assert "Aborted." in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# read-only guarantee on interrupt
# ---------------------------------------------------------------------------


def test_verify_interrupt_creates_no_sidecars_for_delete_journal_database(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WAL-less database must gain no file at all when the walk is cut short."""
    database = _book_db(raw_root)
    _write_healthy_delete_journal_db(database)
    before, files_before = _capture(raw_root, [database])
    assert files_before == {database}

    def interrupted(_path: Path) -> storage._VerifyProblem | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_verify_database", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _invoke(["storage", "verify", "--json"])

    _assert_read_only_aftermath(
        raw_root,
        [database],
        before=before,
        files_before=files_before,
        allow_sidecars=False,
    )


# ---------------------------------------------------------------------------
# non-interrupt failures keep the same JSON contract
# ---------------------------------------------------------------------------


def test_verify_json_non_database_file_reports_one_line_and_exits_one(
    raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _book_db(raw_root)
    database.parent.mkdir(parents=True)
    original = b"this is not an sqlite database\n" * 64
    database.write_bytes(original)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify", "--json"])

    assert exc_info.value.code == 1
    payload, _out, err = _captured_json(capsys)
    assert err == ""
    assert payload["ok"] is False
    assert payload["total"] == 1
    assert payload["healthy"] == 0
    assert payload["corrupt"] == 1
    assert "interrupted" not in payload
    errors = payload["errors"]
    assert isinstance(errors, list) and len(errors) == 1
    assert errors[0]["path"] == storage._redact_path(database)
    assert "not a database" in errors[0]["reason"].lower()
    assert database.read_bytes() == original


def test_verify_json_unreadable_file_reports_one_line_and_exits_one(
    raw_root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _book_db(raw_root)
    _seed(database, "c1")
    before = database.read_bytes()

    def deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage.sqlite3, "connect", deny)

    with pytest.raises(SystemExit) as exc_info:
        _invoke(["storage", "verify", "--json"])

    assert exc_info.value.code == 1
    payload, _out, err = _captured_json(capsys)
    assert err == ""
    assert payload["ok"] is False
    assert payload["total"] == 1
    assert payload["healthy"] == 0
    assert payload["corrupt"] == 1
    assert "interrupted" not in payload
    assert payload["corrupt"] == len(payload["errors"])
    assert "permission denied" in payload["errors"][0]["reason"].lower()
    assert database.read_bytes() == before
