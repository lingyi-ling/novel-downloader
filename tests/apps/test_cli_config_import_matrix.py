#!/usr/bin/env python3
"""
tests.apps.test_cli_config_import_matrix
----------------------------------------

Atomicity / failure-safety matrix for ``novel-cli config import``.

The single invariant this module pins: **a failed import leaves the active
configuration byte-for-byte unchanged**.  Every scenario below therefore
captures the target bytes before the run and compares them afterwards, and
also checks that no temporary file, lock file or half-written ``.bak`` is left
behind.

Covered scenarios
-----------------

* success: the imported document is applied verbatim, read back through the
  real loader, and the previous active file is preserved as ``.bak`` (a second
  import refreshes it rather than keeping the first generation);
* rejected sources: TOML syntax error, empty file, a valid TOML document with
  no recognised root table, a JSON-shaped file, wrong value types, invalid
  UTF-8, an oversized file, and (with ``general.strict_config = true``) an
  unknown processor option;
* advisory unknown keys: an unknown *nested* key is a validator warning, not
  an import error -- the same policy the loader uses, so a configuration the
  application can load can always be re-imported;
* BOM and CRLF sources are accepted (BOM stripped, line endings preserved);
* write-stage failures: the atomic writer raises, ``os.replace`` fails for the
  target (after the backup was taken), the backup copy itself fails, and
  ``fsync`` fails mid-copy -- the target stays intact, an existing ``.bak`` is
  never clobbered by a failed run, and no ``*.tmp`` remains;
* concurrent imports of different files finish with one complete document on
  disk (never an interleaved or truncated one). The implementation has no
  lock; safety comes from same-directory temp files plus ``os.replace``, so
  the test also asserts no lock artifact appears. On Windows a concurrent
  reader can make another process' ``os.replace`` fail with a sharing
  violation (``PermissionError``); that is a clean exit 1 with the target
  untouched, which this test tolerates while still requiring one complete
  file;
* ``--json`` emits exactly one parsed line on stdout for both outcomes;
* ``--from`` directories and dangling symlinks are rejected without writing.
  A symlink to a regular TOML file is *read through* today (``is_file``
  follows links, matching the loader, which resolves symlinks too); the test
  pins that the link and its target are never modified;
* ``config export`` keeps its version/timestamp header and ``--force``
  backup behaviour.

Deliberate non-scenario: "top-level is not a table" is unreachable for this
command.  ``config import`` always parses the source as TOML, and every valid
TOML document decodes to a table.  The closest reachable inputs are a
JSON-shaped file (fails as invalid TOML) and a table with no known root
section (fails the root-table check); both are covered.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.cli.commands import config
from novel_downloader.apps.cli.commands.config import ConfigCmd, ConfigImportCmd
from novel_downloader.infra.config import load_config, validate_config

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

#: Keys of a successful ``config import --json`` document.
_IMPORT_OK_KEYS = frozenset({"command", "path", "source", "status", "backup"})

#: Keys of a failed ``config import --json`` document.
_IMPORT_ERROR_KEYS = _IMPORT_OK_KEYS | {"error"}

#: UTF-8 byte order mark.
_BOM = b"\xef\xbb\xbf"


class _Messages:
    """Records messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []

    def all_text(self) -> str:
        return "\n".join([*self.info, *self.success, *self.warn, *self.error])


def _capture_ui(monkeypatch: pytest.MonkeyPatch) -> _Messages:
    messages = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(config.ui, name, getattr(messages, name).append)
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ConfigCmd.register(subparsers)
    return parser


def _run(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    args.func(args)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run in an empty work directory with an empty application home."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NOVEL_HOME", str(home))
    return work


def _config_text(workers: int = 3, *, extra: str = "", secret: bool = False) -> str:
    """A minimal configuration whose only value under test is ``workers``."""
    lines = ["[general]", f"workers = {workers}"]
    if secret:
        lines.append('proxy = "http://user:supersecret@127.0.0.1:7890"')
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"


def _write_source(tmp_path: Path, name: str, payload: str | bytes) -> Path:
    sources = tmp_path / "sources"
    sources.mkdir(exist_ok=True)
    path = sources / name
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_bytes(payload.encode("utf-8"))
    return path


def _json_document(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    """Assert stdout is exactly one JSON line; return it and all streams text."""
    captured = capsys.readouterr()
    out = captured.out
    assert out.endswith("\n"), f"JSON document is not newline-terminated: {out!r}"
    assert out.count("\n") == 1, f"JSON document spans lines: {out!r}"
    payload = json.loads(out)
    assert isinstance(payload, dict)
    return payload, out + captured.err


def _residual_files(root: Path) -> set[Path]:
    """Temp/lock artifacts the atomic writers must never leave behind."""
    return {
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith((".tmp", ".lock"))
    }


def _expect_exit(argv: list[str], code: int) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(argv)
    assert excinfo.value.code == code


def _assert_import_error(
    payload: dict[str, Any],
    *,
    target: Path,
    source: Path,
    fragment: str,
) -> None:
    assert set(payload) == _IMPORT_ERROR_KEYS
    assert payload["command"] == "config import"
    assert payload["path"] == str(target)
    assert payload["source"] == str(source)
    assert payload["status"] == "error"
    assert payload["backup"] is None
    assert isinstance(payload["error"], str)
    assert fragment in payload["error"], payload["error"]


# ---------------------------------------------------------------------------
# Success: applied config + backup lifecycle
# ---------------------------------------------------------------------------


def test_import_success_applies_config_and_refreshes_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=2, secret=True).encode("utf-8")
    target.write_bytes(original)
    first = _write_source(tmp_path, "first.toml", _config_text(workers=7))
    second = _write_source(tmp_path, "second.toml", _config_text(workers=9))
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(first), "--json"])

    payload, streams = _json_document(capsys)
    assert set(payload) == _IMPORT_OK_KEYS
    assert payload["status"] == "imported"
    assert payload["path"] == str(target)
    assert payload["source"] == str(first)
    assert payload["backup"] == str(backup)
    # The document carries paths only, never configuration values.
    assert "supersecret" not in streams

    assert target.read_bytes() == first.read_bytes()
    assert backup.read_bytes() == original
    # The imported value is what the real loader now sees.
    assert load_config()["general"]["workers"] == 7

    # A second import replaces the backup with the *previous* generation.
    _run(["config", "import", "--from", str(second), "--json"])

    payload, streams = _json_document(capsys)
    assert payload["status"] == "imported"
    assert payload["backup"] == str(backup)
    assert "supersecret" not in streams
    assert target.read_bytes() == second.read_bytes()
    assert backup.read_bytes() == first.read_bytes()
    assert load_config()["general"]["workers"] == 9


def test_import_creates_new_config_without_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    source = _write_source(tmp_path, "new.toml", _config_text(workers=5))
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload, _ = _json_document(capsys)
    assert set(payload) == _IMPORT_OK_KEYS
    assert payload["status"] == "imported"
    assert payload["backup"] is None
    assert (work / "settings.toml").read_bytes() == source.read_bytes()
    assert not (work / "settings.toml.bak").exists()
    assert not _residual_files(work)


@pytest.mark.parametrize(
    ("case_id", "source_bytes"),
    [
        ("utf8-bom", _BOM + b"[general]\nworkers = 6\n"),
        ("crlf", b"[general]\r\nworkers = 6\r\n"),
    ],
)
def test_import_accepts_bom_and_crlf_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_id: str,
    source_bytes: bytes,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.write_bytes(_config_text(workers=1).encode("utf-8"))
    source = _write_source(tmp_path, f"{case_id}.toml", source_bytes)
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload, streams = _json_document(capsys)
    assert payload["status"] == "imported"
    assert payload["backup"] == str(work / "settings.toml.bak")
    assert "supersecret" not in streams
    # The BOM is stripped; CRLF is preserved verbatim.
    assert target.read_bytes() == source_bytes.removeprefix(_BOM)
    assert tomllib.loads(target.read_bytes().decode("utf-8"))["general"] == {
        "workers": 6
    }


def test_import_accepts_an_unknown_nested_key_with_a_warning_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown keys are advisory, exactly like the loader's own validation.

    Import only rejects ``error``-level findings, so a configuration the
    application can load remains restorable from a backup.  ``strict_config``
    is the documented opt-in that makes unknown processor options fatal (see
    the dedicated strict test below).
    """
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.write_bytes(_config_text(workers=1).encode("utf-8"))
    source = _write_source(
        tmp_path, "unknown-key.toml", _config_text(workers=4, extra="frobnicate = 1")
    )
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload, _ = _json_document(capsys)
    assert payload["status"] == "imported"
    assert target.read_bytes() == source.read_bytes()
    assert load_config()["general"]["workers"] == 4

    problems = validate_config(source)
    assert [problem for problem in problems if problem.level == "error"] == []
    assert any(
        problem.level == "warning" and "unknown configuration key" in problem.message
        for problem in problems
    )


# ---------------------------------------------------------------------------
# Rejected sources: exit 1, one-line JSON, active config byte-identical
# ---------------------------------------------------------------------------

#: Sources the importer must reject.  Each one carries a credential-looking
#: line before the fault so the tests can assert it never reaches a message.
_BAD_SOURCES: tuple[tuple[str, bytes, str], ...] = (
    (
        "invalid-toml",
        b'proxy = "http://user:supersecret@127.0.0.1:7890"\n[general\nworkers = 1\n',
        "Invalid TOML",
    ),
    ("empty", b"", "Not a novel-downloader configuration"),
    # Valid TOML whose root table has no recognised section.
    (
        "no-known-root-table",
        b"only_scalar = 1\n",
        "Not a novel-downloader configuration",
    ),
    (
        "unknown-root-table",
        b"[tool.other]\nvalue = 1\n",
        "Not a novel-downloader configuration",
    ),
    # A TOML root is always a table; JSON-shaped input dies at the parser.
    ("json-object", b'{"general": {"workers": 2}}', "Invalid TOML"),
    ("json-array", b"[1, 2, 3]\n", "Invalid TOML"),
    (
        "wrong-type",
        b'[general]\nworkers = "many"\nproxy = "http://u:supersecret@h:1"\n',
        "general.workers",
    ),
    (
        "invalid-utf8",
        b'[general]\nproxy = "\xff\xfe"\n',
        "not valid UTF-8",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "source_bytes", "fragment"),
    _BAD_SOURCES,
    ids=[row[0] for row in _BAD_SOURCES],
)
def test_import_rejects_bad_source_without_touching_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_id: str,
    source_bytes: bytes,
    fragment: str,
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=2, secret=True).encode("utf-8")
    target.write_bytes(original)
    previous_backup = b"# backup from an earlier import\n"
    backup.write_bytes(previous_backup)
    source = _write_source(tmp_path, f"{case_id}.toml", source_bytes)
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, streams = _json_document(capsys)
    _assert_import_error(payload, target=target, source=source, fragment=fragment)
    assert "supersecret" not in streams
    assert target.read_bytes() == original
    # A rejected import must not consume the previous generation of backup.
    assert backup.read_bytes() == previous_backup
    assert not _residual_files(work)


def test_import_rejects_an_oversized_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    # Lower the cap instead of writing an 8 MiB fixture: same code path.
    monkeypatch.setattr(config, "MAX_CONFIG_FILE_BYTES", 512)
    source_bytes = _config_text(workers=3).encode("utf-8") + b"\n#" + b"x" * 700 + b"\n"
    assert len(source_bytes) > 512
    source = _write_source(tmp_path, "oversized.toml", source_bytes)
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(payload, target=target, source=source, fragment="too large")
    assert target.read_bytes() == original
    assert not (work / "settings.toml.bak").exists()
    assert not _residual_files(work)


def test_import_rejects_a_strict_unknown_processor_option(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``general.strict_config = true`` turns the warning into a hard error."""
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source_text = (
        "[general]\n"
        "workers = 2\n"
        "strict_config = true\n"
        "\n"
        "[[general.processors]]\n"
        'name = "cleaner"\n'
        "bogus_option = 1\n"
    )
    source = _write_source(tmp_path, "strict-unknown.toml", source_text)
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload,
        target=target,
        source=source,
        fragment="Configuration validation failed",
    )
    assert "bogus_option" in payload["error"]
    assert target.read_bytes() == original
    assert not (work / "settings.toml.bak").exists()


def test_import_missing_from_is_a_usage_error_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--json"], 2)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert not (work / "settings.toml").exists()
    assert not _residual_files(work)


# ---------------------------------------------------------------------------
# Write-stage failure
# ---------------------------------------------------------------------------


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError("simulated I/O failure")


def test_import_write_failure_keeps_config_and_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The backup is taken first; a failing writer must leave both files."""
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source = _write_source(tmp_path, "new.toml", _config_text(workers=8))
    _capture_ui(monkeypatch)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config, "_write_bytes_atomic", _boom)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(payload, target=target, source=source, fragment="disk full")
    assert target.read_bytes() == original
    # The backup written by this run is retained, not rolled back.
    assert backup.read_bytes() == original
    assert not _residual_files(work)


def test_import_rename_failure_keeps_config_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real atomic writer fails at ``os.replace``; no ``.tmp`` survives."""
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source = _write_source(tmp_path, "new.toml", _config_text(workers=8))
    _capture_ui(monkeypatch)

    real_replace = config.os.replace

    def _replace(src: object, dst: object) -> None:
        if Path(str(dst)) == target:
            raise PermissionError("locked by another process")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(config.os, "replace", _replace)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="locked by another process"
    )
    assert target.read_bytes() == original
    assert backup.read_bytes() == original
    assert not _residual_files(work)


def test_import_backup_failure_preserves_the_previous_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    older = b"# older backup that must survive a failed import\n"
    backup.write_bytes(older)
    source = _write_source(tmp_path, "new.toml", _config_text(workers=8))
    _capture_ui(monkeypatch)

    monkeypatch.setattr(config, "_copy_file_atomic", _raise_oserror)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="simulated I/O failure"
    )
    assert target.read_bytes() == original
    assert backup.read_bytes() == older
    assert not _residual_files(work)


def test_import_fsync_failure_during_backup_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source = _write_source(tmp_path, "new.toml", _config_text(workers=8))
    _capture_ui(monkeypatch)

    monkeypatch.setattr(config.os, "fsync", _raise_oserror)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="simulated I/O failure"
    )
    assert target.read_bytes() == original
    assert not (work / "settings.toml.bak").exists()
    assert not _residual_files(work)


def test_import_write_failure_does_not_create_a_new_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    source = _write_source(tmp_path, "new.toml", _config_text(workers=8))
    _capture_ui(monkeypatch)

    monkeypatch.setattr(config, "_write_bytes_atomic", _raise_oserror)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="simulated I/O failure"
    )
    assert not target.exists()
    assert not (work / "settings.toml.bak").exists()
    assert not _residual_files(work)


# ---------------------------------------------------------------------------
# Concurrent imports: one complete document, never an interleaved one
# ---------------------------------------------------------------------------

_CONCURRENT_THREADS = 8
_CONCURRENT_ROUNDS = 3


def _import_in_thread(
    source: Path,
    outcomes: list[tuple[str, int | str]],
) -> None:
    args = argparse.Namespace(source=str(source), as_json=False)
    try:
        ConfigImportCmd.run(args)
    except SystemExit as exc:
        outcomes.append(("exit", int(exc.code or 0)))
    except BaseException as exc:  # pragma: no cover - a crash is a test failure
        outcomes.append(("crash", type(exc).__name__))
    else:
        outcomes.append(("ok", 0))


def test_concurrent_imports_leave_one_complete_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two or more imports racing must never interleave or truncate the file.

    There is no lock in the implementation: each write goes to a unique
    same-directory temp file and is published with ``os.replace``.  The test
    asserts that publication property (target equals exactly one source), that
    no lock artifact is created, and that every non-success is a clean exit 1
    rather than a partial file.  Off Windows (or when no reader happens to
    overlap a replace) all imports succeed; on Windows a concurrent reader can
    make ``os.replace`` fail with a sharing violation, which is exactly the
    clean-abort path the assertion allows.
    """
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    backup = work / "settings.toml.bak"
    original = _config_text(workers=1).encode("utf-8")
    target.write_bytes(original)
    _capture_ui(monkeypatch)
    sources_dir = tmp_path / "parallel"
    sources_dir.mkdir()

    known_documents: set[bytes] = {original}
    successes = 0
    for round_index in range(_CONCURRENT_ROUNDS):
        sources: list[tuple[Path, bytes]] = []
        for index in range(_CONCURRENT_THREADS):
            text = (
                _config_text(workers=1000 + index) + f"# round-{round_index}-{index}\n"
            )
            path = sources_dir / f"round-{round_index}-{index}.toml"
            path.write_bytes(text.encode("utf-8"))
            sources.append((path, path.read_bytes()))
        known_documents.update(document for _, document in sources)

        # Force every thread past validation before any of them writes, so the
        # backup copies and the replace calls genuinely overlap.
        start = threading.Barrier(_CONCURRENT_THREADS, timeout=60)
        validated = threading.Barrier(_CONCURRENT_THREADS, timeout=60)
        real_read = config._read_toml_config

        def _gated_read(
            path: Path,
            start: threading.Barrier = start,
            validated: threading.Barrier = validated,
            read: Callable[[Path], tuple[str, dict[str, Any]]] = real_read,
        ) -> tuple[str, dict[str, Any]]:
            start.wait()
            result = read(path)
            validated.wait()
            return result

        monkeypatch.setattr(config, "_read_toml_config", _gated_read)
        before_round = target.read_bytes()
        outcomes: list[tuple[str, int | str]] = []
        threads = [
            threading.Thread(
                target=_import_in_thread,
                args=(path, outcomes),
                daemon=True,
            )
            for path, _ in sources
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not any(thread.is_alive() for thread in threads), "import thread hung"

        assert len(outcomes) == _CONCURRENT_THREADS
        assert all(kind in {"ok", "exit"} for kind, _ in outcomes), outcomes
        assert all(code == 1 for kind, code in outcomes if kind == "exit"), outcomes
        round_successes = sum(1 for kind, _ in outcomes if kind == "ok")
        successes += round_successes

        final = target.read_bytes()
        assert final in known_documents | {before_round}, (
            "target is not one complete document"
        )
        # A complete document also parses; this catches truncation/mixing.
        tomllib.loads(final.decode("utf-8"))
        if backup.exists():
            assert backup.read_bytes() in known_documents | {before_round}
        assert not _residual_files(work)
        if round_successes:
            # At least one write won this round; the survivors are complete.
            assert final in {document for _, document in sources}
            break

    assert successes > 0, "every concurrent import failed (no lock, but no winner)"


# ---------------------------------------------------------------------------
# ``--from`` path shape: directories, dangling links, file symlinks
# ---------------------------------------------------------------------------


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks without elevation")


def test_import_rejects_a_directory_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source = tmp_path / "sources" / "a-directory"
    source.mkdir(parents=True)
    (source / "keep.txt").write_text("data", encoding="utf-8")
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="source is not a regular file"
    )
    assert target.read_bytes() == original
    assert (source / "keep.txt").read_text(encoding="utf-8") == "data"


def test_import_rejects_a_dangling_symlink_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    source = tmp_path / "dangling.toml"
    _symlink_or_skip(source, tmp_path / "does-not-exist.toml")
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="source is not a regular file"
    )
    assert target.read_bytes() == original
    assert source.is_symlink()


def test_import_rejects_a_symlink_to_a_directory_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    original = _config_text(workers=2).encode("utf-8")
    target.write_bytes(original)
    directory = tmp_path / "sources" / "directory"
    directory.mkdir(parents=True)
    source = tmp_path / "dir-link.toml"
    _symlink_or_skip(source, directory)
    _capture_ui(monkeypatch)

    _expect_exit(["config", "import", "--from", str(source), "--json"], 1)

    payload, _ = _json_document(capsys)
    _assert_import_error(
        payload, target=target, source=source, fragment="source is not a regular file"
    )
    assert target.read_bytes() == original


def test_import_reads_through_a_file_symlink_without_writing_through_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Current contract: a symlink to a regular file is a valid source.

    ``is_file`` follows links, matching the loader, which resolves symlinked
    configuration files.  The source link is read-only for this command: the
    import writes the resolved target ``settings.toml`` and leaves both the
    link and the file it points at untouched (so nothing is ever written
    *through* an attacker-supplied link).
    """
    work = _isolate(monkeypatch, tmp_path)
    target = work / "settings.toml"
    target.write_bytes(_config_text(workers=1).encode("utf-8"))
    real = tmp_path / "real-config.toml"
    real.write_bytes(_config_text(workers=11).encode("utf-8"))
    source = tmp_path / "linked.toml"
    _symlink_or_skip(source, real)
    real_before = real.read_bytes()
    _capture_ui(monkeypatch)

    _run(["config", "import", "--from", str(source), "--json"])

    payload, _ = _json_document(capsys)
    assert payload["status"] == "imported"
    assert source.is_symlink()
    assert real.read_bytes() == real_before
    assert target.read_bytes() == real_before
    assert (work / "settings.toml.bak").read_bytes() == _config_text(workers=1).encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Export: header + --force must not regress
# ---------------------------------------------------------------------------


def test_export_header_and_force_contract_do_not_regress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work = _isolate(monkeypatch, tmp_path)
    source = _config_text(workers=4)
    (work / "settings.toml").write_bytes(source.encode("utf-8"))
    destination = work / "export.toml"
    backup = work / "export.toml.bak"
    _capture_ui(monkeypatch)

    _run(["config", "export", "--out", "export.toml", "--json"])

    payload, _ = _json_document(capsys)
    assert payload["status"] == "created"
    first_export = destination.read_bytes()
    header = first_export.split(b"\n\n", 1)[0].decode("utf-8")
    assert "# export-format: 1" in header
    assert f"# app-version: {__version__}" in header
    assert re.search(
        r"^# exported-at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        header,
        re.MULTILINE,
    )
    assert first_export.endswith(source.encode("utf-8"))

    # Without --force the existing destination is untouched...
    _expect_exit(["config", "export", "--out", "export.toml", "--json"], 1)
    payload, _ = _json_document(capsys)
    assert payload["status"] == "error"
    assert destination.read_bytes() == first_export
    assert not backup.exists()

    # ...and with --force the previous export is kept as .bak.
    _run(["config", "export", "--out", "export.toml", "--force", "--json"])
    payload, _ = _json_document(capsys)
    assert payload["status"] == "overwritten"
    assert payload["backup"] == str(backup)
    assert backup.read_bytes() == first_export
