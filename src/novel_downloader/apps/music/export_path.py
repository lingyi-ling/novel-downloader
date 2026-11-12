#!/usr/bin/env python3
"""
novel_downloader.apps.music.export_path
----------------------------------------

Confinement rules for filesystem paths supplied by music API callers.

Two kinds of caller-controlled directories are resolved here:

* ``POST /music/export`` accepts ``out_path``.  Without a guard that value is
  an arbitrary-write primitive: a relative ``../../.ssh/authorized_keys`` or
  an absolute path outside the music library lets a caller overwrite any file
  the process can write.
* ``POST /music/download`` and ``POST /music/jobs`` accept ``out_dir``.  The
  download engine creates artist/album directories and writes audio files
  there, so the same escape would turn into an arbitrary-directory-write.

The policy implemented here:

* playlist targets must be ``.m3u`` / ``.m3u8`` files;
* the resolved target must live inside one of the *allowed* root directories
  (the music settings' ``out_dir`` and/or the platform download directory);
* ``..`` components and symlinks cannot escape: paths are ``resolve()``-d
  (which follows existing links) before the containment check;
* a bare directory (no suffix) is accepted as a playlist target and a
  ``playlist-*.m3u8`` file is created inside it.

:class:`PlaylistTargetError` and :class:`OutDirTargetError` deliberately carry
fixed messages: they are returned to clients verbatim, so they must never
embed the rejected absolute path.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "MSG_OUT_DIR_INVALID",
    "MSG_PLAYLIST_TARGET_INVALID",
    "MSG_PLAYLIST_WRITE_FAILED",
    "PLAYLIST_SUFFIXES",
    "OutDirTargetError",
    "PlaylistTargetError",
    "default_playlist_name",
    "playlist_keep_flags",
    "resolve_output_dir",
    "resolve_playlist_target",
]

#: File extensions this module is willing to write.
PLAYLIST_SUFFIXES = frozenset({".m3u", ".m3u8"})

#: Returned to clients when ``out_path`` is rejected.  It intentionally does
#: not contain the offending value (it may be an absolute server path).
MSG_PLAYLIST_TARGET_INVALID = (
    "Playlist out_path must be a .m3u/.m3u8 file inside the music output directory"
)

#: Returned to clients when a download ``out_dir`` is rejected.  Like the
#: playlist message it never echoes the submitted path.
MSG_OUT_DIR_INVALID = "Music out_dir must be inside the music output directory"

#: Returned to clients when the playlist write itself fails.  ``error`` is the
#: exception *class name* only: the message may carry server-internal paths
#: (and is written to the log instead).  The target is not echoed either --
#: it can be an absolute path the client had no business learning.
MSG_PLAYLIST_WRITE_FAILED = "Could not write playlist: {error}"


class PlaylistTargetError(ValueError):
    """``out_path`` resolves outside the allowed export directories."""


class OutDirTargetError(ValueError):
    """``out_dir`` resolves outside the allowed download directories."""


def default_playlist_name(suffix: str = ".m3u8") -> str:
    """Return a timestamped playlist file name (UTC, second resolution)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"playlist-{stamp}{suffix}"


def playlist_keep_flags(paths: Iterable[object]) -> list[bool]:
    """
    Decide, per submitted track, whether it gets a playlist entry.

    A playlist stores file locations, so the same file listed twice would
    appear twice in every player (the second entry is dead weight, not a
    second song). Identity is the normalised absolute path
    (``normcase(abspath(path))``), which folds ``./a.mp3`` into ``a.mp3`` and
    — on Windows — ``A.MP3`` into ``a.mp3``. The first occurrence is kept.

    A blank/whitespace-only path is never an entry: that is the historical
    "count it in ``skipped``" rule. ``OutDirTargetError`` is not raised here;
    this function only looks at the path strings.

    :return: one flag per input, ``True`` where the entry should be written.
    """
    seen: set[str] = set()
    flags: list[bool] = []
    for value in paths:
        text = str(value or "").strip()
        if not text:
            flags.append(False)
            continue
        try:
            key = os.path.normcase(os.path.abspath(text))
        except (OSError, ValueError):
            # A path the OS refuses to absolutise cannot identify a file;
            # treat it like a blank entry instead of failing the whole export.
            flags.append(False)
            continue
        if key in seen:
            flags.append(False)
            continue
        seen.add(key)
        flags.append(True)
    return flags


def _resolve(path: Path) -> Path:
    """``expanduser()`` + ``resolve()``; malformed paths are rejected."""
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PlaylistTargetError(MSG_PLAYLIST_TARGET_INVALID) from exc


def _unique_roots(base_dirs: Iterable[Path | str]) -> list[Path]:
    roots: list[Path] = []
    for item in base_dirs:
        if item is None:
            continue
        root = _resolve(Path(item))
        if root not in roots:
            roots.append(root)
    return roots


def _within(candidate: Path, root: Path) -> bool:
    """``candidate`` must be ``root`` itself or live below it."""
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - platform edge cases
        return False


def resolve_playlist_target(
    out_path: str | None,
    *,
    base_dirs: Iterable[Path | str],
    default_dir: Path | str,
    default_name: str | None = None,
) -> Path:
    """
    Resolve and validate the playlist destination.

    :param out_path: caller-supplied path (file or directory); may be empty.
    :param base_dirs: directories writes are allowed to land in.
    :param default_dir: directory used when ``out_path`` is empty; it is
        treated as an allowed root as well (it is server-controlled).
    :param default_name: file name used for the directory form; defaults to a
        timestamped ``playlist-*.m3u8``.
    :raises PlaylistTargetError: for a bad suffix or an escaping path.
    :return: the resolved, confined playlist file path.
    """
    name = default_name or default_playlist_name()
    roots = _unique_roots(base_dirs)
    default_root = _resolve(Path(default_dir))
    if default_root not in roots:
        roots.append(default_root)
    if not roots:  # pragma: no cover - callers always pass at least one
        raise PlaylistTargetError(MSG_PLAYLIST_TARGET_INVALID)

    text = (out_path or "").strip()
    if not text:
        target = default_root / name
    else:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            # A relative name is relative to the primary output directory,
            # never to the daemon's current working directory.
            candidate = roots[0] / candidate
        if candidate.is_dir():
            # An existing directory whose name merely ends in ``.m3u`` /
            # ``.m3u8`` is still a directory: writing to it would always fail.
            target = candidate / name
        elif candidate.suffix:
            if candidate.suffix.lower() not in PLAYLIST_SUFFIXES:
                raise PlaylistTargetError(MSG_PLAYLIST_TARGET_INVALID)
            target = candidate
        else:
            target = candidate / name

    if target.suffix.lower() not in PLAYLIST_SUFFIXES:
        raise PlaylistTargetError(MSG_PLAYLIST_TARGET_INVALID)

    resolved = _resolve(Path(target))
    if not any(_within(resolved, root) for root in roots):
        raise PlaylistTargetError(MSG_PLAYLIST_TARGET_INVALID)
    return resolved


def resolve_output_dir(
    out_dir: str | None,
    *,
    base_dirs: Iterable[Path | str],
    default_dir: Path | str,
) -> Path:
    """
    Resolve and validate a download output directory.

    The engine creates artist/album subdirectories and writes audio files
    below the returned path, so it must stay inside one of *base_dirs* (or
    *default_dir*, which is server-controlled).

    :param out_dir: caller-supplied directory; empty selects *default_dir*.
    :param base_dirs: directories writes are allowed to land in.
    :param default_dir: directory used when ``out_dir`` is empty; it is also
        treated as an allowed root.
    :raises OutDirTargetError: when the path escapes every allowed root.
    :return: the resolved, confined output directory.
    """
    roots = _unique_roots(base_dirs)
    default_root = _resolve(Path(default_dir))
    if default_root not in roots:
        roots.append(default_root)

    text = (out_dir or "").strip()
    if not text:
        return default_root

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        # Relative output directories are relative to the configured music
        # directory, never to the daemon's current working directory.
        candidate = default_root / candidate

    resolved = _resolve(candidate)
    if not any(_within(resolved, root) for root in roots):
        raise OutDirTargetError(MSG_OUT_DIR_INVALID)
    return resolved
