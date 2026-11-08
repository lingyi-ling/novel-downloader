#!/usr/bin/env python3
"""
Cross-platform music search + the search-result track cache.

The providers are replaced with fakes: these tests cover merging, ranking,
per-platform failure isolation and cache resolution, never the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from novel_downloader.apps.music import aggregate
from novel_downloader.apps.music.downloader import download_mixed
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.trackcache import TrackCache


def _track(
    name: str,
    artists: str,
    provider: str,
    *,
    track_id: str = "",
    vip: bool = False,
    cover: str = "",
    duration: int = 0,
) -> Track:
    return Track(
        provider=provider,
        id=track_id or f"{provider}-{name}",
        name=name,
        artists=artists,
        vip=vip,
        cover_url=cover,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# merge_tracks
# ---------------------------------------------------------------------------


def test_merge_collapses_same_song_and_orders_sources() -> None:
    netease = _track("晴天", "周杰伦", "netease", vip=True)
    qq = _track("晴天", "周杰伦", "qq", cover="http://c.jpg", duration=269)
    kugou = _track("晴天", "周杰伦", "kugou")

    merged = aggregate.merge_tracks(
        [("netease", [netease]), ("qq", [qq]), ("kugou", [kugou])], "晴天"
    )

    assert len(merged) == 1
    primary = merged[0]
    # Non-VIP with a cover wins over the plain/VIP copies.
    assert primary.provider == "qq"
    # Primary first, the rest in registry order (never search completion order).
    assert primary.sources[0] == "qq"
    assert set(primary.sources) == {"qq", "netease", "kugou"}


def test_merge_keeps_different_artists_apart() -> None:
    merged = aggregate.merge_tracks(
        [
            ("netease", [_track("晴天", "周杰伦", "netease")]),
            ("qq", [_track("晴天", "蓝心羽", "qq")]),
        ],
        "晴天",
    )

    assert len(merged) == 2


def test_merge_ranks_exact_titles_first() -> None:
    merged = aggregate.merge_tracks(
        [
            (
                "qq",
                [
                    _track("晴天 (超盛大弦乐)", "Jerry音乐站", "qq"),
                    _track("晴天", "周杰伦", "qq"),
                ],
            )
        ],
        "晴天",
    )

    assert [track.name for track in merged] == ["晴天", "晴天 (超盛大弦乐)"]


def test_merge_limit_counts_songs_not_rows() -> None:
    merged = aggregate.merge_tracks(
        [("netease", [_track("A", "X", "netease"), _track("B", "X", "netease")])],
        "A",
        limit=1,
    )

    assert len(merged) == 1


def test_merge_unknown_artist_falls_back_to_album() -> None:
    left = _track("夜曲", "未知歌手", "netease")
    left.album = "十一月的萧邦"
    right = _track("夜曲", "未知歌手", "qq")
    right.album = "十一月的萧邦"

    merged = aggregate.merge_tracks([("netease", [left]), ("qq", [right])], "夜曲")

    assert len(merged) == 1
    assert merged[0].sources[0] == merged[0].provider
    assert set(merged[0].sources) == {"netease", "qq"}


# ---------------------------------------------------------------------------
# search_all
# ---------------------------------------------------------------------------


def test_search_all_isolates_platform_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aggregate, "available", lambda: [("good", "好"), ("bad", "坏")])

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [_track(keyword, "A", "good")], 1

    def create(key: str) -> Any:
        if key == "bad":
            raise RuntimeError("缺少可选依赖")
        return Good()

    monkeypatch.setattr(aggregate, "create", create)

    result = aggregate.search_all("晴天")

    assert [track.name for track in result.tracks] == ["晴天"]
    outcomes = {item.platform: item for item in result.platforms}
    assert outcomes["good"].ok is True
    assert outcomes["bad"].ok is False
    assert "缺少可选依赖" in outcomes["bad"].error


def test_search_all_reports_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aggregate, "available", lambda: [("slow", "慢")])

    class Slow:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            time.sleep(0.5)
            return [_track(keyword, "A", "slow")], 1

    monkeypatch.setattr(aggregate, "create", lambda key: Slow())

    result = aggregate.search_all("晴天", timeout=0.05)

    assert result.tracks == []
    assert result.platforms[0].ok is False
    assert "超时" in result.platforms[0].error


def test_search_all_rejects_unknown_platforms() -> None:
    with pytest.raises(KeyError):
        aggregate.search_all("晴天", ["not-a-platform"])


def test_search_all_empty_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A")])
    monkeypatch.setattr(aggregate, "create", lambda key: created.append(key))

    result = aggregate.search_all("   ")

    assert result.tracks == []
    assert created == []


# ---------------------------------------------------------------------------
# TrackCache
# ---------------------------------------------------------------------------


def test_trackcache_round_trip_and_position_resolution() -> None:
    cache = TrackCache()
    original = [_track("A", "X", "netease"), _track("B", "Y", "qq")]
    search_id = cache.put(original)

    # The client round-trips reduced copies (same provider/id, no extra).
    submitted = [
        Track(provider="netease", id=original[0].id, name="A", artists="X"),
        Track(provider="qq", id=original[1].id, name="B", artists="Y"),
    ]

    resolved = cache.resolve(search_id, submitted)

    assert resolved == original
    assert resolved[0] is original[0]
    assert resolved[1] is original[1]


def test_trackcache_matches_by_provider_and_id() -> None:
    cache = TrackCache()
    first = _track("A", "X", "netease")
    second = _track("B", "Y", "qq")
    search_id = cache.put([first, second])

    resolved = cache.resolve(search_id, [Track(provider="qq", id=second.id)])

    assert resolved == [second]


def test_trackcache_unknown_id_returns_submitted() -> None:
    cache = TrackCache()
    submitted = [Track(provider="netease", id="1", name="A")]

    assert cache.resolve("missing", submitted) == submitted


def test_trackcache_expires_entries() -> None:
    now = {"t": 100.0}
    cache = TrackCache(ttl=10.0, clock=lambda: now["t"])
    search_id = cache.put([_track("A", "X", "netease")])

    now["t"] += 11
    assert cache.get(search_id) is None


def test_trackcache_evicts_least_recently_used() -> None:
    cache = TrackCache(max_entries=2)
    first = cache.put([_track("A", "X", "netease")])
    second = cache.put([_track("B", "X", "netease")])
    cache.get(first)  # refresh usage order
    third = cache.put([_track("C", "X", "netease")])

    assert cache.get(first) is not None
    assert cache.get(second) is None
    assert cache.get(third) is not None


# ---------------------------------------------------------------------------
# download_mixed
# ---------------------------------------------------------------------------


@dataclass
class _Result:
    status: str = "ok"
    reason: str = ""
    path: str = ""
    bytes_: int = 0


def test_download_mixed_groups_and_keeps_global_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import downloader

    seen: list[tuple[str, list[str]]] = []

    def fake_download_many(
        provider: Any,
        tracks: list[Track],
        options: Any,
        *,
        on_result: Any,
        on_tick: Any = None,
        **_kwargs: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        seen.append((provider, [track.name for track in tracks]))
        for index, track in enumerate(tracks):
            track.path = f"/music/{track.name}.mp3"
            on_result(index, track, _Result(path=track.path))
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(downloader, "download_many", fake_download_many)

    tracks = [
        _track("A", "X", "netease"),
        _track("B", "Y", "qq"),
        _track("C", "Z", "netease"),
    ]
    results: list[tuple[int, str]] = []

    ok, skip, fail, _pairs, _failed = download_mixed(
        provider_for := lambda key: key,
        tracks,
        None,
        on_result=lambda index, _track, _result: results.append((index, _track.name)),
    )

    assert (ok, skip, fail) == (3, 0, 0)
    assert sorted(results) == [(0, "A"), (1, "B"), (2, "C")]
    assert seen == [("netease", ["A", "C"]), ("qq", ["B"])]
    assert provider_for("netease") == "netease"


def test_download_mixed_reports_missing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import downloader

    monkeypatch.setattr(
        downloader, "download_many", lambda *args, **kwargs: (0, 0, 0, [], [])
    )

    track = Track(name="A", artists="X")
    failures: list[tuple[int, str]] = []

    ok, _skip, fail, pairs, failed = download_mixed(
        None,
        [track],
        None,
        on_result=lambda index, _track, result: failures.append((index, result.reason)),
    )

    assert (ok, fail) == (0, 1)
    assert pairs and "缺少平台信息" in pairs[0][1]
    assert failures == [(0, "缺少平台信息，请重新搜索后再下载")]
    assert failed == [track]


def test_download_mixed_isolates_create_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import downloader

    monkeypatch.setattr(
        downloader, "download_many", lambda *args, **kwargs: (0, 0, 0, [], [])
    )

    def create(key: str) -> Any:
        raise RuntimeError("依赖缺失")

    ok, _skip, fail, pairs, _failed = download_mixed(
        create, [_track("A", "X", "broken")], None
    )

    assert (ok, fail) == (0, 1)
    assert "依赖缺失" in pairs[0][1]


# ---------------------------------------------------------------------------
# progressive updates, circuit breaker, download fallback
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_breaker() -> Any:
    aggregate.clear_failure_cache()
    yield
    aggregate.clear_failure_cache()


def test_search_all_reports_partial_results(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """Every finished platform triggers a snapshot, not just the end."""
    monkeypatch.setattr(
        aggregate, "available", lambda: [("fast", "快"), ("slow", "慢")]
    )

    class Fast:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [_track(keyword, "A", "fast")], 1

    class Slow:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            time.sleep(0.15)
            return [_track(keyword, "B", "slow")], 1

    providers = {"fast": Fast(), "slow": Slow()}
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: providers[key])

    snapshots: list[aggregate.AggregateResult] = []
    result = aggregate.search_all("晴天", on_update=snapshots.append)

    assert result.tracks
    assert snapshots
    first = snapshots[0]
    assert len(first.tracks) == 1
    assert [item.platform for item in first.platforms] == ["fast"]
    assert len(snapshots[-1].tracks) == 2


def test_search_all_skips_platforms_that_failed_before(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """A dead platform is skipped on the next aggregate search (breaker)."""
    monkeypatch.setattr(
        aggregate, "available", lambda: [("good", "好"), ("dead", "死")]
    )
    calls: list[str] = []

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [_track(keyword, "A", "good")], 1

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        if key == "dead":
            raise RuntimeError("连接失败")
        return Good()

    monkeypatch.setattr(aggregate, "create", create)

    first = aggregate.search_all("晴天")
    # The failure is still reported once; it is only skipped on later calls.
    assert {item.platform for item in first.platforms} == {"good", "dead"}
    assert next(item for item in first.platforms if item.platform == "dead").ok is False
    assert calls == ["good", "dead"]

    calls.clear()
    second = aggregate.search_all("晴天")
    assert calls == ["good"]  # dead is in the breaker window
    assert [item.platform for item in second.platforms] == ["good"]

    calls.clear()
    aggregate.search_all("晴天", platforms=["good", "dead"])
    assert calls == ["good", "dead"]  # explicit selection retries


def test_merge_attaches_in_memory_alternatives() -> None:
    netease = _track("晴天", "周杰伦", "netease")
    qq = _track("晴天", "周杰伦", "qq", cover="http://c.jpg")
    kugou = _track("晴天", "周杰伦", "kugou")

    merged = aggregate.merge_tracks(
        [("netease", [netease]), ("qq", [qq]), ("kugou", [kugou])], "晴天"
    )

    primary = merged[0]
    alternatives = getattr(primary, "_alternatives", [])
    assert primary is qq
    assert {track.provider for track in alternatives} == {"netease", "kugou"}


def test_download_mixed_falls_back_to_another_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import downloader

    primary = _track("晴天", "周杰伦", "netease")
    alternative = _track("晴天", "周杰伦", "qq")
    primary.__dict__["_alternatives"] = [alternative]

    def fake_download_many(
        provider: Any, tracks: list[Track], opts: Any, *, on_result: Any, **_kw: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        on_result(0, tracks[0], _Result("fail", "VIP 专属"))
        return 0, 0, 1, [(f"{primary.name} - {primary.artists}", "VIP 专属")], [primary]

    def fake_download_track(
        provider: Any, track: Track, opts: Any, **_kw: Any
    ) -> _Result:
        assert provider == "qq"
        track.path = "/music/qing.mp3"
        return _Result("ok", "", "/music/qing.mp3")

    monkeypatch.setattr(downloader, "download_many", fake_download_many)
    monkeypatch.setattr(downloader, "download_track", fake_download_track)

    results: list[tuple[int, str]] = []
    ok, skip, fail, pairs, failed = download_mixed(
        lambda key: key,
        [primary],
        None,
        on_result=lambda index, _track, result: results.append((index, result.status)),
    )

    assert (ok, skip, fail) == (1, 0, 0)
    assert pairs == []
    assert failed == []
    assert primary.path == "/music/qing.mp3"
    assert results[-1] == (0, "ok")


def test_download_mixed_keeps_failure_when_no_alternative_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novel_downloader.apps.music import downloader

    primary = _track("晴天", "周杰伦", "netease")
    alternative = _track("晴天", "周杰伦", "qq")
    primary.__dict__["_alternatives"] = [alternative]

    monkeypatch.setattr(
        downloader,
        "download_many",
        lambda provider, tracks, opts, *, on_result, **_kw: (
            0,
            0,
            1,
            [(f"{primary.name} - {primary.artists}", "VIP 专属")],
            [primary],
        ),
    )
    monkeypatch.setattr(
        downloader,
        "download_track",
        lambda provider, track, opts, **_kw: _Result("fail", "无版权"),
    )

    ok, _skip, fail, pairs, failed = download_mixed(lambda key: key, [primary], None)

    assert (ok, fail) == (0, 1)
    assert pairs and pairs[0][1] == "VIP 专属"
    assert failed == [primary]


def test_breaker_survives_a_process_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Platform health is persisted, so a restarted app does not re-probe dead sites."""
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    previous = aggregate._STATS
    aggregate._STATS = None
    try:
        aggregate._record_failure("dead", 6012)
        aggregate._save_stats()

        aggregate._STATS = None  # simulate a fresh process
        assert aggregate._breaker_open("dead") is True
        assert aggregate.failed_platforms() == ["dead"]

        aggregate.clear_failure_cache()
        assert aggregate._breaker_open("dead") is False
    finally:
        aggregate._STATS = previous


def test_search_all_returns_early_after_a_usable_batch(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """A slow tail must not hold the whole search past the first usable batch."""
    monkeypatch.setattr(aggregate, "EARLY_EXIT_GRACE", 0.1)
    monkeypatch.setattr(
        aggregate, "available", lambda: [("fast", "快"), ("slow", "慢")]
    )

    class Fast:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [_track(keyword, "A", "fast")], 1

    class Slow:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            time.sleep(1.5)
            return [_track(keyword, "B", "slow")], 1

    providers = {"fast": Fast(), "slow": Slow()}
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: providers[key])

    started = time.monotonic()
    result = aggregate.search_all("晴天", timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert [item.platform for item in result.platforms] == ["fast"]
    # The omitted platform was not judged: it may simply have been mid-request.
    assert aggregate._breaker_open("slow") is False
