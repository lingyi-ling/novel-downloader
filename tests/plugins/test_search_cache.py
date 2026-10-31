#!/usr/bin/env python3
"""Tests for the short-lived search-result cache."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import search, search_stream
from novel_downloader.plugins.search_cache import (
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
