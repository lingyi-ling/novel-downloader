#!/usr/bin/env python3
"""
tests.apps.test_login
---------------------

Unit tests for the interactive login helper (no real browser window is opened:
``webview`` is replaced with a stub module).
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from argparse import Namespace
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from novel_downloader.apps.login import main as login_main_module
from novel_downloader.apps.login.main import (
    COOKIE_FILENAME,
    _looks_logged_in,
    login_main,
    run_login,
    save_cookies,
    site_cache_dir,
)

ROOT = Path(__file__).resolve().parents[2]


def test_import_does_not_pull_webview() -> None:
    """Importing the module must keep the optional pywebview import lazy."""
    code = (
        "import sys\n"
        "import novel_downloader.apps.login.main as m\n"
        "assert callable(m.run_login)\n"
        "assert 'webview' not in sys.modules, 'importing login pulled webview'\n"
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


def test_site_cache_dir_honours_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    assert site_cache_dir("fanqienovel") == tmp_path.resolve() / "fanqienovel"


def test_save_cookies_writes_aiohttp_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))

    path = save_cookies("fanqienovel", {"sessionid": "abc", "t": "1"})

    assert path == tmp_path / "fanqienovel" / COOKIE_FILENAME
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"name": "sessionid", "value": "abc"},
        {"name": "t", "value": "1"},
    ]


def test_looks_logged_in_without_probe() -> None:
    # A site without a probe endpoint trusts any non-empty cookie jar.
    assert _looks_logged_in("unknown-site", {"a": "1"}) is True
    assert _looks_logged_in("unknown-site", {}) is False


def test_run_login_rejects_unsupported_site() -> None:
    messages: list[str] = []
    assert run_login("nope", notify=messages.append) is False
    assert messages and "nope" in messages[0]


def test_run_login_saves_cookies_from_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_CACHE_DIR", str(tmp_path))
    # Skip the network probe; any cookie then counts as a logged-in session.
    monkeypatch.setattr(login_main_module, "_LOGIN_PROBES", {})

    jar = SimpleCookie()
    jar["sessionid"] = "secret"
    window = _FakeWindow(jar)
    fake = _FakeWebview(window)
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert run_login("fanqienovel", timeout=30.0, notify=lambda _msg: None) is True
    assert fake.started is True
    assert window.destroyed is True

    saved = json.loads(
        (tmp_path / "fanqienovel" / COOKIE_FILENAME).read_text(encoding="utf-8")
    )
    assert saved == [{"name": "sessionid", "value": "secret"}]


def test_login_main_lists_sites(capsys: pytest.CaptureFixture[str]) -> None:
    assert login_main(["--list"]) == 0
    assert "fanqienovel" in capsys.readouterr().out


class _FakeEvent:
    def __init__(self, *, closed: bool) -> None:
        self._closed = closed

    def is_set(self) -> bool:
        return self._closed

    def wait(self, timeout: float | None = None) -> bool:
        return not self._closed


class _FakeWindow:
    """Minimal stand-in for a pywebview window."""

    def __init__(self, cookies: SimpleCookie[str]) -> None:
        self._cookies = [cookies]
        self.destroyed = False
        self.events = types.SimpleNamespace(
            loaded=_FakeEvent(closed=False),
            closed=_FakeEvent(closed=False),
        )

    def get_cookies(self) -> list[SimpleCookie[str]]:
        return self._cookies

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWebview:
    """Stand-in for the ``webview`` module: ``start`` runs the watcher inline."""

    def __init__(self, window: _FakeWindow) -> None:
        self._window = window
        self.started = False

    def create_window(self, **kwargs: object) -> _FakeWindow:
        return self._window

    def start(self, func: object = None, *args: object, **kwargs: object) -> None:
        self.started = True
        if callable(func):
            func()


def test_cli_login_command_lists_sites(capsys: pytest.CaptureFixture[str]) -> None:
    from novel_downloader.apps.cli.commands.login import LoginCmd

    LoginCmd.run(Namespace(site="fanqienovel", timeout=300.0, list=True))

    assert "fanqienovel" in capsys.readouterr().out
