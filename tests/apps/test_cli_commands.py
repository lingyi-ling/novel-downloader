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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli.commands import (
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


def test_config_init_skips_when_overwrite_is_declined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "settings.toml"
    target.write_text("original", encoding="utf-8")
    messages = _capture_ui(monkeypatch, config)
    monkeypatch.setattr(config.ui, "confirm", lambda *_a, **_k: False)

    args = _parser(ConfigCmd).parse_args(["config", "init"])
    args.func(args)

    assert target.read_text(encoding="utf-8") == "original"
    assert any("Skipped" in message for message in messages.warn)


def test_config_init_force_overwrites_existing_file(
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

    monkeypatch.setattr(clean.shutil, "rmtree", _boom)

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


class _FakeDownloadClient:
    def __init__(
        self,
        *,
        complete: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self.complete = complete
        self.error = error
        self.downloaded: list[str] = []

    async def __aenter__(self) -> _FakeDownloadClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def download_book(self, book: Any, *, ui: _FakeDownloadUI) -> None:
        if self.error is not None:
            raise self.error
        self.downloaded.append(book.book_id)
        if self.complete:
            ui.completed_books.add(book)


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
    payload = json.loads(capsys.readouterr().out)
    assert payload["books"][0]["downloaded"] is False


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


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class _FakeExportClient:
    def __init__(self, results: dict[str, list[Path]]) -> None:
        self.results = results
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
        return self.results


def _export_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeExportClient | None = None,
) -> None:
    monkeypatch.setattr(
        export, "load_or_init_config", lambda *_a, **_k: _general_config(tmp_path)
    )
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
