#!/usr/bin/env python3
"""
novel_downloader.plugins.search
-------------------------------

"""

from __future__ import annotations

__all__ = [
    "GroupedSearchResult",
    "RankedSearchResult",
    "SearchEvent",
    "search",
    "search_stream",
]

import asyncio
import contextlib
import hashlib
import logging
import re
import unicodedata
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.grouping import group_results
from novel_downloader.plugins.ranking import (
    detect_query_intent,
    filter_by_relevance,
    match_tier_of,
    normalize_text,
    parse_update_date,
    parse_word_count,
    rank_score,
    sort_results,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search_cache import CacheKey, SearchCache, build_key
from novel_downloader.schemas import SearchResult

logger = logging.getLogger(__name__)


class GroupedSearchResult(SearchResult, total=False):
    """
    A merged same-book entry returned by :func:`search` with ``group=True``.

    The entry is the group's best source verbatim -- its ``site``/``book_id``/
    ``book_url`` fields already point at the best place to get the book -- plus
    one extra key:

    * ``sources`` -- every site key carrying the book, best first, duplicates
      dropped.  UIs render it as the row's source-site capsules and mark the
      first (best) one.

    The type stays assignment-compatible with :class:`SearchResult`, so a
    grouped list flows through every existing ``SearchResult`` consumer.
    """

    sources: list[str]


class RankedSearchResult(SearchResult, total=False):
    """
    A final :func:`search` row with its ranking telemetry attached.

    ``search`` adds two optional keys to every returned row (and to grouped
    merged rows); :func:`search_stream` deliberately does not, because a
    streamed chunk is a site's raw preview rather than a ranked final result:

    * ``rank_score`` -- the 0..1 quality score the row received for this call,
      i.e. :func:`~novel_downloader.plugins.ranking.rank_score` under the
      intent the call used.  For ``sort="quality"`` this is the score
      component of the ordering key (the match tier is the outer key);
    * ``match_tier`` -- the 0..4 kind-of-match tier of the keyword against the
      intent's primary field, or the better of the two fields under
      ``"either"`` (see
      :func:`~novel_downloader.plugins.ranking.match_tier_of`).  Under
      ``sort="quality"`` with a decided intent it is exactly the outer
      ordering key, so it explains why a sparse exact hit outranks a
      metadata-rich collision.

    Both keys are derived data, not part of the cached snapshot: they are
    attached to copies returned to the caller, and consumers that compare or
    persist rows can ignore them.  ``rank_score`` carries a small recency
    signal, so its value can drift slightly between calls; ``match_tier`` is
    fully deterministic for a given keyword and row.
    """

    rank_score: float
    match_tier: int


#: Per-host connection ceiling for the aggregate session. aiohttp defaults to
#: ``limit=100, limit_per_host=0`` (unlimited per host), so a fan-out of many
#: searchers could burst past a single site's tolerance.
_MAX_CONNECTIONS_PER_HOST = 8
_MAX_CONNECTIONS_TOTAL = 64

#: Reuse DNS answers for a whole search session instead of resolving the same
#: host once per request.
_DNS_CACHE_SECONDS = 300

#: Default relevance floor for ``sort="quality"``.  It is deliberately
#: conservative: unrelated rows that merely share a character or two with the
#: keyword score below 0.45, while genuine derivatives ("斗破苍穹之雷霆震碎")
#: score around 0.7 and survive.  See
#: :func:`~novel_downloader.plugins.ranking.filter_by_relevance`.
DEFAULT_MIN_RELEVANCE = 0.45

#: Upper bound accepted for a word-count filter.  Mirrors the REST schema's
#: ``MAX_SEARCH_WORDS``; real books are far below it, so a larger value is
#: either a unit mix-up or an attempt to describe "no limit" explicitly.
MAX_WORD_FILTER = 1_000_000_000

#: Schemes a ``book_url``/``cover_url`` may use.  Anything else --
#: ``javascript:``, ``file:``, ``data:``, ``vbscript:`` or a
#: scheme-less/relative string -- is cleared before the row reaches a UI that
#: might open it in a browser, load it as an image, or hand it to a downloader.
_SAFE_BOOK_URL_SCHEMES = frozenset({"http", "https"})

#: Longest URL kept on an output row.  Real result links are far shorter; a
#: multi-kilobyte value is either a broken extraction or an attempt to bloat
#: the cache/UI payload, and truncating it would produce a wrong link.
_MAX_URL_LENGTH = 2048

#: A site key is a registry key: it selects a plugin module, names the
#: cache/raw-data directory and appears in log lines.  Built-in keys are
#: lowercase module names (``qidian``, ``mangg_net``); dynamic rule sources may
#: also use ``-`` (``demo-api``).  Dots, separators and whitespace are refused
#: -- the registry refuses them too -- and every real key starts with a letter.
_SAFE_SITE_KEY_RE = re.compile(r"[a-z][a-z0-9_-]*")

#: Longest site key accepted; matches ``apps.api.schemas.MAX_SITE_KEY_LENGTH``.
_MAX_SITE_KEY_LENGTH = 64

#: Every book id a built-in site produces is ASCII letters/digits plus ``_`` or
#: ``-``: ``123``, ``0_143``, ``8-8717``, ``q-list-8156``,
#: ``95139-95139418``, ``fanrenxiuxianchuan``.  No real id contains whitespace,
#: dots, path separators or non-ASCII text, so a value outside this alphabet is
#: the id of no book and is rejected instead of being "repaired" into a
#: different one.
_SAFE_BOOK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

#: Longest book id accepted; matches ``apps.api.schemas._MAX_BOOK_ID_LENGTH``.
_MAX_BOOK_ID_LENGTH = 128

#: Unicode categories never allowed in a URL or a display field: control
#: characters (``Cc``, including NUL and ANSI's ESC), format characters
#: (``Cf``, e.g. bidi overrides and zero-width joiners) and surrogates (``Cs``,
#: which cannot be encoded as UTF-8).
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

#: ANSI escape sequences a terminal would execute: CSI (colors, cursor
#: movement) and OSC (window title, clipboard).  They are removed from display
#: text instead of being printed raw by a CLI.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC
)

#: Text fields carrying site-provided display strings.  They are output as
#: plain text; renderers escape markup themselves, so the aggregator only has
#: to guarantee there are no control sequences in them.
_TEXT_FIELDS = ("title", "author", "latest_chapter", "update_date", "word_count")

#: Upper bound (seconds) on how long cancelled site tasks may take to unwind
#: after the per-site deadline.  Cancellation is only a *request* -- on the
#: Windows proactor loop an in-flight socket or name-resolution operation can
#: delay its delivery -- so the aggregate waits for the corpses, but only for
#: this long: one stuck site must not stretch the search past its budget.
_ABANDON_GRACE = 0.35


def _make_connector() -> aiohttp.TCPConnector:
    """Connection pool shared by every site of one aggregate search."""
    return aiohttp.TCPConnector(
        limit=_MAX_CONNECTIONS_TOTAL,
        limit_per_host=_MAX_CONNECTIONS_PER_HOST,
        ttl_dns_cache=_DNS_CACHE_SECONDS,
    )


#: Process-wide short-lived cache shared by ``search`` and ``search_stream``.
#: It is memory-only unless ``NOVELDL_SEARCH_CACHE_DIR`` was set when this
#: module was imported; the disk layer is opt-in and never touches a file
#: otherwise.
_CACHE = SearchCache()

#: Browser-assisted sites are configured in ``settings.toml``.  The fallback
#: keeps the historical behaviour if the settings module cannot be imported.
_FALLBACK_BROWSER_SITES = frozenset({"fanqienovel"})

SearchEventKind = Literal[
    "browser_assisted",
    "browser_skipped",
    "completed",
    "failed",
]


@dataclass(frozen=True)
class SearchEvent:
    """
    Observable state transition of one site within a search.

    ``kind`` is one of:

    * ``"browser_assisted"`` — the site is configured for browser-assisted
      search and the caller selected it explicitly, so its browser fallback is
      enabled;
    * ``"browser_skipped"`` — the site is configured for browser-assisted
      search but the search is an aggregate ("all sites") one, so it was
      skipped to keep the search fast and window-free;
    * ``"completed"`` — the site's search task finished (``result_count`` is
      the number of results, possibly zero);
    * ``"failed"`` — the site's search task raised, returned a malformed
      payload, or could not even be prepared (constructor / ``nsfw`` flag);

    :param site: Site key.
    :param kind: Event kind (see above).
    :param message: Human-readable detail.
    :param result_count: Result count for ``"completed"`` events.
    """

    site: str
    kind: SearchEventKind
    message: str = ""
    result_count: int | None = None


@dataclass
class _SearchPlan:
    """Searcher instances chosen for one call, plus the browser-site lookup."""

    entries: list[tuple[str, BaseSearcher]]
    browser_sites: frozenset[str]


def _browser_assisted_sites() -> frozenset[str]:
    """Read the configured browser-assisted sites, tolerating config errors."""
    try:
        from novel_downloader.apps.browser.settings import browser_assisted_sites

        return frozenset(browser_assisted_sites())
    except Exception:
        logger.warning(
            "search: could not read browser_assisted_sites; "
            "falling back to the built-in default",
            exc_info=True,
        )
        return _FALLBACK_BROWSER_SITES


def _site_key(cls: type[BaseSearcher]) -> str:
    """Return the site key used by a searcher class."""
    name = getattr(cls, "site_name", "")
    if isinstance(name, str) and name.strip():
        return name.strip().lower()
    parts = cls.__module__.split(".")
    if len(parts) >= 2:
        return parts[-2].lower()
    return cls.__name__.lower()


def _aggregate_config_fingerprint(
    classes: Sequence[type[BaseSearcher]],
    browser_sites: frozenset[str],
) -> str:
    """
    Fingerprint the configuration an aggregate search (``sites=None``) depends on.

    Only the *participating* sites matter: the registry's searcher classes
    minus the browser-assisted ones (an aggregate skips those, see
    :func:`_prepare_searchers`).  The browser-assisted set is folded in
    separately so moving a site into or out of that set -- which changes
    whether it runs at all -- invalidates a cached snapshot too.

    The digest is short and opaque; it is never shown to users, only compared.
    """
    participating = sorted({_site_key(cls) for cls in classes} - browser_sites)
    payload = "\x1f".join(participating) + "\x1e" + ",".join(sorted(browser_sites))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_cache_key(
    keyword: str,
    sites: Sequence[str] | None,
    per_site_limit: int,
    nsfw: bool,
    timeout: float,
    *,
    classes: Sequence[type[BaseSearcher]],
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | None = None,
) -> CacheKey:
    """
    Build the cache key shared by :func:`search` and :func:`search_stream`.

    Explicit selections keep their historical identity (site set + query
    parameters).  An aggregate selection additionally carries the current
    configuration fingerprint, so a registry/browser-settings change does not
    keep serving a snapshot gathered from a different set of sites.  The
    optional local filters are part of the identity too: a filtered query is
    a different request even when the raw site snapshot is the same.
    """
    fingerprint = None
    if sites is None:
        fingerprint = _aggregate_config_fingerprint(classes, _browser_assisted_sites())
    return build_key(
        keyword,
        sites,
        per_site_limit,
        nsfw,
        timeout,
        config_fingerprint=fingerprint,
        min_words=min_words,
        max_words=max_words,
        updated_after=updated_after,
    )


async def _search_one(
    searcher: BaseSearcher,
    keyword: str,
    limit: int,
    budget: float,
) -> list[SearchResult]:
    """
    Run one site's search under a hard per-site deadline.

    ``aiohttp.ClientTimeout(total=...)`` only bounds a single HTTP request, so
    a searcher that issues several requests in sequence (pagination, detail
    enrichment) could otherwise run far past the caller's budget and hold the
    whole aggregate hostage.  ``wait_for`` cancels the site's coroutine at the
    deadline instead, which keeps an aggregate search at roughly ``budget``
    wall-clock seconds.

    :param searcher: The site's searcher instance.
    :param keyword: Search keyword.
    :param limit: Per-site result limit.
    :param budget: Per-site wall-clock budget in seconds.
    :return: The site's results.
    :raises TimeoutError: When the site misses the deadline.
    """
    try:
        return await asyncio.wait_for(
            searcher.search(keyword, limit=limit), timeout=budget
        )
    except TimeoutError as exc:
        raise TimeoutError(
            t("Search timed out after {budget:g}s").format(budget=budget)
        ) from exc


def _timeout_error(budget: float) -> TimeoutError:
    """Build the per-site deadline error reported to callers and events."""
    return TimeoutError(t("Search timed out after {budget:g}s").format(budget=budget))


def _cancel_pending(
    tasks: Iterable[asyncio.Task[Any]],
) -> set[asyncio.Task[Any]]:
    """Request cancellation for every unfinished task and return that set."""
    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()
    return pending


async def _drain(
    tasks: Iterable[asyncio.Task[Any]],
    deadline: float,
) -> None:
    """
    Wait -- bounded by an ``asyncio`` loop time -- for tasks to unwind.

    ``cancel()`` only *requests* cancellation: a task blocked in a socket
    operation may not observe it until the transport is closed (notably on the
    Windows proactor loop, where completion ports can delay delivery).  Closing
    the session immediately afterwards used to destroy such a task while it
    was still pending, which produced ``Task was destroyed but it is pending!``
    and ``Unclosed client session`` / ``Unclosed connector`` noise.  Waiting
    for the cancelled tasks before returning keeps those warnings at zero.

    Every wait is clipped to the time left before ``deadline``, so a task that
    ignores cancellation cannot stretch the caller's wall clock at all.  Tasks
    that are still pending at the deadline get a done-callback that will
    retrieve (and discard) their eventual exception, so they cannot leak an
    ``exception was never retrieved`` warning later.
    """
    loop = asyncio.get_running_loop()
    tasks = tuple(tasks)
    remaining = {task for task in tasks if not task.done()}
    while remaining:
        budget = deadline - loop.time()
        if budget <= 0:
            break
        await asyncio.wait(remaining, timeout=budget)
        remaining = {task for task in remaining if not task.done()}

    for task in tasks:
        if task.done():
            _consume_task(task)
        else:
            task.add_done_callback(_consume_task)


def _consume_task(task: asyncio.Task[Any]) -> None:
    """Retrieve (and discard) an abandoned task's outcome."""
    with contextlib.suppress(BaseException):
        task.exception()


def _emit(on_event: Callable[[SearchEvent], None] | None, event: SearchEvent) -> None:
    """Invoke an optional event callback, never letting it break a search."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        logger.debug("search: event callback failed", exc_info=True)


def _prepare_searchers(
    classes: Sequence[type[BaseSearcher]],
    session: aiohttp.ClientSession,
    *,
    sites: Sequence[str] | None,
    nsfw: bool,
    on_event: Callable[[SearchEvent], None] | None,
    browser_sites: frozenset[str] | None = None,
) -> _SearchPlan:
    """
    Instantiate the searchers to run and emit browser-assistance events.

    Aggregate searches (``sites is None``) skip configured browser-assisted
    sites: they would otherwise start a browser window for every keyword.
    Explicitly requested sites have their browser fallback enabled, which is
    still up to the site's own searcher.

    A class that appears more than once (the registry repeats it when the
    same site key is requested twice) is instantiated and searched once.

    A class that cannot even be instantiated -- or whose ``nsfw`` flag raises
    -- is reported as a per-site ``"failed"`` event instead of aborting the
    whole aggregate.
    """
    browser_sites = (
        _browser_assisted_sites() if browser_sites is None else browser_sites
    )
    explicit = sites is not None
    entries: list[tuple[str, BaseSearcher]] = []
    seen_classes: set[type[BaseSearcher]] = set()

    for cls in classes:
        if cls in seen_classes:
            logger.debug(
                "search: site %s requested more than once; searching it once",
                _site_key(cls),
            )
            continue
        seen_classes.add(cls)

        site = _site_key(cls)

        if not explicit and site in browser_sites:
            logger.info(
                "search: skipping browser-assisted site %s in aggregate search",
                site,
            )
            _emit(
                on_event,
                SearchEvent(
                    site,
                    "browser_skipped",
                    "browser-assisted site skipped; select it explicitly to enable",
                ),
            )
            continue

        try:
            instance = cls(session)
            nsfw_mismatch = instance.nsfw != nsfw
        except Exception as exc:
            # 一个连实例化都失败的 searcher（含 ``nsfw`` 属性抛异常）是
            # 单站故障：按 per-site "failed" 事件上报，而不是让整个聚合
            # 搜索在准备阶段就崩掉。
            logger.warning(
                "search: failed to prepare searcher for site %s: %s", site, exc
            )
            _emit(on_event, SearchEvent(site, "failed", str(exc)))
            continue

        if not explicit and nsfw_mismatch:
            continue

        if explicit:
            instance.explicitly_selected = True
            if site in browser_sites:
                logger.info(
                    "search: browser-assisted search enabled for %s "
                    "(explicitly selected)",
                    site,
                )
                _emit(
                    on_event,
                    SearchEvent(
                        site,
                        "browser_assisted",
                        "explicit selection enables browser-assisted search",
                    ),
                )

        entries.append((site, instance))

    return _SearchPlan(entries=entries, browser_sites=browser_sites)


def _invalid_payload_error(payload: object) -> TypeError:
    """The error reported when a site returns something other than a list."""
    return TypeError(
        f"searcher returned {type(payload).__name__} instead of a list of results"
    )


def _report_failure(
    site: str,
    exc: BaseException,
    browser_sites: frozenset[str],
    on_event: Callable[[SearchEvent], None] | None,
) -> None:
    """Log/report a failed per-site search task."""
    if site in browser_sites:
        logger.warning("search: browser-assisted site %s failed: %s", site, exc)
    else:
        logger.debug("search: site %s failed: %s", site, exc)
    _emit(on_event, SearchEvent(site, "failed", str(exc)))


def _report_completion(
    site: str,
    count: int,
    browser_sites: frozenset[str],
    on_event: Callable[[SearchEvent], None] | None,
) -> None:
    """Log/report a finished per-site search task."""
    message = ""
    if site in browser_sites:
        logger.info(
            "search: browser-assisted site %s returned %d result(s)", site, count
        )
        message = "browser-assisted search completed"
        if count == 0:
            message += (
                "; no results (check the browser logs and the site's login session)"
            )
    else:
        logger.debug("search: site %s returned %d result(s)", site, count)
    _emit(
        on_event,
        SearchEvent(site, "completed", message, result_count=count),
    )


def _merged_results(
    results: Sequence[SearchResult],
    keyword: str,
    *,
    groups: Sequence[Any] | None = None,
) -> list[GroupedSearchResult]:
    """
    Fold ranked ``results`` into one merged entry per book.

    Each group collapses onto its best entry (highest rank, earliest position
    on ties) enriched with ``sources``: the site keys of every source of the
    book, best first, duplicates dropped.  The input sequence is only read,
    so cached snapshots stay pristine.

    ``groups`` lets the caller pass groups it already built (and possibly
    filtered by relevance); when omitted they are computed here.
    """
    merged: list[GroupedSearchResult] = []
    for group in (
        groups if groups is not None else group_results(results, keyword=keyword)
    ):
        sites: list[str] = []
        for entry in group.sources:
            site = str(entry.get("site") or "")
            if site and site not in sites:
                sites.append(site)
        merged.append({**group.primary, "sources": sites})
    return merged


def _chunks_by_site(results: Sequence[SearchResult]) -> list[list[SearchResult]]:
    """
    Split a flat merged snapshot into per-site chunks, first-seen order.

    A cache hit must honour :func:`search_stream`'s chunk contract just like a
    fresh run: one list per site, never a single batch mixing every site.
    ``search()`` and ``search_stream`` both store results site by site, so the
    cached snapshot already consists of contiguous per-site runs; this groups
    them explicitly (and merges any accidental repeats) instead of relying on
    that layout.
    """
    buckets: dict[str, list[SearchResult]] = {}
    for result in results:
        site = str(result.get("site") or "")
        buckets.setdefault(site, []).append(result)
    return list(buckets.values())


def _has_unsafe_url_char(url: str) -> bool:
    """
    Return whether ``url`` contains a character no browser/parser agrees on.

    Whitespace and backslashes are rejected because Python's ``urlsplit`` and a
    browser disagree about them (``https://good.example\\@evil.example`` is two
    different hosts), and control/format characters are invisible or terminal
    instructions rather than URL syntax.
    """
    for char in url:
        if char == "\\" or char.isspace():
            return True
        if unicodedata.category(char) in _CONTROL_CATEGORIES:
            return True
    return False


def _safe_http_url(value: object, *, allow_protocol_relative: bool = False) -> str:
    """
    Return ``value`` when it is an absolute http(s) URL, else ``""``.

    Aggregated rows come from arbitrary site plugins (and, through the rule
    engine, from arbitrary page markup), so both ``book_url`` and ``cover_url``
    are untrusted input for every UI: a ``javascript:`` value handed to a
    WebView/browser helper would execute in the app's context, ``file:`` and
    ``data:`` would reach local or inline content, and a relative string would
    resolve against whichever page happens to host it.  Only ``http``/
    ``https`` URLs with a host and a sane port are therefore kept; everything
    else becomes an empty string (the row itself is kept unless it has no
    other identifier, see :func:`_sanitize_results`).

    ``allow_protocol_relative`` upgrades a ``//host/path`` cover to ``https:``
    instead of dropping it: that spelling is a real image URL, and the desktop
    renderer already prefixed it the same way.

    Over-long values, embedded whitespace/control characters and backslashes
    are rejected rather than truncated or "repaired": the id remains usable
    for the site-keyed download path, while a mangled URL does not.

    The string is stripped but otherwise returned verbatim, so a legitimate
    URL keeps its exact form (including its query and fragment).
    """
    url = str(value if value is not None else "").strip()
    if not url:
        return ""
    if allow_protocol_relative and url.startswith("//"):
        url = f"https:{url}"
    if len(url) > _MAX_URL_LENGTH:
        return ""
    if _has_unsafe_url_char(url):
        return ""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        # ``urlsplit`` raises on malformed IPv6 literals; the ``hostname`` and
        # ``port`` properties raise on invalid hosts and non-numeric/out-of-
        # range ports.
        return ""
    if parsed.scheme.lower() not in _SAFE_BOOK_URL_SCHEMES:
        return ""
    if not parsed.netloc or not host:
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return url


def _plain_text(value: object) -> str:
    """
    Return ``value`` as a display-safe string.

    ANSI escape sequences are removed (a terminal would otherwise execute
    them), and control/format characters are dropped so a title cannot inject
    line breaks, NUL bytes or bidi overrides into a UI, a log line or a JSON
    payload.  Horizontal whitespace controls become a single space; every
    other control character disappears.  Markup such as ``<b>`` is left as
    literal text -- escaping is each renderer's job, and the aggregator never
    treats a title or author as HTML.

    ``None`` becomes ``""`` instead of the string ``"None"``.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    # Fast path: ``str.isprintable`` is true exactly when the string contains
    # no Cc/Cf/Cs (and no other separator than the ASCII space).  Such a
    # string has no ANSI escape and no character the loop below would drop or
    # rewrite, so ``strip()`` alone reproduces the slow path.  It also covers
    # the common case -- CJK titles, authors and dates -- without allocating
    # the per-character list, which used to dominate this function at scale.
    if text.isprintable():
        return text.strip()
    text = _ANSI_ESCAPE_RE.sub("", text)
    chars: list[str] = []
    for char in text:
        # ``isprintable`` is the cheap C-level pre-check; only characters it
        # rejects need the (much slower) category lookup.  The branches below
        # are the exact complement of the old ``category in _CONTROL_CATEGORIES``
        # test: printable characters and non-control separators/private-use
        # characters are kept, tabs/newlines become one space, and every other
        # control/format/surrogate character is dropped.
        if char.isprintable():
            chars.append(char)
        elif char in "\t\n\r\f\v":
            chars.append(" ")
        elif unicodedata.category(char) in _CONTROL_CATEGORIES:
            continue
        else:
            chars.append(char)
    return "".join(chars).strip()


def _safe_site_key(value: object) -> str:
    """
    Return ``value`` when it is usable as a site key, else ``""``.

    ``site`` is a registry key, not a display string: it is interpolated into
    an import path and joined onto the cache/raw-data roots.  Rewriting it
    would route the row to a different site (or no site at all), so an unsafe
    value invalidates the row instead.

    A value is accepted when it is a lowercase slug (built-in keys and most
    dynamic rule sources) *or* the registry already knows it as a searcher
    (a rule source may choose a non-ASCII key).  In both cases it must be
    non-empty, at most :data:`_MAX_SITE_KEY_LENGTH` characters, and free of
    whitespace, control/format characters, path separators, dot segments and
    Windows-reserved characters (``<>:"|?*``).  Dots are not part of either
    form: the registry refuses them, and ``.``/``..`` would escape (or Windows
    would strip them from) the directory the key is joined onto.
    """
    key = "" if value is None else str(value)
    if not key or len(key) > _MAX_SITE_KEY_LENGTH:
        return ""
    if any(
        char.isspace() or unicodedata.category(char) in _CONTROL_CATEGORIES
        for char in key
    ):
        return ""
    if (
        "/" in key
        or "\\" in key
        or "." in key
        or any(char in key for char in '<>:"|?*')
    ):
        # Path separators, dot segments and Windows-reserved characters make
        # the key unjoinable onto the cache/raw-data root; the registry
        # refuses dots separators too, but this check does not depend on it.
        return ""
    if _SAFE_SITE_KEY_RE.fullmatch(key):
        return key
    try:
        registered = registrar.get_registered(key, "searcher") is not None
    except ValueError:
        # The registry rejects dots/whitespace/empties while normalizing.
        return ""
    return key if registered else ""


def _safe_book_id(value: object) -> str:
    """
    Return ``value`` when it is a usable book id, else ``""``.

    An empty or whitespace-only id returns ``""`` -- a row may still carry a
    usable ``book_url``, and the caller keeps that historical behaviour.  A
    *non-empty* value that fails the whitelist has no sensible replacement:
    truncating it would name another book and clearing it would turn a usable
    row into a URL-only one, so it is rejected by returning ``""`` as well and
    :func:`_sanitize_results` drops the row.

    A real id is ASCII letters/digits plus ``_``/``-`` (see
    :data:`_SAFE_BOOK_ID_RE`); it never needs stripping, so a padded value is
    treated as invalid rather than normalized.
    """
    raw = "" if value is None else str(value)
    if not raw.strip():
        return ""
    if len(raw) > _MAX_BOOK_ID_LENGTH:
        return ""
    if not _SAFE_BOOK_ID_RE.fullmatch(raw):
        return ""
    return raw


def _url_identity(url: str) -> str:
    """
    Return the identity of a sanitized http(s) URL for duplicate folding.

    Two spellings of the same page differ in scheme/host case, a default port,
    a trailing dot on the host, an IDN host written as Unicode or punycode, a
    fragment, a trailing slash or the order of query parameters; none of those
    make a different book.  The path's case is preserved because servers may
    treat it as significant, and query *values* are never discarded (two
    different ids are two different books).
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return url
    scheme = parsed.scheme.lower()
    if port is not None and (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        port = None
    host = host.rstrip(".")
    if host:
        with contextlib.suppress(UnicodeError):
            host = host.encode("idna").decode("ascii")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return "\x1f".join(
        (
            scheme,
            host,
            "" if port is None else str(port),
            parsed.path.rstrip("/"),
            query,
        )
    )


def _sanitize_results(results: list[SearchResult]) -> list[SearchResult]:
    """
    Drop unusable aggregate rows and neutralize unsafe output values.

    This runs first in :func:`_finalize`, before de-duplication, ranking,
    grouping and slicing, so a dirty row can never consume a ``limit`` slot,
    win a group's primary position, or influence the relevance floor.  The
    streaming path applies the same function to every chunk (see
    :func:`_sanitize_chunk`).

    Rules (deliberately minimal -- anything else is left untouched):

    * a row whose ``site`` is empty/blank, or is not a registered/safe key
      (control characters, whitespace, path separators, dots, overlong), is
      dropped: it cannot be routed to a plugin nor joined onto a directory
      safely (see :func:`_safe_site_key`);
    * a row whose non-empty ``book_id`` is not a real id (control characters,
      whitespace, path separators, dots, non-ASCII, overlong) is dropped
      rather than rewritten: the id is a route and a raw-data directory name
      (see :func:`_safe_book_id`);
    * a row with neither a usable ``book_id`` nor a usable ``book_url`` is
      dropped: it names no book at all (a ``cover_url`` alone is not an
      identity); a row with a blank ``book_id`` but a usable ``book_url`` is
      still kept, because that is the historical partial-row contract;
    * a non-http(s) ``book_url`` is cleared.  A row that still has a
      ``book_id`` is kept -- it is usable for the site-keyed download path --
      but its URL will not be opened anywhere;
    * a non-http(s) ``cover_url`` is cleared the same way; a protocol-relative
      cover (``//cdn.example.com/c.jpg``) is upgraded to ``https:`` because it
      names a real image;
    * a URL longer than :data:`_MAX_URL_LENGTH`, or containing whitespace,
      control characters or a backslash, is cleared instead of truncated;
    * display text (title, author, latest chapter, update date, word count)
      loses ANSI escape sequences and control/format characters.

    Rows are not mutated: a row that needs a value cleared is copied, so a
    cached snapshot stays pristine.  The input list is returned unchanged
    when nothing needed cleaning.
    """
    cleaned: list[SearchResult] = []
    changed = False
    for result in results:
        # Reading through a plain mapping keeps the field loops below typed:
        # ``SearchResult`` requires literal keys, the loops name them by list.
        row = cast("dict[str, object]", result)
        site = _safe_site_key(row.get("site"))
        raw_book_id = "" if row.get("book_id") is None else str(row.get("book_id"))
        book_id = _safe_book_id(raw_book_id)
        # A non-empty id that failed the whitelist is a bad identity, not an
        # absent one: clearing it would keep a row nobody can download.
        bad_book_id = bool(raw_book_id.strip()) and not book_id
        replacements: dict[str, str] = {}
        for field, allow_relative in (
            ("book_url", False),
            ("cover_url", True),
        ):
            # Only rewrite fields the row actually carries: a partial row is
            # left partial (its type contract is the searcher's business), and
            # it can still be sorted in place without a copy.
            if field not in row:
                continue
            stored = row[field]
            raw = "" if stored is None else str(stored)
            safe = _safe_http_url(raw, allow_protocol_relative=allow_relative)
            if safe != stored:
                replacements[field] = safe
        for field in _TEXT_FIELDS:
            if field not in row:
                continue
            stored = row[field]
            plain = _plain_text(stored)
            if plain != stored:
                replacements[field] = plain

        raw_book_url = row.get("book_url")
        book_url = replacements.get(
            "book_url", "" if raw_book_url is None else str(raw_book_url)
        )
        if not site or bad_book_id or (not book_id and not book_url):
            changed = True
            continue
        if replacements:
            result = cast("SearchResult", {**result, **replacements})
            changed = True
        cleaned.append(result)

    if not changed:
        return results
    if len(cleaned) != len(results):
        logger.debug(
            "search: dropped %d unusable aggregate row(s)",
            len(results) - len(cleaned),
        )
    return cleaned


def _dedupe_exact(results: list[SearchResult]) -> list[SearchResult]:
    """
    Fold exact same-book duplicates, keeping the first occurrence.

    Two rows are the same book when they come from the same site and either
    share a non-empty ``book_id``, share both a normalized title and the same
    ``book_url``, or -- for rows without an id -- share the same normalized
    title and author.  The URL rule folds a site that lists one page twice
    under different (or missing) ids; it deliberately also requires the same
    title, so two entries pointing at one omnibus page are not fused.  URLs
    are compared by :func:`_url_identity`, so case in the scheme/host, a
    default port, a fragment, a trailing slash or the order of query
    parameters do not turn one page into two rows.  Cross-site copies are
    deliberately *not* folded here: that is what ``group=True`` is for.

    ``book_url`` is expected to be sanitized already (see
    :func:`_sanitize_results`): a cleared URL never matches another row's.

    The input list is returned unchanged when nothing was duplicated, so a
    fresh cache snapshot is only copied when it actually needs a copy.
    """
    seen_ids: set[tuple[str, str]] = set()
    seen_urls: set[tuple[str, str, str]] = set()
    seen_titles: set[tuple[str, str, str]] = set()
    kept: list[SearchResult] = []
    for result in results:
        site = str(result.get("site") or "")
        book_id = str(result.get("book_id") or "")
        book_url = str(result.get("book_url") or "")
        title = normalize_text(result.get("title"))
        author = normalize_text(result.get("author"))

        if book_id:
            key = (site, book_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)
        if book_url:
            url_key = (site, title, _url_identity(book_url))
            if url_key in seen_urls:
                continue
            seen_urls.add(url_key)
        if not book_id and title:
            title_key = (site, title, author)
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
        kept.append(result)

    if len(kept) == len(results):
        return results
    logger.debug(
        "search: dropped %d duplicate row(s) from the aggregate",
        len(results) - len(kept),
    )
    return kept


def _sanitize_chunk(chunk: list[SearchResult]) -> list[SearchResult]:
    """
    Apply the final-result cleaning to one streamed per-site chunk.

    ``search_stream`` yields raw site payloads as soon as a site finishes, but
    a progressive UI renders those rows immediately -- before ``search`` would
    ever finalize them.  Running the same first two steps of
    :func:`_finalize` here keeps the preview honest:

    * :func:`_sanitize_results` drops rows with an unsafe site key or book id
      and rows that name no book at all, clears non-http(s)
      ``book_url``/``cover_url`` values (so a ``javascript:`` link never
      reaches a WebView), removes control characters from display text, and
      folds URL spellings that name one page;
    * :func:`_dedupe_exact` folds same-site duplicates.  Its keys are all
      site-scoped, so applying it to a single-site chunk drops exactly the
      rows :func:`_finalize` would drop for that site -- no cross-site
      grouping happens in the stream.

    Ranking, relevance filtering and ``limit`` are deliberately *not* applied:
    the chunk contract stays "this site's rows, unranked", except that a chunk
    left empty by cleaning is not yielded at all.

    Rows are never mutated: a row whose URL must be cleared is copied, so the
    raw cached snapshot stays pristine.
    """
    return _dedupe_exact(_sanitize_results(chunk))


def _annotate_ranked(
    results: list[SearchResult],
    *,
    keyword: str,
    intent: str,
    now: datetime,
) -> list[SearchResult]:
    """
    Return copies of ``results`` carrying ``rank_score`` and ``match_tier``.

    A fresh dict per row is essential: the input list may be -- or point
    into -- the cached snapshot, and :class:`SearchCache` copies only the
    list, not the row dicts.  Writing the fields in place would leak derived
    data into the cache, where a later call would treat it as if the site had
    returned it (and grouping could then be handed stale annotations).

    The scores are computed here, after filtering/grouping/slicing, so only
    rows the caller actually receives are scored, with the same ``now`` the
    ranking used (all rows of one call are therefore comparable).
    """
    annotated: list[SearchResult] = []
    for result in results:
        row = cast(
            "RankedSearchResult",
            {
                **result,
                "rank_score": round(
                    rank_score(result, keyword, intent=intent, now=now), 6
                ),
                "match_tier": match_tier_of(result, keyword, intent=intent),
            },
        )
        annotated.append(row)
    return annotated


#: The only accepted spelling of an ``updated_after`` filter.  A date object
#: (from the REST schema or the CLI) is already unambiguous; a string must be
#: ``YYYY-MM-DD`` so ``2024/01/01`` and ``20240101`` are rejected by callers
#: instead of being interpreted differently by different platforms.
_FILTER_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _coerce_updated_after(value: object) -> date | None:
    """
    Normalize an ``updated_after`` filter to a :class:`datetime.date`.

    ``None`` means "no date filter".  A ``date``/``datetime`` is accepted as
    is (a datetime keeps its calendar date), and a string must be
    ``YYYY-MM-DD``.  Anything else is a programming error and raises
    :class:`ValueError`.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not _FILTER_DATE_RE.fullmatch(text):
        raise ValueError("updated_after must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("updated_after must be a valid YYYY-MM-DD date") from exc


def _filter_results(
    results: Sequence[SearchResult],
    *,
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | None = None,
    now: datetime | None = None,
) -> list[SearchResult]:
    """
    Apply the optional local filters to already cleaned/ranked rows.

    A row survives only when every active filter accepts it:

    * ``min_words`` / ``max_words`` compare the parsed numeric word count
      (:func:`~novel_downloader.plugins.ranking.parse_word_count`), so
      ``"30万字"`` and ``"1,234"`` mean the same scale as the ranking uses;
    * ``updated_after`` keeps rows whose parsed update date is on or after the
      boundary.  It is a *lower bound*, so a row updated on that same day
      passes.

    Rows whose field is missing or unparseable are dropped whenever the
    corresponding filter is active; with no filter the row is untouched.  The
    input sequence is only read, and the returned list is fresh, so a cached
    snapshot is never mutated.  ``now`` anchors relative dates such as
    ``"3天前"``; callers pass one instant for the whole call.
    """
    if min_words is None and max_words is None and updated_after is None:
        return list(results)

    current = now or datetime.now()
    kept: list[SearchResult] = []
    for result in results:
        if min_words is not None or max_words is not None:
            words = parse_word_count(result.get("word_count"))
            if words is None:
                continue
            if min_words is not None and words < min_words:
                continue
            if max_words is not None and words > max_words:
                continue
        if updated_after is not None:
            updated = parse_update_date(result.get("update_date"), now=current)
            if updated is None or updated.date() < updated_after:
                continue
        kept.append(result)
    return kept


def _finalize(
    results: list[SearchResult],
    *,
    keyword: str,
    sort: str,
    order: str | None,
    limit: int | None,
    group: bool,
    min_relevance: float | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | None = None,
) -> list[SearchResult]:
    """
    Rank ``results`` for this caller, then filter, group and/or slice.

    The steps run in this order:

    1. unusable rows are dropped, unsafe ``book_url``/``cover_url`` values are
       cleared and display text is stripped of control sequences (see
       :func:`_sanitize_results`); this happens before every other step, so a
       dirty row can neither consume a ``limit`` slot nor become a group's
       primary;
    2. exact duplicate rows are folded (see :func:`_dedupe_exact`);
    3. the list is ranked (grouping and limiting both build on that order);
    4. the optional local filters (word count, update date) are applied to the
       ranked rows, before grouping and before ``limit`` (see
       :func:`_filter_results`).  Rows missing a field a filter reads are
       dropped only while that filter is active;
    5. with ``group=True`` the rows are grouped, and the relevance floor is
       applied to each book's primary row (a mirror site's alternate entry
       may look irrelevant on its own while the book is clearly relevant);
       without grouping the floor is applied to the rows themselves
       (``sort="quality"`` only, see
       :func:`~novel_downloader.plugins.ranking.filter_by_relevance`);
    6. ``limit`` is applied last, so with ``group=True`` it counts books
       rather than raw rows;
    7. every surviving row is annotated with ``rank_score`` and
       ``match_tier`` (see :class:`RankedSearchResult`) on a *copy*: rows are
       never mutated, so the cached snapshot and any caller-owned input stay
       free of derived fields.

    Filtering happens per call and never rewrites the cached snapshot: the
    filtered subset is a fresh list, while ranked order is re-applied on every
    call anyway.

    Ranking is intent-aware: before scoring we decide whether the keyword
    looks more like a title or an author (see
    :func:`~novel_downloader.plugins.ranking.detect_query_intent`).  For
    ``by="quality"`` this stops a book whose *title* merely contains an
    author's name from outranking that author's own books: the primary
    field's match tier is the outer sort key, so a sparse-metadata author hit
    still beats a metadata-rich collision.  The intent is resolved to
    ``"either"`` — the historical behaviour — whenever the evidence is too
    weak, and other sort keys ignore it entirely.
    """
    work = _sanitize_results(results)
    work = _dedupe_exact(work)
    current = datetime.now()
    intent = detect_query_intent(keyword, work) if sort == "quality" else "either"
    sort_results(
        work, by=sort, order=order, keyword=keyword, intent=intent, now=current
    )
    # Local filters run on the ranked rows (order is preserved), before
    # grouping and ``limit``: a filtered-out row must not consume a book slot
    # nor a limit slot.
    work = _filter_results(
        work,
        min_words=min_words,
        max_words=max_words,
        updated_after=updated_after,
        now=current,
    )

    final: list[SearchResult]
    if group:
        # Group first, then apply the relevance floor to each book's *primary*
        # row.  Filtering raw rows first would drop alternate sources whose
        # title does not literally contain the keyword (an author search keeps
        # the author's books under ``author`` intent, but a mirror site's row
        # may carry an empty author field and would look irrelevant on its
        # own).  The group survives as long as its best row is relevant.
        # ``now`` is forwarded so the primary choice and the annotations below
        # are scored against one instant.
        # ``intent`` is forwarded so the grouped row's primary is the same
        # ranking the flat list used; without it ``group_results`` re-scores
        # with ``"either"`` and can pick a different (worse) primary.
        groups = group_results(work, keyword=keyword, now=current, intent=intent)
        if sort == "quality" and min_relevance is not None:
            primaries = filter_by_relevance(
                [group.primary for group in groups],
                keyword,
                intent=intent,
                min_relevance=min_relevance,
            )
            keep = {id(primary) for primary in primaries}
            groups = [group for group in groups if id(group.primary) in keep]
        merged = _merged_results(work, keyword, groups=groups)
        if limit is not None:
            merged = merged[:limit]
        final = cast("list[SearchResult]", merged)
    else:
        if sort == "quality" and min_relevance is not None:
            # Only quality sorting promises relevance; an explicit "words" or
            # "update" ordering must not silently lose rows.
            work = filter_by_relevance(
                work, keyword, intent=intent, min_relevance=min_relevance
            )
        final = work[:limit] if limit is not None else work

    return _annotate_ranked(final, keyword=keyword, intent=intent, now=current)


async def search(
    keyword: str,
    sites: Sequence[str] | None = None,
    limit: int | None = None,
    per_site_limit: int = 5,
    timeout: float = 5.0,
    nsfw: bool = False,
    sort: str = "quality",
    order: str | None = None,
    *,
    on_event: Callable[[SearchEvent], None] | None = None,
    group: bool = False,
    min_relevance: float | None = DEFAULT_MIN_RELEVANCE,
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | str | None = None,
) -> list[SearchResult]:
    """
    Perform a search for the given keyword across one or more registered sites,
    then aggregate and rank the results.

    By default the results are ordered by an estimated quality score (keyword
    relevance, word count, update recency, site priority and completeness); pass
    ``sort="priority"`` to fall back to the legacy per-site priority ordering.

    :param keyword: The search term or keyword to query.
    :param sites: An optional sequence of site keys to limit which searchers.
        ``None`` means all registered sites; an empty sequence searches no
        site at all.
    :param limit: Maximum total number of results to return (applied after
        ranking, and after grouping when ``group=True``); if None, return all.
    :param per_site_limit: Maximum number of search results per site.
    :param timeout: Wall-clock budget per site (seconds); a site that misses
        it is dropped instead of holding the aggregate
    :param nsfw: Whether to include NSFW searchers.
    :param sort: Ranking key (see ``ranking.SORT_KEYS``); default ``"quality"``.
    :param order: ``"asc"``/``"desc"``; defaults to the key's natural direction.
    :param on_event: Optional observer for per-site search states (browser
        assistance, completion, failure).  Exceptions it raises are ignored.
    :param group: Fold same-book results into one merged entry per book (see
        :class:`GroupedSearchResult`); off by default, and never applied to
        :func:`search_stream`, whose per-site chunks stay flat.
    :param min_relevance: Relevance floor (0..1) applied when
        ``sort="quality"``.  Without grouping it is applied to the rows before
        ``limit``; with ``group=True`` the rows are grouped first and the floor
        is applied to each book's primary row (a mirror site's alternate entry
        may look irrelevant on its own while the book is clearly relevant).
        Rows/books that fall below it are dropped, except that the result set
        is never emptied: if nothing clears the floor, entries with any
        positive relevance are kept, and if none has any, the input is
        returned unchanged.  Pass ``None`` to keep every row (e.g. when the
        caller sorts by word count or update date).  Ignored for other sort
        keys.
    :param min_words: Optional lower bound on the parsed word count.  Rows
        without a parseable word count are dropped while this is set.
    :param max_words: Optional upper bound on the parsed word count.  Rows
        without a parseable word count are dropped while this is set.
    :param updated_after: Optional inclusive lower bound on ``update_date``,
        given as a :class:`datetime.date` or a ``YYYY-MM-DD`` string.  Rows
        without a parseable update date are dropped while this is set.
        Filters run after cleaning/ranking and before grouping and ``limit``;
        when none is set, rows carrying missing fields are kept unchanged.
    :return: A flat list of `SearchResult` objects.  Every row also carries
        the ranking telemetry described by :class:`RankedSearchResult`
        (``rank_score`` 0..1 and ``match_tier`` 0..4); the annotations are
        added to copies, so the cache and the searchers' payloads stay
        pristine.  :func:`search_stream` does not add them.
    """
    if sites is not None and not sites:
        # An explicit empty selection searches nothing.  The registry treats
        # any falsy ``sites`` (``[]`` included) as "all sites", so the empty
        # selection must be short-circuited here.
        return []

    # Resolve the participating classes before the cache lookup: an aggregate
    # key needs their identity (see ``_build_cache_key``), and a hot
    # configuration change must not keep serving an old snapshot.
    after = _coerce_updated_after(updated_after)
    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)
    key = _build_cache_key(
        keyword,
        sites,
        per_site_limit,
        nsfw,
        timeout,
        classes=classes,
        min_words=min_words,
        max_words=max_words,
        updated_after=after,
    )
    cached = _CACHE.get(key)
    if cached is not None:
        # Cache holds the merged, unranked snapshot: rank/group/slice per call
        # so a different sort order, grouping or limit still honours this
        # client's request.
        return _finalize(
            cached,
            keyword=keyword,
            sort=sort,
            order=order,
            limit=limit,
            group=group,
            min_relevance=min_relevance,
            min_words=min_words,
            max_words=max_words,
            updated_after=after,
        )

    if not classes:
        return []

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    results: list[SearchResult] = []
    site_tasks: dict[str, asyncio.Task[list[SearchResult]]] = {}
    late: set[asyncio.Task[list[SearchResult]]] = set()
    #: Loop time by which every cancelled site task must have unwound.  Set
    #: inside the session block and reused after it closes (closing the
    #: session is itself what unblocks some in-flight operations).
    cleanup_deadline: float | None = None

    async with aiohttp.ClientSession(
        connector=_make_connector(),
        timeout=timeout_cfg,
        headers=DEFAULT_USER_HEADERS,
    ) as session:
        try:
            plan = _prepare_searchers(
                classes,
                session,
                sites=sites,
                nsfw=nsfw,
                on_event=on_event,
            )
            for site, inst in plan.entries:
                site_tasks[site] = asyncio.create_task(
                    _search_one(inst, keyword, per_site_limit, timeout),
                    name=f"{inst.__class__.__name__}.search",
                )
            # One deadline for the whole fan-out: every site started together,
            # so a site still running when the budget expires is a timeout, and
            # the aggregate returns instead of waiting for it.
            if site_tasks:
                _, late = await asyncio.wait(site_tasks.values(), timeout=timeout)
        finally:
            # Also reached when the caller cancels ``search`` itself: never
            # leave a site task running against the session we are about to
            # close.  The wait is bounded (see ``_drain``).
            cleanup_deadline = asyncio.get_running_loop().time() + _ABANDON_GRACE
            _cancel_pending(site_tasks.values())
            await _drain(site_tasks.values(), cleanup_deadline)

    if cleanup_deadline is not None:
        # Closing the session may be what finally unblocks a cancelled site's
        # in-flight socket operation; let those tasks observe it within the
        # remaining grace.  This is a no-op when everything already finished.
        await _drain(site_tasks.values(), cleanup_deadline)

    for site, _ in plan.entries:
        task = site_tasks[site]
        if task in late:
            _report_failure(site, _timeout_error(timeout), plan.browser_sites, on_event)
            continue
        try:
            item: object = task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report_failure(site, exc, plan.browser_sites, on_event)
            continue
        if not isinstance(item, list):
            # A searcher that returns something other than a list is a site
            # failure: report it and keep the other sites' results instead of
            # letting it crash the whole aggregate.
            _report_failure(
                site, _invalid_payload_error(item), plan.browser_sites, on_event
            )
            continue
        results.extend(item)
        _report_completion(site, len(item), plan.browser_sites, on_event)

    # Cache the unranked merged results; callers rank/group/slice their own
    # copy.  Empty aggregates are not cached: they often stem from transient
    # site failures and should not be pinned for the whole TTL.
    if results:
        _CACHE.put(key, results)
    return _finalize(
        results,
        keyword=keyword,
        sort=sort,
        order=order,
        limit=limit,
        group=group,
        min_relevance=min_relevance,
        min_words=min_words,
        max_words=max_words,
        updated_after=after,
    )


async def search_stream(
    keyword: str,
    sites: Sequence[str] | None = None,
    per_site_limit: int = 5,
    timeout: float = 5.0,
    nsfw: bool = False,
    *,
    on_event: Callable[[SearchEvent], None] | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | str | None = None,
) -> AsyncGenerator[list[SearchResult]]:
    """
    Stream search results from registered sites as soon as each site finishes.

    The chunk contract is unchanged: each yielded value is the non-empty result
    list of one site.  Per-site state (including whether browser assistance was
    requested or skipped) is reported through ``on_event`` and the logger
    instead of a new chunk type.

    Rows are cleaned before they are yielded, exactly like :func:`search`
    cleans its final list: rows with an unsafe site key or book id, blank-site
    rows and rows naming no book are dropped, non-http(s)
    ``book_url``/``cover_url`` values are cleared, control characters are
    removed from display text, and same-site duplicates are folded (see
    :func:`_sanitize_chunk`).  A chunk emptied by
    that cleaning is not yielded, and cleaning never fetches extra rows, so
    ``per_site_limit`` keeps its meaning (a chunk can only shrink, never grow
    past the site's own limit).  Ranking and relevance filtering stay out of
    the stream: the chunks remain the sites' raw ordering, minus unusable
    rows.  The optional word-count/update-date filters *do* apply (a chunk is
    then this site's rows that pass them), because they only narrow the same
    rows rather than reorder them.  The ranking telemetry :func:`search`
    attaches to final rows
    (``rank_score`` / ``match_tier``, see :class:`RankedSearchResult`) is
    deliberately absent too: a chunk is a progressive preview, not a ranked
    final result.

    The cache stores the *raw* per-site payloads; cleaning happens on every
    yield, so a cache hit produces the same chunks as a fresh run (see
    :func:`_chunks_by_site`).  That also means a later :func:`search` can rank
    rows the stream chose to display cleaned.

    :param keyword: Search keyword or term.
    :param sites: Optional list of site keys; if None, use all registered
        sites.  An empty sequence searches no site at all.
    :param per_site_limit: Maximum number of results per site.
    :param timeout: Timeout per-site (seconds).
    :param nsfw: Whether to include NSFW searchers.
    :param on_event: Optional observer for per-site search states.
    :param min_words: Optional lower bound on the parsed word count.  Rows
        without a parseable word count are dropped while this is set.
    :param max_words: Optional upper bound on the parsed word count.  Rows
        without a parseable word count are dropped while this is set.
    :param updated_after: Optional inclusive lower bound on ``update_date``,
        given as a :class:`datetime.date` or a ``YYYY-MM-DD`` string.  Rows
        without a parseable update date are dropped while this is set.
        Filters are applied to each cleaned chunk (and to cache hits), so a
        warmed stream yields exactly the same rows as a fresh one.
    :yield: Lists of `SearchResult` objects from each completed site.
    """
    if sites is not None and not sites:
        # An explicit empty selection searches nothing (see ``search``).
        return

    # Same ordering as ``search``: resolve classes first so an aggregate key
    # can carry the configuration fingerprint, then consult the shared cache.
    after = _coerce_updated_after(updated_after)
    # One instant for the whole stream, so relative dates ("3天前") in every
    # chunk are evaluated against the same clock.
    current = datetime.now()
    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)
    key = _build_cache_key(
        keyword,
        sites,
        per_site_limit,
        nsfw,
        timeout,
        classes=classes,
        min_words=min_words,
        max_words=max_words,
        updated_after=after,
    )
    cached = _CACHE.get(key)
    if cached is not None:
        # Keep the per-site chunk contract on a cache hit: yielding the whole
        # snapshot as one batch would mix every site into a single chunk.  The
        # cache holds the raw payloads, so clean (and filter) each chunk
        # exactly as the fresh path does -- a warmed stream must not leak rows
        # the fresh run would have dropped.
        for chunk in _chunks_by_site(cached):
            cleaned = _filter_results(
                _sanitize_chunk(chunk),
                min_words=min_words,
                max_words=max_words,
                updated_after=after,
                now=current,
            )
            if cleaned:
                yield cleaned
        return

    if not classes:
        return

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    site_by_task: dict[asyncio.Task[list[SearchResult]], str] = {}
    cleanup_deadline: float | None = None
    accumulated: list[SearchResult] = []
    #: Whether any chunk survived cleaning.  An all-dirty stream is not cached:
    #: it is effectively empty, and pinning it would freeze a bad snapshot.
    emitted = False
    async with aiohttp.ClientSession(
        connector=_make_connector(),
        timeout=timeout_cfg,
        headers=DEFAULT_USER_HEADERS,
    ) as session:
        plan = _prepare_searchers(
            classes,
            session,
            sites=sites,
            nsfw=nsfw,
            on_event=on_event,
        )

        pending: set[asyncio.Task[list[SearchResult]]] = set()
        for site, inst in plan.entries:
            task = asyncio.create_task(
                _search_one(inst, keyword, per_site_limit, timeout),
                name=f"{inst.__class__.__name__}.search",
            )
            pending.add(task)
            site_by_task[task] = site

        search_deadline = asyncio.get_running_loop().time() + timeout
        try:
            while pending:
                remaining = search_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in done:
                    site = site_by_task[task]
                    try:
                        payload: object = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _report_failure(site, exc, plan.browser_sites, on_event)
                        continue

                    if not isinstance(payload, list):
                        # A searcher that returns something other than a list
                        # is a site failure; keep streaming the other sites.
                        _report_failure(
                            site,
                            _invalid_payload_error(payload),
                            plan.browser_sites,
                            on_event,
                        )
                        continue

                    _report_completion(site, len(payload), plan.browser_sites, on_event)
                    if payload:
                        # Cache the raw payload (a later ``search`` may rank
                        # rows this stream chose to hide), but yield it
                        # cleaned.  A site whose every row is unusable yields
                        # no chunk at all, matching the fresh-run contract.
                        accumulated.extend(payload)
                        chunk = _filter_results(
                            _sanitize_chunk(payload),
                            min_words=min_words,
                            max_words=max_words,
                            updated_after=after,
                            now=current,
                        )
                        if chunk:
                            emitted = True
                            yield chunk
        finally:
            # Cancel whatever is left and give it a bounded grace to unwind
            # before the session closes; the wait is clipped to
            # ``_ABANDON_GRACE`` so a slow-to-cancel site cannot stretch the
            # stream (see ``_drain``).
            cleanup_deadline = asyncio.get_running_loop().time() + _ABANDON_GRACE
            for task in _cancel_pending(site_by_task):
                _report_failure(
                    site_by_task[task],
                    _timeout_error(timeout),
                    plan.browser_sites,
                    on_event,
                )
            await _drain(site_by_task, cleanup_deadline)

    if cleanup_deadline is not None:
        # Closing the session can be what unblocks an in-flight operation of a
        # cancelled site; give it the rest of the grace to observe that.
        await _drain(site_by_task, cleanup_deadline)

    # Only reached on normal completion; cancelled/closed streams skip this.
    # Empty streams are not cached (transient failures must not be pinned),
    # and neither is a snapshot every row of which cleaning rejected.
    if accumulated and emitted:
        _CACHE.put(key, accumulated)
