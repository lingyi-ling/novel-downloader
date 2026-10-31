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
"""

from __future__ import annotations

import logging
import os
import sys

__all__ = ["desktop_main"]

logger = logging.getLogger(__name__)


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


def desktop_main() -> int:
    """
    Launch the desktop client.

    :return: Process exit code (``0`` after the window closes).
    """
    try:
        _prepare_home()
    except Exception:  # noqa: BLE001 - the UI can still start and report errors
        logger.exception("Failed to prepare the application home directory")

    from .webview_app import run_webview_app

    return run_webview_app()


if __name__ == "__main__":
    desktop_main()
