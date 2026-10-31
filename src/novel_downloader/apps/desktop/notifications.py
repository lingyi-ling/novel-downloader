#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.notifications
-------------------------------------------

System notification sound for the desktop client.

Status-change toasts live in the front end's JavaScript; this module only
provides the OS-level completion chime, which must never be able to break the
polling loop that triggers it.
"""

from __future__ import annotations

import logging

__all__ = ["play_notification_sound"]

logger = logging.getLogger(__name__)


def play_notification_sound() -> None:
    """
    Play the system notification sound (Windows; a no-op elsewhere).

    ``winsound`` ships with CPython on Windows and ``MessageBeep`` plays the
    system sound asynchronously, so the calling thread is never blocked.  Any
    failure (missing audio device, ...) is swallowed: a silent machine must
    not break the polling loop.
    """
    try:
        import winsound
    except ImportError:  # pragma: no cover - non-Windows platforms
        return
    try:
        winsound.MessageBeep()
    except Exception:  # noqa: BLE001 - a sound problem is never fatal
        logger.exception("Failed to play the task notification sound")
