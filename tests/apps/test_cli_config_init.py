#!/usr/bin/env python3
"""
tests.apps.test_cli_config_init
-------------------------------

Focused tests for ``novel-cli config init``.

The contract these tests pin down:

* an existing ``settings.toml`` is never overwritten by default -- not even
  after a confirmation prompt -- so hand-written comments and edits survive;
* ``--force`` is the only way to replace it, and it first writes the previous
  bytes to ``settings.toml.bak``;
* the replacement itself stays atomic (the bundled
  :func:`copy_default_config` already guarantees that);
* a directory, a symlink, a corrupt config and a failing backup are reported
  instead of producing a traceback or destroying data;
* ``--json`` emits exactly one machine-readable document and no human prose.

Nothing here touches the network or the real user configuration: every path
comes from ``tmp_path`` and the cwd is monkeypatched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import config
from novel_downloader.apps.cli.commands.config import ConfigCmd
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Messages:
    """Records messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []


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


def _init(
    monkeypatch: pytest.MonkeyPatch,
    cwd: Path,
    *argv: str,
) -> None:
    monkeypatch.chdir(cwd)
    args = _parser().parse_args(["config", "init", *argv])
    args.func(args)


def _target(cwd: Path) -> Path:
    return cwd / "settings.toml"


def _backup(cwd: Path) -> Path:
    return cwd / "settings.toml.bak"


def _sample() -> bytes:
    return DEFAULT_CONFIG_FILE.read_bytes()


# ---------------------------------------------------------------------------
# Default behaviour: existing file is left alone
# ---------------------------------------------------------------------------


def test_existing_file_is_not_overwritten_and_never_prompts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = b"# hand-written comment\n[general]\nworkers = 3\n"
    target = _target(tmp_path)
    target.write_bytes(original)
    messages = _capture_ui(monkeypatch)

    def _prompt(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("config init must not prompt without --force")

    monkeypatch.setattr(config.ui, "confirm", _prompt)

    _init(monkeypatch, tmp_path)

    assert target.read_bytes() == original
    assert not _backup(tmp_path).exists()
    assert any("--force" in message for message in messages.warn)
    assert not messages.error


def test_existing_file_check_does_not_parse_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A broken/partial file must not make the default path crash: it is simply
    # reported as existing.
    target = _target(tmp_path)
    target.write_text("this is not [valid toml", encoding="utf-8")
    messages = _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path)

    assert target.read_text(encoding="utf-8") == "this is not [valid toml"
    assert any("--force" in message for message in messages.warn)


# ---------------------------------------------------------------------------
# --force: backup first, then replace
# ---------------------------------------------------------------------------


def test_force_backs_up_then_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = b'# keep me\n[sites.example]\nbook_ids = ["1"]\n'
    target = _target(tmp_path)
    target.write_bytes(original)
    messages = _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--force")

    assert target.read_bytes() == _sample()
    assert _backup(tmp_path).read_bytes() == original
    assert any("Overwriting existing file" in message for message in messages.warn)
    assert any("settings.toml.bak" in message for message in messages.info)
    assert any("Copied" in message for message in messages.success)


def test_force_without_existing_file_creates_no_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--force")

    assert _target(tmp_path).read_bytes() == _sample()
    assert not _backup(tmp_path).exists()


def test_force_recovers_from_corrupt_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corrupt = b"\x00\xffnot-utf8 and not toml"
    target = _target(tmp_path)
    target.write_bytes(corrupt)
    _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--force")

    assert target.read_bytes() == _sample()
    assert _backup(tmp_path).read_bytes() == corrupt


def test_force_backup_failure_keeps_original_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = b"# original\n"
    target = _target(tmp_path)
    target.write_bytes(original)
    # A directory at the backup path makes the atomic replace fail.
    _backup(tmp_path).mkdir()
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _init(monkeypatch, tmp_path, "--force")

    assert excinfo.value.code == 1
    assert target.read_bytes() == original
    assert messages.error


def test_force_does_not_write_through_a_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real = tmp_path / "dotfiles" / "settings.toml"
    real.parent.mkdir()
    real.write_text("# linked real file\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    link = _target(work)
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create file symlinks without elevation")

    _capture_ui(monkeypatch)
    _init(monkeypatch, work, "--force")

    # The link itself is replaced with the bundled sample; the file it pointed
    # at is untouched. Its previous content is still recoverable from the .bak.
    assert not link.is_symlink()
    assert link.read_bytes() == _sample()
    assert real.read_text(encoding="utf-8") == "# linked real file\n"
    assert _backup(work).read_text(encoding="utf-8") == "# linked real file\n"


def test_symlink_is_skipped_without_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.toml"
    real.write_text("# real\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    link = _target(work)
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create file symlinks without elevation")

    messages = _capture_ui(monkeypatch)
    _init(monkeypatch, work)

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "# real\n"
    assert any("--force" in message for message in messages.warn)
    assert not _backup(work).exists()


# ---------------------------------------------------------------------------
# Directory targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [(), ("--force",)])
def test_directory_target_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    target = _target(tmp_path)
    target.mkdir()
    (target / "keep.txt").write_text("data", encoding="utf-8")
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _init(monkeypatch, tmp_path, *argv)

    assert excinfo.value.code == 1
    assert target.is_dir()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "data"
    assert messages.error


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------


def test_json_created_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    messages = _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "config init",
        "path": str(_target(tmp_path)),
        "status": "created",
        "backup": None,
        "force": False,
    }
    assert not messages.info
    assert not messages.warn
    assert not messages.success


def test_json_skipped_existing_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _target(tmp_path)
    target.write_bytes(b"# keep\n")
    messages = _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert payload["backup"] is None
    assert target.read_bytes() == b"# keep\n"
    assert not messages.warn


def test_json_forced_document_reports_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _target(tmp_path)
    target.write_bytes(b"# old\n")
    messages = _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--force", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "overwritten"
    assert payload["force"] is True
    assert payload["backup"] == str(_backup(tmp_path))
    assert _backup(tmp_path).read_bytes() == b"# old\n"
    assert not messages.info
    assert not messages.warn


def test_json_error_document_for_directory_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _target(tmp_path).mkdir()
    messages = _capture_ui(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        _init(monkeypatch, tmp_path, "--json")

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["path"] == str(_target(tmp_path))
    assert "error" in payload
    assert not messages.error


# ---------------------------------------------------------------------------
# Read-only targets
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses os.replace() over a read-only file",
)
def test_force_over_readonly_file_is_atomic_on_posix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = b"# read-only\n"
    target = _target(tmp_path)
    target.write_bytes(original)
    target.chmod(0o444)
    _capture_ui(monkeypatch)

    _init(monkeypatch, tmp_path, "--force")

    assert target.read_bytes() == _sample()
    assert _backup(tmp_path).read_bytes() == original


@pytest.mark.skipif(
    os.name != "nt",
    reason="the read-only attribute only blocks os.replace on Windows",
)
def test_force_over_readonly_file_fails_cleanly_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = b"# read-only\n"
    target = _target(tmp_path)
    target.write_bytes(original)
    target.chmod(0o444)
    messages = _capture_ui(monkeypatch)

    try:
        with pytest.raises(SystemExit) as excinfo:
            _init(monkeypatch, tmp_path, "--force")
    finally:
        target.chmod(0o644)

    assert excinfo.value.code == 1
    assert target.read_bytes() == original
    assert messages.error
    # The failed write must not leave its temporary file behind.
    assert not list(tmp_path.glob("settings.toml.*.tmp"))


# ---------------------------------------------------------------------------
# set-lang must not regress
# ---------------------------------------------------------------------------


def test_set_lang_still_requires_a_language() -> None:
    parser = _parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["config", "set-lang"])

    assert excinfo.value.code == 2
