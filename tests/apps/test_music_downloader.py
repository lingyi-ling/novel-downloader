#!/usr/bin/env python3
"""
tests.apps.test_music_downloader
--------------------------------

Offline tests for the music download helpers:

* ``export_m3u`` playlists (relative/absolute paths, escaping, empty list)
* ``save_lyric`` sidecar files (``.lrc`` / ``.zh.lrc``)
* tag/cover writing degradation (synthetic MP3 bytes, no network)
* ``download_track`` orchestration with fakes (no real downloads)
* ``_download_file`` resume branches (206/200/416, content-type guard)
* destination sanitization: crafted ext / id must stay inside ``out_dir``
* ``download_many``: duplicate tracks never write the same file concurrently
  and the throttled ``on_tick`` callback never runs from two threads at once
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import (
    DownloadOptions,
    export_m3u,
    save_lyric,
    track_lyrics,
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
    # A ``.m3u`` with non-ASCII content carries a UTF-8 BOM; ``utf-8-sig``
    # strips it so the line assertions stay about the format, not the marker.
    content = playlist.read_text(encoding="utf-8-sig")
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


def test_save_lyric_normalizes_interior_crlf_and_lone_cr(tmp_path: Path) -> None:
    """docstring 承诺「换行统一为 \n」：中间的 CRLF / 裸 CR 也要归一。

    部分来源的歌词整段是 CRLF，很多播放器解析不了；行中间的换行
    strip() 够不着，必须显式替换。
    """
    audio = tmp_path / "song.mp3"

    save_lyric(
        str(audio),
        "[00:01.00]a\r\n[00:02.00]b\r[00:03.00]c",
        "[00:01.00]甲\r\n[00:02.00]乙",
    )

    # read_bytes 而不是 read_text：文本模式的通用换行转换会把 CRLF
    # 也显示成 \n，掩盖回归
    assert (tmp_path / "song.lrc").read_bytes().decode("utf-8") == (
        "[00:01.00]a\n[00:02.00]b\n[00:03.00]c\n"
    )
    assert (tmp_path / "song.zh.lrc").read_bytes().decode("utf-8") == (
        "[00:01.00]甲\n[00:02.00]乙\n"
    )


def test_save_lyric_keeps_lrc_timeline_and_writes_utf8_without_bom(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "song.mp3"

    written = save_lyric(
        str(audio), "[00:12.34]第一行\n[01:02.03]第二行", "[00:12.34]first line"
    )

    raw = (tmp_path / "song.lrc").read_bytes()
    assert written == [str(tmp_path / "song.lrc"), str(tmp_path / "song.zh.lrc")]
    assert raw == "[00:12.34]第一行\n[01:02.03]第二行\n".encode()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no UTF-8 BOM
    # The time tags are part of the text and must survive untouched.
    assert raw.decode("utf-8").startswith("[00:12.34]第一行")


def test_lyric_size_cap_is_one_mib() -> None:
    assert downloader.MAX_LYRIC_BYTES == 1024 * 1024


def test_save_lyric_rejects_oversized_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(downloader, "MAX_LYRIC_BYTES", 64)

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        written = save_lyric(str(tmp_path / "song.mp3"), "x" * 200)

    assert written == []
    assert not (tmp_path / "song.lrc").exists()
    assert "歌词保存失败" in caplog.text


def test_save_lyric_skips_identical_content_and_overwrites_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "song.mp3"
    target = tmp_path / "song.lrc"

    assert save_lyric(str(audio), "[00:01.00]same") == [str(target)]

    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def recording_replace(src: str, dst: str) -> None:
        calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(downloader.os, "replace", recording_replace)

    # Byte-identical content is skipped: no write, no mtime churn.
    assert save_lyric(str(audio), "[00:01.00]same") == []
    assert calls == []

    # Different content replaces the old sidecar atomically.
    assert save_lyric(str(audio), "[00:02.00]changed") == [str(target)]
    assert [dst for _src, dst in calls] == [str(target)]
    assert target.read_text(encoding="utf-8") == "[00:02.00]changed\n"


def test_save_lyric_atomic_write_failure_keeps_old_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "song.mp3"
    target = tmp_path / "song.lrc"
    save_lyric(str(audio), "[00:01.00]old")

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(downloader.os, "replace", broken_replace)

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        written = save_lyric(str(audio), "[00:02.00]new")

    assert written == []
    assert target.read_text(encoding="utf-8") == "[00:01.00]old\n"
    # No half-written temp file is left behind.
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert "歌词保存失败" in caplog.text


# ---------------------------------------------------------------------------
# track_lyrics: extracting lyrics already present on the Track
# ---------------------------------------------------------------------------


def test_track_lyrics_reads_track_lyric_field() -> None:
    track = Track(lyric="[00:01.00]from track")

    assert track_lyrics(track) == ("[00:01.00]from track", "")


def test_track_lyrics_reads_lyrics_mapping_with_translation() -> None:
    track = Track()
    track.lyrics = {"lyric": "[00:01.00]A", "tlyric": "[00:01.00]甲"}

    assert track_lyrics(track) == ("[00:01.00]A", "[00:01.00]甲")


def test_track_lyrics_reads_extra_provider_object() -> None:
    track = Track(extra={"info": SimpleNamespace(lyric="[00:02.00]from info")})

    assert track_lyrics(track) == ("[00:02.00]from info", "")


def test_track_lyrics_reads_nested_extra_mapping() -> None:
    track = Track(extra={"raw": {"lrc": {"lyric": "[00:03.00]nested"}}})

    assert track_lyrics(track) == ("[00:03.00]nested", "")


def test_track_lyrics_ignores_missing_and_sentinel_values() -> None:
    assert track_lyrics(Track()) == ("", "")
    assert track_lyrics(Track(lyric="NULL")) == ("", "")
    assert track_lyrics(Track(extra={"info": SimpleNamespace(lyric="  ")})) == ("", "")


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


def test_download_track_falls_back_to_track_lyric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(
        provider="fake",
        id="5",
        name="Song",
        artists="Artist",
        album="Album",
        lyric="[00:03.00]already on the track",
    )
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(_FakeProvider(("", "")), track, opts)

    assert result.status == "ok"
    assert (tmp_path / "Song - Artist.lrc").read_text(encoding="utf-8") == (
        "[00:03.00]already on the track\n"
    )


def test_download_track_without_lyrics_creates_no_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(provider="fake", id="6", name="Song", artists="Artist", album="Album")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(_FakeProvider(("", "")), track, opts)

    assert result.status == "ok"
    assert list(tmp_path.glob("*.lrc")) == []


def test_download_track_lyric_provider_failure_still_uses_track_lyric(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    class BrokenLyricProvider(_FakeProvider):
        def lyric(self, track: Track) -> tuple[str, str]:
            raise RuntimeError("lyric endpoint down")

    track = Track(
        provider="fake",
        id="7",
        name="Song",
        artists="Artist",
        album="Album",
        lyric="[00:04.00]track fallback",
    )
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        result = downloader.download_track(BrokenLyricProvider(), track, opts)

    assert result.status == "ok"
    assert (tmp_path / "Song - Artist.lrc").read_text(encoding="utf-8") == (
        "[00:04.00]track fallback\n"
    )
    assert "歌词获取失败" in caplog.text


def test_lyric_sidecar_shares_the_audio_stem_in_split_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(provider="fake", id="8", name="歌名", artists="歌手", album="专辑")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=True,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        skip_existing=False,
        retries=0,
    )

    result = downloader.download_track(_FakeProvider(), track, opts)

    audio = Path(result.path)
    assert audio.name == "歌名 - 歌手.mp3"
    assert audio.parent == tmp_path / "歌手" / "专辑"
    # Same stem as the audio, only the extension differs.
    assert audio.with_suffix(".lrc").read_text(encoding="utf-8") == (
        "[00:01.00]Original\n"
    )
    assert audio.with_suffix(".zh.lrc").is_file()


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


def _seed_existing_audio(tmp_path: Path, filename: str = "Song - Artist.mp3") -> Path:
    """Create a tagged, >10 KiB MP3 that the ``skip_existing`` guard accepts."""
    pytest.importorskip("mutagen")
    existing = tmp_path / filename
    existing.write_bytes(_mp3_bytes() * 10)
    stem = filename.removesuffix(".mp3")
    title, _, artist = stem.partition(" - ")
    downloader._write_tags(
        str(existing), Track(name=title, artists=artist or "Artist", album="Album")
    )
    return existing


def _skip_opts(tmp_path: Path, *, save_lyric: bool = True) -> DownloadOptions:
    return DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=save_lyric,
        skip_existing=True,
        retries=0,
    )


def test_skip_existing_backfills_missing_lyrics(tmp_path: Path) -> None:
    existing = _seed_existing_audio(tmp_path)
    audio_before = existing.read_bytes()
    track = Track(provider="fake", id="9", name="Song", artists="Artist", album="Album")

    result = downloader.download_track(_FakeProvider(), track, _skip_opts(tmp_path))

    assert result.status == "skip"
    assert result.bytes_ == len(audio_before)
    # The audio itself is untouched: this is a lyric backfill, not a re-download.
    assert existing.read_bytes() == audio_before
    assert (tmp_path / "Song - Artist.lrc").read_text(encoding="utf-8") == (
        "[00:01.00]Original\n"
    )
    assert (tmp_path / "Song - Artist.zh.lrc").read_text(encoding="utf-8") == (
        "[00:01.00]译文\n"
    )


def test_skip_existing_does_not_refetch_existing_lyrics(
    tmp_path: Path,
) -> None:
    _seed_existing_audio(tmp_path)
    lrc = tmp_path / "Song - Artist.lrc"
    lrc.write_text("[00:09.00]keep me\n", encoding="utf-8")

    class CountingProvider(_FakeProvider):
        calls = 0

        def lyric(self, track: Track) -> tuple[str, str]:
            CountingProvider.calls += 1
            return super().lyric(track)

    track = Track(
        provider="fake", id="10", name="Song", artists="Artist", album="Album"
    )

    result = downloader.download_track(CountingProvider(), track, _skip_opts(tmp_path))

    assert result.status == "skip"
    assert CountingProvider.calls == 0
    assert lrc.read_text(encoding="utf-8") == "[00:09.00]keep me\n"
    assert not (tmp_path / "Song - Artist.zh.lrc").exists()


def test_skip_existing_lyric_failure_still_skips_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    existing = _seed_existing_audio(tmp_path)

    class BrokenLyricProvider(_FakeProvider):
        def lyric(self, track: Track) -> tuple[str, str]:
            raise RuntimeError("lyric endpoint down")

    track = Track(
        provider="fake", id="11", name="Song", artists="Artist", album="Album"
    )

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        result = downloader.download_track(
            BrokenLyricProvider(), track, _skip_opts(tmp_path)
        )

    assert result.status == "skip"
    assert result.path == str(existing)
    assert not (tmp_path / "Song - Artist.lrc").exists()
    assert "歌词获取失败" in caplog.text


def test_skip_existing_lyrics_use_the_id_suffixed_audio_path(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mutagen")
    # Same stem but a different tagged title forces the "[id]" destination.
    base = tmp_path / "Song - Artist.mp3"
    base.write_bytes(_mp3_bytes())
    downloader._write_tags(
        str(base), Track(name="Another", artists="Artist", album="Album")
    )
    existing = tmp_path / "Song - Artist [11].mp3"
    existing.write_bytes(_mp3_bytes() * 10)
    downloader._write_tags(
        str(existing), Track(name="Song", artists="Artist", album="Album")
    )

    track = Track(
        provider="fake", id="11", name="Song", artists="Artist", album="Album"
    )

    result = downloader.download_track(_FakeProvider(), track, _skip_opts(tmp_path))

    assert result.status == "skip"
    assert result.path == str(existing)
    # The lyric sidecar follows the actual audio path, keeping the same stem.
    assert (tmp_path / "Song - Artist [11].lrc").is_file()
    assert not (tmp_path / "Song - Artist.lrc").exists()


# ---------------------------------------------------------------------------
# _download_file: resume branches (Range -> 206 / 200 / 416)
# ---------------------------------------------------------------------------


class _StreamResponse:
    """Minimal streaming ``requests.Response`` double."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"data",),
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size: int | None = None):
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


def _install_fake_get(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StreamResponse]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    queue = list(responses)

    def fake_get(url: str, **kwargs: object) -> _StreamResponse:
        calls.append({"url": url, **kwargs})
        assert queue, f"unexpected request: {url}"
        return queue.pop(0)

    monkeypatch.setattr(downloader.requests, "get", fake_get)
    return calls


def test_download_file_resumes_via_206(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "song.mp3"
    part = tmp_path / "song.mp3.part"
    part.write_bytes(b"A" * 512)  # bytes already downloaded earlier
    body = b"B" * 512
    response = _StreamResponse(
        status_code=206,
        headers={
            "Content-Range": "bytes 512-1023/1024",
            "Content-Length": "512",
            "Content-Type": "audio/mpeg",
        },
        chunks=(body,),
    )
    calls = _install_fake_get(monkeypatch, [response])

    size, resumed = downloader._download_file("http://fake.test/a.mp3", str(dest))

    assert (size, resumed) == (1024, 512)
    assert dest.read_bytes() == b"A" * 512 + b"B" * 512
    assert not part.exists()
    # The Range header asked for exactly the missing bytes.
    sent = calls[0]["headers"]
    assert isinstance(sent, dict) and sent.get("Range") == "bytes=512-"


def test_download_file_truncates_when_server_ignores_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "song.mp3"
    part = tmp_path / "song.mp3.part"
    part.write_bytes(b"A" * 512)  # stale partial, server answers 200
    body = b"B" * 1024
    response = _StreamResponse(
        status_code=200,
        headers={"Content-Length": "1024", "Content-Type": "audio/mpeg"},
        chunks=(body,),
    )
    _install_fake_get(monkeypatch, [response])

    size, resumed = downloader._download_file("http://fake.test/a.mp3", str(dest))

    assert (size, resumed) == (1024, 0)
    # The old partial was discarded, not appended to.
    assert dest.read_bytes() == body
    assert not part.exists()


def test_download_file_restarts_when_206_serves_unrequested_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 206 whose Content-Range start differs from the request is untrusted.

    Appending a full-body 206 onto the stale ``.part`` would corrupt the file
    (or at best fail the size check and burn a retry), so the partial must be
    rewritten from scratch instead.
    """
    dest = tmp_path / "song.mp3"
    part = tmp_path / "song.mp3.part"
    part.write_bytes(b"A" * 512)
    body = b"B" * 1024
    response = _StreamResponse(
        status_code=206,
        headers={
            "Content-Range": "bytes 0-1023/1024",  # server ignored bytes=512-
            "Content-Length": "1024",
            "Content-Type": "audio/mpeg",
        },
        chunks=(body[:512], body[512:]),
    )
    calls = _install_fake_get(monkeypatch, [response])

    size, resumed = downloader._download_file("http://fake.test/a.mp3", str(dest))

    assert (size, resumed) == (1024, 0)
    assert dest.read_bytes() == body
    assert not part.exists()
    sent = calls[0]["headers"]
    assert isinstance(sent, dict) and sent.get("Range") == "bytes=512-"


def test_download_file_discards_oversized_part_on_416(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "song.mp3"
    part = tmp_path / "song.mp3.part"
    part.write_bytes(b"A" * 2000)  # larger than the real remote file
    body = b"C" * 100
    too_big = _StreamResponse(status_code=416)
    fresh = _StreamResponse(
        status_code=200,
        headers={"Content-Length": "100", "Content-Type": "audio/mpeg"},
        chunks=(body,),
    )
    _install_fake_get(monkeypatch, [too_big, fresh])

    size, resumed = downloader._download_file("http://fake.test/a.mp3", str(dest))

    assert (size, resumed) == (100, 0)
    assert dest.read_bytes() == body
    assert not part.exists()
    assert too_big.closed


def test_download_file_rejects_non_audio_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "song.mp3"
    response = _StreamResponse(
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=(b"<html>expired link</html>",),
    )
    _install_fake_get(monkeypatch, [response])

    with pytest.raises(OSError, match="不是音频"):
        downloader._download_file("http://fake.test/a.mp3", str(dest))

    assert not dest.exists()
    assert not (tmp_path / "song.mp3.part").exists()


# ---------------------------------------------------------------------------
# destination sanitization (path traversal)
# ---------------------------------------------------------------------------


def _quick_opts(tmp_path: Path) -> DownloadOptions:
    return DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=False,
        skip_existing=False,
        retries=0,
    )


def test_download_track_ext_cannot_escape_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    class EvilExtProvider(_FakeProvider):
        def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
            # A crafted/hijacked platform response embeds path separators and
            # ".." segments into the extension.
            return ResolveResult(url="http://fake.test/audio", ext="mp3/../../pwned")

    track = Track(provider="fake", id="1", name="Song", artists="Artist", album="Album")

    result = downloader.download_track(EvilExtProvider(), track, _quick_opts(tmp_path))

    assert result.status == "ok"
    # Whatever the provider claims, the file must stay inside out_dir.
    assert Path(result.path).resolve().is_relative_to(tmp_path.resolve())
    assert result.path.endswith(".mp3")
    assert (tmp_path / "Song - Artist.mp3").is_file()


def test_download_track_id_suffix_cannot_escape_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mutagen")
    # An existing file with a different title triggers the "[id]" fallback name,
    # which used to interpolate the raw track id into the path.
    existing = tmp_path / "Song - Artist.mp3"
    existing.write_bytes(_mp3_bytes())
    downloader._write_tags(
        str(existing), Track(name="Different Song", artists="Artist", album="Album")
    )

    monkeypatch.setattr(downloader, "_download_file", _fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(
        provider="fake",
        id="../../../../evil",
        name="Song",
        artists="Artist",
        album="Album",
    )

    result = downloader.download_track(_FakeProvider(), track, _quick_opts(tmp_path))

    assert result.status == "ok"
    assert Path(result.path).resolve().is_relative_to(tmp_path.resolve())
    assert (tmp_path / "Song - Artist.mp3").is_file()


# ---------------------------------------------------------------------------
# download_many: same-destination concurrency and tick throttling
# ---------------------------------------------------------------------------


def test_download_many_duplicate_tracks_never_write_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same track twice in one batch must not write one file from two threads."""
    guard = threading.Lock()
    state = {"active": 0}
    overlaps: list[str] = []

    def slow_fake_download(url, dest, **kwargs):
        with guard:
            state["active"] += 1
            if state["active"] > 1:
                overlaps.append(dest)
        try:
            time.sleep(0.05)  # widen the window; concurrent writers overlap here
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).write_bytes(_mp3_bytes() * 10)
            return os.path.getsize(dest), 0
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(downloader, "_download_file", slow_fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    track = Track(provider="fake", id="1", name="Song", artists="Artist", album="Album")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=False,
        skip_existing=True,
        retries=0,
    )

    ok, skip, fail, _, _ = downloader.download_many(
        _FakeProvider(), [track, track], opts, workers=2
    )

    assert overlaps == []
    assert fail == 0
    # The duplicate must be recognized after the first write finished.
    assert (ok, skip) == (1, 1)


def test_download_many_tick_never_runs_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine promises a throttled on_tick; it must also be serialized."""
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)

    def ticking_download(url, dest, progress=None, stop_flag=None, **kwargs):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        for i in range(4):
            if progress:
                progress((i + 1) * 100, 400)
            time.sleep(0.002)
        Path(dest).write_bytes(_mp3_bytes())
        return os.path.getsize(dest), 0

    monkeypatch.setattr(downloader, "_download_file", ticking_download)

    guard = threading.Lock()
    inside = {"n": 0}
    violations: list[float] = []

    def on_tick(fraction: float, done: int) -> None:
        with guard:
            inside["n"] += 1
            if inside["n"] > 1:
                violations.append(fraction)
        time.sleep(0.01)  # hold the callback so overlaps are observable
        with guard:
            inside["n"] -= 1

    tracks = [
        Track(provider="fake", id="1", name="One", artists="Artist", album="Album"),
        Track(provider="fake", id="2", name="Two", artists="Artist", album="Album"),
    ]

    ok, skip, fail, _, _ = downloader.download_many(
        _FakeProvider(),
        tracks,
        _quick_opts(tmp_path),
        workers=2,
        on_tick=on_tick,
        min_interval=0,  # disable the interval so every tick reaches on_tick
    )

    assert (ok, fail) == (2, 0)
    assert violations == []


# ---------------------------------------------------------------------------
# cover download bounds
# ---------------------------------------------------------------------------


class _CoverResponse:
    """Minimal streaming ``requests.Response`` double."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status
        self.closed = False

    def iter_content(self, _size: int):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def test_fetch_cover_enforces_the_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _CoverResponse([b"x" * 200], headers={})
    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: response)
    monkeypatch.setattr(downloader, "MAX_COVER_BYTES", 100)

    assert downloader._fetch_cover("http://fake.test/cover.jpg") is None
    # The connection is released even when the body is abandoned.
    assert response.closed is True


def test_fetch_cover_trusts_a_declared_length_only_as_a_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _CoverResponse([b"x"], headers={"Content-Length": str(10 * 1024 * 1024)})
    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: response)

    assert downloader._fetch_cover("http://fake.test/cover.jpg") is None
    assert response.closed is True


def test_fetch_cover_returns_the_body_within_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _CoverResponse([b"abc", b"def"])
    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: response)

    assert downloader._fetch_cover("http://fake.test/cover.jpg") == b"abcdef"
