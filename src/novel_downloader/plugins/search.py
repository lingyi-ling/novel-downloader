#!/usr/bin/env python3
"""
novel_downloader.plugins.search
-------------------------------

"""

__all__ = ["search", "search_stream"]

import asyncio
from collections.abc import AsyncGenerator, Sequence

import aiohttp

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.ranking import sort_results
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search_cache import SearchCache, build_key
from novel_downloader.schemas import SearchResult

#: Process-wide short-lived cache shared by ``search`` and ``search_stream``.
_CACHE = SearchCache()


async def search(
    keyword: str,
    sites: Sequence[str] | None = None,
    limit: int | None = None,
    per_site_limit: int = 5,
    timeout: float = 5.0,
    nsfw: bool = False,
    sort: str = "quality",
    order: str | None = None,
) -> list[SearchResult]:
    """
    Perform a search for the given keyword across one or more registered sites,
    then aggregate and rank the results.

    By default the results are ordered by an estimated quality score (keyword
    relevance, word count, update recency, site priority and completeness); pass
    ``sort="priority"`` to fall back to the legacy per-site priority ordering.

    :param keyword: The search term or keyword to query.
    :param sites: An optional sequence of site keys to limit which searchers.
    :param limit: Maximum total number of results to return; if None, return all.
    :param per_site_limit: Maximum number of search results per site.
    :param timeout: Per-request time budget (seconds)
    :param nsfw: Whether to include NSFW searchers.
    :param sort: Ranking key (see ``ranking.SORT_KEYS``); default ``"quality"``.
    :param order: ``"asc"``/``"desc"``; defaults to the key's natural direction.
    :return: A flat list of `SearchResult` objects.
    """
    key = build_key(keyword, sites, per_site_limit, nsfw)
    cached = _CACHE.get(key)
    if cached is not None:
        # Cache holds the merged, unranked snapshot: rank/slice per call so a
        # different sort order or limit still honours this client's request.
        sort_results(cached, by=sort, order=order, keyword=keyword)
        return cached[:limit] if limit is not None else cached

    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)

    if not classes:
        return []

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    results: list[SearchResult] = []

    async with aiohttp.ClientSession(
        timeout=timeout_cfg,
        headers=DEFAULT_USER_HEADERS,
    ) as session:
        if sites is None:
            instances = [
                inst for cls in classes if ((inst := cls(session)).nsfw == nsfw)
            ]
        else:
            instances = []
            for cls in classes:
                explicit = cls(session)
                explicit.explicitly_selected = True
                instances.append(explicit)
        coros = [inst.search(keyword, limit=per_site_limit) for inst in instances]
        site_lists = await asyncio.gather(*coros, return_exceptions=True)

    for item in site_lists:
        if isinstance(item, BaseException):
            continue
        results.extend(item)

    # Cache the unranked merged results; callers rank/slice their own copy.
    # Empty aggregates are not cached: they often stem from transient site
    # failures and should not be pinned for the whole TTL.
    if results:
        _CACHE.put(key, results)
    sort_results(results, by=sort, order=order, keyword=keyword)
    return results[:limit] if limit is not None else results


async def search_stream(
    keyword: str,
    sites: Sequence[str] | None = None,
    per_site_limit: int = 5,
    timeout: float = 5.0,
    nsfw: bool = False,
) -> AsyncGenerator[list[SearchResult]]:
    """
    Stream search results from registered sites as soon as each site finishes.

    :param keyword: Search keyword or term.
    :param sites: Optional list of site keys; if None, use all registered sites.
    :param per_site_limit: Maximum number of results per site.
    :param timeout: Timeout per-site (seconds).
    :param nsfw: Whether to include NSFW searchers.
    :yield: Lists of `SearchResult` objects from each completed site.
    """
    key = build_key(keyword, sites, per_site_limit, nsfw)
    cached = _CACHE.get(key)
    if cached is not None:
        yield cached
        return

    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)
    if not classes:
        if False:
            yield []
        return

    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
        timeout=timeout_cfg,
        headers=DEFAULT_USER_HEADERS,
    ) as session:
        if sites is None:
            instances = [
                inst for cls in classes if ((inst := cls(session)).nsfw == nsfw)
            ]
        else:
            instances = []
            for cls in classes:
                explicit = cls(session)
                explicit.explicitly_selected = True
                instances.append(explicit)
        tasks = [
            asyncio.create_task(
                inst.search(keyword, limit=per_site_limit),
                name=f"{cls.__name__}.search",
            )
            for cls, inst in zip(classes, instances, strict=False)
        ]

        accumulated: list[SearchResult] = []
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    site_results = await fut
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if site_results:
                    accumulated.extend(site_results)
                    yield site_results
        finally:
            # ensure no background tasks are left running after cancel/exit
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # Only reached on normal completion; cancelled/closed streams skip this.
    # Empty streams are not cached (transient failures must not be pinned).
    if accumulated:
        _CACHE.put(key, accumulated)
