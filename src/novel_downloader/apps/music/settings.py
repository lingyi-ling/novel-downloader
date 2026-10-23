#!/usr/bin/env python3
"""
novel_downloader.apps.music.settings
-------------------------------------

Load/save the music downloader preferences.

The file lives in :func:`novel_downloader.apps.music.paths.settings_file`;
unknown keys are preserved so older/newer clients do not clobber each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import default_download_dir, settings_file

DEFAULT_WORKERS = 3

_DEFAULTS: dict[str, Any] = {
    "out_dir": str(default_download_dir()),
    "level": "exhigh",
    "embed_tags": True,
    "embed_cover": True,
    "save_lyric": True,
    "split_folder": True,
    "skip_existing": True,
    "workers": DEFAULT_WORKERS,
}


def load_settings() -> dict[str, Any]:
    """Return stored settings merged over the defaults."""
    data: dict[str, Any] = dict(_DEFAULTS)
    path: Path = settings_file()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            data.update(loaded)
    return data


def save_settings(values: dict[str, Any]) -> None:
    """Persist settings, keeping unknown keys from the previous file."""
    current = load_settings()
    current.update(values)
    path = settings_file()
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
