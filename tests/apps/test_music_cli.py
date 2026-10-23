#!/usr/bin/env python3
"""
tests.apps.test_music_cli
-------------------------

Offline tests for the optional music CLI subcommands.

The optional music engine (and its third-party dependencies) is replaced with
small fakes, so these tests never import ``mutagen``/``qrcode``/``websocket``
and never perform network requests.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import music
from novel_downloader.apps.cli.commands.music import MusicCmd


class FakeProvider:
    """Provider stub returning a fixed list of tracks."""

    def __init__(self, tracks: list[Any], total: int | None = None) -> None:
        self.tracks = tracks
        self.total = total
        self.searches: list[tuple[str, int]] = []

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        self.searches.append((keyword, limit))
        total = self.total if self.total is not None else len(self.tracks)
        return list(self.tracks), total

    def is_logged_in(self) -> bool:
        return True


class FakeEngine:
    """Minimal stand-in for ``music._MusicEngine``."""

    login_expired = RuntimeError

    def __init__(
        self,
        *,
        provider: FakeProvider | None = None,
        labels: list[tuple[str, str]] | None = None,
        note: str | None = None,
        create_error: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self._labels = labels or []
        self._note = note
        self._create_error = create_error
        self.created: list[str] = []

    def labels(self) -> list[tuple[str, str]]:
        return list(self._labels)

    def platform_keys(self) -> list[str]:
        return sorted(key for key, _label in self._labels)

    def platform_note(self, platform: str) -> str | None:
        return self._note

    def create(self, platform: str) -> FakeProvider:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(platform)
        if self.provider is None:
            raise AssertionError("test engine has no provider configured")
        return self.provider


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
    """Parse ``argv`` and execute the selected command with a fake engine."""
    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    args = _parser().parse_args(argv)
    args.func(args)


def _track(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id{index}",
        name=f"Song {index}",
        artists="Artist",
        album="Album",
        duration_s=185,
        vip=index % 2 == 0,
        provider="netease",
    )


# ---------------------------------------------------------------------------
# Help output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["music", "--help"], ["platforms", "search", "download"]),
        (["music", "platforms", "--help"], ["platforms"]),
        (
            ["music", "search", "--help"],
            ["keyword", "--platform", "--limit", "--json"],
        ),
        (
            ["music", "download", "--help"],
            ["keyword", "--platform", "--limit", "--level", "--out", "--pick"],
        ),
    ],
)
def test_music_help_exits_zero_and_lists_options(
    argv: list[str],
    expected: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parser().parse_args(argv)

    assert excinfo.value.code == 0
    stdout = capsys.readouterr().out
    for token in expected:
        assert token in stdout


# ---------------------------------------------------------------------------
# search --json
# ---------------------------------------------------------------------------


def test_search_json_prints_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider([_track(1), _track(2)], total=40)
    engine = FakeEngine(provider=provider, labels=[("netease", "NetEase")])

    _run(
        ["music", "search", "hello", "--json", "--limit", "2"],
        monkeypatch,
        engine,
    )

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert payload["platform"] == "netease"
    assert payload["keyword"] == "hello"
    assert payload["total"] == 40
    assert [track["index"] for track in payload["tracks"]] == [1, 2]

    first = payload["tracks"][0]
    assert set(first) == {
        "index",
        "id",
        "name",
        "artists",
        "album",
        "duration_s",
        "vip",
        "platform",
    }
    assert first == {
        "index": 1,
        "id": "id1",
        "name": "Song 1",
        "artists": "Artist",
        "album": "Album",
        "duration_s": 185,
        "vip": False,
        "platform": "netease",
    }

    # The provider was queried once with the normalized arguments.
    assert provider.searches == [("hello", 2)]
    assert engine.created == ["netease"]


def test_search_json_empty_result_is_still_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(provider=FakeProvider([], total=0))

    _run(["music", "search", "nothing", "--json"], monkeypatch, engine)

    payload = json.loads(capsys.readouterr().out)
    assert payload["tracks"] == []
    assert payload["total"] == 0


# ---------------------------------------------------------------------------
# platforms
# ---------------------------------------------------------------------------


def test_platforms_lists_registered_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(
        labels=[("netease", "NetEase"), ("qq", "QQ Music")],
        note="login required",
    )

    _run(["music", "platforms"], monkeypatch, engine)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "netease" in output
    assert "NetEase" in output
    assert "qq" in output
    assert "QQ Music" in output


def test_platforms_with_no_entries_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["music", "platforms"], monkeypatch, FakeEngine(labels=[]))

    assert "No music platforms" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_platform_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = FakeEngine(
        labels=[("netease", "NetEase")],
        create_error=KeyError("unknown-platform"),
    )

    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    args = _parser().parse_args(
        ["music", "search", "hello", "--platform", "unknown-platform"]
    )

    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "unknown-platform" in stderr
    assert "netease" in stderr
