#!/usr/bin/env python3
"""
Contract tests for ``music download --json``.

The documented contract (``docs/guide/cli-examples.md`` and the exit-code
table) is:

* a fully successful batch, an all-skipped batch and an empty search exit
  ``0``;
* a batch with at least one failed song exits ``1`` *after* printing the
  complete single-line JSON report;
* ``total`` is the number of selected songs, and ``ok``/``skip``/``fail``
  partition it;
* ``results[].status`` is exactly ``ok`` / ``skip`` / ``fail``;
* ``path`` is present only for ``ok``/``skip`` results, ``reason`` only for
  failures that carry one, and ``lyric_path`` only for a real sidecar;
* ``lyrics_written`` counts sidecar ``.lrc`` files only, never embedded tags;
* the top-level ``embed_lyric`` is the effective switch for this run.

These tests drive the real command function with an offline engine fake; no
music dependency or network access is involved.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from novel_downloader.apps.cli.commands import music
from novel_downloader.apps.cli.commands.music import MusicCmd

Status = Literal["ok", "skip", "fail"]


@dataclass(frozen=True)
class Outcome:
    """One planned per-track result reported by the fake engine."""

    status: Status
    reason: str = ""
    sidecar: bool = False
    audio: bool = True


def _track(index: int, *, path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id{index}",
        name=f"Song {index}",
        artists="Artist",
        album="Album",
        duration_s=180 + index,
        vip=False,
        provider="netease",
        sources=["netease"],
        path=path,
    )


class FakeProvider:
    def __init__(self, tracks: list[Any]) -> None:
        self.tracks = list(tracks)

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        return self.tracks[:limit], len(self.tracks)

    def is_logged_in(self) -> bool:
        return True


class FakeEngine:
    """Offline engine fake that writes real audio/``.lrc`` files."""

    login_expired = RuntimeError

    def __init__(
        self,
        *,
        tracks: list[Any],
        outcomes: list[Outcome],
        out_dir: Path,
        settings: dict[str, Any] | None = None,
    ) -> None:
        assert len(tracks) == len(outcomes)
        self.tracks = list(tracks)
        self.outcomes = list(outcomes)
        self.out_dir = out_dir
        self._settings = dict(settings or {})
        self.settings = SimpleNamespace(load_settings=lambda: dict(self._settings))
        self.download_options_calls: list[dict[str, Any]] = []

    # -- registry surface ---------------------------------------------------
    def labels(self) -> list[tuple[str, str]]:
        return [("netease", "NetEase")]

    def platform_keys(self) -> list[str]:
        return ["netease"]

    def platform_note(self, _platform: str) -> str | None:
        return None

    def create(self, _platform: str) -> FakeProvider:
        return FakeProvider(self.tracks)

    # -- download surface ---------------------------------------------------
    def default_download_dir(self) -> str:
        return str(self.out_dir)

    def download_options(self, **kwargs: Any) -> SimpleNamespace:
        self.download_options_calls.append(dict(kwargs))
        return SimpleNamespace(**kwargs)

    def _download(self, tracks: list[Any], kwargs: dict[str, Any]) -> tuple[Any, ...]:
        on_result = kwargs.get("on_result")
        on_tick = kwargs.get("on_tick")
        self.out_dir.mkdir(parents=True, exist_ok=True)

        ok = skip = fail = 0
        failed_pairs: list[tuple[str, str]] = []
        failed_tracks: list[Any] = []
        pairs = zip(tracks, self.outcomes, strict=True)
        for index, (track, outcome) in enumerate(pairs):
            audio = self.out_dir / f"{track.name} - {track.artists}.mp3"
            lyric = audio.with_suffix(".lrc")
            if outcome.audio:
                audio.write_bytes(b"AUDIO")
            if outcome.sidecar:
                lyric.write_text("[00:00.00]la\n", encoding="utf-8")

            if outcome.status == "ok":
                ok += 1
                path = str(audio)
            elif outcome.status == "skip":
                skip += 1
                path = str(audio)
                audio.write_bytes(b"AUDIO")
            else:
                fail += 1
                path = ""
                failed_pairs.append((f"{track.name} - {track.artists}", outcome.reason))
                failed_tracks.append(track)

            if on_result is not None:
                on_result(
                    index,
                    track,
                    SimpleNamespace(
                        status=outcome.status,
                        path=path,
                        reason=outcome.reason,
                    ),
                )
            if on_tick is not None:
                on_tick((index + 1) / len(tracks), index + 1)

        return ok, skip, fail, failed_pairs, failed_tracks

    def download_many(
        self, _provider: Any, tracks: list[Any], _options: Any, **kwargs: Any
    ) -> tuple[Any, ...]:
        return self._download(list(tracks), kwargs)

    def download_mixed(
        self, _create_provider: Any, tracks: list[Any], _options: Any, **kwargs: Any
    ) -> tuple[Any, ...]:
        return self._download(list(tracks), kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    MusicCmd.register(subparsers)
    return parser


def _run(
    monkeypatch: pytest.MonkeyPatch,
    engine: FakeEngine,
    out_dir: Path,
    *extra: str,
    platform: str = "netease",
) -> int:
    """Execute ``music download --json`` and return its exit code."""
    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    argv = [
        "music",
        "download",
        "hello",
        "--platform",
        platform,
        "--yes",
        "--json",
        "--out",
        str(out_dir),
        *extra,
    ]
    args = _parser().parse_args(argv)
    try:
        args.func(args)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    return 0


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    # Structured output is a single line on stdout.
    assert captured.out.count("\n") == 1, captured.out
    assert captured.out.endswith("\n")
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def _statuses(payload: dict[str, Any]) -> list[str]:
    return [str(entry["status"]) for entry in payload["results"]]


def _assert_partition(payload: dict[str, Any]) -> None:
    """``total`` is exactly the ok/skip/fail partition of the results."""
    total = payload["total"]
    assert payload["ok"] + payload["skip"] + payload["fail"] == total
    assert len(payload["results"]) == total
    assert _statuses(payload).count("ok") == payload["ok"]
    assert _statuses(payload).count("skip") == payload["skip"]
    assert _statuses(payload).count("fail") == payload["fail"]


BASE_SETTINGS: dict[str, Any] = {"level": "exhigh", "workers": 1}


def test_all_success_exits_zero_and_ok_equals_total(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracks = [_track(1), _track(2), _track(3)]
    outcomes = [
        Outcome("ok", sidecar=True),
        Outcome("ok"),
        Outcome("ok"),
    ]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path)

    assert code == 0
    payload = _payload(capsys)
    assert payload["total"] == 3
    assert payload["ok"] == 3
    assert payload["skip"] == 0
    assert payload["fail"] == 0
    assert payload["embed_lyric"] is False
    assert payload["lyrics_written"] == 1
    assert _statuses(payload) == ["ok", "ok", "ok"]
    _assert_partition(payload)
    for entry in payload["results"]:
        assert entry["path"]
        assert "reason" not in entry
    assert "lyric_path" in payload["results"][0]
    assert "lyric_path" not in payload["results"][1]


def test_all_skipped_exits_zero_with_skip_equal_total(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracks = [_track(1), _track(2)]
    outcomes = [Outcome("skip"), Outcome("skip")]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path)

    assert code == 0
    payload = _payload(capsys)
    assert payload["total"] == 2
    assert payload["skip"] == 2
    assert payload["ok"] == 0
    assert payload["fail"] == 0
    assert payload["lyrics_written"] == 0
    assert _statuses(payload) == ["skip", "skip"]
    _assert_partition(payload)
    for entry in payload["results"]:
        assert entry["path"]
        assert "reason" not in entry


def test_partial_failure_prints_json_then_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracks = [_track(1), _track(2), _track(3)]
    outcomes = [
        Outcome("ok"),
        Outcome("ok"),
        Outcome("fail", reason="vip only"),
    ]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path)

    # Data first: the complete document is on stdout even though the run failed.
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["total"] == 3
    assert payload["ok"] == 2
    assert payload["skip"] == 0
    assert payload["fail"] == 1
    assert _statuses(payload) == ["ok", "ok", "fail"]
    _assert_partition(payload)

    failed = payload["results"][2]
    assert failed["status"] == "fail"
    assert failed["reason"] == "vip only"
    assert "path" not in failed
    assert "lyric_path" not in failed
    assert "reason" not in payload["results"][0]
    assert "Failed: Song 3 - Artist (vip only)" in captured.err


def test_mixed_platform_partial_failure_keeps_the_same_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--platform all`` (the default) uses download_mixed but the same rules."""
    tracks = [_track(1), _track(2), _track(3)]
    outcomes = [Outcome("ok"), Outcome("skip"), Outcome("fail", reason="no copyright")]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )
    monkeypatch.setattr(
        music,
        "_search_all",
        lambda keyword, limit, *, show_status: (tracks, len(tracks), []),
    )

    code = _run(monkeypatch, engine, tmp_path, platform="all")

    assert code == 1
    payload = _payload(capsys)
    assert payload["platform"] == "all"
    assert payload["total"] == 3
    assert payload["ok"] == 1
    assert payload["skip"] == 1
    assert payload["fail"] == 1
    _assert_partition(payload)
    assert payload["results"][2]["reason"] == "no copyright"
    assert "path" not in payload["results"][2]


def test_all_failures_exit_one_with_zero_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracks = [_track(1), _track(2), _track(3)]
    outcomes = [
        Outcome("fail", reason="no copyright"),
        Outcome("fail", reason="login required"),
        Outcome("fail", reason="timeout"),
    ]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path)

    assert code == 1
    payload = _payload(capsys)
    assert payload["total"] == 3
    assert payload["ok"] == 0
    assert payload["skip"] == 0
    assert payload["fail"] == 3
    assert payload["lyrics_written"] == 0
    assert _statuses(payload) == ["fail", "fail", "fail"]
    _assert_partition(payload)
    assert [entry["reason"] for entry in payload["results"]] == [
        "no copyright",
        "login required",
        "timeout",
    ]
    for entry in payload["results"]:
        assert "path" not in entry
        assert "lyric_path" not in entry


def test_empty_search_exits_zero_with_total_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(
        tracks=[], outcomes=[], out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path)

    assert code == 0
    payload = _payload(capsys)
    assert payload["total"] == 0
    assert payload["ok"] == 0
    assert payload["skip"] == 0
    assert payload["fail"] == 0
    assert payload["lyrics_written"] == 0
    assert payload["results"] == []


def test_lyrics_written_counts_sidecars_not_embedded_lyrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a real ``.lrc`` counts; embedded-only lyrics do not."""
    tracks = [_track(1), _track(2), _track(3)]
    outcomes = [
        Outcome("ok", sidecar=True),  # standalone .lrc exists
        Outcome("ok"),  # lyrics embedded in the tag only (no .lrc)
        Outcome("ok"),
    ]
    engine = FakeEngine(
        tracks=tracks, outcomes=outcomes, out_dir=tmp_path, settings=BASE_SETTINGS
    )

    code = _run(monkeypatch, engine, tmp_path, "--embed-lyric")

    assert code == 0
    payload = _payload(capsys)
    assert payload["embed_lyric"] is True
    assert payload["lyrics_written"] == 1
    assert sum("lyric_path" in entry for entry in payload["results"]) == 1
    assert payload["results"][0]["lyric_path"].endswith("Song 1 - Artist.lrc")
    assert "lyric_path" not in payload["results"][1]
    assert "lyric_path" not in payload["results"][2]


@pytest.mark.parametrize(
    ("settings", "flag", "expected"),
    [
        ({"embed_lyric": False}, None, False),
        ({"embed_lyric": True}, None, True),
        ({"embed_lyric": True}, "--no-embed-lyric", False),
        ({"embed_lyric": False}, "--embed-lyric", True),
        ({"embed_lyric": "false"}, None, False),
    ],
)
def test_embed_lyric_top_level_reports_the_effective_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, Any],
    flag: str | None,
    expected: bool,
) -> None:
    engine = FakeEngine(
        tracks=[_track(1)],
        outcomes=[Outcome("ok")],
        out_dir=tmp_path,
        settings={**BASE_SETTINGS, **settings},
    )

    extra = () if flag is None else (flag,)
    code = _run(monkeypatch, engine, tmp_path, *extra)

    assert code == 0
    payload = _payload(capsys)
    assert payload["embed_lyric"] is expected
    assert engine.download_options_calls[0]["embed_lyric"] is expected


def test_failed_result_keeps_reason_but_not_stale_path_or_lyric(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale local file from an earlier run is never reported as this one."""
    stale_audio = tmp_path / "Song 1 - Artist.mp3"
    stale_audio.write_bytes(b"AUDIO")
    stale_audio.with_suffix(".lrc").write_text("[00:00.00]old\n", encoding="utf-8")
    track = _track(1, path=str(stale_audio))
    engine = FakeEngine(
        tracks=[track],
        outcomes=[Outcome("fail", reason="vip only", audio=False)],
        out_dir=tmp_path,
        settings=BASE_SETTINGS,
    )

    code = _run(monkeypatch, engine, tmp_path)

    assert code == 1
    payload = _payload(capsys)
    assert payload["total"] == 1
    assert payload["ok"] == 0
    assert payload["fail"] == 1
    assert payload["lyrics_written"] == 0
    entry = payload["results"][0]
    assert entry["status"] == "fail"
    assert entry["reason"] == "vip only"
    assert "path" not in entry
    assert "lyric_path" not in entry
