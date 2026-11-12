#!/usr/bin/env python3
"""
tests.apps.test_cli_commands
----------------------------

Offline tests for CLI commands that previously had no coverage.

Every test is deterministic and host-independent: the engine/registrar is
replaced with small fakes, paths come from ``tmp_path``, and the interface
language is pinned to English by ``tests/conftest.py``.  Nothing is downloaded
and no network call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli.commands import (
    base,
    clean,
    config,
    download,
    export,
    sources,
)
from novel_downloader.apps.cli.commands.clean import CleanCmd
from novel_downloader.apps.cli.commands.config import ConfigCmd
from novel_downloader.apps.cli.commands.download import DownloadCmd
from novel_downloader.apps.cli.commands.export import ExportCmd
from novel_downloader.apps.cli.commands.sources import SourcesCmd
from novel_downloader.infra import i18n as i18n_module
from novel_downloader.infra.paths import DEFAULT_CONFIG_FILE, PACKAGE_NAME
from novel_downloader.infra.persistence.state import StateManager
from novel_downloader.plugins.mixins.download import IncompleteDownloadError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parser(*commands: type) -> argparse.ArgumentParser:
    """Build a minimal top-level parser with the given command classes."""
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


class _Messages:
    """Records the messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.info: list[str] = []
        self.success: list[str] = []
        self.warn: list[str] = []
        self.error: list[str] = []


def _capture_ui(monkeypatch: pytest.MonkeyPatch, module: Any) -> _Messages:
    messages = _Messages()
    for name in ("info", "success", "warn", "error"):
        monkeypatch.setattr(module.ui, name, getattr(messages, name).append)
    return messages


def _non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or a junction on Windows without privileges."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace")
            pytest.skip(f"cannot create junction: {detail}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"cannot create directory symlink: {exc}")


def _general_config(tmp_path: Path, **general: Any) -> dict[str, Any]:
    general.setdefault("debug", {"log_dir": str(tmp_path / "logs")})
    return {"general": general}


# ---------------------------------------------------------------------------
# main: --help / --version / --lang / dispatch
# ---------------------------------------------------------------------------


def test_cli_help_exits_zero_and_lists_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["novel-cli", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for token in ("download", "search", "export", "clean", "config"):
        assert token in out


def test_cli_version_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["novel-cli", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 0
    assert "NovelDownloader" in capsys.readouterr().out


def test_cli_lang_flag_is_applied_before_subcommands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOVEL_LANG", "en_US")
    monkeypatch.setattr(sys, "argv", ["novel-cli", "--lang", "zh", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 0
    assert os.environ["NOVEL_LANG"] == "zh_CN"
    assert capsys.readouterr().out


def test_preparse_language_accepts_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVEL_LANG", "en_US")

    assert cli_main_module._preparse_language(["--lang", "zh-cn"]) == "zh_CN"
    assert os.environ["NOVEL_LANG"] == "zh_CN"


def test_preparse_language_ignores_unknown_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVEL_LANG", "en_US")

    assert cli_main_module._preparse_language(["--lang", "klingon"]) == ""
    assert os.environ["NOVEL_LANG"] == "en_US"


def test_cli_without_command_starts_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "novel_downloader.apps.cli.interactive.start_interactive",
        lambda: called.append(True),
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli"])

    cli_main_module.cli_main()

    assert called == [True]


def test_cli_unknown_command_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["novel-cli", "does-not-exist"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# config init
# ---------------------------------------------------------------------------


def test_config_init_creates_default_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    messages = _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "init"])
    args.func(args)

    target = tmp_path / "settings.toml"
    assert target.is_file()
    assert target.read_bytes() == DEFAULT_CONFIG_FILE.read_bytes()
    assert messages.success


def test_config_init_skips_existing_file_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "settings.toml"
    target.write_text("original", encoding="utf-8")
    messages = _capture_ui(monkeypatch, config)

    def _fail_prompt(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("config init must not prompt without --force")

    monkeypatch.setattr(config.ui, "confirm", _fail_prompt)

    args = _parser(ConfigCmd).parse_args(["config", "init"])
    args.func(args)

    assert target.read_text(encoding="utf-8") == "original"
    assert any("--force" in message for message in messages.warn)


def test_config_init_force_overwrites_existing_file_after_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "settings.toml"
    target.write_text("original", encoding="utf-8")
    _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "init", "--force"])
    args.func(args)

    assert target.read_bytes() == DEFAULT_CONFIG_FILE.read_bytes()
    assert (tmp_path / "settings.toml.bak").read_text(encoding="utf-8") == "original"


def test_config_init_copy_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    messages = _capture_ui(monkeypatch, config)

    def _boom(_target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config, "copy_default_config", _boom)

    args = _parser(ConfigCmd).parse_args(["config", "init"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("disk full" in message for message in messages.error)
    assert not (tmp_path / "settings.toml").exists()


# ---------------------------------------------------------------------------
# config set-lang
# ---------------------------------------------------------------------------


def test_config_set_lang_persists_normalized_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(i18n_module, "state_mgr", StateManager(state_path))
    messages = _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "set-lang", "en"])
    args.func(args)

    assert json.loads(state_path.read_text(encoding="utf-8"))["lang"] == "en_US"
    assert messages.success


def test_config_set_lang_rejects_unsupported_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(i18n_module, "state_mgr", StateManager(state_path))
    messages = _capture_ui(monkeypatch, config)

    args = _parser(ConfigCmd).parse_args(["config", "set-lang", "xx"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported language" in message for message in messages.error)
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# clean state
# ---------------------------------------------------------------------------


def test_clean_state_missing_reports_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(clean, "STATE_PATH", tmp_path / "home" / "state.json")
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "state"])
    args.func(args)

    assert any("Nothing to clean" in message for message in messages.info)


def test_clean_state_yes_removes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "home" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clean, "STATE_PATH", state_path)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "state", "--yes"])
    args.func(args)

    assert not state_path.exists()
    assert messages.success


def test_clean_state_cancelled_keeps_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "home" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clean, "STATE_PATH", state_path)
    messages = _capture_ui(monkeypatch, clean)
    monkeypatch.setattr(clean.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(CleanCmd).parse_args(["clean", "state"])
    args.func(args)

    assert state_path.exists()
    assert any("Cancelled." in message for message in messages.warn)


def test_clean_state_removal_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "home" / "state.json"
    state_path.mkdir(parents=True)  # a directory cannot be unlinked
    monkeypatch.setattr(clean, "STATE_PATH", state_path)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "state", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("Failed to remove" in message for message in messages.error)


# ---------------------------------------------------------------------------
# clean logs
# ---------------------------------------------------------------------------


def _write_logs(log_dir: Path) -> list[Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    files = [
        log_dir / f"{PACKAGE_NAME}.log.1",
        log_dir / f"{PACKAGE_NAME}.2024-01-01.log",
    ]
    for path in files:
        path.write_text("log", encoding="utf-8")
    (log_dir / "unrelated.log").write_text("keep", encoding="utf-8")
    return files


def test_clean_logs_dry_run_keeps_files_and_skips_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    files = _write_logs(log_dir)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    def _fail_prompt(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("dry-run must not prompt")

    monkeypatch.setattr(clean.ui, "confirm", _fail_prompt)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--dry-run"])
    args.func(args)

    assert all(path.exists() for path in files)
    assert (log_dir / "unrelated.log").exists()
    assert any("Dry-run mode" in message for message in messages.info)


def test_clean_logs_cancelled_keeps_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    files = _write_logs(log_dir)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)
    monkeypatch.setattr(clean.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(CleanCmd).parse_args(["clean", "logs"])
    args.func(args)

    assert all(path.exists() for path in files)
    assert any("Cancelled." in message for message in messages.warn)


def test_clean_logs_yes_removes_only_application_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    files = _write_logs(log_dir)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--yes"])
    args.func(args)

    assert not any(path.exists() for path in files)
    assert (log_dir / "unrelated.log").exists()
    assert messages.success


def test_clean_logs_removal_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    _write_logs(log_dir)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    _capture_ui(monkeypatch, clean)

    def _boom(_self: Path, *_a: Any, **_k: Any) -> None:
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_clean_logs_missing_log_dir_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(
            tmp_path, debug={"log_dir": str(tmp_path / "absent")}
        ),
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--yes"])
    args.func(args)

    assert any("does not exist" in message for message in messages.info)


def test_clean_logs_config_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", lambda *_a, **_k: None)
    _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_clean_logs_path_is_not_a_directory_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, debug={"log_dir": str(blocked)}),
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "logs", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("not a directory" in message for message in messages.error)


# ---------------------------------------------------------------------------
# clean cache
# ---------------------------------------------------------------------------


def _cache_config(tmp_path: Path) -> dict[str, Any]:
    return _general_config(tmp_path, cache_dir=str(tmp_path / "cache"))


def test_clean_cache_site_yes_removes_only_that_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "b520").mkdir(parents=True)
    (cache / "qidian").mkdir()
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "b520", "--yes"])
    args.func(args)

    assert not (cache / "b520").exists()
    assert (cache / "qidian").exists()
    assert messages.success


def test_clean_cache_cancel_keeps_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "b520").mkdir()
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)
    monkeypatch.setattr(clean.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(CleanCmd).parse_args(["clean", "cache"])
    args.func(args)

    assert (cache / "b520").exists()
    assert any("Cancelled." in message for message in messages.warn)


def test_clean_cache_missing_site_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "absent"])
    args.func(args)

    assert any("No cache found" in message for message in messages.info)


def test_clean_cache_removal_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("in use")

    monkeypatch.setattr(clean, "remove_path_safely", _boom)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("in use" in message for message in messages.error)


# ---------------------------------------------------------------------------
# clean book
# ---------------------------------------------------------------------------


class _CleanupClient:
    """Records ``cleanup_book`` calls (and can fail on demand)."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    def cleanup_book(self, book: Any, **kwargs: Any) -> None:
        self.calls.append((book.book_id, kwargs))
        if self.error is not None:
            raise self.error


def _book_env(monkeypatch: pytest.MonkeyPatch, client: _CleanupClient) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", lambda *_a, **_k: {})
    monkeypatch.setattr(clean.registrar, "get_client", lambda *_a, **_k: client)
    _capture_ui(monkeypatch, clean)


def test_clean_book_without_ids_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "book"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("No book IDs" in message for message in messages.error)


def test_clean_book_requires_site_when_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _non_tty(monkeypatch)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "book", "123"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("requires a terminal" in message for message in messages.error)


def test_clean_book_yes_calls_client_with_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _CleanupClient()
    _book_env(monkeypatch, client)

    args = _parser(CleanCmd).parse_args(
        [
            "clean",
            "book",
            "1",
            "2",
            "--site",
            "b520",
            "--yes",
            "--metadata",
            "--media",
        ]
    )
    args.func(args)

    assert [book_id for book_id, _ in client.calls] == ["1", "2"]
    kwargs = client.calls[0][1]
    assert kwargs["remove_chapters"] is True
    assert kwargs["remove_metadata"] is True
    assert kwargs["remove_media"] is True
    assert kwargs["stage"] == "raw"


def test_clean_book_cancel_skips_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _CleanupClient()
    _book_env(monkeypatch, client)
    messages = _capture_ui(monkeypatch, clean)
    monkeypatch.setattr(clean.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(CleanCmd).parse_args(["clean", "book", "1", "--site", "b520"])
    args.func(args)

    assert client.calls == []
    assert any("Cancelled." in message for message in messages.warn)


def test_clean_book_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _CleanupClient(error=RuntimeError("cleanup exploded"))
    _book_env(monkeypatch, client)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "1", "--site", "b520", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("cleanup exploded" in message for message in messages.error)
    # A failed cleanup must not be followed by a success line.
    assert not any("Finished cleaning" in message for message in messages.success)


def test_clean_book_all_failure_exits_one_without_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _CleanupClient(error=RuntimeError("cleanup exploded"))
    _book_env(monkeypatch, client)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "1", "--all", "--site", "b520", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert not any("Finished cleaning" in message for message in messages.success)


def test_clean_book_with_nothing_selected_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--no-chapters`` alone must not prompt or call the client."""
    client = _CleanupClient()
    _book_env(monkeypatch, client)
    messages = _capture_ui(monkeypatch, clean)

    def _fail_prompt(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("nothing to remove must not prompt")

    monkeypatch.setattr(clean.ui, "confirm", _fail_prompt)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "1", "--site", "b520", "--no-chapters"]
    )
    args.func(args)

    assert client.calls == []
    assert any("Nothing to clean." in message for message in messages.info)


def _refuse_config_load(*_a: Any, **_k: Any) -> dict[str, Any]:
    raise AssertionError("config must not be loaded")


def test_clean_cache_invalid_site_fails_before_loading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad --site must not trigger the config prompt/create side effect."""
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "..", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages.error)


def test_clean_book_all_invalid_site_fails_before_loading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "--all", "--site", "../..", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages.error)


def test_clean_empty_site_is_a_usage_error_not_all_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--site ""`` must never fall back to "all sites"."""
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)
    parser = _parser(CleanCmd)

    for argv in (
        ["clean", "cache", "--site", "", "--yes"],
        ["clean", "book", "--all", "--site", "", "--yes"],
        ["clean", "book", "1", "--site", "", "--yes"],
    ):
        args = parser.parse_args(argv)
        with pytest.raises(SystemExit) as excinfo:
            args.func(args)

        assert excinfo.value.code == 2
        assert any("Unsupported site" in message for message in messages.error)


def test_clean_book_all_site_removes_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    (raw / "b520").mkdir(parents=True)
    (raw / "qidian").mkdir()
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "--all", "--site", "b520", "--yes"]
    )
    args.func(args)

    assert not (raw / "b520").exists()
    assert (raw / "qidian").exists()
    assert messages.success


def test_clean_book_all_cancelled_keeps_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    (raw / "b520").mkdir(parents=True)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    messages = _capture_ui(monkeypatch, clean)
    monkeypatch.setattr(clean.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(CleanCmd).parse_args(["clean", "book", "--all"])
    args.func(args)

    assert (raw / "b520").exists()
    assert any("Cancelled." in message for message in messages.warn)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


class _FakeDownloadUI:
    def __init__(self) -> None:
        self.completed_books: set[Any] = set()

    async def on_complete(self, book: Any, *, failed: int = 0) -> None:
        self.completed_books.add(book)


class _FakeDownloadClient:
    def __init__(
        self,
        *,
        complete: bool = True,
        error: BaseException | None = None,
        export_results: dict[str, list[Path]] | None = None,
        book_errors: dict[str, BaseException] | None = None,
        summaries: dict[str, Any] | None = None,
        process_errors: dict[str, BaseException] | None = None,
        export_errors: dict[str, BaseException] | None = None,
        silent: set[str] | None = None,
    ) -> None:
        self.complete = complete
        self.error = error
        self.downloaded: list[str] = []
        #: Full ``BookConfig`` objects passed to ``download_book``, in order.
        self.books: list[Any] = []
        self.export_results = (
            {"txt": [Path("book.txt")]} if export_results is None else export_results
        )
        self.exported: list[str] = []
        #: Per-book download failures, so one book can fail while the rest run.
        self.book_errors = book_errors or {}
        #: Per-book ``DownloadSummary``-shaped dicts (``failed``/``skipped``).
        self.summaries = summaries or {}
        #: Per-book process/export failures (isolation tests).
        self.process_errors = process_errors or {}
        self.export_errors = export_errors or {}
        #: Books whose download returns without calling ``ui.on_complete``.
        self.silent = silent or set()

    async def __aenter__(self) -> _FakeDownloadClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def download_book(
        self, book: Any, *, ui: _FakeDownloadUI, strict: bool = False
    ) -> Any:
        if self.error is not None:
            raise self.error
        if book.book_id in self.book_errors:
            raise self.book_errors[book.book_id]
        self.books.append(book)
        self.downloaded.append(book.book_id)
        if self.complete and book.book_id not in self.silent:
            await ui.on_complete(book)
        summary = self.summaries.get(book.book_id)
        # Mirror ``DownloadMixin.download_book(strict=True)``: the summary is
        # still produced (``on_complete`` already ran) and then raised.
        if strict and isinstance(summary, dict) and summary.get("failed"):
            raise IncompleteDownloadError(summary)
        return summary

    def process_book(
        self, book: Any, processors: Any = None, *, ui: Any = None
    ) -> None:
        if book.book_id in self.process_errors:
            raise self.process_errors[book.book_id]

    def export_book(
        self, book: Any, *, cfg: Any = None, formats: Any = None, ui: Any = None
    ) -> dict[str, list[Path]]:
        self.exported.append(book.book_id)
        if book.book_id in self.export_errors:
            raise self.export_errors[book.book_id]
        return self.export_results


def _download_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeDownloadClient,
) -> None:
    monkeypatch.setattr(
        download, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    monkeypatch.setattr(download, "CLIDownloadUI", _FakeDownloadUI)
    monkeypatch.setattr(download.registrar, "get_client", lambda *_a, **_k: client)


def _resolve_to(info: dict[str, Any] | None) -> Any:
    return lambda _url: info


def _assert_compact_json_line(out: str) -> Any:
    """Assert ``out`` is exactly one compact JSON line and return its payload."""
    assert out.endswith("\n"), f"missing trailing newline: {out!r}"
    raw = out[:-1]
    assert "\n" not in raw, f"JSON spans multiple lines: {out!r}"
    # Compact separators: no space after ':' or ','.
    assert ": " not in raw, f"pretty-printed separator in {raw!r}"
    assert ", " not in raw, f"pretty-printed separator in {raw!r}"
    payload = json.loads(raw)
    expected = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert raw == expected, f"not compact JSON: {raw!r}"
    return payload


def test_download_without_arguments_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("No book ID or URL" in message for message in messages.error)


def test_download_rejects_multiple_urls_without_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "https://a.example/1", "https://b.example/2"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("exactly one URL" in message for message in messages.error)


def test_download_unresolvable_url_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to(None),
    )

    args = _parser(DownloadCmd).parse_args(
        ["download", "https://unknown.example/book/1"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Could not resolve" in message for message in messages.error)


def test_download_url_without_book_id_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to({"site_key": "b520", "book_id": None, "chapter_id": None}),
    )

    args = _parser(DownloadCmd).parse_args(
        ["download", "https://www.b520.cc/chapter/1"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("valid book ID" in message for message in messages.error)


def test_download_json_site_mode_reports_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--json", "--no-export"]
    )
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["site"] == "b520"
    assert payload["books"] == [
        {"book_id": "123", "downloaded": True, "failed": 0, "exported": None}
    ]
    assert client.downloaded == ["123"]


def test_download_json_site_mode_is_one_compact_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` must print exactly one compact line, like the other commands."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--json", "--no-export"]
    )
    args.func(args)

    payload = _assert_compact_json_line(capsys.readouterr().out)
    assert payload == {
        "site": "b520",
        "books": [
            {"book_id": "123", "downloaded": True, "failed": 0, "exported": None}
        ],
    }


def test_download_json_keeps_diagnostics_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Warnings (the incomplete summary) must not pollute the JSON on stdout."""
    summary = {
        "book_id": "123",
        "site": "b520",
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "skipped": 0,
        "failures": [{"chapter_id": "1", "chapter_title": "One", "reason": "boom"}],
        "ledger_path": "ledger.txt",
    }
    client = _FakeDownloadClient(summaries={"123": summary})
    _download_env(monkeypatch, tmp_path, client)
    # Deliberately do NOT stub ``ui``: the real helpers route diagnostics to
    # stderr via the rich console.

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--json", "--no-export"]
    )
    args.func(args)  # exit 0: the book completed, one chapter failed

    captured = capsys.readouterr()
    payload = _assert_compact_json_line(captured.out)
    assert payload["books"][0]["failed"] == 1
    assert "Download incomplete" in captured.err


def test_download_json_url_mode_resolves_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to({"site_key": "b520", "book_id": "8_8187", "chapter_id": None}),
    )

    args = _parser(DownloadCmd).parse_args(
        ["download", "https://www.b520.cc/8_8187/", "--json", "--no-export"]
    )
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["site"] == "b520"
    assert payload["books"][0]["book_id"] == "8_8187"
    assert payload["books"][0]["downloaded"] is True


def test_download_json_reports_unsupported_site_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeDownloadClient(error=ValueError("no parser"))
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "demo", "1", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("currently not supported" in message for message in messages.warn)
    payload = _assert_compact_json_line(capsys.readouterr().out)
    assert payload == {
        "site": "demo",
        "books": [{"book_id": "1", "downloaded": False, "failed": 0, "exported": {}}],
    }


def test_download_runtime_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeDownloadClient(error=RuntimeError("network down"))
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520", "1"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("network down" in message for message in messages.error)


def test_download_without_completed_books_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeDownloadClient(complete=False)
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520", "1"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_download_export_that_writes_nothing_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The download succeeded, but no export file was produced."""
    client = _FakeDownloadClient(export_results={"txt": []})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.exported == ["123"]
    assert any("Export failed" in line and "123" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][0]["exported"] == {"txt": []}


def test_download_one_failing_book_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bad book must not cost the other book IDs in the same invocation."""
    client = _FakeDownloadClient(book_errors={"2": RuntimeError("bad book")})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "1", "2", "3"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.downloaded == ["1", "3"]
    assert client.exported == ["1", "3"]
    assert any("Download failed for book 2" in line for line in messages.error)


def test_download_cached_chapters_are_not_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``skipped`` chapters are part of ``succeeded``; only ``failed`` fails."""
    summary = {
        "book_id": "123",
        "site": "b520",
        "total": 10,
        "succeeded": 10,
        "failed": 0,
        "skipped": 7,
        "failures": [],
        "ledger_path": "",
    }
    client = _FakeDownloadClient(summaries={"123": summary})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520", "123"])
    args.func(args)  # no SystemExit: a cached book is complete

    assert client.exported == ["123"]
    assert not any("incomplete" in line for line in messages.warn)


def test_download_malformed_summary_does_not_crash_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A third-party summary with a non-integer ``failed`` is ignored, not fatal."""
    summary = {
        "book_id": "123",
        "failed": ["not", "a", "count"],
        "total": 1,
        "failures": [],
        "ledger_path": "",
    }
    client = _FakeDownloadClient(summaries={"123": summary})
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520", "123"])
    args.func(args)  # no SystemExit

    assert client.exported == ["123"]


def test_download_summary_without_book_id_does_not_crash_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A summary that reports failures without naming a book must not crash.

    ``--strict`` needs to know which books to keep out of the export, and a
    third-party client may omit ``book_id``. That used to raise ``KeyError``
    before ``--json`` was printed (no report, raw traceback).
    """
    summary = {
        "failed": 2,
        "total": 5,
        "failures": [],
        "ledger_path": "",
    }
    client = _FakeDownloadClient(summaries={"123": summary})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--json"]
    )
    args.func(args)  # no SystemExit: the book itself completed

    assert any("Download incomplete" in line for line in messages.warn)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [
        {
            "book_id": "123",
            "downloaded": True,
            "failed": 0,
            "exported": {"txt": ["book.txt"]},
        }
    ]


def test_download_duplicate_identical_requests_download_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same book (same range) passed twice is one download, one JSON entry."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "123", "--json", "--no-export"]
    )
    args.func(args)

    assert client.downloaded == ["123"]
    payload = json.loads(capsys.readouterr().out)
    assert [book["book_id"] for book in payload["books"]] == ["123"]


def test_download_strips_surrounding_whitespace_from_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``" 123"`` must name the same book (and directory) as ``123``.

    ``ConfigAdapter`` already strips IDs read from the configuration file; the
    command line used to keep a leading space, creating a second storage
    directory and a duplicate download for the same book.
    """
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        [
            "download",
            "--site",
            " b520 ",
            " 123 ",
            " 456",
            "--start",
            " 10 ",
            "--end",
            " 20 ",
            "--no-export",
        ]
    )
    args.func(args)

    assert client.downloaded == ["123", "456"]
    first, second = client.books
    assert (first.book_id, first.start_id, first.end_id) == ("123", "10", "20")
    # The documented contract: the range applies to the first book only.
    assert (second.book_id, second.start_id, second.end_id) == ("456", None, None)


@pytest.mark.parametrize("option", ["--start", "--end"])
def test_download_blank_chapter_range_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    option: str,
) -> None:
    """An explicitly empty range must fail as usage, not download the whole book."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", option, "   ", "--no-export"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert client.downloaded == []
    assert any("Invalid input" in line for line in messages.error)


def test_download_blank_site_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--site ""`` must not fall back to URL mode; it is an invalid site key."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "   ", "123", "--no-export"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert client.downloaded == []
    assert any("Unsupported site" in line for line in messages.error)


def test_download_url_mode_keeps_the_chapter_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A URL plus ``--start/--end`` still downloads only that range."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to({"site_key": "b520", "book_id": "8_8187", "chapter_id": None}),
    )

    args = _parser(DownloadCmd).parse_args(
        [
            "download",
            "https://www.b520.cc/8_8187/",
            "--start",
            "10",
            "--end",
            "20",
            "--no-export",
        ]
    )
    args.func(args)

    assert client.books[0].book_id == "8_8187"
    assert client.books[0].start_id == "10"
    assert client.books[0].end_id == "20"


@pytest.mark.parametrize("strict", [False, True])
def test_download_partial_failure_exit_codes_and_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    strict: bool,
) -> None:
    """Default mode warns and exits 0; ``--strict`` refuses the export and fails."""
    summary = {
        "book_id": "123",
        "site": "b520",
        "total": 5,
        "succeeded": 3,
        "failed": 2,
        "skipped": 0,
        "failures": [
            {
                "chapter_id": "c4",
                "chapter_title": "Chapter 4",
                "reason": "timeout",
            }
        ],
        "ledger_path": "chapter.failures.jsonl",
    }
    client = _FakeDownloadClient(summaries={"123": summary})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    argv = ["download", "--site", "b520", "123", "--json"]
    if strict:
        argv.append("--strict")
    args = _parser(DownloadCmd).parse_args(argv)

    if strict:
        with pytest.raises(SystemExit) as excinfo:
            args.func(args)
        assert excinfo.value.code == 1
        # An incomplete book must not be exported (nor processed) in strict mode.
        assert client.exported == []
        assert any("Download incomplete" in line for line in messages.error)
        exported = {}
    else:
        args.func(args)
        assert client.exported == ["123"]
        assert any("Download incomplete" in line for line in messages.warn)
        exported = {"txt": ["book.txt"]}

    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [
        {
            "book_id": "123",
            "downloaded": True,
            "failed": 2,
            "exported": exported,
        }
    ]


def test_download_url_mode_uses_the_resolved_site_never_the_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The URL is only an argument to the resolver, never a site key."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to({"site_key": "b520", "book_id": "8_8187", "chapter_id": None}),
    )
    seen: dict[str, str] = {}

    def _get_client(site: str, _config: Any) -> Any:
        seen["site"] = site
        return client

    monkeypatch.setattr(download.registrar, "get_client", _get_client)

    url = "https://www.b520.cc/8_8187/"
    args = _parser(DownloadCmd).parse_args(["download", url, "--no-export"])
    args.func(args)

    assert seen["site"] == "b520"
    assert client.downloaded == ["8_8187"]


def test_download_url_mode_reports_an_unsupported_site_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeDownloadClient(error=ValueError("no parser"))
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)
    monkeypatch.setattr(
        "novel_downloader.infra.book_url_resolver.resolve_book_url",
        _resolve_to({"site_key": "demo", "book_id": "1", "chapter_id": None}),
    )

    args = _parser(DownloadCmd).parse_args(["download", "https://demo.example/1"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("currently not supported" in line for line in messages.warn)


def test_download_rejects_a_url_passed_as_the_site(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--site https://...`` is a usage error; the URL never becomes a site."""
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "https://www.b520.cc/8_8187/", "1"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert client.downloaded == []
    assert any("Unsupported site" in line for line in messages.error)


def test_download_passes_expanded_config_directories_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ND_TEST_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("ND_TEST_RAW", str(tmp_path / "raw"))
    config = {
        "general": {
            "output_dir": "$ND_TEST_OUT",
            "raw_data_dir": "$ND_TEST_RAW",
            "debug": {"log_dir": str(tmp_path / "logs")},
        }
    }
    monkeypatch.setattr(download, "load_or_init_config", lambda *_a, **_k: config)
    monkeypatch.setattr(download, "CLIDownloadUI", _FakeDownloadUI)
    client = _FakeDownloadClient()
    seen: dict[str, Any] = {}

    def _get_client(_site: str, client_config: Any) -> Any:
        seen["config"] = client_config
        return client

    monkeypatch.setattr(download.registrar, "get_client", _get_client)
    _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "123", "--no-export"]
    )
    args.func(args)

    assert seen["config"].output_dir == str(tmp_path / "out")
    assert seen["config"].raw_data_dir == str(tmp_path / "raw")


@pytest.mark.parametrize("site", ["..", "../evil", "a/b", "C:"])
def test_download_rejects_a_site_that_escapes_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    site: str,
) -> None:
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", site, "123"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in line for line in messages.error)
    assert client.downloaded == []


@pytest.mark.parametrize("book_id", ["..", "../evil", "a/b", "a\\b"])
def test_download_rejects_a_book_id_that_escapes_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    book_id: str,
) -> None:
    client = _FakeDownloadClient()
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520", book_id])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Invalid input" in line for line in messages.error)
    assert client.downloaded == []


def test_download_process_failure_is_isolated_and_json_is_emitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A process failure on book 2 must not skip book 3 or the JSON report."""
    client = _FakeDownloadClient(
        process_errors={"2": RuntimeError("process boom")},
    )
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "1", "2", "3", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.downloaded == ["1", "2", "3"]
    assert client.exported == ["1", "3"]
    assert any("Process/export failed for book 2" in line for line in messages.error)

    payload = json.loads(capsys.readouterr().out)
    assert [book["book_id"] for book in payload["books"]] == ["1", "2", "3"]
    assert payload["books"][1]["exported"] == {}
    assert payload["books"][2]["exported"] == {"txt": ["book.txt"]}


def test_download_export_failure_is_isolated_and_json_is_emitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An export exception is a per-book failure; later books still export."""
    client = _FakeDownloadClient(
        export_errors={"2": RuntimeError("export boom")},
    )
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "1", "2", "3", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.downloaded == ["1", "2", "3"]
    assert client.exported == ["1", "2", "3"]
    assert any("Process/export failed for book 2" in line for line in messages.error)

    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][1]["downloaded"] is True
    assert payload["books"][1]["exported"] == {}
    assert payload["books"][2]["exported"] == {"txt": ["book.txt"]}


def test_download_client_creation_failure_still_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A site that cannot even build a client must not raise a traceback."""
    monkeypatch.setattr(
        download, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
    monkeypatch.setattr(download, "CLIDownloadUI", _FakeDownloadUI)
    messages = _capture_ui(monkeypatch, download)

    def _get_client(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("no parser")

    monkeypatch.setattr(download.registrar, "get_client", _get_client)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "demo", "1", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("currently not supported" in line for line in messages.warn)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [
        {"book_id": "1", "downloaded": False, "failed": 0, "exported": {}}
    ]


def test_download_book_that_reports_no_completion_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A download that returns without completing must not look successful.

    ``CommonClient.download_book`` returns early without calling
    ``ui.on_complete`` when the chapter plan is empty. With another book
    succeeding, the old code exported only the success and exited 0.
    """
    client = _FakeDownloadClient(silent={"2"})
    _download_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, download)

    args = _parser(DownloadCmd).parse_args(
        ["download", "--site", "b520", "1", "2", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.downloaded == ["1", "2"]
    assert client.exported == ["1"]
    assert any("No chapters downloaded for book 2" in line for line in messages.error)

    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][1] == {
        "book_id": "2",
        "downloaded": False,
        "failed": 0,
        "exported": {},
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class _FakeExportClient:
    def __init__(
        self,
        results: dict[str, list[Path]] | None = None,
        *,
        results_by_book: dict[str, dict[str, list[Path]]] | None = None,
        errors_by_book: dict[str, BaseException] | None = None,
    ) -> None:
        self.results = {} if results is None else results
        self.results_by_book = results_by_book or {}
        self.errors_by_book = errors_by_book or {}
        self.calls: list[tuple[str, tuple[str, ...], str | None]] = []

    def export_book(
        self,
        book: Any,
        *,
        cfg: Any,
        formats: list[str],
        stage: str | None = None,
        ui: Any = None,
    ) -> dict[str, list[Path]]:
        self.calls.append((book.book_id, tuple(formats), stage))
        if book.book_id in self.errors_by_book:
            raise self.errors_by_book[book.book_id]
        if book.book_id in self.results_by_book:
            return self.results_by_book[book.book_id]
        return self.results


def _write_export_book(
    raw_root: Path,
    *,
    site: str = "b520",
    book_id: str = "123",
    stage: str = "raw",
    volumes: list[dict[str, Any]] | None = None,
) -> Path:
    """Create the on-disk shape ``export`` expects for a downloaded book."""
    book_dir = raw_root / site / book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    if volumes is None:
        volumes = [
            {
                "volume_name": "Volume 1",
                "chapters": [{"chapterId": "c1", "title": "Chapter 1"}],
            }
        ]
    (book_dir / f"book_info.{stage}.json").write_text(
        json.dumps({"book_name": "Title", "author": "Author", "volumes": volumes}),
        encoding="utf-8",
    )
    return book_dir


def _export_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeExportClient | None = None,
    *,
    books: tuple[str, ...] = ("123",),
    raw_data_dir: Path | None = None,
    output_dir: Path | None = None,
    create_output_dir: bool = True,
) -> None:
    raw_root = raw_data_dir or (tmp_path / "raw_data")
    out_root = output_dir or (tmp_path / "downloads")
    if create_output_dir:
        out_root.mkdir(parents=True, exist_ok=True)
    for book_id in books:
        _write_export_book(raw_root, book_id=book_id)
    config = _general_config(
        tmp_path,
        raw_data_dir=str(raw_root),
        output_dir=str(out_root),
    )
    monkeypatch.setattr(export, "load_or_init_config", lambda *_a, **_k: config)
    if client is not None:
        monkeypatch.setattr(export.registrar, "get_client", lambda *_a, **_k: client)


def test_export_without_site_requires_a_tty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _export_env(monkeypatch, tmp_path)
    _non_tty(monkeypatch)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(["export"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("requires a terminal" in message for message in messages.error)


def test_export_with_site_but_no_books_requires_a_tty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _export_env(monkeypatch, tmp_path)
    _non_tty(monkeypatch)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(["export", "--site", "b520"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("requires a terminal" in message for message in messages.error)


def test_export_json_empty_result_is_valid_json_and_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeExportClient({})
    _export_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--json", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["site"] == "b520"
    assert payload["books"] == [{"book_id": "123", "exported": {}}]
    assert client.calls == [("123", ("txt",), None)]


def test_export_json_success_reports_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "book.txt"
    client = _FakeExportClient({"txt": [target]})
    _export_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--json", "--format", "txt"]
    )
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][0]["exported"] == {"txt": [str(target)]}


def test_export_config_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(export, "load_or_init_config", lambda *_a, **_k: None)
    _non_tty(monkeypatch)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(["export", "--site", "b520", "1"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1


def test_export_without_output_names_the_failed_book(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A client that returns no file is a failure that must be explained.

    ``CommonClient.export_book`` converts writer errors into an empty list, so
    the CLI cannot rely on an exception to tell the user what happened. The
    exit code must still be non-zero and stderr must name the book while
    stdout stays valid JSON.
    """
    client = _FakeExportClient({"txt": []})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("Export failed" in line and "123" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [{"book_id": "123", "exported": {"txt": []}}]


@pytest.mark.parametrize("site", ["..", "../evil", "a/b", "a\\b", "C:"])
def test_export_rejects_a_site_that_escapes_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    site: str,
) -> None:
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(["export", "--site", site, "123"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in line for line in messages.error)
    assert client.calls == []


@pytest.mark.parametrize("book_id", ["..", "../evil", "a/b", "a\\b", ""])
def test_export_rejects_a_book_id_that_escapes_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    book_id: str,
) -> None:
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(["export", "--site", "b520", book_id])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Invalid input" in line for line in messages.error)
    assert client.calls == []


def test_export_passes_expanded_config_directories_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config ``~/...`` / ``$VAR`` path must not become a literal directory."""
    monkeypatch.setenv("ND_TEST_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("ND_TEST_RAW", str(tmp_path / "raw"))
    _write_export_book(tmp_path / "raw", book_id="123")
    config = {
        "general": {
            "output_dir": "$ND_TEST_OUT",
            "raw_data_dir": "$ND_TEST_RAW",
            "debug": {"log_dir": str(tmp_path / "logs")},
        }
    }
    monkeypatch.setattr(export, "load_or_init_config", lambda *_a, **_k: config)
    seen: dict[str, Any] = {}

    def _get_client(_site: str, client_config: Any) -> Any:
        seen["config"] = client_config
        return _FakeExportClient({"txt": [tmp_path / "book.txt"]})

    monkeypatch.setattr(export.registrar, "get_client", _get_client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    args.func(args)

    assert seen["config"].output_dir == str(tmp_path / "out")
    assert seen["config"].raw_data_dir == str(tmp_path / "raw")
    # The config mapping itself is not mutated (other consumers keep their
    # original view of it).
    assert config["general"]["output_dir"] == "$ND_TEST_OUT"


def test_export_missing_book_data_fails_without_calling_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A book that was never downloaded is reported by path, not exported."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "456", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book data not found" in line for line in messages.error)
    assert any("456" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [{"book_id": "456", "exported": {}}]


def test_export_incomplete_book_metadata_fails_without_calling_the_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A directory without ``book_info.*.json`` is a corrupt/incomplete library."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    (tmp_path / "raw_data" / "b520" / "123").mkdir(parents=True)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book metadata is missing" in line for line in messages.error)


def test_export_missing_selected_stage_names_the_metadata_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--stage cleaner`` must not silently fall back to the raw metadata."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=("123",))
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--stage", "cleaner"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book_info.cleaner.json" in line for line in messages.error)


def test_export_book_without_chapters_is_reported_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A metadata file with no chapters is a failure, not an empty export."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    book_dir = _write_export_book(tmp_path / "raw_data", book_id="123", volumes=[])
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book has no chapters" in line for line in messages.error)
    assert any(str(book_dir / "book_info.raw.json") in line for line in messages.error)


def test_export_book_with_empty_volumes_lists_is_reported_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A volume with no chapters must not reach the exporter either."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    _write_export_book(
        tmp_path / "raw_data",
        book_id="123",
        volumes=[{"volume_name": "Volume 1", "chapters": []}],
    )
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book has no chapters" in line for line in messages.error)


def test_export_corrupt_metadata_names_the_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Truncated/corrupt JSON is a library error, not a silent empty export."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    book_dir = tmp_path / "raw_data" / "b520" / "123"
    book_dir.mkdir(parents=True)
    info_path = book_dir / "book_info.raw.json"
    info_path.write_text('{"book_name": "Broken",', encoding="utf-8")
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("book metadata is corrupt" in line for line in messages.error)
    assert any(str(info_path) in line for line in messages.error)


def test_export_metadata_with_unexpected_structure_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid JSON in the wrong shape is corrupt, not an empty book."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    book_dir = tmp_path / "raw_data" / "b520" / "123"
    book_dir.mkdir(parents=True)
    (book_dir / "book_info.raw.json").write_text(
        json.dumps({"book_name": "Broken", "volumes": "not-a-list"}),
        encoding="utf-8",
    )
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any("unexpected structure" in line for line in messages.error)


def test_export_checks_the_stage_the_exporter_will_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The preflight must read the pipeline's latest stage, not always ``raw``."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client, books=())
    book_dir = _write_export_book(tmp_path / "raw_data", book_id="123", volumes=[])
    # ``cleaner`` is the latest stage with both a database and metadata; the
    # exporter will read it, so its chapters are the ones that matter.
    (book_dir / "chapter.cleaner.sqlite").write_bytes(b"")
    (book_dir / "book_info.cleaner.json").write_text(
        json.dumps(
            {
                "book_name": "Title",
                "author": "Author",
                "volumes": [
                    {
                        "volume_name": "Volume 1",
                        "chapters": [{"chapterId": "c1", "title": "Chapter 1"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (book_dir / "pipeline.json").write_text(
        json.dumps({"pipeline": ["raw", "cleaner"], "executed": {}}),
        encoding="utf-8",
    )
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    args.func(args)

    assert client.calls == [("123", ("txt",), None)]


def test_export_missing_book_between_successes_is_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One missing book must not stop the others, but the run must exit 1."""
    client = _FakeExportClient(
        results_by_book={
            "1": {"txt": [tmp_path / "one.txt"]},
            "3": {"txt": [tmp_path / "three.txt"]},
        }
    )
    _export_env(monkeypatch, tmp_path, client, books=("1", "3"))
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "1", "2", "3", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == [
        ("1", ("txt",), None),
        ("3", ("txt",), None),
    ]
    assert any("book data not found" in line for line in messages.error)
    assert any("Export failed" in line and "2" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert [entry["book_id"] for entry in payload["books"]] == ["1", "2", "3"]
    assert payload["books"][1]["exported"] == {}


def test_export_non_mapping_result_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A third-party client returning ``None`` must not crash the report."""
    client = _FakeExportClient()
    client.results = None  # type: ignore[assignment]
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("unexpected export result" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [{"book_id": "123", "exported": {}}]


def test_export_unknown_format_names_the_failed_format(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unsupported format is a runtime failure that names the format."""
    client = _FakeExportClient({"pdf": []})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "pdf", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == [("123", ("pdf",), None)]
    assert any("(pdf)" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [{"book_id": "123", "exported": {"pdf": []}}]


def test_export_partial_format_failure_names_only_the_failed_format(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One successful format must not hide another one that wrote nothing."""
    target = tmp_path / "book.txt"
    client = _FakeExportClient({"txt": [target], "epub": []})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "epub", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("(epub)" in line for line in messages.error)
    assert not any("(txt)" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][0]["exported"] == {
        "txt": [str(target)],
        "epub": [],
    }


def test_export_normalizes_format_case_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeExportClient(
        {"epub": [tmp_path / "a.epub"], "txt": [tmp_path / "a.txt"]}
    )
    _export_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "EPUB", "Txt", "TXT", "--json"]
    )
    args.func(args)

    assert client.calls == [("123", ("epub", "txt"), None)]
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["books"][0]["exported"]) == {"epub", "txt"}


def test_export_rejects_a_blank_format_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", ""]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert client.calls == []
    assert any("Invalid input" in line for line in messages.error)


def test_export_json_is_a_single_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` output must remain one parseable line for line readers."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--json"]
    )
    args.func(args)

    out = capsys.readouterr().out.strip()
    assert out
    assert len(out.splitlines()) == 1
    assert json.loads(out)["site"] == "b520"


def test_export_rejects_an_output_path_that_is_a_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A misconfigured output_dir must fail once, before any book is touched."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    raw_root = tmp_path / "raw"
    _write_export_book(raw_root, book_id="123")
    out_file = tmp_path / "output.txt"
    out_file.write_text("not a directory", encoding="utf-8")
    config = _general_config(
        tmp_path, raw_data_dir=str(raw_root), output_dir=str(out_file)
    )
    monkeypatch.setattr(export, "load_or_init_config", lambda *_a, **_k: config)
    monkeypatch.setattr(export.registrar, "get_client", lambda *_a, **_k: client)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any(str(out_file) in line for line in messages.error)
    assert any("not a directory" in line for line in messages.error)


def test_export_rejects_an_unwritable_output_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A read-only output directory is detected before export starts."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    out_dir = tmp_path / "downloads"

    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(tempfile, "mkstemp", _deny)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any(str(out_dir) in line and "denied" in line for line in messages.error)


def test_export_out_overrides_config_and_creates_missing_parents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--out`` is expanded, validated, and created even without its parents."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    monkeypatch.setenv("ND_EXPORT_OUT", str(tmp_path / "custom"))
    out_dir = tmp_path / "custom" / "nested" / "books"
    seen: dict[str, Any] = {}

    def _get_client(_site: str, client_config: Any) -> Any:
        seen["config"] = client_config
        return client

    monkeypatch.setattr(export.registrar, "get_client", _get_client)
    _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        [
            "export",
            "--site",
            "b520",
            "123",
            "--format",
            "txt",
            "--out",
            "$ND_EXPORT_OUT/nested/books",
        ]
    )
    args.func(args)

    assert seen["config"].output_dir == str(out_dir)
    assert out_dir.is_dir()


def test_export_out_that_is_an_existing_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ``--out`` path pointing at a file fails once, before any export."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    out_file = tmp_path / "out.txt"
    out_file.write_text("not a directory", encoding="utf-8")
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--out", str(out_file)]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any(str(out_file) in line for line in messages.error)
    assert any("not a directory" in line for line in messages.error)


def test_export_out_unwritable_directory_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A read-only ``--out`` directory is detected before export starts."""
    client = _FakeExportClient({"txt": [tmp_path / "book.txt"]})
    _export_env(monkeypatch, tmp_path, client)
    out_dir = tmp_path / "readonly"
    out_dir.mkdir()

    def _deny(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(tempfile, "mkstemp", _deny)
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "123", "--format", "txt", "--out", str(out_dir)]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == []
    assert any(str(out_dir) in line and "denied" in line for line in messages.error)


def test_export_duplicate_output_path_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two books colliding on one filename must not silently lose an export."""
    shared = tmp_path / "downloads" / "same.txt"
    client = _FakeExportClient(
        results_by_book={
            "1": {"txt": [shared]},
            "2": {"txt": [shared]},
        }
    )
    _export_env(monkeypatch, tmp_path, client, books=("1", "2"))
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "1", "2", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("1" in line and "2" in line for line in messages.error)
    assert any(str(shared) in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [
        {"book_id": "1", "exported": {"txt": [str(shared)]}},
        {"book_id": "2", "exported": {"txt": [str(shared)]}},
    ]


def test_export_exception_for_one_book_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A raising client must not prevent later books from exporting."""
    second = tmp_path / "second.txt"
    client = _FakeExportClient(
        results_by_book={"2": {"txt": [second]}},
        errors_by_book={"1": RuntimeError("export boom")},
    )
    _export_env(monkeypatch, tmp_path, client, books=("1", "2"))
    messages = _capture_ui(monkeypatch, export)

    args = _parser(ExportCmd).parse_args(
        ["export", "--site", "b520", "1", "2", "--format", "txt", "--json"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.calls == [
        ("1", ("txt",), None),
        ("2", ("txt",), None),
    ]
    assert any("export boom" in line for line in messages.error)
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"] == [
        {"book_id": "1", "exported": {}},
        {"book_id": "2", "exported": {"txt": [str(second)]}},
    ]


def test_expand_config_paths_leaves_plain_relative_paths_alone() -> None:
    config = {"general": {"output_dir": "./downloads", "raw_data_dir": "raw"}}

    expanded = base.expand_config_paths(config)

    assert expanded == config
    assert expanded is config


# ---------------------------------------------------------------------------
# sources: --json and error paths through the parser
# ---------------------------------------------------------------------------


def _fake_rule() -> Any:
    return SimpleNamespace(
        display_name="Demo Source",
        search=object(),
        hosts=("demo.example",),
    )


def test_sources_list_json_emits_payload_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [("demo", _fake_rule(), {"origin": "builtin"})],
    )

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--json"])
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["site_key"] == "demo"
    assert payload[0]["name"] == "Demo Source"
    assert payload[0]["search"] is True
    assert payload[0]["hosts"] == ["demo.example"]
    assert payload[0]["origin"] == "builtin"
    assert payload[0]["status"] is None


def test_sources_list_json_probe_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [("broken", _fake_rule(), {"origin": "user"})],
    )
    monkeypatch.setattr(
        SourcesCmd,
        "_probe",
        classmethod(lambda _cls, _key, _has_search: "FAIL: boom"),
    )

    args = _parser(SourcesCmd).parse_args(["sources", "list", "--check", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "FAIL: boom"


def test_sources_defaults_to_list_without_a_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("novel_downloader.plugins.rule.iter_rules", lambda: [])
    messages = _capture_ui(monkeypatch, sources)

    args = _parser(SourcesCmd).parse_args(["sources"])
    args.func(args)  # action is None -> list

    assert any("No rule-based sources" in message for message in messages.warn)


def test_sources_check_missing_file_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages = _capture_ui(monkeypatch, sources)

    args = _parser(SourcesCmd).parse_args(
        ["sources", "check", str(tmp_path / "missing.json")]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    # A nonexistent rule path is a bad argument (usage error, exit 2), not a
    # runtime failure -- aligned with ``sources add`` and the CLI contract.
    assert excinfo.value.code == 2
    assert any("not found" in message for message in messages.error)


def test_sources_add_missing_file_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages = _capture_ui(monkeypatch, sources)

    args = _parser(SourcesCmd).parse_args(
        ["sources", "add", str(tmp_path / "missing.json")]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("not found" in message for message in messages.error)


# ---------------------------------------------------------------------------
# search: "every site failed" is a failure, not an empty result set
# ---------------------------------------------------------------------------


def _search_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub config loading and logging so ``search`` never touches the disk."""
    from novel_downloader.apps.cli.commands import search as search_module

    monkeypatch.setattr(
        search_module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(search_module.ui, "setup_logging", lambda **_k: None)
    return search_module


def _search_args(argv: list[str]) -> argparse.Namespace:
    from novel_downloader.apps.cli.commands.search import SearchCmd

    return _parser(SearchCmd).parse_args(["search", *argv])


def test_search_every_site_failed_exits_one_and_keeps_json_pure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from novel_downloader.plugins.search import SearchEvent

    _search_env(monkeypatch)

    async def fake_search(**kwargs: Any) -> list[Any]:
        on_event = kwargs["on_event"]
        on_event(SearchEvent(site="b520", kind="failed", message="timeout"))
        on_event(SearchEvent(site="qidian", kind="failed", message="refused"))
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    args = _search_args(["--json", "斗破苍穹"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    # An outage must not look like "no such book": exit 1, and the (empty)
    # document is still emitted so a machine consumer can parse stdout.
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "0 site(s) completed / 2 failed" in captured.err


def test_search_every_site_failed_exits_one_without_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.plugins.search import SearchEvent

    search_module = _search_env(monkeypatch)
    messages = _capture_ui(monkeypatch, search_module)

    async def fake_search(**kwargs: Any) -> list[Any]:
        kwargs["on_event"](SearchEvent(site="b520", kind="failed", message="timeout"))
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    args = _search_args(["斗破苍穹"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    # The per-site failure line is the diagnostic; the misleading "no results"
    # warning is not printed for an outage.
    assert not any("No results found" in message for message in messages.warn)


def test_search_partial_site_failure_is_not_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.plugins.search import SearchEvent

    search_module = _search_env(monkeypatch)
    messages = _capture_ui(monkeypatch, search_module)

    async def fake_search(**kwargs: Any) -> list[Any]:
        on_event = kwargs["on_event"]
        on_event(SearchEvent(site="b520", kind="failed", message="timeout"))
        on_event(SearchEvent(site="qidian", kind="completed", result_count=0))
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    args = _search_args(["斗破苍穹"])
    args.func(args)  # one site answered: not an outage, so a normal exit

    assert any("No results found" in message for message in messages.warn)


def test_search_without_events_keeps_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubbed/legacy search that reports no events is still a success."""
    search_module = _search_env(monkeypatch)
    messages = _capture_ui(monkeypatch, search_module)

    async def fake_search(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    args = _search_args(["nothing"])
    args.func(args)  # no SystemExit: an event-less stub cannot look like an outage

    assert any("No results found" in message for message in messages.warn)


def test_search_config_path_expands_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from novel_downloader.apps.cli.commands import search as search_module

    monkeypatch.setenv("SEARCH_CFG_DIR", str(tmp_path))
    monkeypatch.setattr(search_module.ui, "setup_logging", lambda **_k: None)
    seen: dict[str, Path | None] = {}

    def _load(path: Path | None) -> dict[str, Any]:
        seen["path"] = path
        return {"general": {}}

    monkeypatch.setattr(search_module, "load_or_init_config", _load)

    async def fake_search(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    # Windows expands ``%VAR%``, POSIX expands ``$VAR``; the CLI must accept
    # whichever form the user's shell leaves on the command line.
    ref = "%SEARCH_CFG_DIR%" if os.name == "nt" else "$SEARCH_CFG_DIR"
    raw = os.path.join(ref, "s.toml")
    args = _search_args(["--config", raw, "x"])
    args.func(args)

    assert seen["path"] == Path(os.path.expandvars(raw))
    assert str(seen["path"]).startswith(str(tmp_path))


def _search_download_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    export: Any,
) -> tuple[Any, Any]:
    """Wire ``search --first`` to a fake client without touching the network."""
    from novel_downloader.apps.cli.commands import search as search_module

    _search_env(monkeypatch)

    class _SearchDownloadUI:
        def __init__(self) -> None:
            self.completed_books: set[Any] = set()

        async def on_start(self, _book: Any) -> None:
            return None

        async def on_progress(self, _done: int, _total: int) -> None:
            return None

        async def on_complete(self, book: Any, *, failed: int = 0) -> None:
            self.completed_books.add(book)

    class _Client:
        def __init__(self) -> None:
            self.exported: list[str] = []
            self.processed: list[str] = []

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def download_book(self, book: Any, *, ui: Any = None) -> None:
            if ui is not None:
                await ui.on_complete(book)

        def process_book(
            self, book: Any, processors: Any = None, *, ui: Any = None
        ) -> None:
            self.processed.append(book.book_id)

        def export_book(
            self, book: Any, *, cfg: Any = None, formats: Any = None, ui: Any = None
        ) -> Any:
            self.exported.append(book.book_id)
            return export(book)

    client = _Client()
    monkeypatch.setattr(search_module, "CLIDownloadUI", _SearchDownloadUI)
    monkeypatch.setattr(search_module, "CLIProcessUI", lambda: object())
    monkeypatch.setattr(search_module, "CLIExportUI", lambda: object())
    monkeypatch.setattr(search_module.registrar, "get_client", lambda *_a, **_k: client)
    return search_module, client


def _install_first_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return one downloadable result and a successful per-site event."""
    from novel_downloader.plugins.search import SearchEvent

    async def fake_search(**kwargs: Any) -> list[Any]:
        kwargs["on_event"](SearchEvent(site="b520", kind="completed", result_count=1))
        return [{"site": "b520", "book_id": "123"}]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)


def test_search_first_download_that_exports_nothing_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downloaded book with no export file is a failure, naming the book."""
    search_module, client = _search_download_env(
        monkeypatch, export=lambda _book: {"txt": []}
    )
    messages = _capture_ui(monkeypatch, search_module)
    _install_first_search(monkeypatch)

    args = _search_args(["--first", "斗破苍穹"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert client.processed == ["123"]
    assert client.exported == ["123"]
    assert any("Export failed" in line and "123" in line for line in messages.error)


def test_search_first_download_with_an_export_file_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_module, client = _search_download_env(
        monkeypatch, export=lambda _book: {"txt": [Path("out.txt")]}
    )
    _capture_ui(monkeypatch, search_module)
    _install_first_search(monkeypatch)

    args = _search_args(["--first", "斗破苍穹"])
    args.func(args)  # no SystemExit

    assert client.exported == ["123"]


def test_search_first_export_exception_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_book: Any) -> Any:
        raise RuntimeError("write denied")

    search_module, _client = _search_download_env(monkeypatch, export=_raise)
    messages = _capture_ui(monkeypatch, search_module)
    _install_first_search(monkeypatch)

    args = _search_args(["--first", "斗破苍穹"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("Process/export failed for book 123" in line for line in messages.error)


# ---------------------------------------------------------------------------
# doctor: an unbindable --port is a usage error, never a traceback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", ["-1", "0", "65536", "70000"])
def test_doctor_rejects_out_of_range_port(
    port: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from novel_downloader.apps.cli.commands import doctor as doctor_module
    from novel_downloader.apps.cli.commands.doctor import DoctorCmd

    messages = _capture_ui(monkeypatch, doctor_module)

    args = _parser(DoctorCmd).parse_args(["doctor", f"--port={port}", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Invalid input" in message for message in messages.error)
    # The failure happens before any check runs, so no JSON document is left
    # half-written on stdout.
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# browse: --json exits before selection, so --first cannot be honoured
# ---------------------------------------------------------------------------


def test_browse_rejects_json_with_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli.commands import browse as browse_module
    from novel_downloader.apps.cli.commands.browse import BrowseCmd

    messages = _capture_ui(monkeypatch, browse_module)

    args = _parser(BrowseCmd).parse_args(["browse", "fanqienovel", "--json", "--first"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("cannot be combined" in message for message in messages.error)


# ---------------------------------------------------------------------------
# clean: --site is joined onto a data root before rmtree -- it must not escape
# ---------------------------------------------------------------------------


_ESCAPING_SITES = ["..", "../..", "b520/../..", "..\\..", ".", ".. ", "..."]


@pytest.mark.parametrize("value", [".. ", "...", "a.", "a ", " . "])
def test_path_component_rejects_windows_trailing_normalisation(value: str) -> None:
    """Windows strips trailing dots/spaces, turning ``".. "`` into ``".."``."""
    assert not base.is_safe_path_component(value)


@pytest.mark.parametrize("value", ["b520", "8_8187", "a.b", "a..b"])
def test_path_component_accepts_plain_identifiers(value: str) -> None:
    assert base.is_safe_path_component(value)


@pytest.mark.parametrize("site", _ESCAPING_SITES)
def test_clean_cache_refuses_site_that_escapes_the_cache_directory(
    site: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "b520").mkdir(parents=True)
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", site, "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages.error)
    # Nothing outside (or inside) the cache was touched.
    assert cache.is_dir()
    assert (cache / "b520").is_dir()
    assert marker.is_file()


def test_clean_cache_refuses_absolute_site_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "cache", "--site", str(tmp_path), "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages.error)
    assert cache.is_dir()
    assert tmp_path.is_dir()


def test_clean_book_all_refuses_site_that_escapes_the_raw_data_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    (raw / "b520").mkdir(parents=True)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "--all", "--site", "..", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported site" in message for message in messages.error)
    assert raw.is_dir()
    assert (raw / "b520").is_dir()


@pytest.mark.parametrize(
    "book_id",
    ["..", "../..", "a/b", "a\\b", "C:evil", "C:\\Windows", ".. ", "...", "a."],
)
def test_clean_book_refuses_escaping_book_id_before_config_load(
    book_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A book ID is joined onto the raw-data root before any deletion."""
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", book_id, "--site", "b520", "--all", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Invalid input" in message for message in messages.error)


def test_clean_book_refuses_absolute_book_id_before_config_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)
    outside = tmp_path / "outside"
    outside.mkdir()

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", str(outside), "--site", "b520", "--all", "--yes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Invalid input" in message for message in messages.error)
    assert outside.is_dir()


@pytest.mark.parametrize(
    "stage", ["..", "a/b", "a\\b", "C:evil", "C:\\Windows", ".. ", "...", "raw "]
)
def test_clean_book_refuses_unsafe_stage_before_config_load(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--stage`` becomes part of a file name under the book directory."""
    monkeypatch.setattr(clean, "load_or_init_config", _refuse_config_load)
    messages = _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        [
            "clean",
            "book",
            "1",
            "--site",
            "b520",
            "--stage",
            stage,
            "--yes",
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("Unsupported stage" in message for message in messages.error)


def test_clean_cache_removes_junction_not_its_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    target = tmp_path / "cache-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    _make_dir_link(cache / "b520", target)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "b520", "--yes"])
    args.func(args)

    assert not os.path.lexists(cache / "b520")
    assert (target / "keep.txt").is_file()


def test_clean_cache_does_not_follow_nested_junction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "b520").mkdir(parents=True)
    target = tmp_path / "nested-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    _make_dir_link(cache / "b520" / "nested", target)
    monkeypatch.setattr(
        clean, "load_or_init_config", lambda *_a, **_k: _cache_config(tmp_path)
    )
    _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(["clean", "cache", "--site", "b520", "--yes"])
    args.func(args)

    assert not (cache / "b520").exists()
    assert (target / "keep.txt").is_file()


def test_clean_book_all_removes_junction_not_its_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    target = tmp_path / "site-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    _make_dir_link(raw / "b520", target)
    monkeypatch.setattr(
        clean,
        "load_or_init_config",
        lambda *_a, **_k: _general_config(tmp_path, raw_data_dir=str(raw)),
    )
    _capture_ui(monkeypatch, clean)

    args = _parser(CleanCmd).parse_args(
        ["clean", "book", "--all", "--site", "b520", "--yes"]
    )
    args.func(args)

    assert not os.path.lexists(raw / "b520")
    assert (target / "keep.txt").is_file()
