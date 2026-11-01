#!/usr/bin/env python3
"""Offline tests for the music provider abstraction layer.

``apps/music/providers/base.py`` defines the platform-agnostic contract every
provider builds on: the :class:`Track` / :class:`ResolveResult` value objects
and the :class:`MusicProvider` interface with its default (no-op or raising)
method bodies.  These tests pin down those defaults so a refactor cannot
silently change what subclasses inherit.
"""

from __future__ import annotations

import pytest

from novel_downloader.apps.music.providers.base import (
    MusicProvider,
    ResolveResult,
    Track,
)

# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------


def test_track_defaults_are_empty_and_platform_agnostic() -> None:
    track = Track()

    assert track.provider == ""
    assert track.id == ""
    assert track.name == "未知歌曲"
    assert track.artists == "未知歌手"
    assert track.album == "未知专辑"
    assert track.duration_s == 0
    assert track.cover_url == ""
    assert track.track_no == 0
    assert track.ext == ""
    assert track.vip is False
    assert track.download_url == ""
    assert track.http_headers == {}
    assert track.path == ""
    assert track.lyric == ""
    assert track.extra == {}


def test_track_display_joins_name_and_artists() -> None:
    track = Track(name="夜曲", artists="周杰伦")
    assert track.display() == "夜曲 - 周杰伦"


def test_track_display_uses_defaults_when_unfilled() -> None:
    assert Track().display() == "未知歌曲 - 未知歌手"


def test_track_mutable_defaults_not_shared_between_instances() -> None:
    a, b = Track(), Track()

    a.http_headers["Referer"] = "https://music.example.com"
    a.extra["raw"] = {"id": 1}

    assert b.http_headers == {}
    assert b.extra == {}


def test_track_resolve_fields_survive_round_trip() -> None:
    track = Track(
        provider="netease",
        id="42",
        name="N",
        artists="A",
        ext="flac",
        download_url="https://cdn.example.com/42.flac",
        http_headers={"User-Agent": "ua"},
    )
    track.path = "downloads/42.flac"
    track.lyric = "[00:00.00] line"

    assert track.ext == "flac"
    assert track.download_url.endswith(".flac")
    assert track.http_headers == {"User-Agent": "ua"}
    assert track.path == "downloads/42.flac"
    assert track.lyric == "[00:00.00] line"


# ---------------------------------------------------------------------------
# ResolveResult
# ---------------------------------------------------------------------------


def test_resolve_result_ok_true_when_url_present() -> None:
    result = ResolveResult(url="https://cdn.example.com/a.mp3")
    assert result.ok is True


def test_resolve_result_ok_false_when_url_empty() -> None:
    assert ResolveResult(url="").ok is False
    assert ResolveResult().ok is False


def test_resolve_result_defaults() -> None:
    result = ResolveResult()

    assert result.url == ""
    assert result.ext == ""
    assert result.reason == ""
    assert result.retryable is False
    assert result.level == ""
    assert result.http_headers == {}
    assert result.ok is False


def test_resolve_result_failure_fields() -> None:
    result = ResolveResult(reason="region locked", retryable=False, level="standard")

    assert result.ok is False
    assert result.reason == "region locked"
    assert result.retryable is False
    assert result.level == "standard"


# ---------------------------------------------------------------------------
# MusicProvider: interface stubs and inherited defaults
# ---------------------------------------------------------------------------


def test_music_provider_class_level_defaults() -> None:
    assert MusicProvider.key == ""
    assert MusicProvider.display_name == ""
    assert MusicProvider.requires_login is False
    assert MusicProvider.supports_playlists is False


def test_music_provider_search_is_abstract() -> None:
    provider = MusicProvider()

    with pytest.raises(NotImplementedError):
        provider.search("keyword")


def test_music_provider_resolve_is_abstract() -> None:
    provider = MusicProvider()

    with pytest.raises(NotImplementedError):
        provider.resolve(Track())


def test_music_provider_lyric_default_returns_empty_pair() -> None:
    assert MusicProvider().lyric(Track()) == ("", "")


def test_music_provider_is_logged_in_default_true() -> None:
    assert MusicProvider().is_logged_in() is True


def test_music_provider_account_default_is_none_pair() -> None:
    assert MusicProvider().account() == (None, None)


def test_music_provider_login_by_cookie_is_abstract() -> None:
    with pytest.raises(NotImplementedError):
        MusicProvider().login_by_cookie("MUSIC_U=xxx")


def test_music_provider_clear_cookies_default_is_noop() -> None:
    provider = MusicProvider()

    assert provider.clear_cookies() is None


def test_music_provider_cookie_names_default_empty() -> None:
    assert MusicProvider().cookie_names == []


def test_music_provider_playlist_defaults_empty() -> None:
    provider = MusicProvider()

    assert provider.user_playlists(uid="1") == []
    assert provider.playlist_tracks(pid="1") == []


def test_subclass_keeps_defaults_while_implementing_search_and_resolve() -> None:
    """A minimal concrete provider inherits every default untouched."""

    class MiniProvider(MusicProvider):
        key = "mini"
        display_name = "Mini"
        requires_login = True
        supports_playlists = True

        def search(self, keyword, limit=30, offset=0):
            return [Track(provider=self.key, id="1", name=keyword)], 1

        def resolve(self, track, level=None):
            return ResolveResult(url=f"https://mini.example.com/{track.id}.mp3")

    provider = MiniProvider()

    tracks, total = provider.search("kw", limit=5, offset=0)
    assert total == 1
    assert tracks[0].display() == "kw - 未知歌手"

    result = provider.resolve(tracks[0])
    assert result.ok is True
    assert result.url == "https://mini.example.com/1.mp3"

    # Inherited defaults:
    assert provider.key == "mini"
    assert provider.requires_login is True
    assert provider.supports_playlists is True
    assert provider.lyric(tracks[0]) == ("", "")
    assert provider.is_logged_in() is True
    assert provider.account() == (None, None)
    assert provider.clear_cookies() is None
    assert provider.cookie_names == []
    assert provider.user_playlists("u") == []
    assert provider.playlist_tracks("p") == []

    with pytest.raises(NotImplementedError):
        provider.login_by_cookie("cookie")
