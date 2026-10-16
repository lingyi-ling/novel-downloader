#!/usr/bin/env python3
"""
tests.apps.test_desktop
-----------------------

Smoke tests for the desktop entry point.

These deliberately run the app in a *subprocess*: :func:`desktop_main` pins the
process working directory and exports ``NOVEL_HOME``, which must not leak into
the test session.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_desktop(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "novel_downloader.apps.desktop", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_importing_desktop_main_does_not_pull_nicegui() -> None:
    """Importing the entry point must not import NiceGUI or start a window."""
    code = (
        "import sys\n"
        "import novel_downloader.apps.desktop.main as m\n"
        "assert callable(m.desktop_main)\n"
        "assert 'nicegui' not in sys.modules, 'importing desktop pulled nicegui'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_check_reports_and_seeds_config(tmp_path: Path) -> None:
    result = _run_desktop("--home", str(tmp_path), "--check")

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    # ``--check`` copies the bundled default config into the home directory.
    assert (tmp_path / "settings.toml").is_file()
    assert str(tmp_path) in result.stdout


def test_window_size_parses_wxh() -> None:
    from novel_downloader.apps.desktop.main import _window_size

    assert _window_size("1100x800") == (1100, 800)


def test_window_size_rejects_garbage() -> None:
    from novel_downloader.apps.desktop.main import _window_size

    with pytest.raises(argparse.ArgumentTypeError):
        _window_size("huge")
