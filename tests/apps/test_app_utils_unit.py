#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.apps.utils`.

``load_or_init_config`` is exercised through monkeypatched config helpers and
a scripted ui, so no real settings.toml is read or written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_downloader.apps import utils as app_utils
from novel_downloader.apps.utils import load_or_init_config


def test_load_success_passes_path_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel: dict[str, object] = {"ok": True}
    seen: list[Path | None] = []

    def _fake_load(path: Path | None) -> dict[str, object]:
        seen.append(path)
        return sentinel

    monkeypatch.setattr(app_utils, "load_config", _fake_load)
    result = load_or_init_config(tmp_path / "settings.toml")
    assert result is sentinel
    assert seen == [tmp_path / "settings.toml"]


def test_missing_config_and_user_declines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(path: Path | None) -> dict[str, object]:
        raise FileNotFoundError(path)

    monkeypatch.setattr(app_utils, "load_config", _raise)
    monkeypatch.setattr("novel_downloader.apps.utils.ui.confirm", lambda *a, **k: False)

    result = load_or_init_config(tmp_path / "settings.toml")
    assert result is None
    assert not (tmp_path / "settings.toml").exists()


def test_missing_config_and_user_accepts_copies_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(path: Path | None) -> dict[str, object]:
        raise FileNotFoundError(path)

    copied: list[Path] = []

    def _fake_copy(path: Path) -> None:
        copied.append(path)

    monkeypatch.setattr(app_utils, "load_config", _raise)
    monkeypatch.setattr(app_utils, "copy_default_config", _fake_copy)
    monkeypatch.setattr("novel_downloader.apps.utils.ui.confirm", lambda *a, **k: True)

    result = load_or_init_config(tmp_path / "settings.toml")
    assert result is None
    assert copied == [tmp_path / "settings.toml"]


def test_invalid_config_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(path: Path | None) -> dict[str, object]:
        raise ValueError("bad toml")

    monkeypatch.setattr(app_utils, "load_config", _raise)
    result = load_or_init_config(tmp_path / "settings.toml")
    assert result is None


def test_default_path_is_settings_toml_in_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Path | None] = []

    def _raise(path: Path | None) -> dict[str, object]:
        seen.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(app_utils, "load_config", _raise)
    monkeypatch.setattr("novel_downloader.apps.utils.ui.confirm", lambda *a, **k: False)
    monkeypatch.chdir(tmp_path)

    load_or_init_config()
    assert seen == [None]  # load_config called with the given None
