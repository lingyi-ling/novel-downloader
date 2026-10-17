#!/usr/bin/env python3
"""
novel_downloader.plugins.grouping
---------------------------------

Same-book grouping for cross-site aggregated search results.

After a multi-site search the caller holds a flat ``list[SearchResult]``
in which the same book typically appears once per site.  :func:`group_results`
folds that flat list into :class:`ResultGroup` buckets -- one per book --
so a UI row (or a CLI table) can show a single entry per book and offer
the other sites as alternative sources.

Merging is deliberately conservative:

* the group key is ``normalize_text(title)`` (punctuation, spacing and
  letter case are ignored);
* within one title, entries carrying two different non-empty (normalized)
  authors are kept in separate groups -- two books that merely share a
  name must not be fused into one;
* entries whose author is empty ("unknown") are compatible with any group
  of their title and join the first matching one.

Each group exposes ``primary`` (the entry with the highest
:func:`~novel_downloader.plugins.ranking.rank_score`, earliest input
position on ties) and ``sources`` (every entry of the group, best first,
ties keeping input order).  Groups are ordered by the input position of
their primaries, so callers that sort the flat list first and group
second get a stable, predictable layout.

Everything in this module is a pure plugin-layer function: no I/O, no UI
imports, and the input sequence is never modified.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from novel_downloader.plugins.ranking import normalize_text, rank_score
from novel_downloader.schemas import SearchResult

__all__ = [
    "ResultGroup",
    "group_results",
]


@dataclass(frozen=True)
class ResultGroup:
    """
    A single book together with every search result that points to it.

    ``primary`` is the group's best entry -- highest ``rank_score``, the
    earliest input position on ties -- and is always part of ``sources``.
    ``sources`` holds the whole group, best first (ties keep input order);
    the list is fresh memory owned by the group, while the entries are
    the original input dictionaries.
    """

    primary: SearchResult
    sources: list[SearchResult]


class _Cluster:
    """Build-time accumulator for the entries of one candidate group."""

    __slots__ = ("author", "entries")

    def __init__(self) -> None:
        self.entries: list[tuple[int, SearchResult]] = []
        #: Normalized author seen so far; ``""`` while still unknown.
        self.author: str = ""


def _norm_author(result: SearchResult) -> str:
    """Return the normalized author of ``result`` (``""`` when unknown)."""
    return normalize_text(result.get("author"))


def _compatible(author_key: str, cluster: _Cluster) -> bool:
    """
    Can an entry with normalized ``author_key`` join ``cluster``?

    An empty (unknown) author is compatible with anything; a known author
    joins clusters that either have no known author yet or the same one.
    """
    if not author_key or not cluster.author:
        return True
    return cluster.author == author_key


def group_results(
    results: Sequence[SearchResult],
    keyword: str = "",
) -> list[ResultGroup]:
    """
    Fold ``results`` into one :class:`ResultGroup` per distinct book.

    Entries are keyed by ``normalize_text(title)``.  Inside one title,
    entries with two different non-empty authors form separate groups;
    entries with an empty author merge into the first matching group.
    See the module docstring for the full rationale.

    Each group's ``primary`` is its highest-ranked entry (by
    :func:`rank_score` with ``keyword``; ties go to the entry appearing
    first in ``results``) and ``sources`` lists the whole group by
    descending score, ties keeping input order.  The returned groups
    themselves are ordered by the input position of their primaries, so
    an already sorted input keeps its relative order.

    The input sequence is left untouched and the grouping / tie-breaking
    logic is fully deterministic: the same input always yields the same
    output, the only wall-clock dependency being the recency signal inside
    ``rank_score`` -- sampled once per call, so every entry of one call is
    scored against the same instant.

    :param results: aggregated search results, in any (typically
        pre-sorted) order.
    :param keyword: the original search keyword, feeding the relevance
        signal of ``rank_score``.
    :returns: one :class:`ResultGroup` per book, ordered by the input
        position of each group's ``primary``.
    """
    current = datetime.now()
    by_title: dict[str, list[_Cluster]] = {}

    for index, result in enumerate(results):
        title_key = normalize_text(result.get("title"))
        author_key = _norm_author(result)
        clusters = by_title.setdefault(title_key, [])
        target = next((c for c in clusters if _compatible(author_key, c)), None)
        if target is None:
            target = _Cluster()
            clusters.append(target)
        target.entries.append((index, result))
        if author_key:
            target.author = author_key

    groups: list[tuple[int, ResultGroup]] = []
    for clusters in by_title.values():
        for cluster in clusters:
            # (-score, position) keys are unique per entry, so a plain
            # stable sort is fully deterministic: descending score,
            # ascending input position on ties.
            scored = sorted(
                cluster.entries,
                key=lambda item: (-rank_score(item[1], keyword, now=current), item[0]),
            )
            primary_pos, primary = scored[0]
            groups.append(
                (
                    primary_pos,
                    ResultGroup(
                        primary=primary,
                        sources=[result for _, result in scored],
                    ),
                )
            )

    # Primary positions are unique (each entry lands in exactly one group),
    # so this order is deterministic as well.
    groups.sort(key=lambda pair: pair[0])
    return [group for _, group in groups]
