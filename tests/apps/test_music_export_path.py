#!/usr/bin/env python3
"""
tests.apps.test_music_export_path
---------------------------------

Destination resolution for music playlists (``export_path``).

These cases pin down behaviour that the REST/embedded/desktop callers rely on
but that is easy to break while touching the M3U writer:

* an existing directory whose name happens to end in ``.m3u``/``.m3u8`` is a
  directory, not a file the exporter must fail on;
* a suffix-less target is a directory and receives the default file name;
* escapes (``..``, an absolute path outside the roots) are still rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_downloader.apps.music.export_path import (
    PlaylistTargetError,
    resolve_playlist_target,
)


def _resolve(root: Path, out_path: str, default_name: str = "playlist-1.m3u8") -> Path:
    return resolve_playlist_target(
        out_path,
        base_dirs=[root],
        default_dir=root,
        default_name=default_name,
    )


def test_existing_directory_with_playlist_suffix_is_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    directory = root / "playlists.m3u8"
    directory.mkdir()

    resolved = _resolve(root, str(directory))

    # The resolved path must be a file inside the directory, not the
    # directory itself (opening a directory always raises).
    assert resolved == (directory / "playlist-1.m3u8").resolve()
    assert resolved.parent == directory.resolve()


def test_suffixless_target_is_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    directory = root / "exports"
    directory.mkdir()

    resolved = _resolve(root, str(directory))

    assert resolved == (directory / "playlist-1.m3u8").resolve()


def test_new_directory_name_with_playlist_suffix_stays_a_file(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()

    resolved = _resolve(root, str(root / "favorites.m3u8"))

    assert resolved == (root / "favorites.m3u8").resolve()


def test_parent_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()

    with pytest.raises(PlaylistTargetError):
        _resolve(root, str(root / ".." / "outside.m3u8"))


def test_absolute_path_outside_roots_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "outside.m3u8"

    with pytest.raises(PlaylistTargetError):
        _resolve(root, str(outside))
