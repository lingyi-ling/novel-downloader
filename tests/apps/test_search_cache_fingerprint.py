#!/usr/bin/env python3
"""
Fingerprint and cache-key correctness for aggregate search results.

The cache identity is intentionally narrower than "the arguments of a
search".  It contains every input that can change *which raw rows the sites
return*:

* the normalized keyword (NFKC, casefolded, punctuation/whitespace removed,
  with a raw fallback for punctuation-only queries);
* the normalized, sorted site set (``None`` -- all registered sites -- is
  distinct from ``[]``, and carries an aggregate configuration fingerprint);
* ``per_site_limit``, ``nsfw`` and ``timeout``;
* the local filters ``min_words``, ``max_words`` and ``updated_after``.

It deliberately does **not** contain presentation/selection parameters
(``sort``, ``order``, ``limit``, ``group``, ``min_relevance``), because the
snapshot is the merged, unranked site payload and each call re-ranks, filters,
groups and slices its own copy.  ``query_intent`` is derived per call from the
keyword and the rows, so it is recomputed, never cached and never part of the
key.  Novel search has no ``offset`` parameter at all; front ends page by
slicing the returned list themselves.

This module pins both sides of that contract: equivalent spellings must share
one entry, genuinely different inputs must not, and a cache hit must never
serve another call's ranking/filtering output.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

import novel_downloader.plugins.search as search_module
from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import (
    _aggregate_config_fingerprint,
    _build_cache_key,
    search,
)
from novel_downloader.plugins.search_cache import (
    CacheKey,
    CacheStats,
    SearchCache,
    build_key,
)
from novel_downloader.schemas import SearchResult

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _Clock:
    """A manually advanced clock used for both TTL and wall-clock timestamps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _row(
    book_id: str,
    site: str = "alpha",
    **overrides: object,
) -> SearchResult:
    row: dict[str, object] = {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.com/{site}/{book_id}",
        "cover_url": "",
        "title": f"title-{site}-{book_id}",
        "author": "author",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }
    row.update(overrides)
    return cast("SearchResult", row)


# --------------------------------------------------------------------------- #
# fake searchers / registry
# --------------------------------------------------------------------------- #
class _StaticSearcher(BaseSearcher):
    """Return a fixed row set and count fetches and received keywords."""

    site_name: ClassVar[str] = "alpha"
    rows: ClassVar[Sequence[SearchResult]] = ()
    calls: ClassVar[int] = 0
    keywords: ClassVar[list[str]] = []

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        type(self).keywords.append(keyword)
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        rows = [cast("SearchResult", dict(row)) for row in type(self).rows]
        return rows[:limit] if limit is not None else rows


class _AlphaSearcher(_StaticSearcher):
    site_name = "alpha"
    rows = (
        _row(
            "a1", "alpha", title="书一", word_count="10万字", update_date="2024-01-01"
        ),
        _row(
            "a2", "alpha", title="书二", word_count="30万字", update_date="2024-06-01"
        ),
        _row(
            "a3", "alpha", title="书三", word_count="20万字", update_date="2023-01-01"
        ),
    )


class _BetaSearcher(_StaticSearcher):
    site_name = "beta"
    rows = (_row("b1", "beta", title="另一本", word_count="12万字"),)


class _GammaSearcher(_StaticSearcher):
    site_name = "gamma"
    rows = (_row("g1", "gamma", title="第三本", word_count="8万字"),)


_ALL_SEARCHERS = (_AlphaSearcher, _BetaSearcher, _GammaSearcher)


def _reset_searchers() -> None:
    for cls in _ALL_SEARCHERS:
        cls.calls = 0
        cls.keywords = []


def _mirror_registry(
    classes: Sequence[type[BaseSearcher]],
) -> Callable[..., list[type[BaseSearcher]]]:
    """Mirror the registry's normalization and falsy-``sites`` semantics."""

    def _classes(
        sites: Any = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        if not sites:
            return list(classes)
        selected: list[type[BaseSearcher]] = []
        for raw in sites:
            key = str(raw).strip().lower()
            if key and key[0].isdigit():
                key = f"n{key}"
            selected.extend(cls for cls in classes if cls.site_name == key)
        return selected

    return _classes


@pytest.fixture()
def search_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[SearchCache]:
    """Install a fresh process-wide cache and disable browser-assisted sites."""
    cache = SearchCache(now=_Clock())
    monkeypatch.setattr(search_module, "_CACHE", cache)
    monkeypatch.setattr(search_module, "_browser_assisted_sites", lambda: frozenset())
    _reset_searchers()
    yield cache


def _build(
    keyword: str = "斗破苍穹",
    sites: Sequence[str] | None = ("alpha", "beta"),
    per_site_limit: int = 5,
    nsfw: bool = False,
    timeout: float = 5.0,
    *,
    config_fingerprint: str | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    updated_after: date | datetime | str | None = None,
) -> CacheKey:
    return build_key(
        keyword,
        sites,
        per_site_limit,
        nsfw,
        timeout,
        config_fingerprint=config_fingerprint,
        min_words=min_words,
        max_words=max_words,
        updated_after=updated_after,
    )


# --------------------------------------------------------------------------- #
# keyword identity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Case and full-width folding.
        ("ABC", "abc"),
        ("ＡＢＣ", "ABC"),
        ("Straße", "STRASSE"),
        # Whitespace, punctuation and decorations are not part of identity.
        ("斗破 苍穹", "斗破苍穹"),
        ("  斗破苍穹\t\n", "斗破苍穹"),
        ("《斗破苍穹》", "斗破苍穹"),
        ("斗破-苍穹", "斗破苍穹"),
        # Decomposed and composed accents are one query after NFKC + casefold.
        ("Cafe\u0301", "Café"),
    ],
)
def test_equivalent_keyword_spellings_share_one_key(left: str, right: str) -> None:
    assert _build(left) == _build(right)


def test_keyword_normalization_is_nfkc_based() -> None:
    assert unicodedata.normalize("NFKC", "ＤＯＵ") == "DOU"
    assert _build("ＤＯＵ 破") == _build("dou破")


def test_different_words_and_word_order_never_share_a_key() -> None:
    assert _build("斗破 苍穹") != _build("苍穹 斗破")
    assert _build("三体") != _build("三体2")
    assert _build("abc") != _build("abd")


def test_punctuation_only_queries_keep_a_raw_identity() -> None:
    # ``normalize_text`` erases these completely; the fallback must keep them
    # distinct instead of collapsing every punctuation-only query into "".
    assert _build("!!!") != _build("???")
    assert _build("!!!") != _build("")
    assert _build("！！！") == _build("!!!")
    assert _build("  !!!  ") == _build("!!!")


def test_empty_keyword_is_still_constructible() -> None:
    # Both normalize (including the punctuation-only fallback) to the empty
    # identity; front ends short-circuit an empty query before searching.
    assert _build("").keyword == ""
    assert _build("   ").keyword == ""
    assert _build("") == _build("   ")


# --------------------------------------------------------------------------- #
# site identity and aggregate fingerprint
# --------------------------------------------------------------------------- #
def test_sites_are_order_case_whitespace_and_alias_insensitive() -> None:
    assert _build(sites=["beta", "alpha"]) == _build(sites=[" ALPHA ", "Beta"])
    assert _build(sites=["alpha", "alpha"]) == _build(sites=["alpha"])
    # The registry prefixes a leading digit with "n"; both resolve to one site.
    assert _build(sites=["1foo"]) == _build(sites=["n1foo"])


def test_site_set_membership_changes_the_key() -> None:
    assert _build(sites=["alpha"]) != _build(sites=["alpha", "beta"])
    assert _build(sites=["alpha", "beta"]) != _build(sites=["alpha", "gamma"])


def test_aggregate_selection_is_distinct_from_an_empty_one() -> None:
    assert _build(sites=None).sites is None
    assert _build(sites=[]).sites == ()
    assert _build(sites=None) != _build(sites=[])


def test_aggregate_fingerprint_is_order_independent_but_set_sensitive() -> None:
    no_browser = frozenset()
    assert _aggregate_config_fingerprint(
        [_AlphaSearcher, _BetaSearcher], no_browser
    ) == _aggregate_config_fingerprint([_BetaSearcher, _AlphaSearcher], no_browser)
    assert _aggregate_config_fingerprint(
        [_AlphaSearcher, _BetaSearcher], no_browser
    ) != _aggregate_config_fingerprint([_AlphaSearcher], no_browser)
    # Moving a site into the browser-assisted set changes whether an aggregate
    # search runs it at all, so the fingerprint must change too.
    assert _aggregate_config_fingerprint(
        [_AlphaSearcher, _BetaSearcher], no_browser
    ) != _aggregate_config_fingerprint(
        [_AlphaSearcher, _BetaSearcher], frozenset({"alpha"})
    )


def test_config_fingerprint_only_participates_for_aggregate_searches() -> None:
    assert _build(sites=None, config_fingerprint="f1") != _build(
        sites=None, config_fingerprint="f2"
    )
    assert _build(sites=None, config_fingerprint="f1") == _build(
        sites=None, config_fingerprint="f1"
    )
    assert _build(sites=["alpha"], config_fingerprint="f1") == _build(
        sites=["alpha"], config_fingerprint="f2"
    )
    assert _build(sites=["alpha"]).config_fingerprint is None


# --------------------------------------------------------------------------- #
# structural parameters and filters
# --------------------------------------------------------------------------- #
_STRUCTURAL_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("keyword", {"keyword": "斗破苍穹二"}),
    ("sites", {"sites": ["alpha"]}),
    ("per_site_limit", {"per_site_limit": 10}),
    ("nsfw", {"nsfw": True}),
    ("timeout", {"timeout": 60.0}),
    ("min_words", {"min_words": 100_000}),
    ("max_words", {"max_words": 1_000_000}),
    ("updated_after", {"updated_after": "2024-01-01"}),
]


@pytest.mark.parametrize(("name", "override"), _STRUCTURAL_VARIANTS)
def test_each_structural_parameter_changes_the_key(
    name: str, override: dict[str, Any]
) -> None:
    baseline = _build()
    changed = _build(**override)
    assert changed != baseline, name


def test_all_structural_variants_are_pairwise_distinct() -> None:
    keys = [_build(**override) for _, override in _STRUCTURAL_VARIANTS]
    assert len(set(keys)) == len(keys)


def test_updated_after_equivalent_spellings_collapse() -> None:
    same = _build(updated_after="2024-01-01")
    assert same == _build(updated_after=date(2024, 1, 1))
    assert same == _build(updated_after=datetime(2024, 1, 1, 23, 59))
    assert same == _build(updated_after="20240101")
    assert same != _build(updated_after="2024-01-02")


def test_invalid_updated_after_raises_instead_of_building_a_key() -> None:
    for bad in ("2024/01/01", "202401", "yesterday"):
        with pytest.raises(ValueError):
            _build(updated_after=bad)


# --------------------------------------------------------------------------- #
# end-to-end: one snapshot, per-call presentation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_repeated_query_hits_one_entry_and_output_cannot_pollute_it(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AlphaSearcher])
    )

    first = await search("斗破苍穹", sites=["alpha"])
    assert _AlphaSearcher.calls == 1
    assert len(first) == 3
    original_titles = sorted(row["title"] for row in first)

    # Mutate everything a caller owns: the list and the row dicts it returned.
    first.clear()
    first.append(_row("polluted", "alpha", title="polluted"))

    # A different spelling/site ordering of the same query is a cache hit.
    second = await search("斗破 苍穹", sites=["ALPHA"])
    assert _AlphaSearcher.calls == 1
    assert sorted(row["title"] for row in second) == original_titles
    assert all("rank_score" in row for row in second)

    key = _build_cache_key("斗破苍穹", ["alpha"], 5, False, 5.0, classes=[])
    raw = search_cache.get(key)
    assert raw is not None
    assert sorted(row["title"] for row in raw) == original_titles
    # Derived ranking telemetry is per call and never persisted in memory.
    assert all("rank_score" not in row for row in raw)
    assert all("query_intent" not in row for row in raw)

    # Editing a row returned by the cache itself cannot rewrite the snapshot.
    raw[0]["title"] = "polluted-cache"
    again = search_cache.get(key)
    assert again is not None
    assert "polluted-cache" not in {row["title"] for row in again}


@pytest.mark.asyncio
async def test_sort_order_and_limit_are_presentation_only(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AlphaSearcher])
    )
    common = {"sites": ["alpha"], "min_relevance": None}

    ascending = await search("排序", sort="words", order="asc", **common)
    descending = await search("排序", sort="words", order="desc", **common)
    by_update = await search("排序", sort="update", **common)
    limited = await search("排序", sort="words", limit=2, **common)
    best = await search("排序", sort="quality", limit=1, **common)

    # Every call above shares the same raw snapshot and fetched it once.
    assert _AlphaSearcher.calls == 1
    assert search_cache.diagnostics().memory_entries == 1
    assert search_cache.stats() == CacheStats(hits=4, misses=1, evictions=0)

    assert [row["book_id"] for row in ascending] == ["a1", "a3", "a2"]
    assert [row["book_id"] for row in descending] == ["a2", "a3", "a1"]
    assert [row["book_id"] for row in by_update] == ["a2", "a1", "a3"]
    assert [row["book_id"] for row in limited] == ["a2", "a3"]
    assert len(best) == 1
    # ``query_intent`` is attached only to quality-ranked rows.
    assert all(row["query_intent"] is None for row in ascending)
    assert best[0]["query_intent"] in ("title", "author", None)


@pytest.mark.asyncio
async def test_word_and_date_filters_get_their_own_entries(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AlphaSearcher])
    )
    base = {"sites": ["alpha"], "sort": "words", "min_relevance": None}

    baseline = await search("过滤", **base)
    assert {row["book_id"] for row in baseline} == {"a1", "a2", "a3"}
    assert _AlphaSearcher.calls == 1

    lower = await search("过滤", min_words=150_000, **base)
    assert {row["book_id"] for row in lower} == {"a2", "a3"}
    assert _AlphaSearcher.calls == 2

    upper = await search("过滤", max_words=250_000, **base)
    assert {row["book_id"] for row in upper} == {"a1", "a3"}
    assert _AlphaSearcher.calls == 3

    recent = await search("过滤", updated_after="2024-01-01", **base)
    assert {row["book_id"] for row in recent} == {"a1", "a2"}
    assert _AlphaSearcher.calls == 4

    # The date object and the ISO string share one identity: this is a hit.
    again = await search("过滤", updated_after=date(2024, 1, 1), **base)
    assert {row["book_id"] for row in again} == {"a1", "a2"}
    assert _AlphaSearcher.calls == 4

    # Repeating a filtered query is a hit and still serves the filtered set.
    repeat = await search("过滤", min_words=150_000, **base)
    assert {row["book_id"] for row in repeat} == {"a2", "a3"}
    assert _AlphaSearcher.calls == 4

    assert search_cache.diagnostics().memory_entries == 4


@pytest.mark.asyncio
async def test_site_selections_do_not_share_an_entry(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AlphaSearcher, _BetaSearcher]),
    )

    alpha_only = await search("站点", sites=["alpha"])
    assert {row["site"] for row in alpha_only} == {"alpha"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (1, 0)

    beta_only = await search("站点", sites=["beta"])
    assert {row["site"] for row in beta_only} == {"beta"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (1, 1)

    both = await search("站点", sites=["alpha", "beta"])
    assert {row["site"] for row in both} == {"alpha", "beta"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (2, 2)

    # Order and case are normalized: this is the `both` entry, not a new one.
    reordered = await search("站点", sites=["BETA", "alpha"])
    assert {row["site"] for row in reordered} == {"alpha", "beta"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (2, 2)


@pytest.mark.asyncio
async def test_aggregate_fingerprint_prevents_a_stale_registry_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SearchCache(now=_Clock())
    monkeypatch.setattr(search_module, "_CACHE", cache)
    monkeypatch.setattr(search_module, "_browser_assisted_sites", lambda: frozenset())
    _reset_searchers()

    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AlphaSearcher])
    )
    first = await search("聚合")
    assert {row["site"] for row in first} == {"alpha"}
    assert _AlphaSearcher.calls == 1

    # Registering another site changes the fingerprint: the old aggregate
    # snapshot must not be served as if beta had participated.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AlphaSearcher, _BetaSearcher]),
    )
    second = await search("聚合")
    assert {row["site"] for row in second} == {"alpha", "beta"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (2, 1)

    # Class order is irrelevant: the fingerprint and the key are unchanged.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BetaSearcher, _AlphaSearcher]),
    )
    third = await search("聚合")
    assert {row["site"] for row in third} == {"alpha", "beta"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (2, 1)

    # Adding a participant invalidates again.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AlphaSearcher, _BetaSearcher, _GammaSearcher]),
    )
    fourth = await search("聚合")
    assert {row["site"] for row in fourth} == {"alpha", "beta", "gamma"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (3, 2)
    assert _GammaSearcher.calls == 1


@pytest.mark.asyncio
async def test_registry_change_does_not_invalidate_an_explicit_selection(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AlphaSearcher])
    )
    first = await search("指定", sites=["alpha"])
    assert {row["site"] for row in first} == {"alpha"}
    assert _AlphaSearcher.calls == 1

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AlphaSearcher, _BetaSearcher]),
    )
    second = await search("指定", sites=["alpha"])
    assert {row["site"] for row in second} == {"alpha"}
    assert (_AlphaSearcher.calls, _BetaSearcher.calls) == (1, 0)


@pytest.mark.asyncio
async def test_query_intent_is_recomputed_per_call_and_never_cached(
    search_cache: SearchCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _IntentSearcher(_StaticSearcher):
        site_name = "intent"
        rows = (
            _row(
                "i1",
                "intent",
                title="斗破苍穹",
                author="天蚕土豆",
                word_count="30万字",
            ),
        )

    _IntentSearcher.calls = 0
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_IntentSearcher])
    )

    ranked = await search(
        "天蚕土豆", sites=["intent"], sort="quality", min_relevance=None
    )
    assert [row["query_intent"] for row in ranked] == ["author"]

    words = await search("天蚕土豆", sites=["intent"], sort="words", min_relevance=None)
    assert [row["query_intent"] for row in words] == [None]
    # Same cache entry: the intent is derived after the lookup, not part of it.
    assert _IntentSearcher.calls == 1

    key = _build_cache_key("天蚕土豆", ["intent"], 5, False, 5.0, classes=[])
    raw = search_cache.get(key)
    assert raw is not None
    assert "query_intent" not in raw[0]


# --------------------------------------------------------------------------- #
# disk layer: persistence, corrupt files, diagnostics
# --------------------------------------------------------------------------- #
def _disk_cache(
    directory: Path,
    *,
    clock: _Clock,
    ttl: float = 300.0,
    max_disk_entries: int = 16,
    max_disk_bytes: int = 2 * 1024 * 1024,
) -> SearchCache:
    return SearchCache(
        ttl=ttl,
        now=clock,
        wall_clock=clock,
        disk_dir=directory,
        max_disk_entries=max_disk_entries,
        max_disk_bytes=max_disk_bytes,
    )


def _payload_path(cache: SearchCache, key: CacheKey) -> Path:
    return cache._disk_path(key)


def test_disk_entry_survives_a_new_instance(tmp_path: Path) -> None:
    clock = _Clock()
    key = _build("持久")
    row = _row("p1")

    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [row])
    payload = _payload_path(writer, key)
    assert payload.exists()
    assert payload.name.startswith("v1-") and payload.suffix == ".json"

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) == [row]
    diagnostics = reader.diagnostics()
    assert diagnostics.disk_enabled is True
    assert diagnostics.disk_entries == 1
    assert diagnostics.hits == 1
    assert diagnostics.memory_entries == 1
    # The promoted memory entry serves the next lookup.
    assert reader.get(key) == [row]
    assert reader.diagnostics().hits == 2


def test_disk_entry_is_visible_to_a_fresh_process(tmp_path: Path) -> None:
    cache = SearchCache(disk_dir=tmp_path)
    key = build_key("跨进程", ["alpha"])
    cache.put(key, [_row("x1")])

    script = """
import json
from pathlib import Path
from novel_downloader.plugins.search_cache import SearchCache, build_key

cache = SearchCache(disk_dir=Path(__import__("sys").argv[1]))
rows = cache.get(build_key("跨进程", ["alpha"]))
print(json.dumps({"count": 0 if rows is None else len(rows),
                  "title": "" if not rows else rows[0]["title"]},
                 ensure_ascii=False))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"count": 1, "title": "title-alpha-x1"}


_Corruption = tuple[str, Callable[[dict[str, object]], bytes]]

_CORRUPTIONS: list[_Corruption] = [
    ("malformed-json", lambda _payload: b'{"version":1'),
    (
        "unknown-version",
        lambda payload: json.dumps({**payload, "version": 999}).encode(),
    ),
    (
        "wrong-key",
        lambda payload: json.dumps({**payload, "key": "another-query"}).encode(),
    ),
    (
        "bad-timestamp",
        lambda payload: json.dumps({**payload, "stored_at": "yesterday"}).encode(),
    ),
    (
        "results-not-a-list",
        lambda payload: json.dumps({**payload, "results": 7}).encode(),
    ),
    (
        "row-missing-fields",
        lambda payload: json.dumps(
            {**payload, "results": [{"site": "alpha"}]}
        ).encode(),
    ),
    ("payload-not-a-mapping", lambda _payload: b"[]"),
]


@pytest.mark.parametrize(
    "mutate", [case[1] for case in _CORRUPTIONS], ids=[case[0] for case in _CORRUPTIONS]
)
def test_corrupt_disk_payload_is_ignored_and_rebuilt(
    tmp_path: Path, mutate: Callable[[dict[str, object]], bytes]
) -> None:
    clock = _Clock()
    key = _build("损坏")
    row = _row("c1")

    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [row])
    path = _payload_path(writer, key)
    payload = json.loads(path.read_bytes())
    path.write_bytes(mutate(payload))

    reader = _disk_cache(tmp_path, clock=clock)
    assert reader.get(key) is None
    assert reader.diagnostics().misses == 1

    # A miss is followed by a fresh fetch and a rewrite of the same file.
    reader.put(key, [row])
    assert json.loads(path.read_bytes())["version"] == 1
    assert _disk_cache(tmp_path, clock=clock).get(key) == [row]


def test_oversized_disk_payload_is_ignored(tmp_path: Path) -> None:
    clock = _Clock()
    key = _build("超大")
    row = _row("o1")

    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [row])
    path = _payload_path(writer, key)
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * 64)

    reader = _disk_cache(tmp_path, clock=clock, max_disk_bytes=len(raw) + 32)
    assert reader.get(key) is None

    # A normal instance can rebuild it.
    normal = _disk_cache(tmp_path, clock=clock)
    normal.put(key, [row])
    assert _disk_cache(tmp_path, clock=clock).get(key) == [row]


def test_snapshot_larger_than_the_limit_stays_in_memory(tmp_path: Path) -> None:
    clock = _Clock()
    key = _build("内存")
    cache = _disk_cache(tmp_path, clock=clock, max_disk_bytes=64)
    cache.put(key, [_row("m1")])

    assert not _payload_path(cache, key).exists()
    assert cache.get(key) == [_row("m1")]


def test_unreadable_and_unwritable_disk_failures_are_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    key = _build("权限")
    row = _row("r1")
    writer = _disk_cache(tmp_path, clock=clock)
    writer.put(key, [row])
    path = _payload_path(writer, key)
    original_open = Path.open

    def deny_read(self: Path, *args: object, **kwargs: object) -> Any:
        if self == path:
            raise PermissionError("denied by test")
        return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as context:
        context.setattr(Path, "open", deny_read)
        reader = _disk_cache(tmp_path, clock=clock)
        assert reader.get(key) is None
        assert reader.diagnostics().misses == 1

    def deny_write(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied by test")

    with monkeypatch.context() as context:
        context.setattr(SearchCache, "_write_atomically", deny_write)
        cache = _disk_cache(tmp_path, clock=clock)
        cache.put(key, [row])  # must not raise
        assert cache.get(key) == [row]  # memory still serves the search
    # The previous complete payload was not half-overwritten.
    assert json.loads(path.read_bytes())["version"] == 1


def test_diagnostics_counts_match_real_operations() -> None:
    clock = _Clock()
    cache = SearchCache(ttl=10.0, max_entries=2, now=clock)
    k1, k2, k3, k4 = (_build(f"diag-{index}") for index in range(4))

    empty = cache.diagnostics()
    assert (empty.memory_entries, empty.expired_entries) == (0, 0)
    assert empty.disk_enabled is False and empty.disk_entries == 0
    assert (empty.hits, empty.misses, empty.evictions) == (0, 0, 0)

    cache.put(k1, [_row("d1")])
    cache.put(k2, [_row("d2")])
    assert cache.get(k1) is not None  # hit
    assert cache.get(_build("absent")) is None  # miss
    cache.put(k3, [_row("d3")])  # k2 is least recently used -> eviction

    snapshot = cache.diagnostics()
    assert snapshot.memory_entries == 2
    assert snapshot.expired_entries == 0
    assert (snapshot.hits, snapshot.misses, snapshot.evictions) == (1, 1, 1)

    clock.advance(10.0)  # both live entries reach the TTL
    expired = cache.diagnostics()
    assert expired.memory_entries == 2
    assert expired.expired_entries == 2
    # Diagnostics never mutates the cache or its counters.
    assert (expired.hits, expired.misses, expired.evictions) == (1, 1, 1)

    cache.put(k4, [_row("d4")])  # purges expired entries; no new eviction
    after = cache.diagnostics()
    assert after.memory_entries == 1
    assert after.expired_entries == 0
    assert (after.hits, after.misses, after.evictions) == (1, 1, 1)
    assert cache.stats() == CacheStats(hits=1, misses=1, evictions=1)


def test_diagnostics_counts_only_current_format_disk_payloads(tmp_path: Path) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock=clock)
    cache.put(_build("disk-diag"), [_row("dd1")])
    (tmp_path / "v1-extra.json").write_text("{}", encoding="utf-8")
    (tmp_path / "v2-old.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")

    diagnostics = cache.diagnostics()
    assert diagnostics.disk_enabled is True
    assert diagnostics.disk_entries == 2  # the payload plus v1-extra.json
    assert diagnostics.memory_entries == 1


def test_clear_removes_only_v1_payloads_and_keeps_counters(tmp_path: Path) -> None:
    clock = _Clock()
    cache = _disk_cache(tmp_path, clock=clock)
    key = _build("清空")
    cache.put(key, [_row("cl1")])
    cache.put(_build("清空-2"), [_row("cl2")])
    assert cache.get(key) == [_row("cl1")]  # a hit to record

    unrelated = {
        "v1-notes.txt": "keep-v1-text",
        "v2-old.json": "keep-v2",
        "notes.json": "keep-notes",
        ".v1-tmp.json.tmp": "keep-tmp",
    }
    for name, content in unrelated.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    (tmp_path / "v1-orphan.json").write_text("{}", encoding="utf-8")

    before = cache.stats()
    cache.clear()

    assert not list(tmp_path.glob("v1-*.json"))
    for name, content in unrelated.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content
    assert cache.stats() == before
    assert cache.diagnostics().disk_entries == 0
    assert cache.diagnostics().memory_entries == 0


# --------------------------------------------------------------------------- #
# concurrent readers/writers on one directory
# --------------------------------------------------------------------------- #
def _parse_every_payload(directory: Path) -> list[Path]:
    files = sorted(directory.glob("v1-*.json"))
    for path in files:
        json.loads(path.read_bytes())  # must never be a half-written document
    assert not list(directory.glob("*.tmp"))
    assert not list(directory.glob(".*.tmp"))
    return files


def test_threads_do_not_create_half_files_or_crash(tmp_path: Path) -> None:
    clock = _Clock()
    caches = [_disk_cache(tmp_path, clock=clock) for _ in range(4)]
    keys = [build_key(f"thread-{index}", ["alpha"]) for index in range(4)]
    barrier = threading.Barrier(len(caches))
    failures: list[Exception] = []

    def worker(index: int) -> None:
        rng = random.Random(20240101 + index)
        try:
            barrier.wait(timeout=10.0)
            for step in range(30):
                key = keys[rng.randrange(len(keys))]
                caches[index].put(key, [_row(str(step))])
                got = caches[index].get(key)
                assert got is None or len(got) == 1
                caches[index].diagnostics()
        except Exception as exc:  # pragma: no cover - only on a regression
            failures.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,)) for index in range(len(caches))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    files = _parse_every_payload(tmp_path)
    assert len(files) == len(keys)

    reader = _disk_cache(tmp_path, clock=clock)
    assert all(reader.get(key) is not None for key in keys)


def test_processes_do_not_create_half_files_or_crash(tmp_path: Path) -> None:
    script = """
import random
import sys
from pathlib import Path

from novel_downloader.plugins.search_cache import SearchCache, build_key

directory = Path(sys.argv[1])
rng = random.Random(int(sys.argv[2]))
cache = SearchCache(disk_dir=directory, max_disk_entries=16)
keys = [build_key("process-%d" % index, ["alpha"]) for index in range(3)]

for step in range(12):
    order = list(keys)
    rng.shuffle(order)
    for key in order:
        cache.put(key, [{
            "site": "alpha",
            "book_id": str(step),
            "book_url": "https://example.com/%d" % step,
            "cover_url": "",
            "title": "title-%d" % step,
            "author": "author",
            "latest_chapter": "",
            "update_date": "",
            "word_count": "",
            "priority": 1000,
        }])
        cache.get(key)

print("ok")
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(seed)],
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for seed in (11, 22, 33)
    ]
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(f"child process hung: {stdout!r} {stderr!r}")
        assert process.returncode == 0, stderr
        assert stdout.strip() == "ok"

    files = _parse_every_payload(tmp_path)
    assert len(files) == 3

    reader = SearchCache(disk_dir=tmp_path)
    keys = [build_key(f"process-{index}", ["alpha"]) for index in range(3)]
    assert all(reader.get(key) is not None for key in keys)
