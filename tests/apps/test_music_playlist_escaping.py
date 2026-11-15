#!/usr/bin/env python3
"""
tests.apps.test_music_playlist_escaping
---------------------------------------

Escaping and injection contract for the M3U playlist writer (``export_m3u``).

The writer interpolates attacker-influenced metadata (title, artist) and file
locations into a line-oriented format.  Two properties have to hold for every
input:

* one track is exactly two lines -- ``#EXTINF:<seconds>,<artist> - <title>``
  followed by the location -- and nothing in a title, an artist or a path can
  add a line, start a ``#`` directive, or produce a second entry;
* the location is a plain path with forward slashes, relative to the playlist
  folder unless an absolute one was requested, and it never gains a newline.

``str.splitlines()`` is used as the reference line splitter throughout: it
breaks on ``\\r``, ``\\n``, ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e``, NEL
(U+0085), U+2028 and U+2029.  M3U readers and text editors follow the same
rules, so a separator that ``splitlines()`` honours but the writer left intact
is a working injection primitive.

The surrounding contract is pinned here too: encoding/BOM policy, LF-only line
endings, placeholders for blank metadata, >1 MiB playlists, atomic replacement,
and the error each unusable destination produces.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import export_m3u
from novel_downloader.apps.music.providers.base import Track

BOM = b"\xef\xbb\xbf"

#: Placeholders the writer substitutes for blank metadata.
PLACEHOLDER_SONG = "未知歌曲"
PLACEHOLDER_ARTIST = "未知歌手"

#: Every character ``str.splitlines()`` treats as a line boundary, minus the
#: two (``\\n``, ``\\r``) already covered by the CRLF cases.
_LINE_BREAKS = [
    pytest.param("\n", id="lf"),
    pytest.param("\r", id="cr"),
    pytest.param("\r\n", id="crlf"),
    pytest.param("\x0b", id="vertical-tab"),
    pytest.param("\x0c", id="form-feed"),
    pytest.param("\x1c", id="file-separator"),
    pytest.param("\x1d", id="group-separator"),
    pytest.param("\x1e", id="record-separator"),
    pytest.param("\x85", id="nel"),
    pytest.param("\u2028", id="line-separator"),
    pytest.param("\u2029", id="paragraph-separator"),
]


def _track(
    name: str = "Song",
    artists: str = "Artist",
    duration: int | float = 10,
    path: str = "",
) -> Track:
    return Track(name=name, artists=artists, duration_s=duration, path=path)


def _write(
    tmp_path: Path,
    tracks: list[Track],
    name: str = "list.m3u8",
    **kwargs: bool,
) -> Path:
    playlist = tmp_path / name
    export_m3u(tracks, playlist, **kwargs)
    return playlist


def _text(playlist: Path) -> str:
    """Decode a playlist, tolerating the ``.m3u`` BOM."""
    return playlist.read_bytes().decode("utf-8-sig")


def _lines(playlist: Path) -> list[str]:
    """Split on every separator a real M3U reader would honour."""
    return _text(playlist).splitlines()


def _parse_reference(playlist: Path) -> list[tuple[str, str]]:
    """
    Parse the playlist with a minimal, independent M3U reader.

    A line beginning with ``#`` is a directive; ``#EXTINF:`` carries the
    metadata for the next location line.  Anything else is a location.  The
    assertions catch both a location without metadata and a dangling
    ``#EXTINF`` -- the exact shape an injected fake entry produces.
    """
    entries: list[tuple[str, str]] = []
    pending: str | None = None
    for line in _lines(playlist):
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            assert pending is None, f"two #EXTINF lines in a row: {line!r}"
            pending = line[len("#EXTINF:") :]
        elif line.startswith("#"):
            continue
        else:
            assert pending is not None, f"location without #EXTINF: {line!r}"
            entries.append((pending, line))
            pending = None
    assert pending is None, "dangling #EXTINF without a location"
    return entries


# ---------------------------------------------------------------------------
# line injection through metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("break_char", _LINE_BREAKS)
def test_metadata_line_break_cannot_add_a_record(
    tmp_path: Path, break_char: str
) -> None:
    playlist = tmp_path / "list.m3u8"
    evil = f"A{break_char}#EXTINF:-1,evil{break_char}B"

    export_m3u(
        [_track(name=evil, artists=f"X{break_char}Y", path=str(tmp_path / "a.mp3"))],
        playlist,
    )

    lines = _lines(playlist)
    # Header + one EXTINF + one location.  A surviving break would add lines.
    assert len(lines) == 3
    assert sum(line.startswith("#EXTINF:") for line in lines) == 1
    assert "#EXTINF:-1,evil" not in lines
    assert lines[-1] == "a.mp3"

    entries = _parse_reference(playlist)
    assert len(entries) == 1
    metadata, location = entries[0]
    assert location == "a.mp3"
    assert metadata == "10,X Y - A #EXTINF:-1,evil B"


@pytest.mark.parametrize("break_char", _LINE_BREAKS)
def test_metadata_line_break_is_replaced_by_one_space(
    tmp_path: Path, break_char: str
) -> None:
    playlist = _write(
        tmp_path,
        [_track(name=f"A{break_char}B", artists="Art", path=str(tmp_path / "a.mp3"))],
    )

    assert _lines(playlist)[1] == "#EXTINF:10,Art - A B"


def test_fake_extinf_title_does_not_create_an_entry(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u(
        [
            _track(
                name="#EXTINF:-1,evil",
                artists="#EXTINF:0,also evil",
                path=str(tmp_path / "a.mp3"),
            )
        ],
        playlist,
    )

    lines = _lines(playlist)
    assert len(lines) == 3
    assert lines[1] == "#EXTINF:10,#EXTINF:0,also evil - #EXTINF:-1,evil"
    assert len(_parse_reference(playlist)) == 1


def test_metadata_with_leading_hash_stays_inside_the_extinf_line(
    tmp_path: Path,
) -> None:
    playlist = _write(
        tmp_path,
        [_track(name="#1 Hit", artists="#A", path=str(tmp_path / "a.mp3"))],
    )

    lines = _lines(playlist)
    assert len(lines) == 3
    # The only ``#``-prefixed line is the header and the real EXTINF.
    assert [line for line in lines if line.startswith("#")] == [
        "#EXTM3U",
        "#EXTINF:10,#A - #1 Hit",
    ]


def test_c1_controls_and_unicode_separators_are_removed_from_metadata(
    tmp_path: Path,
) -> None:
    playlist = _write(
        tmp_path,
        [
            _track(
                name="A\x80B\x85C\u2028D\u2029E",
                artists="F\x9fG",
                path=str(tmp_path / "a.mp3"),
            )
        ],
    )

    text = _text(playlist)
    assert "#EXTINF:10,F G - A B C D E" in text
    for char in ("\x80", "\x85", "\x9f", "\u2028", "\u2029"):
        assert char not in text


def test_punctuation_and_unicode_metadata_is_preserved(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    name = 'Song, Part 2; "quoted" \\ backslash'
    artists = "周杰伦, A;B"

    export_m3u(
        [_track(name=name, artists=artists, path=str(tmp_path / "a.mp3"))], playlist
    )

    assert f"#EXTINF:10,{artists} - {name}" in _text(playlist)


def test_emoji_title_and_artist_are_preserved(tmp_path: Path) -> None:
    playlist = _write(
        tmp_path,
        [_track(name="🎵晴天😀", artists="周杰伦", path=str(tmp_path / "a.mp3"))],
    )

    assert "#EXTINF:10,周杰伦 - 🎵晴天😀" in _text(playlist)


@pytest.mark.parametrize("value", ["", " ", "\t", "\n", " \r\n "])
@pytest.mark.parametrize("field", ["name", "artists"])
def test_blank_metadata_gets_a_placeholder(
    tmp_path: Path, value: str, field: str
) -> None:
    kwargs = {"name": "Song", "artists": "Artist", field: value}
    playlist = _write(
        tmp_path,
        [_track(**kwargs, path=str(tmp_path / "a.mp3"))],
    )

    expected_song = PLACEHOLDER_SONG if field == "name" else "Song"
    expected_artist = PLACEHOLDER_ARTIST if field == "artists" else "Artist"
    assert _lines(playlist)[1] == f"#EXTINF:10,{expected_artist} - {expected_song}"


def test_very_long_metadata_stays_on_one_line(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    long_name = "长" * 200_000

    export_m3u([_track(name=long_name, path=str(tmp_path / "a.mp3"))], playlist)

    lines = _lines(playlist)
    assert len(lines) == 3
    assert lines[1].count("长") == 200_000
    assert lines[-1] == "a.mp3"


# ---------------------------------------------------------------------------
# line injection through paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("break_char", _LINE_BREAKS)
def test_path_line_break_cannot_add_a_line(tmp_path: Path, break_char: str) -> None:
    playlist = tmp_path / "list.m3u8"
    # A leading newline is the classic "start a new URI line" attempt.
    hostile = f"{tmp_path}{os.sep}{break_char}file:///etc/passwd.mp3"

    export_m3u([_track(path=hostile)], playlist)

    lines = _lines(playlist)
    assert len(lines) == 3
    # ``file:///etc/passwd.mp3`` survives only as a literal path component of
    # the one location line -- it never becomes a line of its own.
    assert "/etc/passwd.mp3" in lines[-1]
    assert not any(line.startswith("file://") for line in lines)
    assert len(_parse_reference(playlist)) == 1


def test_newline_prefixed_file_uri_path_cannot_inject(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path="\nfile:///etc/passwd")], playlist)

    lines = _lines(playlist)
    assert len(lines) == 3
    assert lines[1].startswith("#EXTINF:10,")
    # ``abspath`` folds ``file:///etc/passwd`` into the relative location
    # ``.../file:/etc/passwd``; the point is that it stays on the location
    # line instead of becoming a ``file://`` URI line.
    assert lines[-1].endswith("file:/etc/passwd")
    assert not any(line.startswith("file://") for line in lines)


def test_path_that_is_only_control_characters_still_yields_one_location(
    tmp_path: Path,
) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path="\n\r\t\x00")], playlist)

    lines = _lines(playlist)
    assert len(lines) == 3
    # The location collapses to its directory (the separators became spaces and
    # were stripped); it must still be a non-empty, control-free line rather
    # than an empty one a player would skip.
    assert lines[-1]
    assert lines[-1] == lines[-1].strip()
    assert not any(ord(char) < 0x20 for char in lines[-1])


def test_relative_path_starting_with_hash_is_prefixed(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    # A file literally named ``#EXTINF:-1,evil`` next to the playlist would be
    # read back as a directive line without the ``./`` prefix.
    evil = tmp_path / "#EXTINF:-1,evil"

    export_m3u([_track(path=str(evil))], playlist)

    assert _lines(playlist)[-1] == "./#EXTINF:-1,evil"


def test_relative_directory_starting_with_hash_is_prefixed(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    audio = tmp_path / "#mix" / "a.mp3"

    export_m3u([_track(path=str(audio))], playlist)

    assert _lines(playlist)[-1] == "./#mix/a.mp3"


def test_path_with_spaces_chinese_and_hash_is_preserved(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    audio = tmp_path / "专辑" / "song #1 one.mp3"

    export_m3u([_track(path=str(audio))], playlist)

    location = _lines(playlist)[-1]
    assert location == "专辑/song #1 one.mp3"
    assert "\\" not in location


def test_relative_and_absolute_locations_point_at_the_same_file(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "audio" / "a.mp3"
    relative_playlist = tmp_path / "playlists" / "rel.m3u8"
    absolute_playlist = tmp_path / "playlists" / "abs.m3u8"

    export_m3u([_track(path=str(audio))], relative_playlist)
    export_m3u([_track(path=str(audio))], absolute_playlist, absolute=True)

    relative = _lines(relative_playlist)[-1]
    absolute = _lines(absolute_playlist)[-1]
    assert relative == "../audio/a.mp3"
    assert absolute == Path(os.path.abspath(str(audio))).as_posix()
    assert os.path.normcase(
        os.path.abspath(os.path.join(relative_playlist.parent, relative))
    ) == os.path.normcase(os.path.abspath(absolute))


@pytest.mark.skipif(os.name != "nt", reason="backslash is a separator on Windows")
def test_windows_backslashes_are_rewritten_in_both_modes(tmp_path: Path) -> None:
    audio = tmp_path / "专辑" / "song one.mp3"
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path=str(audio))], playlist)
    export_m3u([_track(path=str(audio))], playlist, absolute=True)

    text = _text(playlist)
    assert "\\" not in text
    assert _lines(playlist)[-1] == Path(os.path.abspath(str(audio))).as_posix()


@pytest.mark.skipif(os.name == "nt", reason="backslash is a separator on Windows")
def test_posix_backslash_in_a_file_name_is_not_rewritten(tmp_path: Path) -> None:
    audio = tmp_path / "a\\b.mp3"
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path=str(audio))], playlist)

    assert _lines(playlist)[-1] == "a\\b.mp3"


def test_file_uri_style_path_is_not_treated_as_a_second_entry(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"

    export_m3u([_track(path="file:///etc/passwd")], playlist)

    lines = _lines(playlist)
    assert len(lines) == 3
    assert len(_parse_reference(playlist)) == 1


# ---------------------------------------------------------------------------
# encoding / line endings
# ---------------------------------------------------------------------------


def test_m3u8_is_utf8_without_bom_and_lf_only(tmp_path: Path) -> None:
    playlist = _write(
        tmp_path,
        [_track(name="晴天", artists="周杰伦", path=str(tmp_path / "晴天.mp3"))],
    )

    raw = playlist.read_bytes()
    assert raw.startswith(b"#EXTM3U\n")
    assert not raw.startswith(BOM)
    assert b"\r" not in raw
    assert raw.decode("utf-8").count("\n") == 3


def test_ascii_m3u_has_no_bom_and_no_cr(tmp_path: Path) -> None:
    playlist = _write(tmp_path, [_track(path=str(tmp_path / "a.mp3"))], "list.m3u")

    raw = playlist.read_bytes()
    assert raw.startswith(b"#EXTM3U\n")
    assert not raw.startswith(BOM)
    assert b"\r" not in raw


def test_non_ascii_m3u_gets_a_bom_but_still_lf_only(tmp_path: Path) -> None:
    playlist = _write(
        tmp_path,
        [_track(name="晴天", artists="周杰伦", path=str(tmp_path / "晴天.mp3"))],
        "list.m3u",
    )

    raw = playlist.read_bytes()
    assert raw.startswith(BOM + b"#EXTM3U\n")
    assert b"\r" not in raw


def test_unknown_suffix_stays_plain_utf8(tmp_path: Path) -> None:
    playlist = _write(
        tmp_path,
        [_track(name="晴天", path=str(tmp_path / "a.mp3"))],
        "list.m3u8.abc.tmp",
    )

    raw = playlist.read_bytes()
    assert not raw.startswith(BOM)
    assert "晴天" in raw.decode("utf-8")


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------


def test_playlist_over_one_mib_is_written_completely(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    tracks = [
        _track(
            name=f"Song {index} " + "x" * 300,
            path=str(tmp_path / "music" / f"{index}.mp3"),
        )
        for index in range(4000)
    ]

    export_m3u(tracks, playlist)

    raw = playlist.read_bytes()
    assert len(raw) > 1024 * 1024
    lines = _lines(playlist)
    assert len(lines) == 1 + 2 * len(tracks)
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:10,Artist - Song 0 " + "x" * 300
    assert lines[-1] == "music/3999.mp3"
    assert len(_parse_reference(playlist)) == len(tracks)


def test_large_playlist_with_hostile_metadata_keeps_its_shape(
    tmp_path: Path,
) -> None:
    playlist = tmp_path / "list.m3u8"
    tracks = [
        _track(
            name=f"#EXTINF:-1,evil{index}\nsecond line",
            artists="A\u2028B",
            path=str(tmp_path / "music" / f"{index}.mp3"),
        )
        for index in range(500)
    ]

    export_m3u(tracks, playlist)

    lines = _lines(playlist)
    assert len(lines) == 1 + 2 * len(tracks)
    assert len(_parse_reference(playlist)) == len(tracks)
    assert not any(line == "#EXTINF:-1,evil0" for line in lines)


# ---------------------------------------------------------------------------
# atomic replace
# ---------------------------------------------------------------------------


def test_failed_replace_keeps_the_previous_playlist_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(BOM + b"#EXTM3U\nprevious\n")

    def boom(_src: str, _dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(downloader.os, "replace", boom)

    with pytest.raises(OSError):
        export_m3u([_track(name="晴天", path=str(tmp_path / "a.mp3"))], playlist)

    assert playlist.read_bytes() == BOM + b"#EXTM3U\nprevious\n"
    assert [p.name for p in tmp_path.iterdir()] == ["list.m3u8"]


def test_write_failure_midway_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short write must not leave a truncated playlist or a temp file."""
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(b"previous")

    real_fdopen = downloader.os.fdopen

    class _FailingHandle:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def __enter__(self) -> _FailingHandle:
            return self

        def __exit__(self, *exc_info: object) -> None:
            self._handle.close()  # type: ignore[attr-defined]

        def write(self, text: str) -> int:
            raise OSError(28, "No space left on device")

    def failing_fdopen(fd: int, *args: object, **kwargs: object) -> _FailingHandle:
        return _FailingHandle(real_fdopen(fd, *args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(downloader.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError):
        export_m3u([_track(path=str(tmp_path / "a.mp3"))], playlist)

    assert playlist.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["list.m3u8"]


def test_successful_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    playlist = _write(tmp_path, [_track(path=str(tmp_path / "a.mp3"))])

    assert [p.name for p in tmp_path.iterdir()] == [playlist.name]


def test_overwrite_replaces_the_whole_file(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(b"#EXTM3U\nstale entry that must disappear\n")

    export_m3u([], playlist)

    assert playlist.read_bytes() == b"#EXTM3U\n"


def test_missing_parent_directories_are_created(tmp_path: Path) -> None:
    playlist = tmp_path / "deep" / "er" / "list.m3u8"

    export_m3u([_track(path=str(tmp_path / "a.mp3"))], playlist)

    assert playlist.is_file()
    assert _lines(playlist)[-1] == "../../a.mp3"


def test_newline_in_the_destination_is_a_clean_oserror(tmp_path: Path) -> None:
    """A caller-supplied target with a newline must fail, not create a file."""
    with pytest.raises(OSError):
        export_m3u([], tmp_path / "a\nb.m3u8")

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# error semantics for unusable destinations
# ---------------------------------------------------------------------------


def test_directory_target_raises_is_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "list.m3u8"
    target.mkdir()

    with pytest.raises(IsADirectoryError):
        export_m3u([], target)

    assert target.is_dir()


def test_parent_that_is_a_file_raises_and_leaves_no_temp(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")

    with pytest.raises(OSError):
        export_m3u([], blocker / "list.m3u8")

    assert blocker.read_bytes() == b"not a directory"
    assert [p.name for p in tmp_path.iterdir()] == ["blocker"]


@pytest.mark.skipif(os.name != "nt", reason="POSIX replace only needs directory perms")
def test_read_only_existing_file_is_a_permission_error_on_windows(
    tmp_path: Path,
) -> None:
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(b"previous")
    os.chmod(playlist, 0o444)
    try:
        with pytest.raises(OSError):
            export_m3u([], playlist)
    finally:
        os.chmod(playlist, 0o666)

    assert playlist.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["list.m3u8"]


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX non-root semantics",
)
def test_read_only_existing_file_is_replaced_on_posix(tmp_path: Path) -> None:
    """``os.replace`` needs write permission on the directory, not the file."""
    playlist = tmp_path / "list.m3u8"
    playlist.write_bytes(b"previous")
    os.chmod(playlist, 0o444)
    try:
        export_m3u([], playlist)
    finally:
        os.chmod(playlist, 0o644)

    assert playlist.read_bytes() == b"#EXTM3U\n"


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX directory permissions",
)
def test_unwritable_directory_raises_and_leaves_no_temp(tmp_path: Path) -> None:
    directory = tmp_path / "locked"
    directory.mkdir()
    os.chmod(directory, 0o500)
    try:
        with pytest.raises(OSError):
            export_m3u([], directory / "list.m3u8")
    finally:
        os.chmod(directory, 0o700)

    assert list(directory.iterdir()) == []


# ---------------------------------------------------------------------------
# third-party parser compatibility
# ---------------------------------------------------------------------------


def test_common_parser_reads_sanitized_playlist(tmp_path: Path) -> None:
    m3u8 = pytest.importorskip("m3u8")
    playlist = tmp_path / "list.m3u8"

    export_m3u(
        [
            _track(
                name="晴天\n#EXTINF:-1,evil",
                artists="周杰伦",
                duration=269,
                path=str(tmp_path / "audio" / "晴天.mp3"),
            )
        ],
        playlist,
    )

    parsed = m3u8.loads(_text(playlist))
    assert [segment.uri for segment in parsed.segments] == ["audio/晴天.mp3"]
    # One record, one segment: the newline inside the title did not start a
    # second ``#EXTINF``.  (The library itself strips a literal ``#EXTINF:``
    # substring from a title, so only the stable parts are asserted.)
    assert len(parsed.segments) == 1
    assert "周杰伦" in parsed.segments[0].title
    assert "晴天" in parsed.segments[0].title
    assert [segment.duration for segment in parsed.segments] == [269]


def test_common_parser_sees_one_entry_per_track(tmp_path: Path) -> None:
    m3u8 = pytest.importorskip("m3u8")
    playlist = tmp_path / "list.m3u8"
    tracks = [
        _track(
            name=f"Song {index}\u2028#EXTINF:0,evil",
            path=str(tmp_path / "m" / f"{index}.mp3"),
        )
        for index in range(3)
    ]

    export_m3u(tracks, playlist)

    parsed = m3u8.loads(_text(playlist))
    assert [segment.uri for segment in parsed.segments] == [
        "m/0.mp3",
        "m/1.mp3",
        "m/2.mp3",
    ]
