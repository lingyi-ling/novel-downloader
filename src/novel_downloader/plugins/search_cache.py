#!/usr/bin/env python3
"""
novel_downloader.plugins.search_cache
-------------------------------------

Short-lived cache for aggregated search results.

Repeating the same query (for example re-running a search just before changing
the sort order) would otherwise re-hit every registered site. This module keeps
the merged, *unranked* results of recent queries for a small TTL so identical
searches can be served without any network I/O.

By default the cache is memory-only and performs **no file I/O at all**.  An
optional on-disk layer can be enabled for cross-process persistence (so a cold
start does not repeat every site request) by passing ``disk_dir`` or setting
the ``NOVELDL_SEARCH_CACHE_DIR`` environment variable.  The disk layer mirrors
the memory layer's rules: the same TTL, the same key (including the aggregate
configuration fingerprint), the same field whitelist, and no derived ranking
data.  Every disk write is atomic (temp file + ``os.replace``) and the file is
readable only by its owner (``0600`` on POSIX).  A missing, unreadable or
corrupt file is simply a miss; the next ``put`` rebuilds it.

The in-memory side is a pure data structure; the clock is injectable via
``now`` so TTL behaviour is deterministic in tests.  The disk side uses a
separate wall clock (``wall_clock``), because timestamps must be comparable
across processes and boots -- a monotonic clock is not.

The cache is a process-wide singleton in practice (``search._CACHE``), and app
front-ends may drive searches from more than one thread, each with its own
event loop.  Every public method therefore takes a lock: without it a
concurrent ``put`` could evict the entry ``get`` had just read, and
``OrderedDict.move_to_end`` would then raise ``KeyError`` out of a search.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import SearchResult

__all__ = [
    "DISK_DIR_ENV",
    "CacheKey",
    "CacheStats",
    "SearchCache",
    "build_key",
]

logger = logging.getLogger(__name__)

#: Environment variable naming the directory of the optional disk layer.  An
#: empty/unset value leaves :class:`SearchCache` memory-only (zero file I/O).
DISK_DIR_ENV = "NOVELDL_SEARCH_CACHE_DIR"

#: On-disk payload version.  It is part of the file name *and* of the payload,
#: so a format change cannot be misread as the previous format: files of an
#: older version are simply never looked up.
_DISK_FORMAT_VERSION = 1
_DISK_PREFIX = f"v{_DISK_FORMAT_VERSION}-"
_DISK_SUFFIX = ".json"

#: Fields of :class:`SearchResult` and their expected types.  Only these are
#: written to disk (and accepted back), so derived per-call annotations such as
#: ``rank_score`` / ``match_tier`` can never be persisted or resurrected.
_RESULT_STR_FIELDS = (
    "site",
    "book_id",
    "book_url",
    "cover_url",
    "title",
    "author",
    "latest_chapter",
    "update_date",
    "word_count",
)


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
    normalization erases the whole input we therefore keep the NFKC-folded raw
    keyword (stripped) instead; folding keeps full-width punctuation on the
    same identity as its half-width spelling, exactly like every other use of
    ``normalize_text``.  It is non-empty for any query the caller actually
    made, and callers short-circuit a truly empty keyword before searching.
    """
    normalized = normalize_text(keyword)
    if normalized:
        return normalized
    return unicodedata.normalize("NFKC", str(keyword)).strip()


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


def _key_canonical(key: CacheKey) -> str:
    """
    Return the stable JSON encoding that identifies ``key`` on disk.

    The full key -- keyword, site selection, query parameters and the aggregate
    configuration fingerprint -- is encoded with sorted keys and no optional
    whitespace.  It is both hashed into the file name and stored verbatim in
    the payload, so a hash collision or a hand-edited file is rejected by the
    comparison in :meth:`SearchCache._parse_disk_payload` instead of returning
    another query's rows.
    """
    # Numeric fields are canonicalized to their declared types: Python treats
    # ``5`` and ``5.0`` (and ``True`` and ``1``) as equal cache keys, so their
    # on-disk identity must not split into two files.
    return json.dumps(
        {
            "keyword": key.keyword,
            "sites": None if key.sites is None else list(key.sites),
            "per_site_limit": int(key.per_site_limit),
            "nsfw": bool(key.nsfw),
            "timeout": float(key.timeout),
            "config_fingerprint": key.config_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _key_digest(canonical: str) -> str:
    """Return the file-name digest of a canonical cache key."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_row(row: object) -> dict[str, object] | None:
    """
    Project one result row onto the persisted :class:`SearchResult` fields.

    Returns ``None`` when ``row`` is not a mapping or a required field has the
    wrong type.  Unknown keys -- notably the per-call ``rank_score`` and
    ``match_tier`` annotations -- are dropped, so what reaches the disk is the
    plain site snapshot and nothing derived from a particular call.
    """
    if not isinstance(row, dict):
        return None
    out: dict[str, object] = {}
    for field in _RESULT_STR_FIELDS:
        value = row.get(field)
        if not isinstance(value, str):
            return None
        out[field] = value
    priority = row.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        return None
    out["priority"] = priority
    return out


def _restore_row(row: object) -> SearchResult | None:
    """Validate one persisted row and return it as a :class:`SearchResult`."""
    restored = _snapshot_row(row)
    if restored is None:
        return None
    return cast("SearchResult", restored)


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
    A TTL + LRU cache of merged search results, optionally disk-backed.

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
    :param disk_dir: Optional directory for the persistent layer.  When
        ``None`` (the default), ``NOVELDL_SEARCH_CACHE_DIR`` is consulted; an
        unset/empty value keeps the cache memory-only and touches no file.
    :param max_disk_entries: Maximum number of payload files kept in
        ``disk_dir``; the oldest are pruned after a successful write.
    :param max_disk_bytes: Maximum size of one payload file.  Larger snapshots
        are kept in memory only (a larger file found on disk is ignored).
    :param wall_clock: Wall-clock source for persisted timestamps; defaults to
        :func:`time.time`.  It must be comparable across processes, unlike
        ``now``.
    """

    def __init__(
        self,
        ttl: float = 300.0,
        max_entries: int = 64,
        now: Callable[[], float] = time.monotonic,
        *,
        disk_dir: str | os.PathLike[str] | None = None,
        max_disk_entries: int = 256,
        max_disk_bytes: int = 2 * 1024 * 1024,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._now = now
        raw_dir = (
            os.fspath(disk_dir)
            if disk_dir is not None
            else os.environ.get(DISK_DIR_ENV, "")
        )
        raw_dir = raw_dir.strip()
        self._disk_dir = Path(raw_dir).expanduser() if raw_dir else None
        self._max_disk_entries = max(1, int(max_disk_entries))
        self._max_disk_bytes = max(1, int(max_disk_bytes))
        self._wall_clock = wall_clock
        # Ordered oldest (least recently used) first.
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def disk_dir(self) -> Path | None:
        """The persistent directory in use, or ``None`` for memory-only mode."""
        return self._disk_dir

    def _disk_path(self, key: CacheKey) -> Path:
        """Return the payload path for ``key`` (caller checked ``disk_dir``)."""
        directory = self._disk_dir
        if directory is None:  # pragma: no cover - public callers check first
            raise RuntimeError("disk cache is disabled")
        digest = _key_digest(_key_canonical(key))
        return directory / f"{_DISK_PREFIX}{digest}{_DISK_SUFFIX}"

    def _parse_disk_payload(
        self, payload: object, key: CacheKey
    ) -> tuple[list[SearchResult], float] | None:
        """
        Validate a decoded payload and return its rows and age, or ``None``.

        Every mismatch is a miss rather than an error: a wrong version, a key
        that does not match the file name, an expired timestamp, a non-list
        payload, or a single malformed row all mean "this file is not usable".
        The caller re-fetches and :meth:`put` overwrites it.

        The returned age (wall-clock seconds since the write) lets :meth:`get`
        give the promoted memory entry the *remaining* TTL instead of a fresh
        one, so disk promotion cannot extend an entry's lifetime.
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != _DISK_FORMAT_VERSION:
            return None
        try:
            canonical = _key_canonical(key)
        except (TypeError, ValueError):
            return None
        if payload.get("key") != canonical:
            return None
        stored_at = payload.get("stored_at")
        if not isinstance(stored_at, (int, float)) or isinstance(stored_at, bool):
            return None
        age = self._wall_clock() - float(stored_at)
        if age >= self._ttl:
            return None
        rows = payload.get("results")
        if not isinstance(rows, list):
            return None
        restored: list[SearchResult] = []
        for value in rows:
            row = _restore_row(value)
            if row is None:
                return None
            restored.append(row)
        return restored, age

    def _disk_get(self, key: CacheKey) -> tuple[list[SearchResult], float] | None:
        """Read ``key`` from disk, or return ``None`` on any kind of miss."""
        if self._disk_dir is None:
            return None
        try:
            path = self._disk_path(key)
        except (TypeError, ValueError):
            return None
        try:
            with path.open("rb") as handle:
                # Read one byte past the limit so an oversized file is detected
                # without loading an unbounded amount of data.
                raw = handle.read(self._max_disk_bytes + 1)
        except (OSError, ValueError):
            return None
        if len(raw) > self._max_disk_bytes:
            logger.debug("search cache: ignoring oversized payload %s", path.name)
            return None
        try:
            payload: object = json.loads(raw)
        except (ValueError, RecursionError):
            logger.debug("search cache: ignoring corrupt payload %s", path.name)
            return None
        return self._parse_disk_payload(payload, key)

    def _write_atomically(self, path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` via a same-directory temp + replace."""
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                # ``mkstemp`` already uses 0600, but make the guarantee explicit
                # (and independent of a permissive umask on other platforms).
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def _disk_prune(self, directory: Path, *, keep: Path) -> None:
        """
        Keep at most ``max_disk_entries`` payload files, oldest first.

        ``keep`` -- the file just written -- is never a victim, so a burst of
        writes cannot prune the entry the caller is about to serve.  Ties in
        mtime are broken by name, which keeps the behaviour deterministic.
        """
        found: list[tuple[int, str, Path]] = []
        try:
            candidates = list(directory.glob(f"{_DISK_PREFIX}*{_DISK_SUFFIX}"))
        except OSError:
            return
        for candidate in candidates:
            try:
                found.append((candidate.stat().st_mtime_ns, candidate.name, candidate))
            except OSError:
                continue
        excess = len(found) - self._max_disk_entries
        if excess <= 0:
            return
        found.sort(key=lambda item: (item[0], item[1]))
        victims = [item for item in found if item[2] != keep]
        for _, _, stale in victims[:excess]:
            with contextlib.suppress(OSError):
                stale.unlink()

    def _disk_put(self, key: CacheKey, results: Sequence[SearchResult]) -> None:
        """Persist ``results`` under ``key``; failures are silent misses."""
        if self._disk_dir is None:
            return
        rows: list[dict[str, object]] = []
        for result in results:
            row = _snapshot_row(result)
            if row is not None:
                rows.append(row)
        if results and not rows:
            return
        try:
            canonical = _key_canonical(key)
            data = json.dumps(
                {
                    "version": _DISK_FORMAT_VERSION,
                    "key": canonical,
                    "stored_at": self._wall_clock(),
                    "results": rows,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(data) > self._max_disk_bytes:
            logger.debug(
                "search cache: snapshot for one key exceeds %d bytes; "
                "keeping it in memory only",
                self._max_disk_bytes,
            )
            return
        directory = self._disk_dir
        path: Path | None = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._disk_path(key)
            self._write_atomically(path, data)
        except (OSError, ValueError):
            # An unwritable directory must never break a search: the entry is
            # still served from memory for the rest of this process.
            logger.debug("search cache: could not write %s", directory, exc_info=True)
            return
        if path is not None:
            self._disk_prune(directory, keep=path)

    def _disk_clear(self) -> None:
        """Best-effort removal of this cache format's payload files."""
        if self._disk_dir is None:
            return
        try:
            candidates = list(self._disk_dir.glob(f"{_DISK_PREFIX}*{_DISK_SUFFIX}"))
        except (OSError, ValueError):
            return
        for candidate in candidates:
            with contextlib.suppress(OSError):
                candidate.unlink()

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

    def _store_locked(
        self, key: CacheKey, results: Sequence[SearchResult], now: float
    ) -> None:
        """
        Insert ``results`` as the most recently used entry.

        The caller must hold the lock.  Expired entries are purged first and
        the oldest live ones are evicted until the cache fits ``max_entries``.
        """
        self._purge_expired(now)
        self._entries[key] = (now, list(results))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    def get(self, key: CacheKey) -> list[SearchResult] | None:
        """
        Return a copy of the cached results for ``key``, or ``None``.

        An entry whose age has reached ``ttl`` is treated as missing and
        removed. A successful lookup marks the entry as most recently used
        and counts as a hit; a missing or expired entry counts as a miss.

        When the persistent layer is enabled, a memory miss falls back to
        disk.  A valid disk payload is promoted into memory (so later calls in
        this process do not re-read the file) and counts as a hit.  Its
        freshness is checked against the persisted wall-clock timestamp, and
        the promoted entry keeps the *remaining* TTL rather than getting a
        fresh one.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                stored_at, results = entry
                if self._now() - stored_at < self._ttl:
                    self._hits += 1
                    self._entries.move_to_end(key)
                    return list(results)
                del self._entries[key]
            loaded = self._disk_get(key)
            if loaded is not None:
                results, age = loaded
                self._hits += 1
                # Inherit the disk entry's remaining lifetime.  ``age`` is
                # clamped at zero so a backwards wall-clock jump cannot push
                # the memory expiry into the future.
                self._store_locked(key, results, self._now() - max(age, 0.0))
                return list(results)
            self._misses += 1
            return None

    def put(self, key: CacheKey, results: Sequence[SearchResult]) -> None:
        """
        Store a snapshot of ``results`` under ``key``.

        The caller's sequence is copied, so later mutation of it cannot affect
        the cache. Expired entries are dropped first, so they never take a slot
        from a live entry. The entry becomes the most recently used one, and
        the oldest entries are evicted while the cache exceeds ``max_entries``.

        With the persistent layer enabled the same snapshot is also written to
        disk.  A write failure (unwritable directory, full disk) is ignored:
        the memory entry stays valid and the search is unaffected.
        """
        with self._lock:
            now = self._now()
            self._store_locked(key, results, now)
            self._disk_put(key, results)

    def clear(self) -> None:
        """Drop every entry, in memory and (when enabled) on disk.

        The usage counters are cumulative for the cache's lifetime and are
        deliberately left untouched, so a caller can clear entries without
        losing its hit/miss/eviction history.  Clearing the disk layer only
        removes payload files of this cache's format; unrelated files in the
        directory are left alone.
        """
        with self._lock:
            self._entries.clear()
            self._disk_clear()

    def stats(self) -> CacheStats:
        """Return a snapshot of the cumulative usage counters."""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )
