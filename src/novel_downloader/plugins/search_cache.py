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

The cache is a process-wide singleton in practice (``search._CACHE``), and app
front-ends may drive searches from more than one thread, each with its own
event loop.  Every public method therefore takes a lock: without it a
concurrent ``put`` could evict the entry ``get`` had just read, and
``OrderedDict.move_to_end`` would then raise ``KeyError`` out of a search.
"""

from __future__ import annotations

import threading
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
    timeout: float = 5.0
    #: Fingerprint of the aggregate configuration (registered site set plus
    #: browser-assisted sites).  Only set for ``sites is None``: an explicit
    #: selection already names every site it depends on.
    config_fingerprint: str | None = None


def _keyword_identity(keyword: str) -> str:
    """
    Return the cache identity of ``keyword``.

    ``normalize_text`` strips punctuation and whitespace, so a query made only
    of punctuation collapses to ``""``.  Without a fallback ``"!!!"`` and
    ``"???"`` (and even an empty keyword) would all share one snapshot.  When
    normalization erases the whole input we therefore keep the raw keyword
    (stripped) instead; it is non-empty for any query the caller actually made,
    and callers short-circuit a truly empty keyword before searching.
    """
    normalized = normalize_text(keyword)
    if normalized:
        return normalized
    return str(keyword).strip()


def _normalize_site(site: object) -> str:
    """
    Normalize one site key the way the registry does.

    This mirrors the registry's ``PluginRegistry._normalize_key``: strip,
    lowercase, and -- because a module name cannot start with a digit -- prefix
    an ``"n"`` when the first character is one.  Without that prefix
    ``sites=["1foo"]`` and ``sites=["n1foo"]`` would build different cache keys
    although the registry resolves both to the same searcher, so the same query
    would be fetched twice.

    Empty keys are returned as ``""`` instead of raising: the registry rejects
    them at lookup time, but a cache key must stay constructible for any input.
    """
    key = str(site).strip().lower()
    if not key or not key[0].isdigit():
        return key
    return f"n{key}"


def build_key(
    keyword: str,
    sites: Sequence[str] | None,
    per_site_limit: int = 5,
    nsfw: bool = False,
    timeout: float = 5.0,
    config_fingerprint: str | None = None,
) -> CacheKey:
    """
    Build a normalized :class:`CacheKey` for a search query.

    ``keyword`` is normalized with :func:`ranking.normalize_text` (casefolded,
    punctuation/whitespace stripped).  A keyword that normalization reduces to
    nothing (pure punctuation such as ``"!!!"``) falls back to its stripped
    raw text, so different punctuation-only queries do not collide while
    ``"斗破 苍穹"`` and ``"斗破苍穹"`` still share one entry.  Each site key is
    normalized exactly like the registry resolves a selection: stripped,
    lowercased, and prefixed with ``"n"`` when it starts with a digit.
    ``["Qidian"]``, ``[" qidian "]`` and ``["qidian"]`` are therefore one
    identity, as are ``["1foo"]`` and ``["n1foo"]`` (both resolve to the same
    registered searcher).  The set is then sorted into a tuple so different
    orderings of the same site set collapse to one key. ``None`` (meaning "all
    registered sites") is kept distinct from an empty selection.

    ``timeout`` is part of the identity because it bounds how long slow sites
    may take: two otherwise identical searches with different budgets can
    produce different result sets (a short budget drops slow sites), so they
    must not share a cache entry.

    ``config_fingerprint`` identifies the registry/browser configuration an
    *aggregate* search (``sites is None``) ran against; see
    ``search._aggregate_config_fingerprint``.  It is ignored for an explicit
    site selection: that key already names every site it depends on, so a
    hot configuration change cannot alter its result set.

    Presentation/selection parameters (``sort``, ``order``, ``limit``,
    ``group`` and ``min_relevance``) are deliberately *not* part of the key:
    the cache stores the raw merged snapshot, and every call ranks, groups,
    filters and slices its own copy.
    """
    if sites is None:
        site_tuple: tuple[str, ...] | None = None
        fingerprint = config_fingerprint
    else:
        site_tuple = tuple(sorted({_normalize_site(site) for site in sites}))
        fingerprint = None
    return CacheKey(
        keyword=_keyword_identity(keyword),
        sites=site_tuple,
        per_site_limit=per_site_limit,
        nsfw=nsfw,
        timeout=timeout,
        config_fingerprint=fingerprint,
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

    All methods are safe to call from multiple threads.  Expired entries are
    dropped before capacity is applied on a write, so a dead entry can never
    take the slot that a live one needs.

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
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def _purge_expired(self, now: float) -> None:
        """
        Drop every entry whose age has reached ``ttl``.

        The caller must hold the lock.  ``get`` only expires the key it was
        asked for, so a stale entry that was touched just before its deadline
        can end up at the recently-used end of the order; purging explicitly
        before eviction keeps it from displacing a live entry.
        """
        for key, (stored_at, _) in list(self._entries.items()):
            if now - stored_at >= self._ttl:
                del self._entries[key]

    def get(self, key: CacheKey) -> list[SearchResult] | None:
        """
        Return a copy of the cached results for ``key``, or ``None``.

        An entry whose age has reached ``ttl`` is treated as missing and
        removed. A successful lookup marks the entry as most recently used
        and counts as a hit; a missing or expired entry counts as a miss.
        """
        with self._lock:
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
        the cache. Expired entries are dropped first, so they never take a slot
        from a live entry. The entry becomes the most recently used one, and
        the oldest entries are evicted while the cache exceeds ``max_entries``.
        """
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            self._entries[key] = (now, list(results))
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
        with self._lock:
            self._entries.clear()

    def stats(self) -> CacheStats:
        """Return a snapshot of the cumulative usage counters."""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )
