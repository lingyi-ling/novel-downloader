#!/usr/bin/env python3
"""
novel_downloader.plugins.search_cache
-------------------------------------

Short-lived, in-memory cache for aggregated search results.

Repeating the same query (for example re-running a search just before changing
the sort order) would otherwise re-hit every registered site. This module keeps
the merged, *unranked* results of recent queries for a small TTL so identical
searches can be served without any network I/O.

Everything here is a pure, side-effect-free data structure (no I/O, no async);
the clock is injectable via ``now`` so TTL behaviour is deterministic in tests.
As the cache is only ever touched from a single event loop, plain synchronous
containers are sufficient; no locks are used.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import SearchResult

__all__ = ["CacheKey", "CacheStats", "SearchCache", "build_key"]


@dataclass(frozen=True)
class CacheKey:
    """Identity of a cached query (order- and formatting-independent)."""

    keyword: str
    sites: tuple[str, ...] | None
    per_site_limit: int
    nsfw: bool


def build_key(
    keyword: str,
    sites: Sequence[str] | None,
    per_site_limit: int = 5,
    nsfw: bool = False,
) -> CacheKey:
    """
    Build a normalized :class:`CacheKey` for a search query.

    ``keyword`` is normalized with :func:`ranking.normalize_text` (casefolded,
    punctuation/whitespace stripped) and ``sites`` becomes a sorted tuple so
    different orderings of the same site set collapse to one key. ``None``
    (meaning "all registered sites") is kept distinct from an empty selection.
    """
    site_tuple: tuple[str, ...] | None = (
        None if sites is None else tuple(sorted({str(site) for site in sites}))
    )
    return CacheKey(
        keyword=normalize_text(keyword),
        sites=site_tuple,
        per_site_limit=per_site_limit,
        nsfw=nsfw,
    )


@dataclass(frozen=True)
class CacheStats:
    """Cumulative counters describing a :class:`SearchCache`'s usage."""

    hits: int
    misses: int
    evictions: int


#: A stored entry: the time it was written plus its result snapshot.
_Entry = tuple[float, list[SearchResult]]


class SearchCache:
    """
    A TTL + LRU cache of merged search results.

    Entries live for at most ``ttl`` seconds (measured with ``now``) and the
    cache holds at most ``max_entries`` of them; when full, the least recently
    used entry is evicted. Both :meth:`get` and :meth:`put` return / store
    copies, so callers can never mutate the cached payload in place.

    :param ttl: Time-to-live in seconds (``>= ttl`` counts as expired).
    :param max_entries: Maximum number of live entries before LRU eviction.
    :param now: Clock source; defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        ttl: float = 300.0,
        max_entries: int = 64,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._now = now
        # Ordered oldest (least recently used) first.
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: CacheKey) -> list[SearchResult] | None:
        """
        Return a copy of the cached results for ``key``, or ``None``.

        An entry whose age has reached ``ttl`` is treated as missing and
        removed. A successful lookup marks the entry as most recently used
        and counts as a hit; a missing or expired entry counts as a miss.
        """
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        stored_at, results = entry
        if self._now() - stored_at >= self._ttl:
            del self._entries[key]
            self._misses += 1
            return None
        self._hits += 1
        self._entries.move_to_end(key)
        return list(results)

    def put(self, key: CacheKey, results: Sequence[SearchResult]) -> None:
        """
        Store a snapshot of ``results`` under ``key``.

        The caller's sequence is copied, so later mutation of it cannot affect
        the cache. The entry becomes the most recently used one, and the oldest
        entries are evicted while the cache exceeds ``max_entries``.
        """
        self._entries[key] = (self._now(), list(results))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    def clear(self) -> None:
        """Drop every entry.

        The usage counters are cumulative for the cache's lifetime and are
        deliberately left untouched, so a caller can clear entries without
        losing its hit/miss/eviction history.
        """
        self._entries.clear()

    def stats(self) -> CacheStats:
        """Return a snapshot of the cumulative usage counters."""
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
        )
