#!/usr/bin/env python3
"""
novel_downloader.plugins.search
-------------------------------

"""

from __future__ import annotations

__all__ = [
    "GroupedSearchResult",
    "SearchEvent",
    "search",
    "search_stream",
]

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import aiohttp

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.grouping import group_results
from novel_downloader.plugins.ranking import sort_results
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search_cache import SearchCache, build_key
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


#: Process-wide short-lived cache shared by ``search`` and ``search_stream``.
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
    * ``"failed"`` — the site's search task raised.

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
) -> _SearchPlan:
    """
    Instantiate the searchers to run and emit browser-assistance events.

    Aggregate searches (``sites is None``) skip configured browser-assisted
    sites: they would otherwise start a browser window for every keyword.
    Explicitly requested sites have their browser fallback enabled, which is
    still up to the site's own searcher.

    A class that appears more than once (the registry repeats it when the
    same site key is requested twice) is instantiated and searched once.
    """
    browser_sites = _browser_assisted_sites()
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

        instance = cls(session)
        if not explicit and instance.nsfw != nsfw:
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
    results: Sequence[SearchResult], keyword: str
) -> list[GroupedSearchResult]:
    """
    Fold ranked ``results`` into one merged entry per book.

    Each group collapses onto its best entry (highest rank, earliest position
    on ties) enriched with ``sources``: the site keys of every source of the
    book, best first, duplicates dropped.  The input sequence is only read,
    so cached snapshots stay pristine.
    """
    merged: list[GroupedSearchResult] = []
    for group in group_results(results, keyword=keyword):
        sites: list[str] = []
        for entry in group.sources:
            site = str(entry.get("site") or "")
            if site and site not in sites:
                sites.append(site)
        merged.append({**group.primary, "sources": sites})
    return merged


def _finalize(
    results: list[SearchResult],
    *,
    keyword: str,
    sort: str,
    order: str | None,
    limit: int | None,
    group: bool,
) -> list[SearchResult]:
    """
    Rank ``results`` for this caller, then group and/or slice as requested.

    Grouping happens after ranking (merged entries inherit the ranked order)
    and before ``limit``, so with ``group=True`` the limit counts books rather
    than raw rows.  The flat branch keeps the historical contract of returning
    the ranked input list itself; the grouped branch always builds fresh
    entries, which keeps the search cache shareable between both modes.
    """
    sort_results(results, by=sort, order=order, keyword=keyword)
    if group:
        merged = _merged_results(results, keyword)
        if limit is not None:
            merged = merged[:limit]
        return cast("list[SearchResult]", merged)
    if limit is not None:
        return results[:limit]
    return results


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
    :param timeout: Per-request time budget (seconds)
    :param nsfw: Whether to include NSFW searchers.
    :param sort: Ranking key (see ``ranking.SORT_KEYS``); default ``"quality"``.
    :param order: ``"asc"``/``"desc"``; defaults to the key's natural direction.
    :param on_event: Optional observer for per-site search states (browser
        assistance, completion, failure).  Exceptions it raises are ignored.
    :param group: Fold same-book results into one merged entry per book (see
        :class:`GroupedSearchResult`); off by default, and never applied to
        :func:`search_stream`, whose per-site chunks stay flat.
    :return: A flat list of `SearchResult` objects.
    """
    if sites is not None and not sites:
        # An explicit empty selection searches nothing.  The registry treats
        # any falsy ``sites`` (``[]`` included) as "all sites", so the empty
        # selection must be short-circuited here.
        return []

    key = build_key(keyword, sites, per_site_limit, nsfw, timeout)
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
        )

    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)

    if not classes:
        return []

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    results: list[SearchResult] = []

    async with aiohttp.ClientSession(
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
        coros = [inst.search(keyword, limit=per_site_limit) for _, inst in plan.entries]
        site_lists = await asyncio.gather(*coros, return_exceptions=True)

    for (site, _), item in zip(plan.entries, site_lists, strict=False):
        if isinstance(item, BaseException):
            _report_failure(site, item, plan.browser_sites, on_event)
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
    )


async def search_stream(
    keyword: str,
    sites: Sequence[str] | None = None,
    per_site_limit: int = 5,
    timeout: float = 5.0,
    nsfw: bool = False,
    *,
    on_event: Callable[[SearchEvent], None] | None = None,
) -> AsyncGenerator[list[SearchResult]]:
    """
    Stream search results from registered sites as soon as each site finishes.

    The chunk contract is unchanged: each yielded value is the non-empty result
    list of one site.  Per-site state (including whether browser assistance was
    requested or skipped) is reported through ``on_event`` and the logger
    instead of a new chunk type.

    :param keyword: Search keyword or term.
    :param sites: Optional list of site keys; if None, use all registered
        sites.  An empty sequence searches no site at all.
    :param per_site_limit: Maximum number of results per site.
    :param timeout: Timeout per-site (seconds).
    :param nsfw: Whether to include NSFW searchers.
    :param on_event: Optional observer for per-site search states.
    :yield: Lists of `SearchResult` objects from each completed site.
    """
    if sites is not None and not sites:
        # An explicit empty selection searches nothing (see ``search``).
        return

    key = build_key(keyword, sites, per_site_limit, nsfw, timeout)
    cached = _CACHE.get(key)
    if cached is not None:
        yield cached
        return

    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)
    if not classes:
        return

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
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
        site_by_task: dict[asyncio.Task[list[SearchResult]], str] = {}
        for site, inst in plan.entries:
            task = asyncio.create_task(
                inst.search(keyword, limit=per_site_limit),
                name=f"{inst.__class__.__name__}.search",
            )
            pending.add(task)
            site_by_task[task] = site

        accumulated: list[SearchResult] = []
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
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
                        accumulated.extend(payload)
                        yield payload
        finally:
            # ensure no background tasks are left running after cancel/exit
            for task in site_by_task:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*site_by_task, return_exceptions=True)

    # Only reached on normal completion; cancelled/closed streams skip this.
    # Empty streams are not cached (transient failures must not be pinned).
    if accumulated:
        _CACHE.put(key, accumulated)
