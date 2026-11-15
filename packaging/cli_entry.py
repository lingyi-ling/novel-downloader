#!/usr/bin/env python3
"""Frozen entry point for the ``novel-cli`` console executable."""

from __future__ import annotations

import contextlib
import sys


def _redirect_utf8(stream: object) -> None:
    """Make a redirected text stream emit UTF-8 instead of the ANSI code page.

    PyInstaller freezes CPython with the host's locale, so a redirected
    ``sys.stdout``/``sys.stderr`` is a text wrapper over cp936 (or cp1252,
    ...). ``ui.emit`` already writes UTF-8 bytes for machine-readable output,
    but argparse help, logging and Rich diagnostics go through the text
    layer: a strict UTF-8 consumer then sees a mixture of valid UTF-8 and
    legacy-code-page bytes. Reconfiguring only non-TTY streams leaves the
    interactive Windows console alone (CPython drives it through the Unicode
    console API, so its encoding is already UTF-8) and cannot change how the
    app renders on screen.

    Objects without ``reconfigure`` (an embedder-supplied stream, a closed
    stream) keep their behaviour: the probe is best-effort only.
    """
    isatty = getattr(stream, "isatty", None)
    try:
        if isatty is not None and isatty():
            return
    except (OSError, ValueError):
        pass
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(OSError, ValueError):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def _force_utf8_output() -> None:
    """Normalise redirected stdout/stderr before any command module imports."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            _redirect_utf8(stream)


if __name__ == "__main__":
    _force_utf8_output()

    from novel_downloader.apps.cli.main import cli_main

    cli_main()
