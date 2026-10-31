#!/usr/bin/env python3
"""Offline unit tests for :mod:`novel_downloader.apps.music.providers.netease`.

The provider is exercised with a stubbed client, so no WEAPI traffic or
cookie file is involved.  Focus: level fallback ladder, resolve() failure
reasons, and the raw-song -> Track conversion.

The target module is deliberately untyped (see the ``[tool.mypy.overrides]``
relaxation in ``pyproject.toml``), so calls into it skip that check here too.
"""

# mypy: disallow-untyped-calls=False

from __future__ import annotations

from typing import Any

import pytest

from novel_downloader.apps.music.providers import netease as netease_mod
from novel_downloader.apps.music.providers.base import ResolveResult, Track
from novel_downloader.apps.music.providers.netease import (
    NeteaseProvider,
    _ext_from,
    _levels_to_try,
)

# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("want", "expected"),
    [
        ("standard", ["standard"]),
        ("exhigh", ["exhigh", "standard"]),
        ("hires", ["hires", "standard"]),
        ("bogus", ["bogus"]),
    ],
)
def test_levels_to_try(want: str, expected: list[str]) -> None:
    assert _levels_to_try(want) == expected


@pytest.mark.parametrize(
    ("res", "expected"),
    [
        ({"type": "FLAC"}, "flac"),
        ({"type": "mp3"}, "mp3"),
        ({"type": "weird", "url": "http://x/song.M4A?from=x"}, "m4a"),
        ({"type": "weird", "url": "http://x/a.flac"}, "flac"),
        ({"type": "", "url": ""}, "mp3"),
        ({"url": "http://x/no-extension"}, "mp3"),
    ],
)
def test_ext_from(res: dict[str, Any], expected: str) -> None:
    assert _ext_from(res) == expected


# --------------------------------------------------------------------------- #
# provider with stub client
# --------------------------------------------------------------------------- #


class FakeClient:
    key = "netease"

    def __init__(self) -> None:
        self.clear_calls = 0
        self.song_url_results: list[list[dict[str, Any]]] = []
        self.song_url_calls: list[tuple[list[Any], str]] = []
        self.lyric_result: tuple[str, str] = ("[00:01]lrc", "译")
        self.lyric_raises = False
        self.search_result: tuple[list[dict[str, Any]], int] = ([], 0)
        self.playlists: list[dict[str, Any]] = []
        self.tracks: list[dict[str, Any]] = []

    @property
    def logged_in(self) -> bool:
        return True

    def song_urls(self, ids: list[Any], level: str = "exhigh") -> list[dict[str, Any]]:
        self.song_url_calls.append((list(ids), level))
        if self.song_url_results:
            return self.song_url_results.pop(0)
        return []

    def clear_cookies(self) -> None:
        self.clear_calls += 1

    def lyric(self, sid: str) -> tuple[str, str]:
        if self.lyric_raises:
            raise RuntimeError("boom")
        return self.lyric_result

    def search(
        self, keyword: str, limit: int = 30, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        return self.search_result

    def user_playlists(self, uid: int) -> list[dict[str, Any]]:
        return self.playlists

    def playlist_tracks(self, pid: int) -> list[dict[str, Any]]:
        return self.tracks


def _provider(client: FakeClient) -> NeteaseProvider:
    return NeteaseProvider(client=client)


def _song(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 111,
        "name": "Song A",
        "ar": [{"name": "Artist"}],
        "al": {"name": "Album", "picUrl": "http://cover"},
        "dt": 200000,
        "no": 2,
        "fee": 0,
        "tid": "999",
    }
    base.update(overrides)
    return base


def test_to_track_mapping() -> None:
    client = FakeClient()
    provider = _provider(client)
    track = provider._to_track(_song())
    assert track.provider == "netease"
    assert track.id == "111"
    assert track.name == "Song A"
    assert track.artists == "Artist"
    assert track.album == "Album"
    assert track.duration_s == 200
    assert track.cover_url == "http://cover"
    assert track.track_no == 2
    assert track.vip is False
    assert track.extra["tid"] == "999"
    assert track.extra["raw"]["id"] == 111


def test_to_track_marks_vip() -> None:
    provider = _provider(FakeClient())
    assert provider._to_track(_song(fee=1)).vip is True


def test_search_returns_tracks_and_total() -> None:
    client = FakeClient()
    client.search_result = ([_song(), _song(id=222)], 500)
    provider = _provider(client)
    tracks, total = provider.search("keyword")
    assert total == 500
    assert [t.id for t in tracks] == ["111", "222"]


def test_playlist_tracks_conversion() -> None:
    client = FakeClient()
    client.tracks = [_song()]
    provider = _provider(client)
    tracks = provider.playlist_tracks(7)
    assert len(tracks) == 1
    assert tracks[0].id == "111"


def test_user_playlists_passthrough() -> None:
    client = FakeClient()
    client.playlists = [{"name": "fav"}]
    provider = _provider(client)
    assert provider.user_playlists(1) == [{"name": "fav"}]


def test_account_and_cookie_passthrough() -> None:
    client = FakeClient()
    provider = _provider(client)

    assert provider.logged_in is True
    assert provider.is_logged_in() is True

    provider.clear_cookies()
    assert client.clear_calls == 1


def test_lyric_passthrough() -> None:
    provider = _provider(FakeClient())
    track = Track(provider="netease", id="111")
    assert provider.lyric(track) == ("[00:01]lrc", "译")


def test_lyric_swallows_client_errors() -> None:
    client = FakeClient()
    client.lyric_raises = True
    provider = _provider(client)
    track = Track(provider="netease", id="111")
    assert provider.lyric(track) == ("", "")


# --------------------------------------------------------------------------- #
# resolve()
# --------------------------------------------------------------------------- #


def _track_with_raw(**raw: Any) -> Track:
    return Track(provider="netease", id="111", extra={"raw": raw, "tid": "999"})


def test_resolve_success_on_first_level() -> None:
    client = FakeClient()
    client.song_url_results = [
        [{"url": "http://audio/1.flac", "type": "flac", "br": 999}]
    ]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=0), level="hires")

    assert isinstance(result, ResolveResult)
    assert result.ok is True
    assert result.url == "http://audio/1.flac"
    assert result.ext == "flac"
    assert result.level == "hires"
    assert result.http_headers == {"Referer": "https://music.163.com/"}
    assert client.song_url_calls == [(["999"], "hires")]


def test_resolve_falls_back_to_standard() -> None:
    client = FakeClient()
    client.song_url_results = [
        [{"url": None, "freeTrialInfo": None}],  # hires: no url
        [{"url": "http://audio/1.mp3", "type": "mp3"}],  # standard: hit
    ]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=0), level="hires")
    assert result.ok is True
    assert result.level == "standard"
    assert [lv for _, lv in client.song_url_calls] == ["hires", "standard"]


def test_resolve_reports_vip_trial() -> None:
    client = FakeClient()
    client.song_url_results = [[{"url": None, "freeTrialInfo": [{"t": 1}]}]]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=1), level="lossless")
    assert result.ok is False
    assert "试听" in result.reason


def test_resolve_reports_vip_fee() -> None:
    client = FakeClient()
    client.song_url_results = [[{}]]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=1), level="exhigh")
    assert result.ok is False
    assert result.reason == "VIP 专属，当前账号无权限"


def test_resolve_reports_purchasable_album() -> None:
    client = FakeClient()
    client.song_url_results = [[{}]]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=4), level="exhigh")
    assert result.reason == "需购买专辑"


def test_resolve_reports_unavailable_when_no_reason_matches() -> None:
    client = FakeClient()
    client.song_url_results = [[{}]]
    provider = _provider(client)
    result = provider.resolve(_track_with_raw(fee=8), level="exhigh")
    assert result.reason == "无版权或已下架"


def test_resolve_uses_track_id_when_no_tid() -> None:
    client = FakeClient()
    client.song_url_results = [[{"url": "http://a/1.mp3", "type": "mp3"}]]
    provider = _provider(client)
    track = Track(provider="netease", id="42", extra={})
    result = provider.resolve(track, level="standard")
    assert result.ok is True
    # standard is not retried on a second level
    assert client.song_url_calls == [(["42"], "standard")]


def test_resolve_defaults_to_exhigh_level() -> None:
    client = FakeClient()
    client.song_url_results = [[{"url": "http://a/1.mp3", "type": "mp3"}]]
    provider = _provider(client)
    track = Track(provider="netease", id="42", extra={})
    provider.resolve(track)
    assert client.song_url_calls == [(["42"], "exhigh")]


# --------------------------------------------------------------------------- #
# module import hygiene
# --------------------------------------------------------------------------- #


def test_provider_metadata() -> None:
    assert NeteaseProvider.key == "netease"
    assert NeteaseProvider.requires_login is True
    assert NeteaseProvider.supports_playlists is True
    assert "NeteaseProvider" in dir(netease_mod)
