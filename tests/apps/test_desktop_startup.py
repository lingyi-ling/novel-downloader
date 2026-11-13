"""Cold-start regressions for the pywebview desktop client.

The window must appear before the task queue (and the network stack it
imports) is restored.  These tests measure a fresh interpreter because an
in-process import would already be warm and would not catch a regression that
moved a heavy import back to module scope.
"""

from __future__ import annotations

import subprocess
import sys
import time
from types import ModuleType
from typing import Any

import pytest

#: Modules that cost real time and are not needed to show the window.
DEFERRED_MODULES = (
    "aiohttp",
    "novel_downloader.apps.core.task_manager",
    "novel_downloader.apps.common.updater",
    "urllib.request",
    "webview",
)

#: Deliberately loose: a cold CI/Windows filesystem can be much slower than a
#: developer machine, while a regression that re-adds aiohttp/urllib to the
#: window path lands far above this.
IMPORT_BUDGET_S = 5.0


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_window_path_does_not_import_heavy_modules() -> None:
    """Importing the pywebview host must stay free of network/task modules."""
    code = (
        "import sys\n"
        "import novel_downloader.apps.desktop.webview_app\n"
        f"names = {DEFERRED_MODULES!r}\n"
        "print(' '.join(name for name in names if name in sys.modules))\n"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        "heavy modules imported before window creation: " + proc.stdout.strip()
    )


def test_window_path_import_stays_within_budget() -> None:
    """A loose wall-clock guard for the cold import of the window module."""
    code = (
        "import time\n"
        "start = time.perf_counter()\n"
        "import novel_downloader.apps.desktop.webview_app\n"
        "print(time.perf_counter() - start)\n"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr
    elapsed = float(proc.stdout.strip())
    assert elapsed < IMPORT_BUDGET_S, f"window import took {elapsed:.2f}s"


class _Signal:
    """pywebview event stand-in supporting ``handler += callback``."""

    def __iadd__(self, _handler: Any) -> _Signal:
        return self


class _Events:
    def __init__(self) -> None:
        self.closing = _Signal()
        self.closed = _Signal()


class _Window:
    width = 1220
    height = 800
    x = 0
    y = 0

    def __init__(self) -> None:
        self.events = _Events()


def test_run_webview_app_does_not_wait_for_the_queue_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A slow restore runs on a daemon thread, not between click and paint."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    started = []

    def slow_start_services(self: wa.ApiBridge) -> None:
        started.append(True)
        time.sleep(1.0)

    monkeypatch.setattr(wa.ApiBridge, "start_services", slow_start_services)

    fake_webview = ModuleType("webview")

    def create_window(*_args: Any, **_kwargs: Any) -> _Window:
        return _Window()

    fake_webview.create_window = create_window  # type: ignore[attr-defined]
    fake_webview.start = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    start = time.perf_counter()
    assert wa.run_webview_app() == 0
    elapsed = time.perf_counter() - start

    assert started, "the queue restore was never scheduled"
    assert elapsed < 0.5, f"window creation waited {elapsed:.2f}s for the restore"
