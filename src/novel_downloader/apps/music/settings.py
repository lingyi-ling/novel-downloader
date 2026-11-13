#!/usr/bin/env python3
"""
novel_downloader.apps.music.settings
-------------------------------------

Load/save the music downloader preferences.

The file lives in :func:`novel_downloader.apps.music.paths.settings_file`;
unknown keys are preserved so older/newer clients do not clobber each other.

It is ordinary application state, not a trusted configuration input: the
desktop panel, the embedded engine and the REST music endpoints all read it,
so a corrupted (or absurdly large) file must degrade to the defaults instead
of being loaded whole.  Writes are serialized and atomic (temp file +
``os.replace``), so a crash or a concurrent save cannot leave a truncated
document behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .paths import default_download_dir, settings_file

DEFAULT_WORKERS = 3

#: A settings document is a handful of scalar values.  Anything larger is
#: corruption (or a file this version does not understand), and reading it
#: would allocate an unbounded amount of memory.
MAX_SETTINGS_BYTES = 256 * 1024

#: Serializes the read-merge-write sequence inside one process.  Cross-process
#: writers still last-one-wins, but the atomic replace means neither can
#: observe a half-written document.
_WRITE_LOCK = threading.RLock()

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


def _read_stored(path: Path) -> dict[str, Any]:
    """Read the stored document, treating every defect as "no settings"."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_SETTINGS_BYTES:
        return {}
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_settings() -> dict[str, Any]:
    """Return stored settings merged over the defaults."""
    data: dict[str, Any] = dict(_DEFAULTS)
    path: Path = settings_file()
    if path.is_file():
        data.update(_read_stored(path))
    return data


def save_settings(values: dict[str, Any]) -> None:
    """Persist settings, keeping unknown keys from the previous file.

    The read-merge-write happens under a process-wide lock, and the payload is
    written to a sibling temporary file that is fsynced and then moved into
    place.  A failed or interrupted save therefore leaves the previous file
    intact instead of truncating it.
    """
    with _WRITE_LOCK:
        current = load_settings()
        current.update(values)
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise
