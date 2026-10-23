#!/usr/bin/env python3
"""
Frozen-application launcher for the Novel Downloader desktop UI.

PyInstaller uses this module as ``Analysis`` entry point instead of the package
module itself for two reasons:

* ``multiprocessing.freeze_support()`` must run before any project import.
  NiceGUI opens the native pywebview window in a *spawned* child process, and
  a frozen child re-executes this executable with multiprocessing arguments.
* A windowed build has no console, so an unhandled startup exception would
  otherwise disappear silently. The handler below appends the traceback to a
  log file next to the application logs.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path

PACKAGE_NAME = "novel_downloader"


def _crash_log_path() -> Path:
    """
    Return the crash log location, mirroring ``infra.paths.app_home()``.

    ``platformdirs`` is imported lazily so this module stays stdlib-only until
    ``freeze_support`` has handled a possible multiprocessing child startup.
    """
    override = os.environ.get("NOVEL_HOME")
    if override:
        home = Path(override).expanduser()
    else:
        try:
            from platformdirs import user_data_path

            home = user_data_path(PACKAGE_NAME, appauthor=False)
        except Exception:  # pragma: no cover - defensive fallback
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            home = Path(base) if base else Path.home() / ".local" / "share"
            home = home / PACKAGE_NAME
    return Path(home) / "logs" / "desktop-crash.log"


def _write_crash_log(exc: BaseException) -> None:
    """Best-effort traceback dump for console-less runs."""
    try:
        path = _crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n=== NovelDownloader desktop startup failure ===\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except Exception:
        # Never mask the original failure.
        pass


def main() -> int:
    """Run ``desktop_main`` with frozen-app safeguards."""
    multiprocessing.freeze_support()

    try:
        from novel_downloader.apps.desktop.main import desktop_main

        return int(desktop_main() or 0)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except BaseException as exc:
        _write_crash_log(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
