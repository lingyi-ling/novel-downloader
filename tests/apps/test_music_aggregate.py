#!/usr/bin/env python3
"""
Cross-platform music search + the search-result track cache.

The providers are replaced with fakes: these tests cover merging, ranking,
per-platform failure isolation and cache resolution, never the network.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

import pytest

from novel_downloader.apps.music import aggregate
from novel_downloader.apps.music.downloader import download_mixed
from novel_downloader.apps.music.providers.base import Track
from novel_downloader.apps.music.trackcache import TrackCache
from novel_downloader.apps.music.weapi import LoginExpired


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


def test_search_all_redacts_signed_urls_from_platform_errors(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """Platform errors are client-visible; signed query strings must go."""
    monkeypatch.setattr(aggregate, "available", lambda: [("signed", "签名")])

    class Signed:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            raise RuntimeError(
                "403 for https://cdn.example.com/a.mp3?token=SECRET"
                "&expires=1700000000#frag"
            )

    monkeypatch.setattr(aggregate, "create", lambda key: Signed())

    result = aggregate.search_all("晴天")

    assert result.platforms[0].ok is False
    error = result.platforms[0].error
    assert "SECRET" not in error
    assert "expires" not in error
    assert "#frag" not in error
    # The diagnostic part (host + path + reason) survives.
    assert "cdn.example.com/a.mp3" in error
    assert "403" in error


def test_search_all_reports_timeouts(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
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


def test_trackcache_position_match_rejects_mismatched_keys() -> None:
    """Same length is not enough: unrelated provider/id pairs stay submitted."""
    cache = TrackCache()
    cached = [_track("A", "X", "netease", track_id="1")]
    search_id = cache.put(cached)

    submitted = [Track(provider="netease", id="999", name="A", artists="X")]

    resolved = cache.resolve(search_id, submitted)

    assert resolved[0] is submitted[0]


def test_trackcache_resolves_a_reordered_submission_by_key() -> None:
    cache = TrackCache()
    first = _track("A", "X", "netease", track_id="1")
    second = _track("B", "Y", "qq", track_id="2")
    search_id = cache.put([first, second])

    submitted = [
        Track(provider="qq", id=second.id, name="B", artists="Y"),
        Track(provider="netease", id=first.id, name="A", artists="X"),
    ]

    resolved = cache.resolve(search_id, submitted)

    assert resolved[0] is second
    assert resolved[1] is first


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
    assert "slow" not in aggregate.failed_platforms()


def test_search_all_records_a_budget_timeout_in_the_breaker(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """A platform that never answers inside the budget is a real failure.

    It used to be reported as ``TimeoutError`` without touching the breaker,
    so every subsequent aggregate search paid the same full budget again.
    Early-stopped platforms must stay excluded (see the test above).
    """
    monkeypatch.setattr(
        aggregate, "available", lambda: [("good", "好"), ("slow", "慢")]
    )
    calls: list[str] = []

    class Good:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            return [_track(keyword, "A", "good")], 1

    class Slow:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            time.sleep(0.5)
            return [_track(keyword, "B", "slow")], 1

    providers = {"good": Good(), "slow": Slow()}

    def create(key: str, **kwargs: Any) -> Any:
        calls.append(key)
        return providers[key]

    monkeypatch.setattr(aggregate, "create", create)

    first = aggregate.search_all("晴天", timeout=0.05)
    outcomes = {item.platform: item for item in first.platforms}
    assert outcomes["good"].ok is True
    assert outcomes["slow"].ok is False
    assert "超时" in outcomes["slow"].error

    # The breaker now knows about the platform, with the actual deadline.
    assert aggregate._breaker_open("slow") is True
    assert "slow" in aggregate.failed_platforms()
    assert aggregate._load_stats()["slow"]["last_ms"] == 100  # clamped 0.1s budget

    calls.clear()
    second = aggregate.search_all("晴天", timeout=0.05)
    assert calls == ["good"]  # the timed-out platform is skipped
    assert [item.platform for item in second.platforms] == ["good"]


# ---------------------------------------------------------------------------
# Regression tests for audited defects (slot 2: music engine)
# ---------------------------------------------------------------------------


def test_ordered_keys_deduplicates_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicated key must not start the same platform twice."""
    monkeypatch.setattr(
        aggregate, "available", lambda: [("a", "A"), ("b", "B"), ("c", "C")]
    )

    ordered = aggregate._ordered_keys(["b", "a", "b", "c", "a"])

    assert ordered == ["a", "b", "c"]


def test_search_all_starts_every_platform_with_a_small_worker_pool(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """``workers`` is a concurrency limit, not a truncation of the platform list.

    The old implementation only created threads for ``keys[:workers]``: with
    ``workers=1`` every platform after the first was never searched, yet was
    silently omitted from the result (or mislabelled as a timeout).
    """
    monkeypatch.setattr(
        aggregate, "available", lambda: [("a", "A"), ("b", "B"), ("c", "C")]
    )
    calls: list[str] = []

    class Provider:
        def __init__(self, key: str) -> None:
            self.key = key

        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            calls.append(self.key)
            return [_track(f"{self.key}-{keyword}", "A", self.key)], 1

    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Provider(key))

    result = aggregate.search_all("晴天", workers=1, timeout=3.0)

    assert sorted(calls) == ["a", "b", "c"]
    assert sorted(item.platform for item in result.platforms) == ["a", "b", "c"]
    assert len(result.tracks) == 3


def test_search_all_total_counts_songs_before_limit(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """``AggregateResult.total`` documents the pre-limit match count."""
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A")])

    class Provider:
        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            tracks = [
                _track(f"{keyword}{index}", "A", "a", track_id=str(index))
                for index in range(4)
            ]
            return tracks, len(tracks)

    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Provider())

    result = aggregate.search_all("晴天", limit=2, workers=1, timeout=3.0)

    assert len(result.tracks) == 2
    assert result.total == 4


def test_save_stats_is_safe_under_concurrent_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Concurrent saves must never interleave bytes in the same temp file.

    ``_save_stats`` used to serialise outside the lock into a fixed
    ``search_stats.json.tmp``; two searches finishing together produced a
    corrupted file (and could raise an uncaught ``RuntimeError`` when a record
    mutated a nested dict while it was being encoded).
    """
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path))
    monkeypatch.setattr(
        aggregate,
        "_STATS",
        {"a": {"ok": True, "last_ms": 1, "failed_until": 0.0}},
    )

    # Write the JSON in two halves with a delay so two writers would overlap.
    def slow_dump(obj: Any, fh: Any, **kwargs: Any) -> None:
        text = json.dumps(obj, **kwargs)
        cut = max(1, len(text) // 2)
        fh.write(text[:cut])
        fh.flush()
        time.sleep(0.05)
        fh.write(text[cut:])

    monkeypatch.setattr(aggregate.json, "dump", slow_dump)
    errors: list[BaseException] = []

    def save() -> None:
        try:
            aggregate._save_stats()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    payload = json.loads((tmp_path / "search_stats.json").read_text(encoding="utf-8"))
    assert payload["platforms"]["a"]["ok"] is True


def test_search_all_serializes_progressive_updates(
    monkeypatch: pytest.MonkeyPatch, clean_breaker: Any
) -> None:
    """Snapshots must be delivered serially and never go backwards.

    Several platforms can finish in the same instant; the UI used to receive
    an older snapshot after a newer one, so its row list visibly shrank again.
    """
    barrier = threading.Barrier(4)

    class Provider:
        def __init__(self, key: str) -> None:
            self.key = key

        def search(self, keyword: str, limit: int = 30, offset: int = 0):
            barrier.wait(timeout=5)
            return [_track(f"{self.key}-{keyword}", "A", self.key)], 1

    monkeypatch.setattr(
        aggregate, "available", lambda: [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    )
    monkeypatch.setattr(aggregate, "create", lambda key, **kw: Provider(key))

    guard = threading.Lock()
    active = {"now": 0, "max": 0}
    sizes: list[int] = []
    all_delivered = threading.Event()

    def on_update(partial: aggregate.AggregateResult) -> None:
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        sizes.append(len(partial.platforms))
        time.sleep(0.03)
        with guard:
            active["now"] -= 1
            if len(sizes) >= 4:
                all_delivered.set()

    aggregate.search_all("晴天", on_update=on_update, timeout=5.0)

    # The search can return while the last callback is still running.
    assert all_delivered.wait(5)
    assert active["max"] == 1
    assert sizes == sorted(sizes)
    assert sizes[-1] == 4


class _FakeConn:
    def __init__(self, payload: Any = None, *, has_payload: bool = False) -> None:
        self._payload = payload
        self._has_payload = has_payload

    def poll(self, deadline: float) -> bool:
        return self._has_payload

    def recv(self) -> Any:
        return self._payload

    def close(self) -> None:
        pass


class _FakeProcess:
    daemon = True

    def __init__(self, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        pass

    def terminate(self) -> None:
        pass


class _FakeContext:
    def __init__(self, payload: Any = None, *, has_payload: bool = False) -> None:
        self._payload = payload
        self._has_payload = has_payload

    def Pipe(self, duplex: bool = False) -> tuple[_FakeConn, _FakeConn]:
        return (
            _FakeConn(self._payload, has_payload=self._has_payload),
            _FakeConn(),
        )

    def Process(self, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess()


def test_search_all_isolated_rejects_a_malformed_child_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage payload must become a per-platform failure, not ValueError."""
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A")])
    monkeypatch.setattr(
        aggregate.multiprocessing,
        "get_context",
        lambda method: _FakeContext("garbage", has_payload=True),
    )

    result = aggregate.search_all_isolated("晴天")

    assert [item.platform for item in result.platforms] == ["a"]
    assert result.platforms[0].ok is False
    assert "无效结果" in result.platforms[0].error


def test_search_all_isolated_marks_every_platform_when_the_child_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aggregate, "available", lambda: [("a", "A"), ("b", "B")])
    monkeypatch.setattr(
        aggregate.multiprocessing,
        "get_context",
        lambda method: _FakeContext(),
    )

    result = aggregate.search_all_isolated("晴天", timeout=0.1)

    assert {item.platform for item in result.platforms} == {"a", "b"}
    assert all(item.ok is False for item in result.platforms)
    assert all("未返回结果" in item.error for item in result.platforms)


def test_download_mixed_removes_the_rescued_tracks_failure_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-labelled songs must not swap their failure rows.

    The fallback used to delete the first ``failed_pairs`` entry whose label
    matched, which can belong to a *different* song that failed earlier.
    """
    from novel_downloader.apps.music import downloader

    failed_first = _track("晴天", "周杰伦", "qq", track_id="2")
    rescued = _track("晴天", "周杰伦", "n", track_id="1")
    alternative = _track("晴天", "周杰伦", "k", track_id="3")
    rescued.__dict__["_alternatives"] = [alternative]

    def fake_download_many(
        provider: Any, tracks: list[Track], opts: Any, *, on_result: Any, **_kw: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        track = tracks[0]
        if track is failed_first:
            reason = "QQ 失败"
            on_result(0, track, _Result("fail", reason))
            return 0, 0, 1, [(f"{track.name} - {track.artists}", reason)], [track]
        reason = "网易云失败"
        on_result(0, track, _Result("fail", reason))
        return 0, 0, 1, [(f"{track.name} - {track.artists}", reason)], [track]

    rescue_calls: list[str] = []

    def fake_download_track(
        provider: Any, track: Track, opts: Any, **_kw: Any
    ) -> _Result:
        rescue_calls.append(str(provider))
        track.path = "/music/qing.mp3"
        return _Result("ok", "", "/music/qing.mp3")

    monkeypatch.setattr(downloader, "download_many", fake_download_many)
    monkeypatch.setattr(downloader, "download_track", fake_download_track)

    ok, _skip, fail, pairs, failed = download_mixed(
        lambda key: key, [failed_first, rescued], None
    )

    assert rescue_calls == ["k"]
    assert (ok, fail) == (1, 1)
    assert pairs == [("晴天 - 周杰伦", "QQ 失败")]
    assert failed == [failed_first]


def test_download_mixed_does_not_report_completion_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped batch must not tell the UI it reached 100%."""
    from novel_downloader.apps.music import downloader

    monkeypatch.setattr(
        downloader, "download_many", lambda *args, **kwargs: (0, 0, 0, [], [])
    )
    ticks: list[tuple[float, int]] = []

    download_mixed(
        lambda key: key,
        [_track("A", "X", "n"), _track("B", "X", "n")],
        None,
        on_tick=lambda fraction, done: ticks.append((fraction, done)),
        should_stop=lambda: True,
    )

    assert ticks == [(0.0, 0)]


def test_download_mixed_propagates_login_expiry_from_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login expiry inside the fallback must abort the batch, not be swallowed.

    The primary source fails as an ordinary track failure; the fallback then
    raises :class:`LoginExpired`. Treating it as "try the next source" would
    turn an expired session into a silent per-track failure, so it has to
    propagate to the caller (which shows the re-login hint).
    """
    from novel_downloader.apps.music import downloader

    primary = _track("晴天", "周杰伦", "n", track_id="1")
    primary.__dict__["_alternatives"] = [_track("晴天", "周杰伦", "k", track_id="2")]
    fallback_calls: list[tuple[Any, Any]] = []

    def fake_download_many(
        provider: Any, tracks: list[Track], opts: Any, *, on_result: Any, **_kw: Any
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        track = tracks[0]
        on_result(0, track, _Result("fail", "无权限"))
        return 0, 0, 1, [(f"{track.name} - {track.artists}", "无权限")], [track]

    def expired(provider: Any, track: Any, *_args: Any, **_kwargs: Any) -> Any:
        fallback_calls.append((provider, track))
        raise LoginExpired("登录态已失效")

    monkeypatch.setattr(downloader, "download_many", fake_download_many)
    monkeypatch.setattr(downloader, "download_track", expired)

    with pytest.raises(LoginExpired, match="登录态已失效"):
        download_mixed(lambda key: key, [primary], None)

    # Proves the exception came from the fallback path, not from the primary.
    assert fallback_calls == [("k", primary.__dict__["_alternatives"][0])]


def test_merged_alternatives_are_not_dataclass_fields() -> None:
    """``_alternatives`` must stay out of ``asdict``/JSON (it is process-local)."""
    left = _track("晴天", "周杰伦", "netease")
    right = _track("晴天", "周杰伦", "qq", cover="http://c.jpg")

    merged = aggregate.merge_tracks([("netease", [left]), ("qq", [right])], "晴天")

    primary = merged[0]
    assert primary.__dict__["_alternatives"]
    payload = asdict(primary)
    assert "_alternatives" not in payload
    assert payload["sources"] == primary.sources
    json.dumps(payload)  # the public shape is JSON-serialisable


def test_track_dict_drops_provider_private_data() -> None:
    """The wire representation must never carry ``extra`` or ``_alternatives``."""
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS, track_dict

    track = _track("晴天", "周杰伦", "netease", track_id="186016", duration=269)
    track.album = "叶惠美"
    track.cover_url = "http://c.jpg"
    track.ext = "mp3"
    track.vip = True
    track.download_url = "http://cdn/1.mp3"
    track.http_headers = {"Referer": "http://music.163.com"}
    track.path = "/music/晴天.mp3"
    track.lyric = "[00:00]晴天"
    track.sources = ["netease", "qq"]
    track.extra = {
        "raw": {"cookie": "MUSIC_U=secret", "track": object()},
        "songinfo": object(),
    }
    track.__dict__["_alternatives"] = [_track("晴天", "周杰伦", "qq")]

    payload = track_dict(track)

    assert tuple(payload) == PUBLIC_TRACK_FIELDS
    assert "extra" not in payload
    assert "_alternatives" not in payload
    assert payload["provider"] == "netease"
    assert payload["id"] == "186016"
    assert payload["name"] == "晴天"
    assert payload["artists"] == "周杰伦"
    assert payload["album"] == "叶惠美"
    assert payload["duration_s"] == 269
    assert payload["cover_url"] == "http://c.jpg"
    assert payload["ext"] == "mp3"
    assert payload["vip"] is True
    assert payload["download_url"] == "http://cdn/1.mp3"
    assert payload["http_headers"] == {"Referer": "http://music.163.com"}
    assert payload["path"] == "/music/晴天.mp3"
    assert payload["lyric"] == "[00:00]晴天"
    assert payload["sources"] == ["netease", "qq"]
    json.dumps(payload)  # JSON-safe despite the private objects above


def test_track_dict_fills_defaults_for_duck_typed_tracks() -> None:
    """Provider/test doubles without the full attribute set still serialise."""
    from novel_downloader.apps.music.serialize import PUBLIC_TRACK_FIELDS, track_dict

    payload = track_dict(object())

    assert tuple(payload) == PUBLIC_TRACK_FIELDS
    assert payload["duration_s"] == 0
    assert payload["track_no"] == 0
    assert payload["vip"] is False
    assert payload["http_headers"] == {}
    assert payload["sources"] == []
