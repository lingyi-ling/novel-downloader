#!/usr/bin/env python3
"""Tests for the short-lived search-result cache."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import search, search_stream
from novel_downloader.plugins.search_cache import (
    DISK_DIR_ENV,
    CacheKey,
    CacheStats,
    SearchCache,
    build_key,
)
from novel_downloader.schemas import SearchResult


class _Clock:
    """A manually advanced monotonic clock (never sleeps)."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _result(book_id: str = "1", site: str = "alpha") -> SearchResult:
    return {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.com/{book_id}",
        "cover_url": "",
        "title": f"title-{book_id}",
        "author": "author",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }


def _key(
    keyword: str = "k",
    sites: list[str] | None = None,
    per_site_limit: int = 5,
    nsfw: bool = False,
) -> CacheKey:
    return build_key(keyword, sites, per_site_limit, nsfw)


# --------------------------------------------------------------------------- #
# build_key
# --------------------------------------------------------------------------- #
def test_build_key_normalizes_keyword() -> None:
    assert _key("《斗破 苍穹》") == _key("斗破苍穹")
    assert _key("ABC_def-1").keyword == "abcdef1"


def test_build_key_is_order_independent_for_sites() -> None:
    assert _key("k", ["a", "b"]) == _key("k", ["b", "a"])


def test_build_key_none_sites_distinct_from_empty() -> None:
    assert _key("k", None).sites is None
    assert _key("k", []).sites == ()


def test_build_key_includes_limit_and_nsfw() -> None:
    assert _key("k", None, 5, False) != _key("k", None, 10, False)
    assert _key("k", None, 5, False) != _key("k", None, 5, True)


def test_build_key_includes_timeout() -> None:
    # The timeout bounds how long slow sites may take, so two otherwise
    # identical searches with different budgets can yield different result
    # sets and must not share a cache entry.
    fast = build_key("k", None, 5, False, timeout=1.0)
    slow = build_key("k", None, 5, False, timeout=60.0)

    assert fast != slow
    assert fast == build_key("k", None, 5, False, timeout=1.0)


def test_build_key_keeps_punctuation_only_keywords_apart() -> None:
    # ``normalize_text("!!!")`` is empty; without the raw-keyword fallback
    # every punctuation-only query (and an empty one) would share a snapshot.
    assert _key("!!!") != _key("???")
    assert _key("!!!") != _key("")
    assert _key("!!!") != _key("!!")
    # Whitespace around the same punctuation is still the same query.
    assert _key("  !!!  ") == _key("!!!")


def test_build_key_folds_full_width_punctuation_only_keywords() -> None:
    # The raw-keyword fallback must fold full-width forms like every other
    # keyword identity: "！！！" and "!!!" are the same query, but still
    # distinct from "？？？".
    assert _key("！！！") == _key("!!!")
    assert _key("？？？") == _key("???")
    assert _key("！！！") != _key("？？？")


def test_build_key_text_keywords_still_use_normalized_identity() -> None:
    # The fallback only applies when normalization erases everything; a
    # punctuation-decorated real keyword keeps sharing with its plain form.
    assert _key("斗破 苍穹") == _key("斗破苍穹")
    assert _key("《斗破苍穹》") == _key("斗破苍穹")


def test_build_key_aggregate_includes_config_fingerprint() -> None:
    # ``sites=None`` means "whatever is registered", so the configuration the
    # snapshot was gathered from is part of its identity.
    assert build_key("k", None, config_fingerprint="f1") != build_key(
        "k", None, config_fingerprint="f2"
    )
    assert build_key("k", None, config_fingerprint="f1") == build_key(
        "k", None, config_fingerprint="f1"
    )
    assert build_key("k", None) == build_key("k", None)


def test_build_key_ignores_fingerprint_for_explicit_sites() -> None:
    # An explicit selection names every site it depends on, so a registry /
    # browser-settings change cannot alter its result set.
    assert build_key("k", ["a"], config_fingerprint="f1") == build_key(
        "k", ["a"], config_fingerprint="f2"
    )
    assert build_key("k", ["a"]).config_fingerprint is None


def test_cache_key_is_hashable_and_frozen() -> None:
    key = _key()
    assert len({_key(), _key()}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.keyword = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# basic get / put
# --------------------------------------------------------------------------- #
def test_get_returns_put_results() -> None:
    cache = SearchCache(now=_Clock())
    key = _key()
    results = [_result("1")]
    cache.put(key, results)
    assert cache.get(key) == results


def test_get_miss_returns_none() -> None:
    cache = SearchCache(now=_Clock())
    assert cache.get(_key("absent")) is None


def test_default_construction_smoke() -> None:
    # Uses the real monotonic clock; no sleeping required.
    cache = SearchCache()
    key = _key()
    cache.put(key, [_result("1")])
    assert cache.get(key) == [_result("1")]


def test_cache_hits_across_equivalent_keywords_and_site_order() -> None:
    cache = SearchCache(now=_Clock())
    cache.put(_key("《斗破 苍穹》", ["a", "b"]), [_result("1")])
    assert cache.get(_key("斗破苍穹", ["b", "a"])) == [_result("1")]


# --------------------------------------------------------------------------- #
# copies (no shared mutable state)
# --------------------------------------------------------------------------- #
def test_get_returns_fresh_list_each_call() -> None:
    cache = SearchCache(now=_Clock())
    key = _key()
    cache.put(key, [_result("1")])
    first = cache.get(key)
    second = cache.get(key)
    assert first is not None and second is not None
    assert first == second
    assert first is not second


def test_caller_list_mutation_does_not_pollute_cache() -> None:
    # ``get`` returns a shallow *list* copy: reordering/append/clear on it must
    # not affect the cached snapshot (the inner dicts are shared by design).
    cache = SearchCache(now=_Clock())
    key = _key()
    cache.put(key, [_result("1")])

    fetched = cache.get(key)
    assert fetched is not None
    fetched.append(_result("2"))
    del fetched[0]

    again = cache.get(key)
    assert again is not None
    assert again == [_result("1")]


def test_put_snapshots_input_sequence() -> None:
    cache = SearchCache(now=_Clock())
    key = _key()
    source = [_result("1")]
    cache.put(key, source)
    source.append(_result("2"))
    assert cache.get(key) == [_result("1")]


# --------------------------------------------------------------------------- #
# TTL expiry
# --------------------------------------------------------------------------- #
def test_entry_valid_just_before_ttl() -> None:
    clock = _Clock()
    cache = SearchCache(ttl=10.0, now=clock)
    key = _key()
    cache.put(key, [_result("1")])
    clock.advance(9.9)
    assert cache.get(key) == [_result("1")]


def test_entry_expires_at_ttl_and_is_cleaned() -> None:
    clock = _Clock()
    cache = SearchCache(ttl=10.0, max_entries=1, now=clock)
    key = _key()
    cache.put(key, [_result("1")])
    clock.advance(10.0)  # age == ttl => expired
    assert cache.get(key) is None
    # Expired entry must not linger and occupy capacity.
    other = _key("other")
    cache.put(other, [_result("2")])
    assert cache.get(other) == [_result("2")]


def test_clear_removes_expired_and_live_entries() -> None:
    clock = _Clock()
    cache = SearchCache(ttl=5.0, now=clock)
    live = _key("live")
    cache.put(live, [_result("1")])
    clock.advance(5.0)
    cache.put(_key("fresh"), [_result("2")])
    assert cache.get(live) is None  # lazy expiry on read

    cache.clear()
    assert cache.get(_key("fresh")) is None


# --------------------------------------------------------------------------- #
# LRU eviction
# --------------------------------------------------------------------------- #
def test_eviction_drops_oldest_when_over_capacity() -> None:
    cache = SearchCache(max_entries=2, now=_Clock())
    ka, kb, kc = _key("a"), _key("b"), _key("c")
    cache.put(ka, [_result("a")])
    cache.put(kb, [_result("b")])
    cache.put(kc, [_result("c")])  # evicts ka
    assert cache.get(ka) is None
    assert cache.get(kb) == [_result("b")]
    assert cache.get(kc) == [_result("c")]


def test_get_refreshes_recency() -> None:
    cache = SearchCache(max_entries=2, now=_Clock())
    ka, kb, kc = _key("a"), _key("b"), _key("c")
    cache.put(ka, [_result("a")])
    cache.put(kb, [_result("b")])
    assert cache.get(ka) is not None  # touch ka => kb is now the LRU
    cache.put(kc, [_result("c")])  # evicts kb, not ka
    assert cache.get(kb) is None
    assert cache.get(ka) == [_result("a")]
    assert cache.get(kc) == [_result("c")]


def test_reput_refreshes_recency() -> None:
    cache = SearchCache(max_entries=2, now=_Clock())
    ka, kb, kc = _key("a"), _key("b"), _key("c")
    cache.put(ka, [_result("a")])
    cache.put(kb, [_result("b")])
    cache.put(ka, [_result("a2")])  # refresh ka => kb is the LRU
    cache.put(kc, [_result("c")])  # evicts kb
    assert cache.get(kb) is None
    assert cache.get(ka) == [_result("a2")]


def test_clear() -> None:
    cache = SearchCache(now=_Clock())
    ka, kb = _key("a"), _key("b")
    cache.put(ka, [_result("a")])
    cache.put(kb, [_result("b")])
    cache.clear()
    assert cache.get(ka) is None
    assert cache.get(kb) is None


def test_put_purges_expired_entries_before_evicting_live_ones() -> None:
    # ``get`` only expires the key it was asked for, so an entry touched just
    # before its deadline sits at the recently-used end while already dead.  A
    # write must drop such entries first: otherwise the dead one keeps its slot
    # and a *live* entry is evicted to make room.
    clock = _Clock()
    cache = SearchCache(ttl=10.0, max_entries=2, now=clock)
    stale, live = _key("stale"), _key("live")
    cache.put(stale, [_result("stale")])
    clock.advance(1.0)
    cache.put(live, [_result("live")])

    clock.advance(8.9)  # stale is 9.9s old (fresh), live is 8.9s old
    assert cache.get(stale) == [_result("stale")]  # touch => stale is now MRU
    clock.advance(1.0)  # stale is 10.9s old (expired), live is 9.9s old

    cache.put(_key("fresh"), [_result("fresh")])

    assert cache.get(live) == [_result("live")]  # not displaced by the corpse
    assert cache.get(_key("fresh")) == [_result("fresh")]
    assert cache.get(stale) is None


# --------------------------------------------------------------------------- #
# thread safety (the cache is a process-wide singleton)
# --------------------------------------------------------------------------- #
def test_concurrent_get_put_and_stats_never_raise() -> None:
    # Guard for the process-wide ``search._CACHE``: searches may run on
    # different threads/event loops, so ``get``/``put``/``stats`` must not
    # corrupt the ``OrderedDict`` (the pre-lock ``move_to_end`` could raise
    # ``KeyError`` when a concurrent ``put`` evicted the entry being read).
    cache = SearchCache(ttl=60.0, max_entries=8)
    keys = [_key(f"k{index}") for index in range(16)]
    barrier = threading.Barrier(4)
    failures: list[Exception] = []

    def worker(offset: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for step in range(300):
                cache.put(keys[(offset + step) % len(keys)], [_result(str(step))])
                cache.get(keys[(offset * 7 + step) % len(keys)])
                cache.stats()
        except Exception as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(cache._entries) <= 8


class _BlockingClock:
    """Clock whose second call waits until another thread has written."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 2:
            # Called from ``get`` after it read the entry but before it touches
            # the LRU order: let a writer try to evict that very entry now.
            self.entered.set()
            self.release.wait(timeout=0.5)
        return 0.0


def test_get_survives_a_writer_evicting_it_between_lookup_and_touch() -> None:
    # Deterministic reproduction of the interleaving the lock prevents:
    # ``get`` reads entry A, then a concurrent ``put`` of B (max_entries=1)
    # evicts A, and the ``move_to_end(A)`` that follows used to raise KeyError.
    clock = _BlockingClock()
    cache = SearchCache(ttl=10.0, max_entries=1, now=clock)
    key = _key()
    cache.put(key, [_result("1")])  # first clock call, not blocked

    def writer() -> None:
        clock.entered.wait(timeout=2.0)
        cache.put(_key("other"), [_result("2")])
        clock.release.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert cache.get(key) == [_result("1")]
    finally:
        thread.join(timeout=3.0)

    assert not thread.is_alive()


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def test_stats_start_at_zero() -> None:
    cache = SearchCache(now=_Clock())
    assert cache.stats() == CacheStats(hits=0, misses=0, evictions=0)


def test_stats_counts_hits_and_misses() -> None:
    cache = SearchCache(now=_Clock())
    key = _key()
    cache.put(key, [_result("1")])

    assert cache.get(key) is not None  # hit
    assert cache.get(key) is not None  # hit
    assert cache.get(_key("absent")) is None  # miss

    stats = cache.stats()
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.evictions == 0


def test_stats_counts_expired_lookup_as_miss() -> None:
    clock = _Clock()
    cache = SearchCache(ttl=10.0, now=clock)
    key = _key()
    cache.put(key, [_result("1")])
    assert cache.get(key) is not None  # hit while fresh
    clock.advance(10.0)
    assert cache.get(key) is None  # expired => miss, not eviction

    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.evictions == 0


def test_stats_counts_lru_evictions() -> None:
    cache = SearchCache(max_entries=2, now=_Clock())
    cache.put(_key("a"), [_result("a")])
    cache.put(_key("b"), [_result("b")])
    cache.put(_key("c"), [_result("c")])  # evicts "a"
    cache.put(_key("d"), [_result("d")])  # evicts "b"

    stats = cache.stats()
    assert stats.evictions == 2
    assert stats.hits == 0
    assert stats.misses == 0


def test_stats_are_cumulative_and_clear_keeps_them() -> None:
    cache = SearchCache(max_entries=1, now=_Clock())
    cache.put(_key("a"), [_result("a")])
    assert cache.get(_key("a")) is not None  # hit
    assert cache.get(_key("missing")) is None  # miss
    cache.put(_key("b"), [_result("b")])  # evicts "a"

    before = cache.stats()
    cache.clear()
    assert cache.stats() == before


def test_stats_returns_a_frozen_snapshot() -> None:
    cache = SearchCache(now=_Clock())
    cache.put(_key(), [_result("1")])
    snapshot = cache.stats()
    cache.get(_key())

    assert snapshot.hits == 0  # not retroactively updated
    assert cache.stats().hits == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.hits = 10  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# optional disk layer
# --------------------------------------------------------------------------- #
def _disk_cache(
    directory: Path,
    *,
    clock: _Clock,
    ttl: float = 300.0,
    max_disk_entries: int = 256,
    max_disk_bytes: int = 2 * 1024 * 1024,
) -> SearchCache:
    """Build a cache whose memory and disk layers share one manual clock."""
    return SearchCache(
        ttl=ttl,
        now=clock,
        wall_clock=clock,
        disk_dir=directory,
        max_disk_entries=max_disk_entries,
        max_disk_bytes=max_disk_bytes,
    )


def _with_bad_priority(payload: dict[str, object]) -> object:
    rows = payload["results"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    return {**payload, "results": [{**row, "priority": "high"}]}


_CORRUPTIONS = [
    ("wrong-version", lambda payload: {**payload, "version": 999}),
    ("wrong-key", lambda payload: {**payload, "key": "someone-else"}),
    ("bad-timestamp", lambda payload: {**payload, "stored_at": "yesterday"}),
    ("results-not-a-list", lambda payload: {**payload, "results": 7}),
    ("row-not-a-mapping", lambda payload: {**payload, "results": ["row"]}),
    ("row-missing-fields", lambda payload: {**payload, "results": [{"site": "a"}]}),
    ("row-wrong-type", _with_bad_priority),
    ("payload-not-a-mapping", lambda payload: ["not", "a", "mapping"]),
]


def test_memory_only_cache_never_touches_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DISK_DIR_ENV, raising=False)
    cache = SearchCache(now=_Clock())
    assert cache.disk_dir is None

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected file I/O")

    monkeypatch.setattr(Path, "open", _forbidden)
    monkeypatch.setattr(Path, "mkdir", _forbidden)
    monkeypatch.setattr(Path, "stat", _forbidden)
    monkeypatch.setattr(Path, "glob", _forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", _forbidden)

    key = _key()
    cache.put(key, [_result("1")])
    assert cache.get(key) == [_result("1")]
    assert cache.get(_key("missing")) is None
    cache.clear()
    assert cache.stats().hits == 1


def test_env_var_enables_disk_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    target = tmp_path / "search-cache"
    monkeypatch.setenv(DISK_DIR_ENV, str(target))

    cache = SearchCache(now=clock, wall_clock=clock)
    assert cache.disk_dir == target
    key = _key()
    cache.put(key, [_result("1")])
    assert list(target.glob("v1-*.json"))

    reader = SearchCache(now=clock, wall_clock=clock)
    assert reader.get(key) == [_result("1")]


def test_empty_or_whitespace_env_disables_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DISK_DIR_ENV, "   ")
    assert SearchCache().disk_dir is None

    # An explicit empty string also opts out, even when the env var is set.
    monkeypatch.setenv(DISK_DIR_ENV, str(tmp_path))
    assert SearchCache(disk_dir="").disk_dir is None


def test_explicit_disk_dir_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DISK_DIR_ENV, str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"
    assert SearchCache(disk_dir=explicit).disk_dir == explicit


def test_disk_roundtrip_across_instances(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    results = [_result("1"), _result("2")]
    _disk_cache(tmp_path, clock=clock).put(key, results)

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) == results
    assert reader.stats().hits == 1


def test_disk_hit_is_promoted_to_memory(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    _disk_cache(tmp_path, clock=clock).put(key, [_result("1")])

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) == [_result("1")]
    # Once promoted, the memory entry keeps serving even if the file vanishes.
    reader._disk_path(key).write_bytes(b"{}")
    assert reader.get(key) == [_result("1")]
    assert reader.stats().hits == 2


def test_disk_promotion_keeps_the_remaining_ttl(tmp_path: Path) -> None:
    # Loading from disk must not restart the TTL: the promoted memory entry
    # expires when the persisted one would have.
    clock = _Clock()
    key = _key()
    _disk_cache(tmp_path, clock=clock, ttl=10.0).put(key, [_result("1")])

    clock.advance(9.9)
    reader = _disk_cache(tmp_path, clock=clock, ttl=10.0)
    assert reader.get(key) == [_result("1")]  # promoted from disk at age 9.9

    clock.advance(0.1)  # total age == ttl
    assert reader.get(key) is None


def test_disk_entry_expires_at_ttl(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    _disk_cache(tmp_path, clock=clock, ttl=10.0).put(key, [_result("1")])

    clock.advance(9.9)
    assert _disk_cache(tmp_path, clock=clock, ttl=10.0).get(key) == [_result("1")]

    clock.advance(0.1)  # age == ttl
    assert _disk_cache(tmp_path, clock=clock, ttl=10.0).get(key) is None


def test_disk_key_includes_config_fingerprint(tmp_path: Path) -> None:
    clock = _Clock()
    writer = _disk_cache(tmp_path, clock=clock)
    first = build_key("k", None, config_fingerprint="f1")
    second = build_key("k", None, config_fingerprint="f2")
    writer.put(first, [_result("1")])

    assert writer._disk_path(first) != writer._disk_path(second)
    assert _disk_cache(tmp_path, clock=clock).get(second) is None
    assert _disk_cache(tmp_path, clock=clock).get(first) == [_result("1")]


def test_disk_identity_matches_numeric_key_equality(tmp_path: Path) -> None:
    # Python compares ``5`` and ``5.0`` as equal cache keys, so the disk
    # identity must not split them into two files.
    clock = _Clock()
    int_timeout = build_key("k", None, 5, False, timeout=5)  # type: ignore[arg-type]
    float_timeout = build_key("k", None, 5, False, timeout=5.0)
    assert int_timeout == float_timeout
    _disk_cache(tmp_path, clock=clock).put(int_timeout, [_result("1")])
    assert _disk_cache(tmp_path, clock=clock).get(float_timeout) == [_result("1")]


def test_disk_payload_is_versioned_whitelisted_and_private(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    cache = _disk_cache(tmp_path, clock=clock)
    dirty = _result("1")
    dirty["rank_score"] = 0.93  # type: ignore[typeddict-unknown-key]
    dirty["match_tier"] = 2  # type: ignore[typeddict-unknown-key]
    cache.put(key, [dirty])

    path = cache._disk_path(key)
    payload = json.loads(path.read_bytes())
    assert payload["version"] == 1
    assert isinstance(payload["key"], str)
    assert payload["results"] == [_result("1")]
    assert set(payload["results"][0]) == {
        "site",
        "book_id",
        "book_url",
        "cover_url",
        "title",
        "author",
        "latest_chapter",
        "update_date",
        "word_count",
        "priority",
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_malformed_json_is_ignored_and_rebuilt(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [_result("1")])
    writer._disk_path(key).write_bytes(b"{ definitely not json")

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) is None
    reader.put(key, [_result("2")])
    assert _disk_cache(tmp_path, clock=clock).get(key) == [_result("2")]


@pytest.mark.parametrize(
    "name,corrupt", _CORRUPTIONS, ids=[name for name, _ in _CORRUPTIONS]
)
def test_corrupt_disk_payload_is_ignored_and_rebuilt(
    tmp_path: Path,
    name: str,
    corrupt: Callable[[dict[str, object]], object],
) -> None:
    clock = _Clock()
    key = _key()
    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [_result("1")])
    path = writer._disk_path(key)
    payload = json.loads(path.read_bytes())
    path.write_bytes(json.dumps(corrupt(payload)).encode("utf-8"))

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) is None
    reader.put(key, [_result("2")])
    assert _disk_cache(tmp_path, clock=clock).get(key) == [_result("2")]


def test_oversized_snapshot_stays_in_memory(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    cache = _disk_cache(tmp_path, clock=clock, max_disk_bytes=80)
    cache.put(key, [_result("1")])

    assert not list(tmp_path.glob("v1-*.json"))
    assert cache.get(key) == [_result("1")]
    assert _disk_cache(tmp_path, clock=clock, max_disk_bytes=80).get(key) is None


def test_oversized_disk_payload_is_ignored(tmp_path: Path) -> None:
    clock = _Clock()
    key = _key()
    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [_result("1")])
    path = writer._disk_path(key)
    valid = path.read_bytes()
    path.write_bytes(valid + b" " * 200)

    reader = _disk_cache(tmp_path, clock=clock, max_disk_bytes=len(valid) + 100)
    assert reader.get(key) is None


def test_disk_entry_limit_prunes_oldest(tmp_path: Path) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock=clock, max_disk_entries=2)
    for index in range(5):
        cache.put(_key(f"k{index}"), [_result(str(index))])

    assert len(list(tmp_path.glob("v1-*.json"))) <= 2
    # Pruning never drops the file just written.
    assert _disk_cache(tmp_path, clock=clock).get(_key("k4")) == [_result("4")]


def test_unwritable_disk_dir_is_a_silent_miss(tmp_path: Path) -> None:
    # A file where the cache directory should be makes every mkdir/write fail.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    clock = _Clock()
    key = _key()
    cache = _disk_cache(blocker / "cache", clock=clock)

    cache.put(key, [_result("1")])  # memory still works
    assert cache.get(key) == [_result("1")]
    assert _disk_cache(blocker / "cache", clock=clock).get(key) is None
    cache.clear()  # must not raise


def test_clear_removes_disk_payloads_but_not_unrelated_files(tmp_path: Path) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock=clock)
    key = _key()
    cache.put(key, [_result("1")])
    payload_path = cache._disk_path(key)
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    cache.clear()

    assert not payload_path.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert _disk_cache(tmp_path, clock=clock).get(key) is None


def test_concurrent_disk_puts_from_several_instances(tmp_path: Path) -> None:
    # Separate instances model separate processes: each has its own
    # same-process lock, so the only common ground is the atomic temp+replace
    # protocol.  Lost updates are allowed; corruption is not.
    clock = _Clock()
    key = _key("shared")
    caches = [_disk_cache(tmp_path, clock=clock) for _ in range(4)]
    barrier = threading.Barrier(len(caches))
    failures: list[Exception] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for step in range(40):
                caches[index].put(key, [_result(str(step))])
                caches[index].get(key)
        except Exception as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,)) for index in range(len(caches))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)

    files = list(tmp_path.glob("v1-*.json"))
    assert len(files) == 1
    json.loads(files[0].read_bytes())  # never half-written
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))

    loaded = _disk_cache(tmp_path, clock=clock).get(key)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["book_id"] in {str(step) for step in range(40)}


# --------------------------------------------------------------------------- #
# search.py integration (shared module singleton)
# --------------------------------------------------------------------------- #
class _CountingSearcher(BaseSearcher):
    """Searcher that counts fetches and always returns one result."""

    site_name = "counting"
    calls = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_result("1", site="counting")]


class _SecondSearcher(_CountingSearcher):
    """A distinct registered site, used to mutate the registry set."""

    site_name = "counting2"
    calls = 0

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_result("1", site="counting2")]


@pytest.fixture
def _wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import novel_downloader.plugins.search as search_mod

    search_mod._CACHE.clear()
    _CountingSearcher.calls = 0

    def _classes(
        sites: object = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return [_CountingSearcher]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    yield
    search_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_search_hits_cache_and_returns_isolated_list(_wired: None) -> None:
    first = await search("keyword")
    assert _CountingSearcher.calls == 1

    second = await search("keyword")
    assert _CountingSearcher.calls == 1  # served from cache
    assert second == first

    first.clear()  # mutating the earlier copy must not touch the cache
    third = await search("keyword")
    assert third == second
    assert _CountingSearcher.calls == 1


@pytest.mark.asyncio
async def test_search_stream_populates_then_hits_cache(_wired: None) -> None:
    streamed = [chunk async for chunk in search_stream("keyword")]
    assert _CountingSearcher.calls == 1
    assert streamed == [[_result("1", site="counting")]]

    cached = [chunk async for chunk in search_stream("keyword")]
    assert _CountingSearcher.calls == 1  # second run is a cache hit
    assert cached == streamed


@pytest.mark.asyncio
async def test_search_and_stream_share_cache(_wired: None) -> None:
    await search("keyword")
    assert _CountingSearcher.calls == 1

    chunks = [chunk async for chunk in search_stream("keyword")]
    assert _CountingSearcher.calls == 1  # search() warmed the shared cache
    assert chunks == [[_result("1", site="counting")]]


@pytest.mark.asyncio
async def test_punctuation_only_keywords_do_not_share_a_snapshot(
    _wired: None,
) -> None:
    # ``normalize_text`` erases both queries; the raw keyword must keep them
    # apart so one punctuation search cannot be served the other's snapshot.
    await search("!!!")
    await search("???")
    assert _CountingSearcher.calls == 2

    await search("!!!")
    assert _CountingSearcher.calls == 2  # identical raw query: cache hit


@pytest.mark.asyncio
async def test_aggregate_snapshot_invalidated_by_registry_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.plugins.search as search_mod

    search_mod._CACHE.clear()
    _CountingSearcher.calls = 0
    _SecondSearcher.calls = 0
    active: list[type[BaseSearcher]] = [_CountingSearcher]

    def _classes(
        sites: object = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return list(active)

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    monkeypatch.setattr(search_mod, "_browser_assisted_sites", lambda: frozenset())

    await search("keyword")
    await search("keyword")
    assert _CountingSearcher.calls == 1  # stable config: second call is a hit

    active.append(_SecondSearcher)
    await search("keyword")  # new participant => new fingerprint => refetch
    assert _CountingSearcher.calls == 2
    assert _SecondSearcher.calls == 1

    await search("keyword")
    assert _CountingSearcher.calls == 2  # same configuration again: cache hit
    assert _SecondSearcher.calls == 1

    search_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_stream_aggregate_snapshot_invalidated_by_registry_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.plugins.search as search_mod

    search_mod._CACHE.clear()
    _CountingSearcher.calls = 0
    _SecondSearcher.calls = 0
    active: list[type[BaseSearcher]] = [_CountingSearcher]

    def _classes(
        sites: object = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return list(active)

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    monkeypatch.setattr(search_mod, "_browser_assisted_sites", lambda: frozenset())

    first = [chunk async for chunk in search_stream("keyword")]
    assert _CountingSearcher.calls == 1
    assert {rows[0]["site"] for rows in first} == {"counting"}

    active.append(_SecondSearcher)
    second = [chunk async for chunk in search_stream("keyword")]
    assert _CountingSearcher.calls == 2  # refetched, not served from old key
    assert _SecondSearcher.calls == 1
    assert {rows[0]["site"] for rows in second} == {"counting", "counting2"}

    search_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_aggregate_snapshot_invalidated_by_browser_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.plugins.search as search_mod

    search_mod._CACHE.clear()
    _CountingSearcher.calls = 0
    browser: set[str] = set()

    def _classes(
        sites: object = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return [_CountingSearcher]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    monkeypatch.setattr(
        search_mod, "_browser_assisted_sites", lambda: frozenset(browser)
    )

    await search("keyword")
    await search("keyword")
    assert _CountingSearcher.calls == 1  # stable configuration: cache hit

    browser.add("elsewhere")  # hot settings change
    await search("keyword")
    assert _CountingSearcher.calls == 2

    await search("keyword")
    assert _CountingSearcher.calls == 2  # new configuration is now stable

    search_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_search_cache_is_isolated_by_timeout(_wired: None) -> None:
    # A sparse snapshot fetched with an aggressive budget must not serve a
    # caller that asked for a generous one.
    await search("keyword", timeout=5.0)
    assert _CountingSearcher.calls == 1

    await search("keyword", timeout=5.0)
    assert _CountingSearcher.calls == 1  # same budget: cache hit

    results = await search("keyword", timeout=60.0)
    assert results  # re-fetched, not served from the 5s-budget snapshot
    assert _CountingSearcher.calls == 2


@pytest.mark.asyncio
async def test_search_stream_cache_is_isolated_by_timeout(_wired: None) -> None:
    chunks = [chunk async for chunk in search_stream("keyword", timeout=5.0)]
    assert _CountingSearcher.calls == 1

    cached = [chunk async for chunk in search_stream("keyword", timeout=5.0)]
    assert _CountingSearcher.calls == 1  # same budget: cache hit
    assert cached == chunks

    refetched = [chunk async for chunk in search_stream("keyword", timeout=60.0)]
    assert refetched  # re-fetched, not a stale cache hit
    assert _CountingSearcher.calls == 2


@pytest.mark.asyncio
async def test_search_cache_is_isolated_by_per_site_limit_and_nsfw(
    _wired: None,
) -> None:
    # The snapshot is fetched with these two arguments, so they are part of the
    # cache identity: changing either must re-fetch instead of serving rows
    # gathered under a different limit / nsfw selection.
    await search("keyword", sites=["counting"])
    assert _CountingSearcher.calls == 1

    await search("keyword", sites=["counting"])
    assert _CountingSearcher.calls == 1  # identical query: cache hit

    await search("keyword", sites=["counting"], per_site_limit=9)
    assert _CountingSearcher.calls == 2  # different per-site limit

    await search("keyword", sites=["counting"], per_site_limit=9, nsfw=True)
    assert _CountingSearcher.calls == 3  # different nsfw flag


class _EmptySearcher(BaseSearcher):
    """Searcher that reports a (transient) empty result set."""

    site_name = "empty"
    calls = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


@pytest.fixture
def _wired_empty(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import novel_downloader.plugins.search as search_mod

    search_mod._CACHE.clear()
    _EmptySearcher.calls = 0

    def _classes(
        sites: object = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return [_EmptySearcher]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)
    yield
    search_mod._CACHE.clear()


@pytest.mark.asyncio
async def test_empty_search_results_are_not_cached(_wired_empty: None) -> None:
    # An empty aggregate usually means the sites failed transiently; pinning it
    # for the whole TTL would hide a recovery, so the next call must fetch again.
    assert await search("keyword") == []
    assert await search("keyword") == []
    assert _EmptySearcher.calls == 2

    import novel_downloader.plugins.search as search_mod

    assert search_mod._CACHE.get(build_key("keyword", None, 5, False, 5.0)) is None


@pytest.mark.asyncio
async def test_empty_stream_results_are_not_cached(_wired_empty: None) -> None:
    assert [chunk async for chunk in search_stream("keyword")] == []
    assert [chunk async for chunk in search_stream("keyword")] == []
    assert _EmptySearcher.calls == 2

    import novel_downloader.plugins.search as search_mod

    assert search_mod._CACHE.get(build_key("keyword", None, 5, False, 5.0)) is None
