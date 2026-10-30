#!/usr/bin/env python3
"""
novel_downloader.apps.music.paths
----------------------------------

Runtime paths for the music downloader.

User data (session cookies, settings, platform credentials and the dedicated
browser profile) lives in the platform data directory, never inside the
repository. Set ``NOVEL_MUSIC_HOME`` to override the location.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir, user_music_dir

_ENV_VAR = "NOVEL_MUSIC_HOME"


def music_data_dir() -> Path:
    """Return (and create) the directory holding music runtime data."""
    override = os.environ.get(_ENV_VAR)
    if override:
        base = Path(override).expanduser()
    else:
        base = Path(user_data_dir("novel_downloader", appauthor=False)) / "music"
    base.mkdir(parents=True, exist_ok=True)
    return base


def session_file() -> Path:
    """Cookies captured at login (the NetEase ``MUSIC_U`` credential)."""
    return music_data_dir() / "session.json"


def settings_file() -> Path:
    """Download preferences shared by the web page and the legacy GUI."""
    return music_data_dir() / "settings.json"


def creds_file() -> Path:
    """Per-platform credentials (only platforms that need an account)."""
    return music_data_dir() / "platform_creds.json"


def default_download_dir() -> Path:
    """Default output directory when the user has not picked one yet."""
    return Path(user_music_dir()) / "novel_downloader"
