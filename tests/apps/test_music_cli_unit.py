#!/usr/bin/env python3
"""tests.apps.test_music_cli_unit
---------------------------------

Unit coverage for the helper layer and remaining command flows of
:mod:`novel_downloader.apps.cli.commands.music`.

``test_music_cli.py`` covers the ``search --json``/``platforms`` output; these
tests add the pure helpers (duration formatting, index parsing, ``--pick``
resolution, playlist file names, playable split), the interactive paged
picker and the ``download`` / ``export-m3u`` / ``health`` command paths, all
offline with a fake engine.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import music
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.music.downloader import export_m3u as real_export_m3u

#: UTF-8 byte-order mark the ``.m3u`` writer prepends to non-ASCII playlists.
BOM = b"\xef\xbb\xbf"


class FakeLoginExpired(Exception):
    """Stand-in for the engine's LoginExpired marker."""


class ThrowingProvider:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        raise self.exc


class FakeProvider:
    def __init__(self, tracks: list[Any]) -> None:
        self.tracks = tracks

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        return list(self.tracks), len(self.tracks)

    def is_logged_in(self) -> bool:
        return True


class FakeEngine:
    """Stand-in for ``music._MusicEngine`` covering download/export/health."""

    login_expired = FakeLoginExpired

    def __init__(
        self,
        *,
        provider: Any = None,
        labels: list[tuple[str, str]] | None = None,
        note: str | None = None,
        settings: dict[str, Any] | None = None,
        download_result: tuple[Any, ...] | None = None,
        download_error: BaseException | None = None,
        export_result: str | None = None,
        export_error: BaseException | None = None,
        export_writer: Any = None,
        health_results: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider or FakeProvider([])
        self._labels = labels if labels is not None else [("netease", "NetEase")]
        self._note = note
        self._settings = settings if settings is not None else {}
        # The real engine exposes the settings module object directly.
        self.settings = SimpleNamespace(load_settings=lambda: dict(self._settings))
        self.download_result = download_result or (0, 0, 0, [], None)
        self.download_error = download_error
        self.export_result = export_result or ""
        self.export_error = export_error
        # Optional drop-in for the real exporter, used by the encoding tests.
        self.export_writer = export_writer
        self.health_results = health_results or {}
        self.created: list[str] = []
        self.download_calls: list[dict[str, Any]] = []
        self.mixed_calls: list[dict[str, Any]] = []
        self.export_calls: list[tuple[list[Any], str]] = []
        self.health_calls: list[tuple[str, str]] = []
        self.download_options_calls: list[dict[str, Any]] = []

    # -- registry surface ---------------------------------------------------
    def labels(self) -> list[tuple[str, str]]:
        return list(self._labels)

    def platform_keys(self) -> list[str]:
        return sorted(key for key, _label in self._labels)

    def platform_note(self, platform: str) -> str | None:
        return self._note

    def create(self, platform: str) -> Any:
        self.created.append(platform)
        return self.provider

    # -- engine surface ------------------------------------------------------
    def load_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def default_download_dir(self) -> str:
        return "/default/music"

    def download_options(self, **kwargs: Any) -> SimpleNamespace:
        self.download_options_calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    def download_many(
        self, provider: Any, tracks: list[Any], options: Any, **kwargs: Any
    ) -> tuple[Any, ...]:
        if self.download_error is not None:
            raise self.download_error
        self.download_calls.append(
            {"tracks": list(tracks), "options": options, **kwargs}
        )
        return self.download_result

    def download_mixed(
        self, create_provider: Any, tracks: list[Any], options: Any, **kwargs: Any
    ) -> tuple[Any, ...]:
        if self.download_error is not None:
            raise self.download_error
        self.mixed_calls.append({"tracks": list(tracks), "options": options, **kwargs})
        return self.download_result

    def export_m3u(self, tracks: list[Any], out_path: str) -> str:
        if self.export_error is not None:
            raise self.export_error
        self.export_calls.append((list(tracks), out_path))
        if self.export_writer is not None:
            return str(self.export_writer(tracks, out_path))
        # The real exporter writes the playlist to the path it is given; the
        # command hands it a sibling temp file and renames atomically.
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#EXTM3U\n")
            for track in tracks:
                fh.write(f"#EXTINF:185,{track.artists} - {track.name}\n")
                fh.write(f"{track.path}\n")
        return self.export_result or out_path

    def health_check(self, key: str, keyword: str) -> dict[str, Any]:
        self.health_calls.append((key, keyword))
        return self.health_results.get(key, {"ok": True, "elapsed_ms": 5, "error": ""})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    MusicCmd.register(subparsers)
    return parser


def _run(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    engine: FakeEngine,
) -> None:
    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    args = _parser().parse_args(argv)
    args.func(args)


def _track(
    index: int,
    *,
    path: str = "",
    name: str | None = None,
    artists: str = "Artist",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id{index}",
        name=name or f"Song {index}",
        artists=artists,
        album="Album",
        duration_s=185,
        vip=index % 2 == 0,
        provider="netease",
        path=path,
    )


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "-"),
        (-5, "-"),
        (59, "0:59"),
        (185, "3:05"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
    ],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert music._format_duration(seconds) == expected


def test_track_rows_render_vip_and_missing_duration() -> None:
    tracks = [
        SimpleNamespace(name="A", artists="X", album="Al", duration_s=0, vip=True),
        SimpleNamespace(name="B", artists="Y", album="Al", duration_s=61, vip=False),
    ]

    rows = music._track_rows(tracks)

    assert rows[0] == ["1", "A", "X", "Al", "-", "VIP"]
    assert rows[1] == ["2", "B", "Y", "Al", "1:01", "-"]


def test_track_payload_serialises_all_fields() -> None:
    payload = music._track_payload(3, _track(3))

    assert payload == {
        "index": 3,
        "id": "id3",
        "name": "Song 3",
        "artists": "Artist",
        "album": "Album",
        "duration_s": 185,
        "vip": False,
        "platform": "netease",
        "sources": [],
    }


def test_track_payload_defaults_when_provider_missing() -> None:
    track = SimpleNamespace(
        id="7", name="N", artists="A", album="B", duration_s=None, vip=None
    )

    payload = music._track_payload(1, track)

    assert payload["platform"] == ""
    assert payload["duration_s"] == 0
    assert payload["vip"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,3,2", [1, 2, 3]),  # sorted + deduplicated
        (" 2 ,2 ", [2]),
        ("3", [3]),
    ],
)
def test_parse_indices_valid(value: str, expected: list[int]) -> None:
    assert music._parse_indices(value, 5) == expected


@pytest.mark.parametrize("value", ["", "0", "6", "1,x", "1;2"])
def test_parse_indices_invalid_returns_none(value: str) -> None:
    assert music._parse_indices(value, 5) is None


def test_pick_tracks_accepts_indices_ids_and_mixed() -> None:
    tracks = [_track(1), _track(2), _track(3)]

    by_index = music._pick_tracks(tracks, "1,3")
    by_id = music._pick_tracks(tracks, "id2")
    mixed = music._pick_tracks(tracks, "1,id3")

    assert by_index == [tracks[0], tracks[2]]
    assert by_id == [tracks[1]]
    assert mixed == [tracks[0], tracks[2]]


def test_pick_tracks_deduplicates_and_skips_empty_tokens() -> None:
    tracks = [_track(1), _track(2)]

    picked = music._pick_tracks(tracks, "1,,1, 2 ")

    assert picked == [tracks[0], tracks[1]]


def test_pick_tracks_unknown_value_exits_with_usage_error() -> None:
    tracks = [_track(1)]

    with pytest.raises(SystemExit) as excinfo:
        music._pick_tracks(tracks, "9")

    assert excinfo.value.code == 2


def test_normalize_platform_keeps_unknown_input_for_the_error_message() -> None:
    engine = FakeEngine(labels=[("netease", "NetEase")])

    assert music._normalize_platform(engine, " NETease ") == "netease"
    assert music._normalize_platform(engine, "ALL") == "all"
    assert music._normalize_platform(engine, "") == "all"
    assert music._normalize_platform(engine, " BoGus ") == "BoGus"


def test_is_logged_in_never_raises() -> None:
    class Broken:
        def is_logged_in(self) -> bool:
            raise RuntimeError("boom")

    assert music._is_logged_in(FakeProvider([])) is True
    assert music._is_logged_in(Broken()) is False


# --------------------------------------------------------------------------- #
# playlist file helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("hello", "hello.m3u8"),
        # every class-listed char is replaced; a backslash is not in the class
        ('a<b>:"c/d|e?f*g', "a_b___c_d_e_f_g.m3u8"),  # trailing _ is stripped
        ("   ", "playlist.m3u8"),
        ("x" * 200, "x" * 120 + ".m3u8"),
        # Windows DOS device names cannot be created as files, even with an
        # extension (``AUX.m3u8`` raises ENOENT).
        ("CON", "_CON.m3u8"),
        ("aux", "_aux.m3u8"),
        ("COM1", "_COM1.m3u8"),
        ("nul.txt", "_nul.txt.m3u8"),
    ],
)
def test_playlist_filename_sanitises(keyword: str, expected: str) -> None:
    assert music._playlist_filename(keyword) == expected


def test_default_playlist_path_joins_out_dir() -> None:
    import os

    assert music._default_playlist_path("kw", "C:/music") == os.path.join(
        "C:/music", "kw.m3u8"
    )


def test_split_playable_separates_local_and_missing_paths() -> None:
    ready_track = _track(1, path="C:/music/song.mp3")
    blank_track = _track(2, path="   ")
    missing_track = _track(3)
    non_string = SimpleNamespace(path=123)

    ready, skipped = music._split_playable(
        [ready_track, blank_track, missing_track, non_string]
    )

    assert ready == [ready_track]
    assert skipped == [blank_track, missing_track, non_string]


def test_resolve_playable_finds_downloaded_file_by_naming_convention(
    tmp_path, monkeypatch
) -> None:
    # A fresh search result has an empty ``path``; the file was written by the
    # downloader as ``<out>/<artist>/<album>/<name> - <artist>.<ext>``.
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    audio = folder / "Song 1 - Artist.mp3"
    audio.write_bytes(b"x")

    track = _track(1)
    ready, skipped = music._resolve_playable([track], str(tmp_path), split_folder=True)

    assert ready == [track]
    assert skipped == []
    assert track.path == str(audio)


def test_resolve_playable_uses_flat_folder_when_not_split(tmp_path) -> None:
    audio = tmp_path / "Song 1 - Artist.flac"
    audio.write_bytes(b"x")

    track = _track(1)
    ready, _skipped = music._resolve_playable(
        [track], str(tmp_path), split_folder=False
    )

    assert ready == [track]
    assert track.path == str(audio)


def test_resolve_playable_prefers_plain_name_over_id_fallback(tmp_path) -> None:
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    plain = folder / "Song 1 - Artist.mp3"
    plain.write_bytes(b"x")
    (folder / "Song 1 - Artist [id1].mp3").write_bytes(b"y")

    track = _track(1)
    ready, _skipped = music._resolve_playable([track], str(tmp_path), split_folder=True)

    assert ready == [track]
    assert track.path == str(plain)


def test_resolve_playable_keeps_existing_path_without_scanning(tmp_path) -> None:
    track = _track(1, path="already/set.mp3")
    ready, skipped = music._resolve_playable([track], str(tmp_path), split_folder=True)

    assert ready == [track]
    assert skipped == []
    assert track.path == "already/set.mp3"


def test_resolve_playable_ignores_non_audio_and_missing_folder(tmp_path) -> None:
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    (folder / "Song 1 - Artist.lrc").write_text("lyric", encoding="utf-8")

    track = _track(1)
    ready, skipped = music._resolve_playable([track], str(tmp_path), split_folder=True)
    assert ready == []
    assert skipped == [track]

    # A non-existent output directory is not an error either.
    missing = _track(2)
    ready, skipped = music._resolve_playable(
        [missing], str(tmp_path / "nope"), split_folder=True
    )
    assert ready == []
    assert skipped == [missing]


# --------------------------------------------------------------------------- #
# interactive picker
# --------------------------------------------------------------------------- #


def _install_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    asked: list[str] = []

    def fake_prompt(message: str, **kwargs: Any) -> str:
        asked.append(message)
        return answers.pop(0) if answers else ""

    monkeypatch.setattr(music.ui, "prompt", fake_prompt)
    return asked


def test_select_interactive_empty_input_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_prompts(monkeypatch, [""])

    assert music._select_interactive([_track(1)]) == []


def test_select_interactive_a_downloads_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_prompts(monkeypatch, ["a"])
    tracks = [_track(1), _track(2)]

    assert music._select_interactive(tracks) == tracks


def test_select_interactive_pages_forward_back_and_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 25 tracks -> 3 pages of 10.
    tracks = [_track(i) for i in range(1, 26)]
    _install_prompts(monkeypatch, ["n", "n", "p", "3"])

    selected = music._select_interactive(tracks)

    assert selected == [tracks[2]]


def test_select_interactive_next_on_last_page_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 15 tracks -> 2 pages; 'n' on the last page re-prompts.
    tracks = [_track(i) for i in range(1, 16)]
    _install_prompts(monkeypatch, ["n", "1"])

    selected = music._select_interactive(tracks)

    assert selected == [tracks[0]]


def test_select_interactive_invalid_then_valid_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracks = [_track(1), _track(2)]
    _install_prompts(monkeypatch, ["zz", "2"])

    selected = music._select_interactive(tracks)

    assert selected == [tracks[1]]


# --------------------------------------------------------------------------- #
# download command flow
# --------------------------------------------------------------------------- #


def test_download_with_pick_invokes_engine(monkeypatch, tmp_path, capsys) -> None:
    provider = FakeProvider([_track(1), _track(2)])
    engine = FakeEngine(
        provider=provider,
        settings={"out_dir": str(tmp_path), "level": "exhigh", "workers": 5},
        download_result=(2, 0, 0, [], None),
    )

    _run(
        ["music", "download", "--platform", "netease", "hello", "--pick", "1,2"],
        monkeypatch,
        engine,
    )

    call = engine.download_calls[0]
    assert len(call["tracks"]) == 2
    assert call["workers"] == 5
    options = call["options"]
    assert options.out_dir == str(tmp_path)
    assert options.level == "exhigh"

    out = capsys.readouterr()
    assert "Done: 2 succeeded" in out.out + out.err


def test_download_reports_failed_pairs(monkeypatch, capsys) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(
        provider=provider,
        settings={},
        download_result=(0, 0, 1, [("Song 1", "vip")], None),
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "download", "--platform", "netease", "hello", "--pick", "1"],
            monkeypatch,
            engine,
        )

    # A failed song must not look like a successful batch to scripts.
    assert excinfo.value.code == 1
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert "Failed: Song 1 (vip)" in text
    assert "Done: 0 succeeded, 0 skipped, 1 failed" in text


def test_download_requires_pick_or_yes_when_non_interactive(
    monkeypatch, capsys
) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(provider=provider)
    monkeypatch.setattr(music.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "download", "--platform", "netease", "hello"], monkeypatch, engine
        )

    assert excinfo.value.code == 2
    assert "--pick" in capsys.readouterr().err


def test_download_login_expired_shows_hint(monkeypatch, capsys) -> None:
    provider = ThrowingProvider(FakeLoginExpired("session gone"))
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "download", "--platform", "netease", "hello", "--yes"],
            monkeypatch,
            engine,
        )

    assert excinfo.value.code == 1
    assert "expired" in capsys.readouterr().err.lower()


def test_download_search_failure_exits_one(monkeypatch, capsys) -> None:
    provider = ThrowingProvider(RuntimeError("network down"))
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "download", "--platform", "netease", "hello", "--yes"],
            monkeypatch,
            engine,
        )

    assert excinfo.value.code == 1
    assert "Search failed" in capsys.readouterr().err


def test_download_no_results_returns_cleanly(monkeypatch, capsys) -> None:
    engine = FakeEngine(provider=FakeProvider([]))

    _run(
        ["music", "download", "--platform", "netease", "hello", "--yes"],
        monkeypatch,
        engine,
    )

    assert engine.download_calls == []
    assert "No results found" in capsys.readouterr().err


def test_download_keyword_flag_matches_positional(monkeypatch, tmp_path) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(provider=provider, settings={"out_dir": str(tmp_path)})

    _run(
        ["music", "download", "--platform", "netease", "--keyword", "flag-kw", "--yes"],
        monkeypatch,
        engine,
    )

    assert engine.download_calls[0]["tracks"]


def test_download_missing_keyword_is_a_usage_error(monkeypatch, capsys) -> None:
    engine = FakeEngine()

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "download"], monkeypatch, engine)

    assert excinfo.value.code == 2
    assert "No keyword" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# export-m3u command flow
# --------------------------------------------------------------------------- #


def test_export_m3u_writes_ready_tracks_to_default_path(
    monkeypatch, tmp_path, capsys
) -> None:
    provider = FakeProvider([_track(1, path="a.mp3"), _track(2)])
    engine = FakeEngine(provider=provider, settings={"out_dir": str(tmp_path)})

    _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    ready, temp_path = engine.export_calls[0]
    import os

    playlist = os.path.join(str(tmp_path), "hello.m3u8")
    # The exporter writes a sibling temp file; the command renames it into
    # place only after the write completed.
    assert os.path.dirname(temp_path) == str(tmp_path)
    assert temp_path != playlist
    # The temp file keeps the target extension so the exporter picks the
    # right encoding (``.m3u8`` without BOM).
    assert os.path.splitext(temp_path)[1] == ".m3u8"
    assert os.path.isfile(playlist)
    with open(playlist, encoding="utf-8") as fh:
        assert fh.read().startswith("#EXTM3U\n")
    assert [track.id for track in ready] == ["id1"]
    captured = capsys.readouterr()
    assert "Exported 1 track(s)" in captured.out + captured.err


def test_export_m3u_out_flag_overrides_default(monkeypatch, tmp_path) -> None:
    provider = FakeProvider([_track(1, path="a.mp3")])
    engine = FakeEngine(provider=provider)
    target = tmp_path / "custom.m3u8"

    _run(
        ["music", "export-m3u", "-k", "hello", "--out", str(target)],
        monkeypatch,
        engine,
    )

    temp_path = engine.export_calls[0][1]
    assert os.path.dirname(temp_path) == str(tmp_path)
    assert temp_path != str(target)
    assert os.path.splitext(temp_path)[1] == ".m3u8"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("#EXTM3U\n")


def test_export_m3u_platform_is_case_insensitive_and_aliased(
    monkeypatch, tmp_path
) -> None:
    engine = FakeEngine(
        provider=FakeProvider([_track(1, path="a.mp3")]),
        labels=[("netease", "国内 · 网易云音乐")],
        settings={"out_dir": str(tmp_path)},
    )

    _run(
        ["music", "export-m3u", "-k", "hello", "--platform", "WANGYIYUN"],
        monkeypatch,
        engine,
    )

    assert engine.created == ["netease"]


def test_export_m3u_without_local_paths_fails(monkeypatch, capsys) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert engine.export_calls == []


def test_export_m3u_write_failure_exits(monkeypatch, capsys, tmp_path) -> None:
    provider = FakeProvider([_track(1, path="a.mp3")])
    engine = FakeEngine(
        provider=provider,
        settings={"out_dir": str(tmp_path)},
        export_error=OSError("disk full"),
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert "Could not write playlist" in capsys.readouterr().err
    # No half-written target and no leftover temp file.
    assert not (tmp_path / "hello.m3u8").exists()
    assert list(tmp_path.iterdir()) == []


def test_export_m3u_directory_target_is_an_error(monkeypatch, capsys, tmp_path) -> None:
    provider = FakeProvider([_track(1, path="a.mp3")])
    engine = FakeEngine(provider=provider, settings={"out_dir": str(tmp_path)})
    target = tmp_path / "playlist-dir"
    target.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "export-m3u", "-k", "hello", "--out", str(target)],
            monkeypatch,
            engine,
        )

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "Could not write playlist" in stderr
    assert "Is a directory" in stderr
    # The exporter is never asked to open a directory.
    assert engine.export_calls == []


def test_export_m3u_failure_keeps_existing_playlist(
    monkeypatch, capsys, tmp_path
) -> None:
    """A failed write must not truncate the previous good playlist."""
    target = tmp_path / "custom.m3u8"
    target.write_text("#EXTM3U\nold\n", encoding="utf-8")

    class PartialWriter(FakeEngine):
        def export_m3u(self, tracks: list[Any], out_path: str) -> str:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("#EXTMU")  # simulate a write that dies halfway
            raise OSError("disk full")

    engine = PartialWriter(provider=FakeProvider([_track(1, path="a.mp3")]))

    with pytest.raises(SystemExit) as excinfo:
        _run(
            ["music", "export-m3u", "-k", "hello", "--out", str(target)],
            monkeypatch,
            engine,
        )

    assert excinfo.value.code == 1
    assert target.read_text(encoding="utf-8") == "#EXTM3U\nold\n"
    # The failed attempt leaves nothing behind next to the old playlist.
    assert [entry.name for entry in tmp_path.iterdir()] == ["custom.m3u8"]


# --------------------------------------------------------------------------- #
# health command flow
# --------------------------------------------------------------------------- #


def test_health_unknown_platform_is_usage_error(monkeypatch, capsys) -> None:
    engine = FakeEngine(labels=[("netease", "NetEase")])

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health", "--platform", "bogus"], monkeypatch, engine)

    assert excinfo.value.code == 2
    assert engine.health_calls == []


def test_health_single_platform_probe(monkeypatch, capsys) -> None:
    engine = FakeEngine(labels=[("netease", "NetEase"), ("qq", "QQ")])

    _run(
        ["music", "health", "--platform", "netease", "--keyword", "probe"],
        monkeypatch,
        engine,
    )

    assert engine.health_calls == [("netease", "probe")]
    captured = capsys.readouterr()
    assert "ok" in (captured.out + captured.err).lower()


def test_health_reports_failure_exit_code(monkeypatch, capsys) -> None:
    engine = FakeEngine(
        labels=[("netease", "NetEase")],
        health_results={"netease": {"ok": False, "elapsed_ms": 9, "error": "timeout"}},
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health"], monkeypatch, engine)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "timeout" in captured.out + captured.err


def test_health_without_platforms_warns(monkeypatch, capsys) -> None:
    engine = FakeEngine(labels=[])

    _run(["music", "health"], monkeypatch, engine)

    assert "No music platforms" in capsys.readouterr().err
    assert engine.health_calls == []


def test_health_json_without_platforms_still_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["music", "health", "--json"], monkeypatch, FakeEngine(labels=[]))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"keyword": "test", "platforms": [], "failed": 0}


@pytest.mark.parametrize("platform", ["Netease", "163", "网易云音乐"])
def test_health_platform_is_case_insensitive_and_aliased(
    platform: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(labels=[("netease", "国内 · 网易云音乐"), ("qq", "QQ音乐")])

    _run(["music", "health", "--platform", platform], monkeypatch, engine)

    assert engine.health_calls == [("netease", "test")]


def test_health_all_keyword_checks_every_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(labels=[("netease", "NetEase"), ("qq", "QQ")])

    _run(["music", "health", "--platform", "ALL"], monkeypatch, engine)

    assert engine.health_calls == [("netease", "test"), ("qq", "test")]


def test_health_json_matches_documented_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(
        labels=[("netease", "NetEase"), ("qq", "QQ")],
        health_results={
            "netease": {"ok": True, "elapsed_ms": 12, "error": ""},
            "qq": {"ok": False, "elapsed_ms": 34, "error": "timeout"},
        },
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health", "--json"], monkeypatch, engine)

    assert excinfo.value.code == 1
    stdout = capsys.readouterr().out
    # Single machine-readable document; the human table stays off stdout.
    assert stdout.count("\n") == 1
    assert "Music platform health" not in stdout
    payload = json.loads(stdout)
    assert payload["keyword"] == "test"
    assert payload["failed"] == 1
    assert payload["platforms"] == [
        {"platform": "netease", "ok": True, "elapsed_ms": 12, "error": ""},
        {"platform": "qq", "ok": False, "elapsed_ms": 34, "error": "timeout"},
    ]


def test_health_json_success_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(labels=[("netease", "NetEase")])

    _run(
        ["music", "health", "--platform", "Netease", "--keyword", "probe", "--json"],
        monkeypatch,
        engine,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["platforms"][0]["platform"] == "netease"


def test_health_check_exception_is_reported_not_crashing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class RaisingEngine(FakeEngine):
        def health_check(self, key: str, keyword: str) -> dict[str, Any]:
            raise RuntimeError("probe exploded")

    engine = RaisingEngine(labels=[("netease", "NetEase"), ("qq", "QQ")])

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "health", "--json"], monkeypatch, engine)

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert [item["ok"] for item in payload["platforms"]] == [False, False]
    assert "probe exploded" in payload["platforms"][0]["error"]


# --------------------------------------------------------------------------- #
# engine creation failures
# --------------------------------------------------------------------------- #


def test_create_provider_generic_failure_exits_one(monkeypatch, capsys) -> None:
    class _BrokenCreate(FakeEngine):
        def create(self, platform: str) -> Any:
            raise RuntimeError("provider exploded")

    engine = _BrokenCreate()

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "--platform", "netease", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert "Could not start this platform" in capsys.readouterr().err


def test_require_engine_exits_with_install_hint(monkeypatch, capsys) -> None:
    def _raise() -> None:
        raise music._MusicUnavailable("mutagen not installed")

    monkeypatch.setattr(music, "_load_engine", _raise)

    with pytest.raises(SystemExit) as excinfo:
        args = _parser().parse_args(["music", "platforms"])
        args.func(args)

    assert excinfo.value.code == 1
    assert "novel-downloader[music]" in capsys.readouterr().err


def test_platforms_json_goes_through_ui_emit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``platforms --json`` must use ``ui.emit``, not a bare ``print``.

    Labels are localized (``国内 · 网易云音乐``); ``ui.emit`` is the helper that
    falls back to UTF-8 bytes when a redirected Windows stdout uses a legacy
    code page. A raw ``print`` raises ``UnicodeEncodeError`` there and would
    kill the command mid-document.
    """
    engine = FakeEngine(labels=[("netease", "国内 · 网易云音乐")])
    emitted: list[str] = []
    monkeypatch.setattr(music.ui, "emit", emitted.append)

    _run(["music", "platforms", "--json"], monkeypatch, engine)

    assert json.loads(emitted[0]) == {"netease": "国内 · 网易云音乐"}
    # Nothing bypassed the emit path onto stdout.
    assert capsys.readouterr().out == ""


def test_search_empty_keyword_is_usage_error(monkeypatch, capsys) -> None:
    engine = FakeEngine()

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "--platform", "netease", "   "], monkeypatch, engine)

    assert excinfo.value.code == 2
    assert "No keyword" in capsys.readouterr().err


def test_download_with_all_platforms_uses_mixed_downloader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--platform all`` downloads each track on its own provider."""
    track = _track(1)
    engine = FakeEngine()
    engine.download_result = (1, 0, 0, [], [])

    monkeypatch.setattr(
        music, "_search_all", lambda keyword, limit, *, show_status: ([track], 1, [])
    )

    _run(["music", "download", "hello", "--yes"], monkeypatch, engine)

    assert engine.download_calls == []
    assert len(engine.mixed_calls) == 1
    assert engine.mixed_calls[0]["tracks"] == [track]


# --------------------------------------------------------------------------- #
# aggregate search failure visibility / exit codes
# --------------------------------------------------------------------------- #


def _outcome(platform: str, *, ok: bool, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        platform=platform,
        ok=ok,
        count=0,
        error=error,
        as_dict=lambda: {
            "platform": platform,
            "ok": ok,
            "count": 0,
            "error": error,
        },
    )


def test_search_json_all_platforms_failed_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine()
    failed = [
        _outcome("netease", ok=False, error="timeout"),
        _outcome("qq", ok=False, error="timeout"),
    ]
    monkeypatch.setattr(
        music, "_search_all", lambda keyword, limit, *, show_status: ([], 0, failed)
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "hello", "--json"], monkeypatch, engine)

    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["tracks"] == []
    assert [item["ok"] for item in payload["platforms"]] == [False, False]


def test_search_all_platforms_failed_reports_each_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine()
    failed = [_outcome("netease", ok=False, error="timeout")]
    monkeypatch.setattr(
        music, "_search_all", lambda keyword, limit, *, show_status: ([], 0, failed)
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    # The per-platform reason must survive even though the merged list is
    # empty (previously the early "no results" return swallowed it).
    assert "Platform netease failed: timeout" in stderr


def test_search_json_empty_results_with_healthy_platforms_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine()
    healthy = [_outcome("netease", ok=True)]
    monkeypatch.setattr(
        music, "_search_all", lambda keyword, limit, *, show_status: ([], 0, healthy)
    )

    _run(["music", "search", "hello", "--json"], monkeypatch, engine)

    payload = json.loads(capsys.readouterr().out)
    assert payload["tracks"] == []
    assert payload["platforms"][0]["ok"] is True


def test_download_all_platforms_failed_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine()
    failed = [_outcome("netease", ok=False, error="timeout")]
    monkeypatch.setattr(
        music, "_search_all", lambda keyword, limit, *, show_status: ([], 0, failed)
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "download", "hello", "--yes"], monkeypatch, engine)

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "Platform netease failed: timeout" in stderr
    assert engine.mixed_calls == []


# --------------------------------------------------------------------------- #
# paths and encoding
# --------------------------------------------------------------------------- #


def test_export_m3u_resolves_fresh_search_result_to_downloaded_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "Artist" / "Album"
    folder.mkdir(parents=True)
    audio = folder / "Song 1 - Artist.mp3"
    audio.write_bytes(b"x")

    engine = FakeEngine(
        provider=FakeProvider([_track(1)]), settings={"out_dir": str(tmp_path)}
    )

    _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    ready, temp_path = engine.export_calls[0]
    playlist = os.path.join(str(tmp_path), "hello.m3u8")
    assert [track.path for track in ready] == [str(audio)]
    assert os.path.dirname(temp_path) == str(tmp_path)
    assert os.path.isfile(playlist)
    assert "Exported 1 track(s)" in capsys.readouterr().err


def test_export_m3u_non_ascii_dot_m3u_gets_a_utf8_bom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """``--out x.m3u`` must keep the BOM the engine emits for non-ASCII text.

    The command writes through a sibling temp file; if that file had a
    generic ``.tmp`` suffix the exporter would see an unknown format and
    write plain UTF-8, leaving Windows players to guess the code page.
    """
    target = tmp_path / "custom.m3u"
    track = _track(1, path=str(tmp_path / "a.mp3"), name="晴天", artists="周杰伦")
    engine = FakeEngine(
        provider=FakeProvider([track]),
        export_writer=real_export_m3u,
    )

    _run(
        ["music", "export-m3u", "-k", "hello", "--out", str(target)],
        monkeypatch,
        engine,
    )

    temp_path = engine.export_calls[0][1]
    assert os.path.splitext(temp_path)[1] == ".m3u"
    raw = target.read_bytes()
    assert raw.startswith(BOM + b"#EXTM3U\n")
    text = raw.decode("utf-8-sig")
    assert "#EXTINF:185,周杰伦 - 晴天" in text
    # Only the final playlist remains.
    assert [entry.name for entry in tmp_path.iterdir()] == ["custom.m3u"]


def test_export_m3u_non_ascii_dot_m3u8_has_no_bom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    target = tmp_path / "custom.m3u8"
    track = _track(1, path=str(tmp_path / "a.mp3"), name="晴天", artists="周杰伦")
    engine = FakeEngine(
        provider=FakeProvider([track]),
        export_writer=real_export_m3u,
    )

    _run(
        ["music", "export-m3u", "-k", "hello", "--out", str(target)],
        monkeypatch,
        engine,
    )

    temp_path = engine.export_calls[0][1]
    assert os.path.splitext(temp_path)[1] == ".m3u8"
    raw = target.read_bytes()
    # ``.m3u8`` declares UTF-8: a BOM must not precede the header.
    assert raw.startswith(b"#EXTM3U\n")
    assert not raw.startswith(BOM)
    assert "周杰伦 - 晴天" in raw.decode("utf-8")


def test_export_m3u_ascii_dot_m3u_has_no_bom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    target = tmp_path / "custom.m3u"
    track = _track(1, path=str(tmp_path / "a.mp3"))
    engine = FakeEngine(
        provider=FakeProvider([track]),
        export_writer=real_export_m3u,
    )

    _run(
        ["music", "export-m3u", "-k", "hello", "--out", str(target)],
        monkeypatch,
        engine,
    )

    raw = target.read_bytes()
    assert raw.startswith(b"#EXTM3U\n")
    assert not raw.startswith(BOM)


def test_download_expands_tilde_in_out_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine(provider=FakeProvider([_track(1)]))

    _run(
        [
            "music",
            "download",
            "--platform",
            "netease",
            "hello",
            "--pick",
            "1",
            "--out",
            "~/MusicND",
        ],
        monkeypatch,
        engine,
    )

    options = engine.download_calls[0]["options"]
    assert options.out_dir == os.path.expanduser("~/MusicND")
    assert options.out_dir != "~/MusicND"


class _LegacyStdout:
    """Stdout that behaves like a cp936 pipe and exposes a byte buffer."""

    encoding = "gbk"

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        raise UnicodeEncodeError("gbk", text, 0, max(1, len(text)), "illegal")

    def flush(self) -> None:
        pass


def test_emit_falls_back_to_utf8_bytes_on_legacy_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.cli import ui

    stream = _LegacyStdout()
    monkeypatch.setattr(ui.sys, "stdout", stream)

    ui.emit('{"name": "🔥"}')

    assert stream.buffer.getvalue().decode("utf-8") == '{"name": "🔥"}\n'
