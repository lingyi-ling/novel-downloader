#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.cookie_io
---------------------------------------------

Shared helpers for reading and writing session cookie files.

The on-disk format is a JSON list of objects with ``name`` and ``value``
strings, optionally extended with ``domain`` and ``path`` so cookies keep
their scope across a save/load round-trip.  Unknown keys are ignored and
malformed entries are skipped instead of raising.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from novel_downloader.infra.cookies import (
    _is_valid_cookie_name,
    _strip_control_chars,
)

__all__ = [
    "load_cookie_items",
    "resolve_cookie_path",
    "write_cookie_items",
]


def resolve_cookie_path(cookies_dir: Path, filename: str) -> Path:
    """
    Join *filename* onto *cookies_dir*, rejecting path traversal attempts.

    :param cookies_dir: Directory that must contain the cookie file.
    :param filename: Plain file name (no directory components).
    :return: The resolved path.
    :raises ValueError: If the file name is empty or contains separators.
    """
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise ValueError(f"Invalid cookie filename: {filename!r}")
    return Path(cookies_dir) / filename


def load_cookie_items(path: Path) -> list[dict[str, str]] | None:
    """
    Read a cookie JSON file and return normalized entries.

    :param path: Cookie file path.
    :return: List of ``{"name", "value", ["domain"], ["path"]}`` dicts, or
        ``None`` when the file cannot be read or is not a JSON list.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(data, list):
        return None

    items: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue

        name = entry.get("name")
        value = entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not _is_valid_cookie_name(name):
            continue

        item = {"name": name, "value": _strip_control_chars(value)}
        domain = entry.get("domain")
        path_value = entry.get("path")
        if isinstance(domain, str) and domain:
            item["domain"] = domain
        if isinstance(path_value, str) and path_value.startswith("/"):
            item["path"] = path_value
        items.append(item)

    return items


def write_cookie_items(path: Path, items: Iterable[Mapping[str, str]]) -> None:
    """
    Atomically write cookie entries to *path*.

    The temporary file is created next to the destination and moved into
    place with :func:`os.replace`, so readers never observe a half-written
    file.  On platforms that support it, the file is restricted to the owner.

    :param path: Destination file path.
    :param items: Cookie entry mappings to serialize.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(list(items), ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Best effort: Windows does not implement POSIX permission bits.
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
