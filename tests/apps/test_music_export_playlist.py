#!/usr/bin/env python3
"""
tests.apps.test_music_export_playlist
-------------------------------------

Cross-platform contract for the music playlist writer (``export_m3u``).

The playlist is consumed by third-party players, so the byte-level format
matters as much as the parsed lines:

* ``.m3u8`` is UTF-8 by definition and is written without a BOM;
* ``.m3u`` carries no encoding declaration, so non-ASCII content gets a UTF-8
  BOM -- otherwise Windows players that fall back to the ANSI code page show
  mojibake for Chinese titles and paths;
* locations use forward slashes, keep spaces/Unicode, and stay relative to the
  playlist folder (with an absolute fallback when a relative path is
  impossible, e.g. across Windows drives);
* the playlist is replaced atomically: a failed write never destroys the
  previous file and never leaves a temp file behind;
* duplicates, blank metadata, control characters and huge playlists are
  handled without breaking the line structure.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import export_m3u
from novel_downloader.apps.music.providers.base import Track

BOM = b"\xef\xbb\xbf"


def _track(
    name: str = "Song",
    artists: str = "Artist",
    duration: int | float = 10,
    path: str = "",
) -> Track:
    return Track(name=name, artists=artists, duration_s=duration, path=path)


# ---------------------------------------------------------------------------
# encoding / BOM policy
# ---------------------------------------------------------------------------


def test_m3u8_is_utf8_without_bom(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "晴天.mp3"
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(name="晴天", artists="周杰伦", path=str(audio))], playlist)

    raw = playlist.read_bytes()
    # ``.m3u8`` declares UTF-8; no BOM so strict parsers see the header first.
    assert raw.startswith(b"#EXTM3U\n")
    assert not raw.startswith(BOM)
    text = raw.decode("utf-8")
    assert "#EXTINF:10,周杰伦 - 晴天" in text
    assert "audio/晴天.mp3" in text


def test_m3u8_uppercase_suffix_is_plain_utf8(tmp_path: Path) -> None:
    playlist = tmp_path / "LIST.M3U8"

    export_m3u([_track(name="歌", path=str(tmp_path / "歌.mp3"))], playlist)

    raw = playlist.read_bytes()
    assert not raw.startswith(BOM)
    assert "歌" in raw.decode("utf-8")


def test_m3u_with_non_ascii_gets_a_utf8_bom(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "晴天.mp3"
    playlist = tmp_path / "list.m3u"

    export_m3u([_track(name="晴天", artists="周杰伦", path=str(audio))], playlist)

    raw = playlist.read_bytes()
    # ``.m3u`` has no encoding declaration: the BOM is what tells an
    # ANSI-assuming Windows player that the bytes are UTF-8.
    assert raw.startswith(BOM + b"#EXTM3U\n")
    text = raw.decode("utf-8-sig")
    assert text.startswith("#EXTM3U\n")
    assert "audio/晴天.mp3" in text


def test_m3u_uppercase_suffix_with_non_ascii_gets_a_bom(tmp_path: Path) -> None:
    playlist = tmp_path / "LIST.M3U"

    export_m3u([_track(name="歌", path=str(tmp_path / "歌.mp3"))], playlist)

    assert playlist.read_bytes().startswith(BOM + b"#EXTM3U\n")


def test_ascii_m3u_stays_plain_utf8(tmp_path: Path) -> None:
    """A pure-ASCII ``.m3u`` needs no marker and stays byte-compatible."""
    playlist = tmp_path / "list.m3u"

    export_m3u([], playlist)

    assert playlist.read_bytes() == b"#EXTM3U\n"


def test_unknown_suffix_falls_back_to_plain_utf8(tmp_path: Path) -> None:
    """A path with no recognised playlist suffix stays plain UTF-8."""
    temp_path = tmp_path / "playlist.m3u8.abc123.tmp"

    export_m3u([_track(name="晴天", path=str(tmp_path / "a.mp3"))], temp_path)

    raw = temp_path.read_bytes()
    assert not raw.startswith(BOM)
    assert "晴天" in raw.decode("utf-8")


def test_empty_playlist_is_header_only_for_both_suffixes(tmp_path: Path) -> None:
    for suffix in (".m3u", ".m3u8"):
        playlist = tmp_path / f"empty{suffix}"
        export_m3u([], playlist)
        assert playlist.read_bytes() == b"#EXTM3U\n"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_relative_location_uses_forward_slashes_spaces_and_unicode(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "audio" / "专辑" / "song one.mp3"
    playlist = tmp_path / "playlists" / "list.m3u8"

    export_m3u([_track(path=str(audio))], playlist)

    text = playlist.read_text(encoding="utf-8")
    assert "audio/专辑/song one.mp3" in text
    assert "\\" not in text


def test_duplicate_locations_are_written_once_first_wins(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    playlist = tmp_path / "list.m3u8"
    first = _track(name="First", path=str(audio))
    # ``abspath`` folds both a ``./`` prefix (relative to the current
    # directory) and a ``..`` component onto the same file.
    second = _track(name="Second", path="./" + os.path.relpath(audio, os.getcwd()))
    third = _track(name="Third", path=str(tmp_path / "sub" / ".." / "a.mp3"))

    export_m3u([first, second, third], playlist)

    text = playlist.read_text(encoding="utf-8")
    assert text.count("a.mp3") == 1
    assert "First" in text
    assert "Second" not in text
    assert "Third" not in text


def test_different_files_keep_their_order(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    names = ["one", "two", "three"]
    tracks = [_track(name=n, path=str(tmp_path / f"{n}.mp3")) for n in names]

    export_m3u(tracks, playlist)

    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert [line.rsplit("/", 1)[-1] for line in lines[2::2]] == [
        "one.mp3",
        "two.mp3",
        "three.mp3",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters only")
def test_cross_drive_falls_back_to_absolute_forward_slash_path(
    tmp_path: Path,
) -> None:
    playlist = tmp_path / "list.m3u8"
    # No filesystem access is needed: ``relpath`` only compares drive letters
    # and raises ``ValueError`` when they differ.
    export_m3u([_track(path="D:/music/a.mp3")], playlist)

    text = playlist.read_text(encoding="utf-8")
    assert "D:/music/a.mp3" in text
    assert "\\" not in text


@pytest.mark.skipif(os.name == "nt", reason="backslash is a separator on Windows")
def test_posix_backslash_is_not_rewritten(tmp_path: Path) -> None:
    weird = tmp_path / "a\\b.mp3"
    playlist = tmp_path / "list.m3u"

    export_m3u([_track(path=str(weird))], playlist, absolute=True)

    assert "a\\b.mp3" in playlist.read_text(encoding="utf-8-sig")


def test_relative_path_starting_with_hash_is_neutralised(tmp_path: Path) -> None:
    audio = tmp_path / "#mix.mp3"
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path=str(audio))], playlist)

    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "./#mix.mp3"


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_metadata_cannot_inject_extra_lines(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    track = _track(
        name="A,B #1\r\n#EXTINF:1,Injected",
        artists="X\nY#Z",
        path=str(tmp_path / "a.mp3"),
    )

    export_m3u([track], playlist)

    text = playlist.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 3
    assert "#EXTINF:10,X Y#Z - A,B #1 #EXTINF:1,Injected" in text
    assert text.splitlines()[-1].endswith("a.mp3")


def test_infinite_duration_is_unknown_not_a_crash(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(duration=float("inf"), path=str(tmp_path / "a.mp3"))], playlist)

    assert "#EXTINF:-1,Artist - Song" in playlist.read_text(encoding="utf-8")


def test_blank_metadata_gets_placeholders(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    track = _track(name="  ", artists="", duration=0, path=str(tmp_path / "a.mp3"))

    export_m3u([track], playlist)

    assert "#EXTINF:-1,未知歌手 - 未知歌曲" in playlist.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# atomic replace
# ---------------------------------------------------------------------------


def test_existing_playlist_survives_a_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(b"previous playlist\n")

    def boom(_src: str, _dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(downloader.os, "replace", boom)

    with pytest.raises(OSError):
        export_m3u([_track(path=str(tmp_path / "a.mp3"))], playlist)

    assert playlist.read_bytes() == b"previous playlist\n"
    assert not [p for p in playlist.parent.iterdir() if p.name.startswith(".")]


def test_successful_write_leaves_no_temp_files(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path=str(tmp_path / "a.mp3"))], playlist)

    assert [p.name for p in tmp_path.iterdir()] == ["list.m3u8"]


def test_directory_target_raises_isdirectory(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    playlist.mkdir()

    with pytest.raises(IsADirectoryError):
        export_m3u([], playlist)

    assert playlist.is_dir()


def test_unusable_parent_directory_raises_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")

    with pytest.raises(OSError):
        export_m3u([], blocker / "list.m3u8")

    # The offending file is untouched and no temp file was created next to it.
    assert blocker.read_bytes() == b"not a directory"
    assert [p.name for p in tmp_path.iterdir()] == ["blocker"]


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------


def test_large_playlist_is_complete_and_ordered(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    tracks = [
        _track(name=f"Song {index}", path=str(tmp_path / "music" / f"{index}.mp3"))
        for index in range(2000)
    ]

    export_m3u(tracks, playlist)

    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert len(lines) == 1 + 2 * len(tracks)
    assert lines[-1] == "music/1999.mp3"
    assert lines[1] == "#EXTINF:10,Artist - Song 0"
