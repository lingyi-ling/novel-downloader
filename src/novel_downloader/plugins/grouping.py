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
  of their title and join the first matching one;
* a second, equally conservative pass folds the *same* book listed with a
  decorative publication marker (``[网游]斗罗`` vs ``斗罗``,
  ``斗罗（全本）`` vs ``斗罗``).  It only fires when both sides have the
  same non-empty author **and** their decorative-stripped core titles are
  exactly equal after normalization.  Only a fixed whitelist of marker
  contents (:data:`_DECORATIVE_MARKERS`) is removed from paired leading
  or trailing ``[...]`` / ``【...】`` / ``(...)`` tags, so a bracketed
  word that is part of the real title is never dropped and nothing is
  matched fuzzily.  Volume/part numbers live outside the whitelist, so
  ``第①卷`` and ``第2卷`` stay separate books.

Each group exposes ``primary`` (the entry with the best ranking -- the same
tier-then-score order ``sort_results`` uses, under the caller's ``intent``;
earliest input position on ties) and ``sources`` (every entry of the group,
best first, ties keeping input order).  Groups are ordered by the input
position of their primaries, so callers that sort the flat list first and
group second get a stable, predictable layout.

Everything in this module is a pure plugin-layer function: no I/O, no UI
imports, and the input sequence is never modified.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from novel_downloader.plugins.ranking import match_tier_of, normalize_text, rank_score
from novel_downloader.schemas import SearchResult

__all__ = [
    "ResultGroup",
    "group_results",
]

#: Bracket pairs that may carry a decorative publication marker.  The title is
#: NFKC-folded before matching, so ``［网游］`` and ``（全本）`` go through the
#: same ASCII-bracket rules as ``[网游]`` and ``(全本)``.  The pattern has no
#: ``^`` anchor: :func:`_core_title_cached` calls ``match(text, start)`` to
#: continue after an already-stripped tag, which anchors at ``start`` itself.
_LEADING_TAG_RE = re.compile(r"(?:\[([^\]]*)\]|【([^】]*)】|\(([^)]*)\))")

#: Closing bracket -> opening bracket for the trailing pass.  The suffix is
#: parsed by index instead of with a ``$``-anchored ``re.search``: that search
#: retried every start position and rescanned to the end, so a title such as
#: ``"[" * 50_000`` was quadratic.  Index parsing touches each character once.
_TRAILING_PAIRS = {"]": "[", "】": "【", ")": "("}

#: Contents (after :func:`normalize_text`) that are publication markers rather
#: than part of a title.  Deliberately a small closed set: anything else --
#: ``第1卷``, ``上``, ``同人`` -- keeps the two rows in separate groups.
_DECORATIVE_MARKERS = frozenset(
    {
        "epub",
        "mobi",
        "pdf",
        "txt",
        "全本",
        "全本小说",
        "免费阅读",
        "完本",
        "完结",
        "小说网",
        "小说",
        "校对",
        "校对版",
        "精校",
        "精校版",
        "网游",
        "电子书",
    }
)


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

    __slots__ = ("author", "core", "entries", "mixed_core")

    def __init__(self, core: str) -> None:
        self.entries: list[tuple[int, SearchResult]] = []
        #: Normalized author seen so far; ``""`` while still unknown.
        self.author: str = ""
        #: Normalized title with decorative tags stripped.
        self.core: str = core
        #: True once two entries of this exact-title cluster disagree on core.
        #: Such a cluster is never merged across title keys (ambiguous title).
        self.mixed_core: bool = False

    def note_core(self, core: str) -> None:
        if core != self.core:
            self.mixed_core = True


def _tag_content(match: re.Match[str]) -> str:
    """Return the single non-``None`` bracket-content group of ``match``."""
    return next(group for group in match.groups() if group is not None)


def _is_decorative(content: str) -> bool:
    return normalize_text(content) in _DECORATIVE_MARKERS


@lru_cache(maxsize=8192)
def _core_title_cached(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip()
    start = 0
    end = len(text)

    # Strip every paired leading decorative tag, then every paired trailing
    # one.  A tag whose content is not whitelisted stops the loop, which keeps
    # titles such as ``[同人]斗罗`` or ``斗罗（第1卷）`` untouched.  Both passes
    # keep an index into ``text`` instead of slicing on every step, so a title
    # made of thousands of tags (or thousands of unclosed brackets) is scanned
    # once: stripping used to copy the remaining suffix each time, and the old
    # trailing regex rescanned the whole title from every start position.
    while True:
        match = _LEADING_TAG_RE.match(text, start)
        if match is None or not _is_decorative(_tag_content(match)):
            break
        start = match.end()
        while start < end and text[start].isspace():
            start += 1

    while end > start:
        close = text[end - 1]
        opener = _TRAILING_PAIRS.get(close)
        if opener is None:
            break
        # The regex equivalent is ``(?:\[[^\]]*\]|【[^】]*】|\([^)]*\))$``: the
        # opener must be the leftmost one after the last inner ``close`` so
        # the bracketed content cannot contain its own closing bracket.
        inner_close = text.rfind(close, start, end - 1)
        open_index = text.find(opener, max(start, inner_close + 1), end - 1)
        if open_index < 0 or not _is_decorative(text[open_index + 1 : end - 1]):
            break
        end = open_index
        while end > start and text[end - 1].isspace():
            end -= 1

    return normalize_text(text[start:end])


def _core_title(value: object) -> str:
    """Return the decorative-stripped, normalized core title of ``value``."""
    return _core_title_cached(str(value if value is not None else ""))


def _merge_decorated(by_title: dict[str, list[_Cluster]]) -> list[_Cluster]:
    """
    Fold clusters whose core title and author are both exactly equal.

    Only clusters with a known author, a non-empty core title and a single
    unambiguous core are eligible, so this pass can never fuse two different
    authors, an unknown-author row into another title, or a title whose core
    depends on which entry happened to be seen first.  Returns the surviving
    clusters; the order is irrelevant because the caller re-orders groups by
    their primary's unique input position.
    """
    buckets: dict[tuple[str, str], _Cluster] = {}
    merged: list[_Cluster] = []
    for clusters in by_title.values():
        for cluster in clusters:
            if cluster.mixed_core or not cluster.core or not cluster.author:
                merged.append(cluster)
                continue
            key = (cluster.core, cluster.author)
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = cluster
                merged.append(cluster)
            else:
                existing.entries.extend(cluster.entries)
    return merged


def _norm_author(result: SearchResult) -> str:
    """Return the normalized author of ``result`` (``""`` when unknown)."""
    return normalize_text(result.get("author"))


def group_results(
    results: Sequence[SearchResult],
    keyword: str = "",
    *,
    now: datetime | None = None,
    intent: str = "either",
) -> list[ResultGroup]:
    """
    Fold ``results`` into one :class:`ResultGroup` per distinct book.

    Entries are keyed by ``normalize_text(title)``.  Inside one title,
    entries with two different non-empty authors form separate groups;
    entries with an empty author merge into the first matching group.
    Afterwards, compatible groups whose author is non-empty and equal and
    whose decorative-stripped core titles are exactly equal are folded
    across title keys; see the module docstring for the full rationale.

    Each group's ``primary`` is its best-ranked entry and ``sources`` lists
    the whole group best first (ties keep input order).  "Best" is the same
    ordering ``sort_results`` uses for ``by="quality"``: with a decided
    ``intent`` (``"title"``/``"author"``) the match tier of the primary field
    comes first, then the :func:`rank_score` for that intent; otherwise it is
    the ``"either"`` score alone.  Without ``intent`` the historical layout is
    unchanged.  Passing the same intent a caller used to rank the flat list
    keeps the grouped row identical to that list's first row for the book --
    re-scoring with ``"either"`` used to pick a different primary whenever the
    two fields disagreed.  The returned groups themselves are ordered by the
    input position of their primaries, so an already sorted input keeps its
    relative order.

    The input sequence is left untouched and the grouping / tie-breaking
    logic is fully deterministic: the same input always yields the same
    output, the only wall-clock dependency being the recency signal inside
    ``rank_score`` -- sampled once per call, so every entry of one call is
    scored against the same instant.

    :param results: aggregated search results, in any (typically
        pre-sorted) order.
    :param keyword: the original search keyword, feeding the relevance
        signal of ``rank_score``.
    :param now: the instant the recency signal is measured against; defaults
        to :func:`datetime.now`.  Callers that also rank and annotate the same
        rows can pass one instant so every score of the call is comparable.
    :param intent: ``"title"``, ``"author"`` or ``"either"`` (default); any
        other value is treated as ``"either"``, mirroring ``rank_score``.
    :returns: one :class:`ResultGroup` per book, ordered by the input
        position of each group's ``primary``.
    """
    current = now or datetime.now()
    decided = isinstance(intent, str) and intent in {"author", "title"}
    resolved_intent = intent if decided else "either"

    def _order_key(item: tuple[int, SearchResult]) -> tuple[int, float, int]:
        # (-tier, -score, position) keys: descending tier/score, ascending
        # input position on ties.  Under "either" every tier is 0, so this is
        # exactly the historical (-score, position) order.
        index, result = item
        score = rank_score(result, keyword, intent=resolved_intent, now=current)
        tier = (
            match_tier_of(result, keyword, intent=resolved_intent)
            if resolved_intent != "either"
            else 0
        )
        return (-tier, -score, index)

    by_title: dict[str, list[_Cluster]] = {}
    # title -> (normalized author -> cluster); built incrementally so each
    # entry is dispatched in O(1) instead of scanning the title's clusters.
    by_author: dict[str, dict[str, _Cluster]] = {}
    # title -> the single cluster that still has no known author.  It can only
    # ever be the first cluster of its title: an entry without an author always
    # joins the first cluster, and the first known author to show up adopts it.
    unknown: dict[str, _Cluster] = {}

    for index, result in enumerate(results):
        title_key = normalize_text(result.get("title"))
        core_key = _core_title(result.get("title"))
        author_key = _norm_author(result)
        clusters = by_title.get(title_key)
        if clusters is None:
            clusters = []
            by_title[title_key] = clusters

        if author_key:
            title_authors = by_author.get(title_key)
            if title_authors is None:
                title_authors = {}
                by_author[title_key] = title_authors
            target = title_authors.get(author_key)
            if target is None:
                # First entry of this author: adopt the still-anonymous first
                # cluster when there is one (mirrors "first compatible"), else
                # start a new group.
                target = unknown.pop(title_key, None)
                if target is None:
                    target = _Cluster(core_key)
                    clusters.append(target)
                target.author = author_key
                title_authors[author_key] = target
        elif clusters:
            target = clusters[0]
        else:
            target = _Cluster(core_key)
            clusters.append(target)
            unknown[title_key] = target

        target.note_core(core_key)
        target.entries.append((index, result))

    groups: list[tuple[int, ResultGroup]] = []
    for cluster in _merge_decorated(by_title):
        # ``_order_key`` keys are unique per entry (the input position is the
        # last component), so a plain sort is fully deterministic.
        scored = sorted(cluster.entries, key=_order_key)
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
