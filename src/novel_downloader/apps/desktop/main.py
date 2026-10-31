#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.main
----------------------------------

Entry point for the native desktop client (pywebview front end).

Importing this module performs no UI work: the pywebview window is created
only when :func:`desktop_main` is called.  The application home directory is
resolved and used as the working directory first, so the existing
relative-path logic (``settings.toml`` and ``./downloads``) keeps working no
matter where the program was launched from.

Mature-app housekeeping lives here too: a rotating file log for support and
an exclusive instance lock so a second launch reports "already running"
instead of racing the first one for the download queue.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

__all__ = ["desktop_main"]

logger = logging.getLogger(__name__)

#: Rotating log kept beside the app data (1 MB x 3 backups).
_LOG_NAME = "novel-desktop.log"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3
#: Exclusive lock preventing two instances from sharing one queue.
_LOCK_NAME = "desktop.lock"


def _absolutize_argv0() -> None:
    """
    Resolve ``sys.argv[0]`` against the original working directory.

    pywebview derives the base URI for inline HTML from ``sys.argv[0]``.
    A relative script path would stop resolving once the working directory
    has been switched to the application home, so it is made absolute before
    :func:`os.chdir` runs.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and not os.path.isabs(argv0):
        resolved = os.path.abspath(argv0)
        if os.path.exists(resolved):
            sys.argv[0] = resolved


def _prepare_home() -> None:
    """Seed ``settings.toml`` in the application home and switch to it."""
    from novel_downloader.infra.config import copy_default_config
    from novel_downloader.infra.paths import DEFAULT_CONFIG_FILENAME, app_home

    _absolutize_argv0()

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)

    config_path = home / DEFAULT_CONFIG_FILENAME
    if not config_path.is_file():
        copy_default_config(config_path)


def _setup_file_logging(home: Path) -> None:
    """
    Attach a rotating file log (best effort).

    The console stays untouched; the file captures INFO and above from every
    logger so a support request only needs one attachment.
    """
    try:
        logs_dir = home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            logs_dir / _LOG_NAME,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except OSError:
        logger.debug("Could not attach the file log", exc_info=True)


def _acquire_instance_lock(path: Path) -> Any:
    """
    Take an exclusive OS lock on ``path`` for the process lifetime.

    :return: The locked file handle (keep a reference until exit), or ``None``
        when another instance holds it or the platform call failed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        handle.close()
        return None
    return handle


def _warn_already_running() -> None:
    """Show a native message when a second instance is launched."""
    message = (
        "Novel Downloader is already running.\n请查看任务栏或系统托盘中的已有窗口。"
    )
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "Novel Downloader",
                0x40,
            )
            return
        except Exception:  # noqa: BLE001 - fall through to the log
            pass
    logger.error("%s", message)


def desktop_main() -> int:
    """
    Launch the desktop client.

    :return: Process exit code (``0`` after the window closes, ``1`` when
        another instance already holds the lock).
    """
    try:
        _prepare_home()
    except Exception:  # noqa: BLE001 - the UI can still start and report errors
        logger.exception("Failed to prepare the application home directory")

    from novel_downloader.infra.paths import app_home

    home = app_home()
    _setup_file_logging(home)
    logger.info("Desktop client starting (pid=%s)", os.getpid())

    lock = _acquire_instance_lock(home / _LOCK_NAME)
    if lock is None:
        _warn_already_running()
        return 1

    from .webview_app import run_webview_app

    try:
        return run_webview_app()
    finally:
        logger.info("Desktop client exited")
        # The lock stays held until process teardown; the handle simply goes
        # out of scope here.


if __name__ == "__main__":
    desktop_main()
