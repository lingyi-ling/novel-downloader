#!/usr/bin/env python3
"""
tests.apps.test_cli_noninteractive_config
-----------------------------------------

Regression tests for the "no ``settings.toml``" automation path.

Historically a command run without a configuration file -- ``search --json``
in CI, for example -- reached ``load_or_init_config`` and asked "Would you like
to create a default config?".  A non-TTY stdin could not answer, so the
command exited 1 without producing its JSON document.  Every command now tells
the loader whether prompting is allowed; when it is not, the bundled defaults
are used in memory and no file is written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps import utils as app_utils
from novel_downloader.apps.cli import main as cli_main_module


def _must_not_prompt(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("non-interactive config loading must not prompt")


def _must_not_write(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("non-interactive config loading must not write a file")


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run inside ``tmp_path`` with an isolated application home and language."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVEL_LANG", "en_US")


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Call ``cli_main`` in-process and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["novel-cli", *argv])
    try:
        cli_main_module.cli_main()
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    return 0


# ---------------------------------------------------------------------------
# load_or_init_config contract
# ---------------------------------------------------------------------------


def test_non_interactive_missing_config_uses_defaults_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)
    monkeypatch.setattr(app_utils, "copy_default_config", _must_not_write)

    config = app_utils.load_or_init_config(non_interactive=True)

    assert config is not None
    assert config["general"]["output_dir"] == "./downloads"
    assert not (tmp_path / "settings.toml").exists()
    assert not (tmp_path / "home" / "settings.toml").exists()


def test_non_interactive_also_applies_to_an_explicit_missing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)
    monkeypatch.setattr(app_utils, "copy_default_config", _must_not_write)

    config = app_utils.load_or_init_config(
        tmp_path / "missing.toml", non_interactive=True
    )

    assert config is not None
    assert not (tmp_path / "missing.toml").exists()


def test_read_only_app_home_is_not_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing config must not be created under an unwritable app home."""
    _isolate(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    # Any attempt to copy the default config into the home/cwd fails the test,
    # which is what a read-only directory would make happen for real.
    monkeypatch.setattr(app_utils, "copy_default_config", _must_not_write)
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)

    config = app_utils.load_or_init_config(non_interactive=True)

    assert config is not None
    assert list(home.iterdir()) == []


def test_interactive_mode_still_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit interactive callers keep the historical prompt."""
    _isolate(monkeypatch, tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(
        app_utils.ui,
        "confirm",
        lambda message, *_a, **_k: asked.append(message) or False,
    )

    result = app_utils.load_or_init_config()

    assert result is None
    assert len(asked) == 1
    assert "Would you like to create a default config?" in asked[0]


# ---------------------------------------------------------------------------
# Commands: non-TTY + --json
# ---------------------------------------------------------------------------


def test_search_json_non_tty_without_config_is_parseable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)
    monkeypatch.setattr("novel_downloader.apps.cli.ui.setup_logging", lambda **_k: None)

    async def fake_search(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = _run_cli(
        monkeypatch, ["search", "--json", "--site", "qidian", "--limit", "1", "test"]
    )

    out, err = capsys.readouterr()
    assert code == 0
    assert json.loads(out) == []
    assert "Would you like to create a default config?" not in err
    assert not (tmp_path / "settings.toml").exists()


def test_search_json_on_a_tty_also_skips_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` alone is enough: automation must not depend on stdin."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)
    monkeypatch.setattr("novel_downloader.apps.cli.ui.setup_logging", lambda **_k: None)

    async def fake_search(**_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    code = _run_cli(monkeypatch, ["search", "--json", "--site", "qidian", "test"])

    out, _err = capsys.readouterr()
    assert code == 0
    assert json.loads(out) == []
    assert not (tmp_path / "settings.toml").exists()


def test_download_json_non_tty_without_config_is_parseable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)
    monkeypatch.setattr("novel_downloader.apps.cli.ui.setup_logging", lambda **_k: None)

    from novel_downloader.apps.cli.commands import download as download_module

    def _unsupported(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("offline test")

    monkeypatch.setattr(download_module.registrar, "get_client", _unsupported)

    code = _run_cli(monkeypatch, ["download", "--json", "--site", "b520", "1"])

    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["site"] == "b520"
    assert payload["books"][0]["book_id"] == "1"
    # The site is reported as unsupported, so the run exits non-zero -- but it
    # still produced the promised JSON document and never prompted.
    assert code == 1
    assert "Would you like to create a default config?" not in err
    assert not (tmp_path / "settings.toml").exists()


def test_music_search_json_non_tty_without_config_is_parseable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(app_utils.ui, "confirm", _must_not_prompt)

    from novel_downloader.apps.cli.commands import music as music_module

    class _Provider:
        def search(self, _keyword: str, *, limit: int) -> tuple[list[Any], int]:
            return [], 0

        def is_logged_in(self) -> bool:
            return True

    class _Engine:
        login_expired = RuntimeError

        def labels(self) -> list[tuple[str, str]]:
            return [("netease", "NetEase")]

        def platform_keys(self) -> list[str]:
            return ["netease"]

        def platform_note(self, _platform: str) -> None:
            return None

        def create(self, _platform: str) -> _Provider:
            return _Provider()

    monkeypatch.setattr(music_module, "_require_engine", _Engine)

    code = _run_cli(
        monkeypatch, ["music", "search", "hello", "--platform", "netease", "--json"]
    )

    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["platform"] == "netease"
    assert payload["tracks"] == []
    assert code == 0
    assert "Would you like to create a default config?" not in err
    assert not (tmp_path / "settings.toml").exists()
