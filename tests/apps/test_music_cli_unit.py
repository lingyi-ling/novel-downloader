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
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import music
from novel_downloader.apps.cli.commands.music import MusicCmd


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
        self.health_results = health_results or {}
        self.created: list[str] = []
        self.download_calls: list[dict[str, Any]] = []
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

    def export_m3u(self, tracks: list[Any], out_path: str) -> str:
        if self.export_error is not None:
            raise self.export_error
        self.export_calls.append((list(tracks), out_path))
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


def _track(index: int, *, path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id{index}",
        name=f"Song {index}",
        artists="Artist",
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

    _run(["music", "download", "hello", "--pick", "1,2"], monkeypatch, engine)

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

    _run(["music", "download", "hello", "--pick", "1"], monkeypatch, engine)

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
        _run(["music", "download", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 2
    assert "--pick" in capsys.readouterr().err


def test_download_login_expired_shows_hint(monkeypatch, capsys) -> None:
    provider = ThrowingProvider(FakeLoginExpired("session gone"))
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "download", "hello", "--yes"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert "expired" in capsys.readouterr().err.lower()


def test_download_search_failure_exits_one(monkeypatch, capsys) -> None:
    provider = ThrowingProvider(RuntimeError("network down"))
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "download", "hello", "--yes"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert "Search failed" in capsys.readouterr().err


def test_download_no_results_returns_cleanly(monkeypatch, capsys) -> None:
    engine = FakeEngine(provider=FakeProvider([]))

    _run(["music", "download", "hello", "--yes"], monkeypatch, engine)

    assert engine.download_calls == []
    assert "No results found" in capsys.readouterr().err


def test_download_keyword_flag_matches_positional(monkeypatch, tmp_path) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(provider=provider, settings={"out_dir": str(tmp_path)})

    _run(["music", "download", "--keyword", "flag-kw", "--yes"], monkeypatch, engine)

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

    ready, out_path = engine.export_calls[0]
    import os

    assert out_path == os.path.join(str(tmp_path), "hello.m3u8")
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

    assert engine.export_calls[0][1] == str(target)


def test_export_m3u_without_local_paths_fails(monkeypatch, capsys) -> None:
    provider = FakeProvider([_track(1)])
    engine = FakeEngine(provider=provider)

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert engine.export_calls == []


def test_export_m3u_write_failure_exits(monkeypatch, capsys) -> None:
    provider = FakeProvider([_track(1, path="a.mp3")])
    engine = FakeEngine(provider=provider, export_error=OSError("disk full"))

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "export-m3u", "-k", "hello"], monkeypatch, engine)

    assert excinfo.value.code == 1
    assert "Could not write playlist" in capsys.readouterr().err


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


# --------------------------------------------------------------------------- #
# engine creation failures
# --------------------------------------------------------------------------- #


def test_create_provider_generic_failure_exits_one(monkeypatch, capsys) -> None:
    class _BrokenCreate(FakeEngine):
        def create(self, platform: str) -> Any:
            raise RuntimeError("provider exploded")

    engine = _BrokenCreate()

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "hello"], monkeypatch, engine)

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


def test_search_empty_keyword_is_usage_error(monkeypatch, capsys) -> None:
    engine = FakeEngine()

    with pytest.raises(SystemExit) as excinfo:
        _run(["music", "search", "   "], monkeypatch, engine)

    assert excinfo.value.code == 2
    assert "No keyword" in capsys.readouterr().err
