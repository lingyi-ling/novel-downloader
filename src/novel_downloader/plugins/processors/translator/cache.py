#!/usr/bin/env python3
"""
novel_downloader.plugins.processors.translator.cache
----------------------------------------------------

Process-local, thread-safe LRU cache shared by the translation processors.

Design notes
------------
* Entries live in memory only. Nothing is written to disk and no log line
  contains the source text, so translated chapter text disappears when the
  process exits. Keys are SHA-256 digests, not the raw text.
* Keys are ``engine|source|target|sha256(text)``: the same text translated by
  a different engine, from a different source language or into a different
  target language is a different entry.
* Only *successful* translations are inserted (the processors call
  :meth:`TranslationCache.put` after a 2xx response with a parsable payload),
  so timeouts, HTTP 4xx/5xx and malformed payloads are retried next time.
* ``max_text_length`` keeps very long paragraphs out of the cache: hashing and
  storing them is comparatively expensive and they rarely repeat. Units made
  of several ordinary paragraphs are still cached. Set it to ``0`` to cache
  any length.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SIZE = 4096
DEFAULT_CACHE_MAX_TEXT_LENGTH = 1000
DEFAULT_DEDUPE_MAX_LENGTH = 1000


def parse_int(value: Any, default: int, *, name: str) -> int:
    """Return ``value`` as an int, falling back to ``default`` when invalid."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r; falling back to the default (%d).", name, value, default
        )
        return default


def parse_ttl(value: Any, *, name: str = "cache_ttl") -> float | None:
    """
    Normalize a TTL in seconds.

    ``None`` (the default) means "never expire". Negative or malformed values
    are treated as "never expire" so a typo cannot silently disable caching.
    A value of ``0`` expires entries immediately, which is useful in tests.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; entries will not expire.", name, value)
        return None
    if seconds < 0:
        logger.warning(
            "Invalid %s=%r (negative); entries will not expire.", name, value
        )
        return None
    return seconds


def build_cache(
    engine: str,
    source: str,
    target: str,
    config: dict[str, Any],
) -> TranslationCache:
    """Build a cache from a processor ``config`` mapping."""
    return TranslationCache(
        engine=engine,
        source=source,
        target=target,
        maxsize=parse_int(
            config.get("cache_size"), DEFAULT_CACHE_SIZE, name="cache_size"
        ),
        ttl=parse_ttl(config.get("cache_ttl")),
        max_text_length=parse_int(
            config.get("cache_max_text_length"),
            DEFAULT_CACHE_MAX_TEXT_LENGTH,
            name="cache_max_text_length",
        ),
    )


def dedupe_settings(config: dict[str, Any]) -> tuple[bool, int]:
    """Return ``(enabled, max_paragraph_length)`` for within-batch dedupe."""
    enabled = bool(config.get("dedupe", True))
    max_length = parse_int(
        config.get("dedupe_max_length"),
        DEFAULT_DEDUPE_MAX_LENGTH,
        name="dedupe_max_length",
    )
    return enabled, max(0, max_length)


class _Flight:
    """Per-key lock used to coalesce concurrent requests for the same text."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


class TranslationCache:
    """
    A bounded LRU cache for translated text.

    All public methods are safe to call from multiple threads. ``get``/``put``
    are guarded by a single lock; ``single_flight`` additionally coalesces
    concurrent requests for the same key so a burst of duplicate work results
    in one outbound HTTP request.
    """

    def __init__(
        self,
        *,
        engine: str,
        source: str,
        target: str,
        maxsize: int = DEFAULT_CACHE_SIZE,
        ttl: float | None = None,
        max_text_length: int = DEFAULT_CACHE_MAX_TEXT_LENGTH,
    ) -> None:
        self._engine = engine
        self._source = source or "auto"
        self._target = target
        self._maxsize = max(0, int(maxsize))
        self._ttl = ttl
        self._max_text_length = max(0, int(max_text_length))

        self._entries: OrderedDict[str, tuple[str, float | None]] = OrderedDict()
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expired = 0

    @property
    def enabled(self) -> bool:
        """Whether the cache stores anything at all."""
        return self._maxsize > 0

    def make_key(self, text: str) -> str | None:
        """
        Build a cache key for ``text``.

        Returns ``None`` when caching is disabled or a paragraph of the text
        exceeds ``max_text_length``; callers should then bypass the cache
        entirely (no hit/miss accounting for skipped work). Checking each
        paragraph rather than the whole unit lets ordinary multi-paragraph
        chunks be cached while keeping very long paragraphs out.
        """
        if not self.enabled:
            return None
        if not within_length_limit(text, self._max_text_length):
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self._engine}|{self._source}|{self._target}|{digest}"

    def get(self, key: str) -> str | None:
        """Return the cached translation, or ``None`` on miss/expiry."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if expires_at is not None and now >= expires_at:
                del self._entries[key]
                self._expired += 1
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: str, value: str) -> None:
        """Insert/replace an entry, evicting the least recently used one."""
        if not self.enabled:
            return
        expires_at = None if self._ttl is None else time.monotonic() + self._ttl
        with self._lock:
            self._entries[key] = (value, expires_at)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)
                self._evictions += 1

    def clear(self) -> None:
        """Drop every entry and reset all counters."""
        with self._lock:
            self._entries.clear()
            self._flights.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._expired = 0

    @property
    def stats(self) -> dict[str, Any]:
        """Snapshot of cache counters (safe to read at any time)."""
        with self._lock:
            return {
                "engine": self._engine,
                "source": self._source,
                "target": self._target,
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
                "maxsize": self._maxsize,
                "evictions": self._evictions,
                "expired": self._expired,
                "ttl": self._ttl,
            }

    @contextmanager
    def single_flight(self, key: str) -> Iterator[None]:
        """
        Serialize work for one cache key.

        Callers re-check the cache inside this block; the first thread performs
        the request, any thread that was waiting behind it observes the cached
        value instead of issuing a duplicate request. The lock is per key, so
        unrelated translations still proceed in parallel.
        """
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
            flight.users += 1

        flight.lock.acquire()
        try:
            yield
        finally:
            flight.lock.release()
            with self._lock:
                flight.users -= 1
                if flight.users == 0 and self._flights.get(key) is flight:
                    del self._flights[key]


def translate_chunks(
    translate_one: Callable[[str], tuple[str, bool]],
    chunks: list[str],
    *,
    dedupe: bool,
    max_length: int,
) -> list[str]:
    """
    Translate ``chunks`` once per distinct eligible chunk.

    ``translate_one`` returns ``(translated_text, succeeded)``. Successful
    results are reused for identical chunks within this batch; failures are
    deliberately *not* reused so a later duplicate gets its own retry.

    A chunk is eligible for dedupe when every paragraph in it is at most
    ``max_length`` characters (``max_length <= 0`` means no limit). This keeps
    the historic behaviour for very long paragraphs, which the cache also
    leaves untouched.
    """
    seen: dict[str, str] = {}
    out: list[str] = []
    for chunk in chunks:
        eligible = dedupe and within_length_limit(chunk, max_length)
        if eligible:
            previous = seen.get(chunk)
            if previous is not None:
                out.append(previous)
                continue
        text, ok = translate_one(chunk)
        if eligible and ok:
            seen[chunk] = text
        out.append(text)
    return out


def within_length_limit(text: str, max_length: int) -> bool:
    """
    Whether every paragraph in ``text`` fits in ``max_length`` characters.

    ``max_length <= 0`` means "no limit". A translation unit made of several
    short paragraphs therefore qualifies even when its total length is large.
    """
    if max_length <= 0:
        return True
    lines = text.splitlines() or [text]
    return all(len(line) <= max_length for line in lines)
