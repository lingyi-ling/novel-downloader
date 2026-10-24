#!/usr/bin/env python3
"""
tests.apps.test_music_downloader
--------------------------------

Offline tests for the music download helpers:

* ``export_m3u`` playlists (relative/absolute paths, escaping, empty list)
* ``save_lyric`` sidecar files (``.lrc`` / ``.zh.lrc``)
* tag/cover writing degradation (synthetic MP3 bytes, no network)
* ``download_track`` orchestration with fakes (no real downloads)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import (
    DownloadOptions,
    export_m3u,
    save_lyric,
)
from novel_downloader.apps.music.providers.base import ResolveResult, Track

# Minimal MPEG-1 Layer III frames (128 kbps, 44.1 kHz) accepted by mutagen.
_MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413


def _mp3_bytes() -> bytes:
    return _MP3_FRAME * 4


# ---------------------------------------------------------------------------
# export_m3u
# ---------------------------------------------------------------------------


def test_export_m3u_writes_header_extinf_and_relative_path(tmp_path: Path) -> None:
    audio = tmp_path / "audio" / "song one.mp3"
    playlist = tmp_path / "list.m3u"
    track = Track(name="晴天", artists="周杰伦", duration_s=269, path=str(audio))

    result = export_m3u([track], playlist)

    assert result == str(playlist)
    content = playlist.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:269,周杰伦 - 晴天"
    assert lines[2] == "audio/song one.mp3"
    assert content.endswith("\n")


def test_export_m3u_absolute_paths(tmp_path: Path) -> None:
    audio = tmp_path / "song.mp3"
    playlist = tmp_path / "abs.m3u"
    track = Track(name="Song", artists="Artist", duration_s=10, path=str(audio))

    export_m3u([track], playlist, absolute=True)

    content = playlist.read_text(encoding="utf-8")
    expected = str(audio).replace("\\", "/")
    assert expected in content


def test_export_m3u_escapes_newlines_and_keeps_unicode(tmp_path: Path) -> None:
    audio = tmp_path / "特殊 歌名 (live).mp3"
    playlist = tmp_path / "special.m3u"
    track = Track(
        name="A,B #1\r\nInjected",
        artists="X\nY",
        duration_s=185,
        path=str(audio),
    )

    export_m3u([track], playlist)

    content = playlist.read_text(encoding="utf-8")
    assert "\r" not in content
    # Only the three real lines: header + EXTINF + location
    assert len(content.splitlines()) == 3
    assert "#EXTINF:185,X Y - A,B #1 Injected" in content
    assert "特殊 歌名 (live).mp3" in content


def test_export_m3u_path_starting_with_hash_is_escaped(tmp_path: Path) -> None:
    audio = tmp_path / "#mix.mp3"
    playlist = tmp_path / "hash.m3u"
    track = Track(name="Mix", artists="DJ", path=str(audio))

    export_m3u([track], playlist)

    assert "./#mix.mp3" in playlist.read_text(encoding="utf-8")


def test_export_m3u_empty_list_is_still_valid(tmp_path: Path) -> None:
    playlist = tmp_path / "empty.m3u"

    export_m3u([], playlist)

    assert playlist.read_text(encoding="utf-8") == "#EXTM3U\n"


def test_export_m3u_unknown_duration_uses_minus_one(tmp_path: Path) -> None:
    playlist = tmp_path / "unknown.m3u"
    track = Track(
        name="Song", artists="Artist", duration_s=0, path=str(tmp_path / "a.mp3")
    )

    export_m3u([track], playlist)

    assert "#EXTINF:-1,Artist - Song" in playlist.read_text(encoding="utf-8")


def test_export_m3u_accepts_tuple_and_mapping_entries(tmp_path: Path) -> None:
    playlist = tmp_path / "mixed.m3u"
    track = Track(name="Tuple", artists="T", duration_s=5)
    mapping = {"name": "Dict", "artists": "D", "duration_s": 6, "path": "d.mp3"}

    export_m3u([(track, str(tmp_path / "t.mp3")), mapping], playlist)

    content = playlist.read_text(encoding="utf-8")
    assert "#EXTINF:5,T - Tuple" in content
    assert "t.mp3" in content
    assert "#EXTINF:6,D - Dict" in content
    assert "d.mp3" in content


def test_export_m3u_requires_a_local_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="本地路径"):
        export_m3u([Track(name="Song", artists="Artist")], tmp_path / "bad.m3u")


def test_export_m3u_creates_parent_directory(tmp_path: Path) -> None:
    playlist = tmp_path / "nested" / "deep" / "list.m3u"

    export_m3u([], playlist)

    assert playlist.is_file()


# ---------------------------------------------------------------------------
# save_lyric
# ---------------------------------------------------------------------------


def test_save_lyric_writes_original_and_translation(tmp_path: Path) -> None:
    audio = tmp_path / "song.mp3"

    written = save_lyric(str(audio), "[00:01.00]Hello", "[00:01.00]你好")

    original = tmp_path / "song.lrc"
    translation = tmp_path / "song.zh.lrc"
    assert written == [str(original), str(translation)]
    assert original.read_text(encoding="utf-8") == "[00:01.00]Hello\n"
    assert translation.read_text(encoding="utf-8") == "[00:01.00]你好\n"


def test_save_lyric_translation_only_falls_back_to_main_lrc(tmp_path: Path) -> None:
    audio = tmp_path / "song.flac"

    written = save_lyric(str(audio), "", "[00:02.00]只有翻译")

    assert written == [str(tmp_path / "song.lrc")]
    assert (tmp_path / "song.lrc").read_text(encoding="utf-8") == "[00:02.00]只有翻译\n"
    assert not (tmp_path / "song.zh.lrc").exists()


def test_save_lyric_blank_text_writes_nothing(tmp_path: Path) -> None:
    written = save_lyric(str(tmp_path / "song.mp3"), "   ", "\n")

    assert written == []
    assert not (tmp_path / "song.lrc").exists()
    assert not (tmp_path / "song.zh.lrc").exists()


def test_save_lyric_normalizes_trailing_newlines_and_creates_dirs(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "disc" / "song.mp3"

    save_lyric(str(audio), "[00:01.00]line\r\n\r\n", "")

    assert (tmp_path / "disc" / "song.lrc").read_text(encoding="utf-8") == (
        "[00:01.00]line\n"
    )


# ---------------------------------------------------------------------------
# Tag / cover embedding
# ---------------------------------------------------------------------------


def test_write_tags_embeds_metadata_and_png_cover(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    from mutagen import File as MutaFile
    from mutagen.id3 import ID3

    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())
    track = Track(name="歌曲", artists="歌手", album="专辑", track_no=3)
    cover = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"

    downloader._write_tags(str(audio), track, cover)

    tags = MutaFile(str(audio), easy=True)
    assert tags is not None
    assert tags["title"] == ["歌曲"]
    assert tags["artist"] == ["歌手"]
    assert tags["album"] == ["专辑"]
    assert tags["tracknumber"] == ["3"]

    pictures = ID3(str(audio)).getall("APIC")
    assert len(pictures) == 1
    assert pictures[0].mime == "image/png"
    assert pictures[0].data == cover


def test_write_tags_cover_failure_degrades_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mutagen")
    import mutagen.id3
    from mutagen import File as MutaFile

    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())
    track = Track(name="Song", artists="Artist", album="Album")

    def broken_id3(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cover format unsupported")

    monkeypatch.setattr(mutagen.id3, "ID3", broken_id3)

    # Must not raise even though cover embedding is impossible.
    downloader._write_tags(str(audio), track, b"\xff\xd8\xff\xe0fake-jpeg")

    tags = MutaFile(str(audio), easy=True)
    assert tags is not None
    assert tags["title"] == ["Song"]


def test_write_tags_ignores_unparsable_audio(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")

    audio = tmp_path / "broken.mp3"
    audio.write_bytes(b"\x00" * 64)

    # Should simply give up: no exception, file untouched.
    downloader._write_tags(str(audio), Track(name="S", artists="A"), b"cover")

    assert audio.read_bytes() == b"\x00" * 64


def test_write_tags_missing_mutagen_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())

    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object):
        if name.startswith("mutagen"):
            raise ImportError("mutagen missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    downloader._write_tags(str(audio), Track(name="S", artists="A"), b"cover")

    assert audio.read_bytes() == _mp3_bytes()


# ---------------------------------------------------------------------------
# download_track orchestration
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, lyric_result: tuple[str, str] | None = None) -> None:
        self._lyric = lyric_result or ("[00:01.00]Original", "[00:01.00]译文")

    def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
        return ResolveResult(url="http://fake.test/audio.mp3", ext="mp3", level=level)

    def lyric(self, track: Track) -> tuple[str, str]:
        return self._lyric


def _fake_download(url, dest, **kwargs):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    Path(dest).write_bytes(_mp3_bytes())
    return os.path.getsize(dest), 0


def test_download_track_writes_lyrics_and_remembers_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(provider="fake", id="1", name="歌名", artists="歌手", album="专辑")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(_FakeProvider(), track, opts)

    assert result.status == "ok"
    assert result.path == str(tmp_path / "歌名 - 歌手.mp3")
    # downloader back-fills Track.path so export_m3u can consume the same objects
    assert track.path == result.path
    assert Path(tmp_path / "歌名 - 歌手.lrc").read_text(encoding="utf-8") == (
        "[00:01.00]Original\n"
    )
    assert Path(tmp_path / "歌名 - 歌手.zh.lrc").read_text(encoding="utf-8") == (
        "[00:01.00]译文\n"
    )


def test_download_track_lyric_failure_does_not_fail_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    class BrokenLyricProvider(_FakeProvider):
        def lyric(self, track: Track) -> tuple[str, str]:
            raise RuntimeError("lyric endpoint down")

    track = Track(provider="fake", id="2", name="Song", artists="Artist", album="Album")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(BrokenLyricProvider(), track, opts)

    assert result.status == "ok"
    assert not (tmp_path / "Song - Artist.lrc").exists()


def test_download_track_cover_request_failure_does_not_fail_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    captured: dict[str, bytes | None] = {}

    def capture_tags(path, track, cover_bytes=None):
        captured["cover"] = cover_bytes

    monkeypatch.setattr(downloader, "_write_tags", capture_tags)

    def broken_get(*args: object, **kwargs: object) -> None:
        raise OSError("cover CDN unreachable")

    monkeypatch.setattr(downloader.requests, "get", broken_get)

    track = Track(
        provider="fake",
        id="3",
        name="Song",
        artists="Artist",
        album="Album",
        cover_url="http://fake.test/cover.jpg",
    )
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=True,
        embed_cover=True,
        save_lyric=False,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(_FakeProvider(), track, opts)

    assert result.status == "ok"
    assert captured["cover"] is None


def test_download_track_skip_remembers_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mutagen")
    existing = tmp_path / "Song - Artist.mp3"
    existing.write_bytes(_mp3_bytes() * 10)
    # Same title as the track, so the "different song, same name" guard keeps
    # the plain destination and the skip path is exercised.
    downloader._write_tags(
        str(existing), Track(name="Song", artists="Artist", album="Album")
    )

    track = Track(provider="fake", id="4", name="Song", artists="Artist", album="Album")
    opts = DownloadOptions(
        out_dir=str(tmp_path), split_folder=False, embed_tags=False, embed_cover=False
    )

    result = downloader.download_track(_FakeProvider(), track, opts)

    assert result.status == "skip"
    assert track.path == str(existing)
