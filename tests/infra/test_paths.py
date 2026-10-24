"""Tests for :mod:`novel_downloader.infra.paths`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra import paths

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# app_home resolution
# ---------------------------------------------------------------------------
def test_package_name_is_stable() -> None:
    assert paths.PACKAGE_NAME == "novel_downloader"


def test_app_home_uses_novel_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "custom-home"
    monkeypatch.setenv("NOVEL_HOME", str(target))

    assert paths.app_home() == target.resolve()


def test_app_home_relies_on_platformdirs_when_no_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = tmp_path / "platform-data"
    calls: list[tuple[str, Any]] = []

    def fake_user_data_path(name: str, appauthor: Any = None) -> Path:
        calls.append((name, appauthor))
        return sentinel

    monkeypatch.delenv("NOVEL_HOME", raising=False)
    monkeypatch.setattr(paths, "user_data_path", fake_user_data_path)

    assert paths.app_home() == sentinel
    assert calls == [(paths.PACKAGE_NAME, False)]


def test_empty_override_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = tmp_path / "platform-data"
    monkeypatch.setenv("NOVEL_HOME", "")
    monkeypatch.setattr(paths, "user_data_path", lambda *_args, **_kwargs: sentinel)

    assert paths.app_home() == sentinel


def test_relative_override_resolves_against_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVEL_HOME", "relative-home")

    assert paths.app_home() == (tmp_path / "relative-home").resolve()


def test_tilde_override_is_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    _point_home_at(monkeypatch, home)
    monkeypatch.setenv("NOVEL_HOME", "  ~/novel-home  ")

    assert paths.app_home() == (home / "novel-home").resolve()


def test_whitespace_only_override_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blank override must not create a directory literally named ``" "``."""
    sentinel = tmp_path / "platform-data"
    monkeypatch.setenv("NOVEL_HOME", "   ")
    monkeypatch.setattr(paths, "user_data_path", lambda *_args, **_kwargs: sentinel)

    assert paths.app_home() == sentinel


def test_app_home_does_not_create_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``app_home`` is a pure resolver; consumers create the directory.

    Directory creation is covered by :class:`StateManager` (on save),
    ``setup_logging`` (log dir), and the desktop bootstrap.
    """
    target = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv("NOVEL_HOME", str(target))

    result = paths.app_home()

    assert result == target.resolve()
    assert not result.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_windows_absolute_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVEL_HOME", r"C:\Users\Public\NovelHome")

    result = paths.app_home()

    assert isinstance(result, Path)
    assert result.is_absolute()
    assert result.drive.upper() == "C:"
    assert result.name == "NovelHome"


def test_state_manager_creates_a_missing_app_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first persisted preference materializes a missing home directory."""
    from novel_downloader.infra.persistence.state import StateManager

    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "fresh-home"))
    home = paths.app_home()
    assert not home.exists()

    StateManager(home / "state.json").set_language("en_US")

    assert home.is_dir()
    assert (home / "state.json").is_file()


def test_state_path_lives_under_app_home() -> None:
    assert paths.app_home() / "state.json" == paths.STATE_PATH
    assert paths.STATE_PATH.is_absolute()


def _point_home_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make ``Path.expanduser()`` resolve ``~`` to ``home`` on this platform."""
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
        if home.drive:
            monkeypatch.setenv("HOMEDRIVE", home.drive)
            monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive) :])
    else:
        monkeypatch.setenv("HOME", str(home))


# ---------------------------------------------------------------------------
# Resource paths
# ---------------------------------------------------------------------------
RESOURCE_CONSTANTS = [
    "DEFAULT_CONFIG_FILE",
    "APP_ICON_PATH",
    "APP_FAVICON_PATH",
    "EPUB_CSS_STYLE_PATH",
    "HTML_CSS_INDEX_PATH",
    "HTML_CSS_CHAPTER_PATH",
    "FANQIENOVEL_MAP_PATH",
    "HONGXIUZHAO_MAP_PATH",
    "LINOVELIB_MAP_PATH",
    "N69YUE_MAP_PATH",
    "XIGUASHUWU_MAP_PATH",
    "YODU_MAP_PATH",
    "EXPR_TO_JSON_SCRIPT_PATH",
    "QD_DECRYPT_SCRIPT_PATH",
    "QQ_DECRYPT_SCRIPT_PATH",
    "HTML_JS_MAIN_PATH",
]


@pytest.mark.parametrize("constant", RESOURCE_CONSTANTS)
def test_resource_file_exists_and_is_not_empty(constant: str) -> None:
    resource = getattr(paths, constant)

    assert resource.is_file(), f"{constant} does not point at a file: {resource}"
    assert resource.read_bytes(), f"{constant} is empty"


def test_icon_assets_have_valid_signatures() -> None:
    ico = paths.APP_ICON_PATH.read_bytes()
    png = paths.APP_FAVICON_PATH.read_bytes()

    assert ico[:4] == b"\x00\x00\x01\x00"
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_default_config_filename_constant() -> None:
    assert paths.DEFAULT_CONFIG_FILENAME == "settings.toml"


def test_apps_package_directory_exists() -> None:
    apps = ROOT / "src" / "novel_downloader" / "apps"

    assert apps.is_dir()
    assert (apps / "__init__.py").is_file()


def test_resources_package_directory_exists() -> None:
    resources = ROOT / "src" / "novel_downloader" / "resources"

    assert resources.is_dir()
    for child in ("config", "icons", "json", "scripts", "styles"):
        assert (resources / child).is_dir(), f"missing resource dir: {child}"
