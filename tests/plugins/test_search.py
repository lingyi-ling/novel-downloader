#!/usr/bin/env python3
"""Tests for the cross-site search orchestration."""

import asyncio
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import SearchEvent, search, search_stream
from novel_downloader.schemas import SearchResult

_SEEN: list[bool] = []


class _FlagSearcher(BaseSearcher):
    """Searcher recording whether it was explicitly selected."""

    site_name = "flag_site"

    async def _fetch_html(self, keyword: str) -> str:
        _SEEN.append(self.explicitly_selected)
        return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


@pytest.fixture(autouse=True)
def _only_flag_searcher(monkeypatch: pytest.MonkeyPatch) -> None:
    _SEEN.clear()

    def _classes(
        sites: Any = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        return [_FlagSearcher]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)


@pytest.mark.asyncio
async def test_aggregate_search_leaves_selection_implicit() -> None:
    await search("keyword")
    assert _SEEN == [False]


@pytest.mark.asyncio
async def test_explicit_sites_are_marked_as_selected() -> None:
    await search("keyword", sites=["flag_site"])
    assert _SEEN == [True]


@pytest.mark.asyncio
async def test_search_stream_marks_explicit_sites() -> None:
    async for _ in search_stream("keyword", sites=["flag_site"]):
        pass
    assert _SEEN == [True]


@pytest.mark.asyncio
async def test_search_stream_aggregate_leaves_selection_implicit() -> None:
    async for _ in search_stream("keyword"):
        pass
    assert _SEEN == [False]


# --------------------------------------------------------------------------- #
# selection semantics, per-site isolation and cache-hit re-ranking
# --------------------------------------------------------------------------- #


def _stub_result(site: str, book_id: str) -> SearchResult:
    return {
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


_DEBUG_KEYS = frozenset({"rank_score", "match_tier"})


def _plain(rows: Sequence[SearchResult]) -> list[SearchResult]:
    """Strip the ranking telemetry ``search`` attaches to final rows.

    ``search`` annotates every returned row with ``rank_score``/``match_tier``
    (see ``RankedSearchResult``); tests that compare against a raw site row
    use this to ignore those derived keys.
    """
    return [
        cast(
            "SearchResult",
            {key: value for key, value in row.items() if key not in _DEBUG_KEYS},
        )
        for row in rows
    ]


class _RecordingSearcher(BaseSearcher):
    """Searcher counting fetches and returning one stub result."""

    site_name = "recorder"
    calls = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_stub_result(self.site_name, "1")]


class _NonePayloadSearcher(_RecordingSearcher):
    """Searcher whose parser returns ``None`` (a buggy site plugin)."""

    site_name = "nonepayload"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return None  # type: ignore[return-value]


class _RaisingSearcher(_RecordingSearcher):
    """Searcher whose fetch always raises."""

    site_name = "raising"

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        raise RuntimeError("site down")


class _EmptyResultSearcher(_RecordingSearcher):
    """Searcher that succeeds but reports no rows at all."""

    site_name = "empty"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


class _DirtyRowsSearcher(BaseSearcher):
    """Searcher returning rows a progressively rendered UI must not trust."""

    site_name = "dirty"
    calls = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).calls += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        good = _stub_result(self.site_name, "good")
        rows: list[SearchResult] = [
            good,
            # No site key: no consumer can route a download for it.
            {**_stub_result("", "orphan"), "site": "   "},
            # A script URL must never reach a WebView/browser helper.
            {**_stub_result(self.site_name, "js"), "book_url": "javascript:alert(1)"},
            # Same site, title and URL as ``good`` but a different id: the
            # exact-duplicate fold keeps only the first occurrence.
            {
                **_stub_result(self.site_name, "good-copy"),
                "title": good["title"],
                "book_url": good["book_url"],
            },
            # Neither an id nor a URL: names no book at all.
            {
                **_stub_result(self.site_name, ""),
                "book_id": "",
                "book_url": "",
            },
        ]
        return rows if limit is None else rows[:limit]


class _AllDirtySearcher(_RecordingSearcher):
    """Searcher whose every row is unusable once cleaned."""

    site_name = "alldirty"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            {
                **_stub_result(self.site_name, ""),
                "book_id": "",
                "book_url": "javascript:alert(1)",
            },
            {
                **_stub_result("", ""),
                "site": "",
                "book_id": "",
                "book_url": "",
            },
        ]


class _MultiResultSearcher(BaseSearcher):
    """Searcher returning one strong and one weak candidate."""

    site_name = "multi"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        weak: SearchResult = {
            "site": self.site_name,
            "book_id": "weak",
            "book_url": "https://example.com/multi/weak",
            "cover_url": "",
            "title": "keyword外传",
            "author": "author",
            "latest_chapter": "",
            "update_date": "2020-01-01",
            "word_count": "1万字",
            "priority": 1000,
        }
        strong: SearchResult = {
            "site": self.site_name,
            "book_id": "strong",
            "book_url": "https://example.com/multi/strong",
            "cover_url": "https://example.com/multi/cover.jpg",
            "title": "keyword",
            "author": "author",
            "latest_chapter": "第100章",
            "update_date": "2024-06-14",
            "word_count": "300万字",
            "priority": 1000,
        }
        return [weak, strong]


def _mirror_registry(
    classes: Sequence[type[BaseSearcher]],
) -> Callable[..., list[type[BaseSearcher]]]:
    """
    Build a fake ``get_searcher_classes`` mirroring the real registry.

    Like the registry it treats a *falsy* ``sites`` value (``None`` or an
    empty list) as "all sites" and repeats a class once per requested key.
    """

    def _classes(
        sites: Any = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        if sites:
            selected: list[type[BaseSearcher]] = []
            for site in sites:
                for cls in classes:
                    if getattr(cls, "site_name", "") == str(site):
                        selected.append(cls)
            return selected
        return list(classes)

    return _classes


@pytest.fixture()
def _fresh_state() -> Iterator[None]:
    """Reset the shared search cache and the stub searcher counters."""
    import novel_downloader.plugins.search as search_module

    search_module._CACHE.clear()
    _RecordingSearcher.calls = 0
    _DirtyRowsSearcher.calls = 0
    yield
    search_module._CACHE.clear()


@pytest.mark.asyncio
async def test_explicit_empty_site_selection_searches_nothing(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The registry treats a falsy ``sites`` (``[]`` included) as "all sites";
    # an explicit empty selection must search nothing instead.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_RecordingSearcher])
    )

    results = await search("keyword", sites=[])

    assert results == []
    assert _RecordingSearcher.calls == 0


@pytest.mark.asyncio
async def test_stream_explicit_empty_site_selection_searches_nothing(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_RecordingSearcher])
    )

    chunks = [chunk async for chunk in search_stream("keyword", sites=[])]

    assert chunks == []
    assert _RecordingSearcher.calls == 0


@pytest.mark.asyncio
async def test_duplicate_site_selection_searches_site_once(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # Repeating a site key must not run the searcher twice nor duplicate its
    # results in the aggregate.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_RecordingSearcher])
    )

    results = await search("keyword", sites=["recorder", "recorder"])

    assert _RecordingSearcher.calls == 1
    assert _plain(results) == [_stub_result("recorder", "1")]


@pytest.mark.asyncio
async def test_stream_duplicate_site_selection_searches_site_once(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_RecordingSearcher])
    )

    chunks = [
        chunk
        async for chunk in search_stream("keyword", sites=["recorder", "recorder"])
    ]

    assert _RecordingSearcher.calls == 1
    assert chunks == [[_stub_result("recorder", "1")]]


@pytest.mark.asyncio
async def test_duplicate_site_selection_shares_cache_with_singleton(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # ``sites=["recorder", "recorder"]`` and ``sites=["recorder"]`` are the
    # same logical query: the searcher runs once, and the second spelling is
    # served from the cache entry written by the first.  This locks the
    # invariant that ``build_key``'s set-based site normalization and
    # ``_prepare_searchers``' class dedupe stay consistent.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_RecordingSearcher])
    )

    first = await search("keyword", sites=["recorder", "recorder"])
    second = await search("keyword", sites=["recorder"])

    assert _RecordingSearcher.calls == 1
    assert first == second
    assert _plain(first) == [_stub_result("recorder", "1")]


# --------------------------------------------------------------------------- #
# cache-key site normalization
# --------------------------------------------------------------------------- #
def test_build_key_prefixes_digit_leading_site_like_the_registry() -> None:
    from novel_downloader.plugins.search_cache import build_key

    # The registry cannot import a module whose name starts with a digit, so it
    # prefixes "n": both spellings resolve to the plugin ``plugins.sites.n1foo``
    # and must therefore share one cache entry instead of hitting the site
    # twice.
    assert build_key("k", ["1foo"]).sites == ("n1foo",)
    assert build_key("k", ["1foo"]) == build_key("k", ["n1foo"])
    assert build_key("k", [" 1FOO "]) == build_key("k", ["N1FOO"])


def test_build_key_site_rule_matches_registry_normalization() -> None:
    from novel_downloader.plugins.search_cache import build_key

    for name in ("1foo", "n1foo", "Foo", " foo ", "23qb", "n23qb"):
        assert build_key("k", [name]).sites == (registrar._normalize_key(name),)


def test_build_key_keeps_different_sites_apart() -> None:
    from novel_downloader.plugins.search_cache import build_key

    # The digit prefix must not collapse genuinely different site keys.
    assert build_key("k", ["1foo"]) != build_key("k", ["2foo"])
    assert build_key("k", ["1foo"]) != build_key("k", ["foo"])
    assert build_key("k", ["n1foo"]) != build_key("k", ["nfoo"])


@pytest.mark.asyncio
async def test_digit_leading_site_alias_shares_cache(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # End-to-end: the registry resolves "1recorder" and "n1recorder" to the same
    # plugin key, so the two spellings build one cache key and the searcher runs
    # once even though the caller spelled the site differently.
    def _classes(
        sites: Any = None, *, load_all_if_none: bool = True
    ) -> list[type[BaseSearcher]]:
        if not sites:
            return [_RecordingSearcher]
        return [
            _RecordingSearcher
            for site in sites
            if registrar._normalize_key(str(site)) == "n1recorder"
        ]

    monkeypatch.setattr(registrar, "get_searcher_classes", _classes)

    first = await search("keyword", sites=["1recorder"])
    second = await search("keyword", sites=["n1recorder"])

    assert _RecordingSearcher.calls == 1
    assert first == second
    assert _plain(first) == [_stub_result("recorder", "1")]


@pytest.mark.asyncio
async def test_invalid_site_payload_is_isolated_and_reported(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # A searcher returning something other than a list is a site failure: it
    # must not crash the aggregate nor swallow the other sites' results.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _NonePayloadSearcher, _RaisingSearcher]),
    )
    events: list[SearchEvent] = []

    results = await search("keyword", on_event=events.append)

    assert _plain(results) == [_stub_result("recorder", "1")]
    kinds = {(event.site, event.kind) for event in events}
    assert ("recorder", "completed") in kinds
    assert ("nonepayload", "failed") in kinds
    assert ("raising", "failed") in kinds


@pytest.mark.asyncio
async def test_stream_invalid_site_payload_is_isolated_and_reported(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _NonePayloadSearcher, _RaisingSearcher]),
    )
    events: list[SearchEvent] = []

    chunks = [chunk async for chunk in search_stream("keyword", on_event=events.append)]

    assert chunks == [[_stub_result("recorder", "1")]]
    kinds = {(event.site, event.kind) for event in events}
    assert ("recorder", "completed") in kinds
    assert ("nonepayload", "failed") in kinds
    assert ("raising", "failed") in kinds


# --------------------------------------------------------------------------- #
# prepare-time searcher failures and cancellation propagation
# --------------------------------------------------------------------------- #


class _BrokenInitSearcher(_RecordingSearcher):
    """Searcher whose constructor always fails (a broken site plugin)."""

    site_name = "broken_init"

    def __init__(self, session: Any) -> None:
        raise RuntimeError("constructor boom")


class _BrokenNsfwSearcher(_RecordingSearcher):
    """Searcher whose ``nsfw`` property always fails."""

    site_name = "broken_nsfw"

    @property
    def nsfw(self) -> bool:
        raise RuntimeError("nsfw boom")


class _CancellingSearcher(_RecordingSearcher):
    """Searcher whose search task raises CancelledError."""

    site_name = "cancelling"

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_broken_constructor_is_isolated_per_site(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # A searcher that cannot even be instantiated is one site's failure: the
    # aggregate must still return the other sites' results.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BrokenInitSearcher, _RecordingSearcher]),
    )
    events: list[SearchEvent] = []

    results = await search("keyword", on_event=events.append)

    assert _plain(results) == [_stub_result("recorder", "1")]
    kinds = {(event.site, event.kind) for event in events}
    assert ("broken_init", "failed") in kinds
    assert ("recorder", "completed") in kinds


@pytest.mark.asyncio
async def test_broken_nsfw_property_is_isolated_per_site(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BrokenNsfwSearcher, _RecordingSearcher]),
    )
    events: list[SearchEvent] = []

    results = await search("keyword", on_event=events.append)

    assert _plain(results) == [_stub_result("recorder", "1")]
    kinds = {(event.site, event.kind) for event in events}
    assert ("broken_nsfw", "failed") in kinds
    assert ("recorder", "completed") in kinds


@pytest.mark.asyncio
async def test_cancelled_searcher_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # Cancellation must reach the caller (matching the streaming path): the
    # aggregate path used to swallow it as a per-site "failed" event.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _CancellingSearcher]),
    )
    events: list[SearchEvent] = []

    with pytest.raises(asyncio.CancelledError):
        await search("keyword", on_event=events.append)

    # No per-site failure event was fabricated for the cancellation itself.
    assert ("cancelling", "failed") not in {(e.site, e.kind) for e in events}


@pytest.mark.asyncio
async def test_cache_hit_ranks_and_slices_per_call(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The cache stores the unranked merged snapshot; every call (fresh or
    # cached) applies its own sort order and truncates only after ranking.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MultiResultSearcher])
    )

    top = await search("keyword", limit=1)
    assert [r["book_id"] for r in top] == ["strong"]

    full = await search("keyword")  # cache hit: full snapshot, re-ranked
    assert [r["book_id"] for r in full] == ["strong", "weak"]

    asc = await search("keyword", sort="words", order="asc", limit=1)
    assert [r["book_id"] for r in asc] == ["weak"]


@pytest.mark.asyncio
async def test_finalize_never_reorders_or_drops_from_the_cache_snapshot(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # ``_finalize`` sorts (and may dedupe) *its* list; the cached snapshot must
    # stay in fetch order so a later call can rank it differently.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MultiResultSearcher])
    )

    await search("keyword")  # the fresh call ranks its own copy
    assert [r["book_id"] for r in _cache_snapshot("keyword")] == ["weak", "strong"]

    await search("keyword", sort="words", order="desc")
    assert [r["book_id"] for r in _cache_snapshot("keyword")] == ["weak", "strong"]


# --------------------------------------------------------------------------- #
# same-book grouping (group=True)
# --------------------------------------------------------------------------- #

_RESULT_KEYS = frozenset(
    {
        "site",
        "book_id",
        "book_url",
        "cover_url",
        "title",
        "author",
        "latest_chapter",
        "update_date",
        "word_count",
        "priority",
    }
)


class _BookCopySearcher(BaseSearcher):
    """Searcher offering one copy of a configurable book on its own site."""

    site_name = "alpha"
    book_title = "三体"
    book_author = "刘慈欣"
    word_count = "10万字"
    fetches = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).fetches += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            {
                "site": self.site_name,
                "book_id": f"{self.site_name}-1",
                "book_url": f"https://example.com/{self.site_name}/1",
                "cover_url": "",
                "title": self.book_title,
                "author": self.book_author,
                "latest_chapter": "",
                "update_date": "",
                "word_count": self.word_count,
                "priority": 100,
            }
        ]


class _StrongCopySearcher(_BookCopySearcher):
    """A second site carrying a (ranked) better copy of the same book."""

    site_name = "beta"
    word_count = "300万字"


class _NamesakeSearcher(_BookCopySearcher):
    """A second site with the same title but a different author."""

    site_name = "delta"
    book_author = "天蚕土豆"


class _OtherBookSearcher(_BookCopySearcher):
    """A second, distinct book (used to check the limit counts books)."""

    site_name = "gamma"
    book_title = "球状闪电"
    word_count = "50万字"


class _DuplicateSiteSearcher(_BookCopySearcher):
    """One site returning the same book twice (two book ids)."""

    site_name = "alpha"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        first = dict(super()._parse_html(html_str, limit)[0])
        duplicate = dict(first)
        duplicate["book_id"] = "alpha-2"
        duplicate["book_url"] = "https://example.com/alpha/2"
        return [first, duplicate]


@pytest.mark.asyncio
async def test_group_merges_same_book_across_sites(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    merged = await search("三体", group=True)

    assert len(merged) == 1
    entry = merged[0]
    # The merged entry is the group's best copy (higher word count), enriched
    # with the source capsules, best first.
    assert entry["site"] == "beta"
    assert entry["book_id"] == "beta-1"
    assert entry["book_url"] == "https://example.com/beta/1"
    assert entry["title"] == "三体"
    assert entry["sources"] == ["beta", "alpha"]
    # A merged entry is still a complete SearchResult.
    assert set(entry) >= _RESULT_KEYS


@pytest.mark.asyncio
async def test_search_stays_flat_without_group(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    results = await search("三体")

    assert sorted(r["site"] for r in results) == ["alpha", "beta"]
    assert all("sources" not in r for r in results)


@pytest.mark.asyncio
async def test_group_dedupes_capsules_of_one_site(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_DuplicateSiteSearcher])
    )

    merged = await search("三体", group=True)

    assert len(merged) == 1
    # Two copies of the book from the same site collapse to one capsule.
    assert merged[0]["sources"] == ["alpha"]
    assert merged[0]["book_id"] == "alpha-1"


@pytest.mark.asyncio
async def test_group_keeps_distinct_authors_separate(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _NamesakeSearcher]),
    )

    merged = await search("三体", group=True)

    # Two books that merely share a title must not be fused into one group.
    assert sorted(entry["author"] for entry in merged) == ["刘慈欣", "天蚕土豆"]
    assert all(entry["sources"] == [entry["site"]] for entry in merged)


@pytest.mark.asyncio
async def test_group_applies_limit_after_merging(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher, _OtherBookSearcher]),
    )

    merged = await search("三体", group=True, limit=1)

    # With grouping the limit counts books, not raw rows: the top book (the
    # one with the strongest copy) survives, the other is dropped whole.
    assert len(merged) == 1
    assert merged[0]["title"] == "三体"
    assert merged[0]["sources"] == ["beta", "alpha"]


@pytest.mark.asyncio
async def test_group_on_cache_hit_merges_cached_snapshot(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The flat call writes the cache entry; the grouped call is served from
    # it without re-searching, and grouping is applied per call.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )
    _BookCopySearcher.fetches = 0
    _StrongCopySearcher.fetches = 0

    flat = await search("三体")
    merged = await search("三体", group=True)

    assert len(flat) == 2
    assert len(merged) == 1
    assert merged[0]["sources"] == ["beta", "alpha"]
    assert _BookCopySearcher.fetches == 1
    assert _StrongCopySearcher.fetches == 1
    # The cached snapshot itself stays flat and pristine.
    cached = _cache_snapshot("三体")
    assert all("sources" not in r for r in cached)


def _cache_snapshot(keyword: str) -> list[SearchResult]:
    """Read the shared cache's entry for ``keyword`` (test introspection)."""
    import novel_downloader.plugins.search as search_module

    # An aggregate key now includes a configuration fingerprint; rebuild it
    # through the same helper ``search`` uses so the probe finds the entry.
    classes = registrar.get_searcher_classes(None, load_all_if_none=True)
    key = search_module._build_cache_key(keyword, None, 5, False, 5.0, classes=classes)
    cached = search_module._CACHE.get(key)
    assert cached is not None
    return cached


def test_finalize_group_merges_decorated_duplicate_titles() -> None:
    """A decorative ``[网游]`` prefix must not leave a duplicate book row."""
    from novel_downloader.plugins.search import _finalize

    results: list[SearchResult] = [
        {
            "site": "alpha",
            "book_id": "plain",
            "title": "斗罗",
            "author": "甲",
            "word_count": "1万字",
            "url": "https://alpha/1",
        },
        {
            "site": "beta",
            "book_id": "decorated",
            "title": "[网游]斗罗",
            "author": "甲",
            "word_count": "300万字",
            "url": "https://beta/2",
        },
        {
            "site": "gamma",
            "book_id": "other",
            "title": "另一本书",
            "author": "乙",
            "word_count": "1万字",
            "url": "https://gamma/3",
        },
    ]

    merged = _finalize(
        results,
        keyword="",
        sort="quality",
        order=None,
        limit=None,
        group=True,
    )

    by_title = {entry["title"]: entry["sources"] for entry in merged}
    assert by_title == {"[网游]斗罗": ["beta", "alpha"], "另一本书": ["gamma"]}


@pytest.mark.asyncio
async def test_stream_cache_hit_keeps_per_site_chunks(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # ``search()`` warms the shared cache with the raw per-site snapshot.  A
    # later ``search_stream`` must still honour the chunk contract -- one chunk
    # per site -- instead of yielding the whole snapshot as one mixed batch.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    await search("三体")

    chunks = [chunk async for chunk in search_stream("三体")]

    assert len(chunks) == 2
    assert {tuple(item["site"] for item in chunk) for chunk in chunks} == {
        ("alpha",),
        ("beta",),
    }


@pytest.mark.asyncio
async def test_stream_cache_hit_chunks_match_a_fresh_run(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    def _shape(chunks: list[list[SearchResult]]) -> list[tuple[str, ...]]:
        return sorted(tuple(item["book_id"] for item in chunk) for chunk in chunks)

    fresh = [chunk async for chunk in search_stream("三体")]
    cached = [chunk async for chunk in search_stream("三体")]

    assert _shape(cached) == _shape(fresh)
    # Every cached chunk still belongs to exactly one site.
    assert all(len({item["site"] for item in chunk}) == 1 for chunk in cached)


@pytest.mark.asyncio
async def test_stream_cache_hit_omits_sites_that_returned_no_rows(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The snapshot stores rows, not per-site slots, so a site that succeeded
    # with zero results has no chunk -- neither in a fresh run nor on a cache
    # hit (the contract is "one non-empty list per site").
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _EmptyResultSearcher]),
    )

    await search("keyword")
    assert _RecordingSearcher.calls == 1

    chunks = [chunk async for chunk in search_stream("keyword")]

    assert chunks == [[_stub_result("recorder", "1")]]
    assert _RecordingSearcher.calls == 1  # served from the cache, no refetch


# --------------------------------------------------------------------------- #
# streamed chunks are cleaned before they reach a progressive UI
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_chunks_are_cleaned_like_final_results(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_DirtyRowsSearcher])
    )

    chunks = [chunk async for chunk in search_stream("三体", sites=["dirty"])]

    assert len(chunks) == 1
    chunk = chunks[0]
    # The per-site chunk contract survives cleaning.
    assert {item["site"] for item in chunk} == {"dirty"}
    # Blank-site, identity-less and same-URL duplicate rows are gone; the
    # unsafe URL is cleared on a copy instead of discarding the usable row.
    assert [item["book_id"] for item in chunk] == ["good", "js"]
    assert chunk[0]["book_url"] == "https://example.com/dirty/good"
    assert chunk[1]["book_url"] == ""


@pytest.mark.asyncio
async def test_stream_cache_hit_chunks_are_cleaned_identically(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import novel_downloader.plugins.search as search_module
    from novel_downloader.plugins.search_cache import build_key

    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_DirtyRowsSearcher])
    )

    fresh = [chunk async for chunk in search_stream("三体", sites=["dirty"])]
    cached = [chunk async for chunk in search_stream("三体", sites=["dirty"])]

    # A warmed stream yields byte-identical chunks: cleaning runs on every
    # output, so the cache cannot leak rows the fresh run would have dropped.
    assert cached == fresh
    assert _DirtyRowsSearcher.calls == 1  # the second run came from the cache

    # The cache itself keeps the raw payload: cleaning is an output-time
    # concern, so a later ``search`` can still rank/filter the raw rows.
    raw = search_module._CACHE.get(build_key("三体", ["dirty"], 5, False, 5.0))
    assert raw is not None
    assert any(row["book_url"] == "javascript:alert(1)" for row in raw)

    # And that raw snapshot is cleaned by ``search``'s own finalize step.
    final = await search("三体", sites=["dirty"], sort="priority")
    assert {row["book_id"] for row in final} == {"good", "js"}
    assert all("javascript:" not in row["book_url"] for row in final)


@pytest.mark.asyncio
async def test_stream_cleaning_shrinks_a_chunk_without_refetching(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # ``per_site_limit`` is applied by the site *before* cleaning, so cleaning
    # can only remove rows: a chunk never grows past the limit, and it never
    # triggers a second fetch to backfill the rows that were dropped.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_DirtyRowsSearcher])
    )

    chunks = [
        chunk
        async for chunk in search_stream("三体", sites=["dirty"], per_site_limit=2)
    ]

    assert len(chunks) == 1
    assert [item["book_id"] for item in chunks[0]] == ["good"]
    assert len(chunks[0]) <= 2
    assert _DirtyRowsSearcher.calls == 1


@pytest.mark.asyncio
async def test_stream_omits_a_chunk_cleaning_emptied(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import novel_downloader.plugins.search as search_module
    from novel_downloader.plugins.search_cache import build_key

    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_AllDirtySearcher])
    )

    chunks = [chunk async for chunk in search_stream("keyword", sites=["alldirty"])]

    # A site whose every row is unusable produces no chunk at all (the chunk
    # contract is "non-empty per-site list"), not an empty list.
    assert chunks == []
    # An effectively empty stream is not pinned in the cache.
    assert (
        search_module._CACHE.get(build_key("keyword", ["alldirty"], 5, False, 5.0))
        is None
    )


# ---------------------------------------------------------------------------
# intent-aware ranking is wired into the search pipeline
# ---------------------------------------------------------------------------


def test_finalize_ranks_an_author_query_by_author() -> None:
    """Searching an author's name must not be outranked by a title that contains it."""
    from novel_downloader.plugins.search import _finalize

    results: list[SearchResult] = [
        {
            "site": "a",
            "book_id": "1",
            "book_url": "https://a/1",
            "title": "天蚕土豆",
            "author": "路人甲",
        },
        {
            "site": "a",
            "book_id": "2",
            "book_url": "https://a/2",
            "title": "斗破苍穹",
            "author": "天蚕土豆",
        },
    ]

    _finalize(
        results,
        keyword="天蚕土豆",
        sort="quality",
        order=None,
        limit=None,
        group=False,
    )

    assert results[0]["title"] == "斗破苍穹"


def test_finalize_sparse_author_work_beats_rich_title_collision() -> None:
    """Richer metadata on a title collision must not defeat intent ranking."""
    from novel_downloader.plugins.search import _finalize

    collision: SearchResult = {
        "site": "a",
        "book_id": "1",
        "book_url": "https://a/1",
        "title": "天蚕土豆",
        "author": "路人甲",
        "word_count": "300万字",
        "update_date": "2024-06-14",
        "cover_url": "https://a/c.jpg",
        "latest_chapter": "第100章",
        "priority": 5,
    }
    author_book: SearchResult = {
        "site": "b",
        "book_id": "2",
        "book_url": "https://b/2",
        "title": "斗破苍穹",
        "author": "天蚕土豆",
    }

    ranked = _finalize(
        [collision, author_book],
        keyword="天蚕土豆",
        sort="quality",
        order=None,
        limit=None,
        group=False,
    )

    # The author's own (metadata-sparse) book wins; the collision follows.
    assert [entry["title"] for entry in ranked] == ["斗破苍穹", "天蚕土豆"]


def test_finalize_non_quality_sorts_ignore_query_intent() -> None:
    """``by`` other than ``quality`` keeps its own key, unchanged by intent."""
    from novel_downloader.plugins.search import _finalize

    results: list[SearchResult] = [
        {
            "site": "a",
            "book_id": "1",
            "book_url": "https://a/1",
            "title": "短",
            "author": "x",
        },
        {
            "site": "a",
            "book_id": "2",
            "book_url": "https://a/2",
            "title": "长很多的书名",
            "author": "x",
        },
    ]

    _finalize(results, keyword="x", sort="title", order="asc", limit=None, group=False)

    assert [r["title"] for r in results] == ["短", "长很多的书名"]


# --------------------------------------------------------------------------- #
# per-site deadline: a hung site must not hold the whole aggregate hostage
# --------------------------------------------------------------------------- #


class _SlowSearcher(_RecordingSearcher):
    """Searcher that never answers within a test-sized budget."""

    site_name = "slow"

    async def _fetch_html(self, keyword: str) -> str:
        await asyncio.sleep(30)
        return "html"


@pytest.mark.asyncio
async def test_aggregate_search_drops_a_site_that_misses_its_budget(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import time

    events: list[SearchEvent] = []
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowSearcher]),
    )

    started = time.perf_counter()
    results = await search(
        "keyword",
        sites=["recorder", "slow"],
        timeout=0.2,
        on_event=events.append,
    )
    elapsed = time.perf_counter() - started

    # The healthy site's results survive; the hung one is cut at the budget
    # instead of letting a 30-second sleep define the search's wall clock.
    assert [item["site"] for item in results] == ["recorder"]
    assert elapsed < 5.0
    failed = [event for event in events if event.kind == "failed"]
    assert failed
    assert "timed out" in failed[0].message.lower()


@pytest.mark.asyncio
async def test_stream_drops_a_site_that_misses_its_budget(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import time

    events: list[SearchEvent] = []
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowSearcher]),
    )

    started = time.perf_counter()
    chunks = [
        chunk
        async for chunk in search_stream(
            "keyword",
            sites=["recorder", "slow"],
            timeout=0.2,
            on_event=events.append,
        )
    ]
    elapsed = time.perf_counter() - started

    assert [[item["site"] for item in chunk] for chunk in chunks] == [["recorder"]]
    assert elapsed < 5.0
    assert any(event.kind == "failed" for event in events)


@pytest.mark.asyncio
async def test_search_connector_bounds_per_host_connections() -> None:
    """A 59-site fan-out must not open the default 100 sockets per host."""
    from novel_downloader.plugins.search import (
        _MAX_CONNECTIONS_PER_HOST,
        _MAX_CONNECTIONS_TOTAL,
        _make_connector,
    )

    connector = _make_connector()
    try:
        assert connector.limit == _MAX_CONNECTIONS_TOTAL
        assert connector.limit_per_host == _MAX_CONNECTIONS_PER_HOST
        assert _MAX_CONNECTIONS_PER_HOST < _MAX_CONNECTIONS_TOTAL
    finally:
        await connector.close()


# --------------------------------------------------------------------------- #
# relevance filtering (on by default for sort="quality")
# --------------------------------------------------------------------------- #


def _search_result(
    site: str, book_id: str, title: str, **overrides: object
) -> SearchResult:
    base: dict[str, object] = {
        "site": site,
        "book_id": book_id,
        "book_url": f"https://example.com/{site}/{book_id}",
        "cover_url": "",
        "title": title,
        "author": "作者",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 1000,
    }
    base.update(overrides)
    return cast("SearchResult", base)


class _MixedRelevanceSearcher(BaseSearcher):
    """Searcher returning one exact, one derivative and one unrelated book."""

    site_name = "mixed"
    fetches = 0

    async def _fetch_html(self, keyword: str) -> str:
        type(self).fetches += 1
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            _search_result("mixed", "exact", "斗破苍穹", author="天蚕土豆"),
            _search_result("mixed", "deriv", "斗破苍穹之雷霆震碎", author="别人"),
            _search_result("mixed", "unrelated", "三体", author="刘慈欣"),
        ]


@pytest.mark.asyncio
async def test_quality_search_drops_unrelated_rows_by_default(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    results = await search("斗破苍穹")

    # The unrelated book is gone; the genuine derivative survives (its
    # relevance is ~0.7, well above the 0.45 floor).
    assert [item["book_id"] for item in results] == ["exact", "deriv"]


@pytest.mark.asyncio
async def test_quality_search_can_disable_the_relevance_floor(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    results = await search("斗破苍穹", min_relevance=None)

    assert {item["book_id"] for item in results} == {"exact", "deriv", "unrelated"}


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["words", "update", "title"])
async def test_non_quality_sorts_never_filter_rows(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None, sort: str
) -> None:
    # An explicit word-count/update/title ordering must not silently lose data.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    results = await search("斗破苍穹", sort=sort)

    assert {item["book_id"] for item in results} == {"exact", "deriv", "unrelated"}


@pytest.mark.asyncio
async def test_relevance_filter_never_returns_an_empty_page(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    class _AllUnrelatedSearcher(_MixedRelevanceSearcher):
        site_name = "unrelated_only"

        def _parse_html(
            self, html_str: str, limit: int | None = None
        ) -> list[SearchResult]:
            return [
                _search_result("unrelated_only", "a", "三体", author="刘慈欣"),
                _search_result("unrelated_only", "b", "活着", author="余华"),
            ]

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AllUnrelatedSearcher]),
    )

    results = await search("斗破苍穹")

    # Nothing matches: the raw rows come back rather than an empty page.
    assert {item["book_id"] for item in results} == {"a", "b"}


@pytest.mark.asyncio
async def test_relevance_filter_does_not_rewrite_the_cache_snapshot(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The cache keeps every row; filtering is applied per call in _finalize.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    filtered = await search("斗破苍穹")
    everything = await search("斗破苍穹", min_relevance=None)

    assert len(filtered) == 2
    assert len(everything) == 3
    assert len(_cache_snapshot("斗破苍穹")) == 3


# --------------------------------------------------------------------------- #
# exact duplicate folding
# --------------------------------------------------------------------------- #


class _ExactDuplicateSearcher(_BookCopySearcher):
    """One site returning the very same (site, book_id) row twice."""

    site_name = "same"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        first = dict(super()._parse_html(html_str, limit)[0])
        duplicate = dict(first)  # same site and book_id
        other = _search_result("same", "other", "三体前传", author="刘慈欣")
        return [first, duplicate, other]  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_exact_duplicate_rows_are_folded(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_ExactDuplicateSearcher])
    )

    results = await search("三体")

    assert sorted(item["book_id"] for item in results) == ["other", "same-1"]
    # The cached snapshot is untouched, so a later call can still change its mind.
    assert len(_cache_snapshot("三体")) == 3


@pytest.mark.asyncio
async def test_duplicate_folding_happens_before_the_limit(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # If ``limit`` were applied first, the two copies would consume both slots
    # and the second book would disappear.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_ExactDuplicateSearcher])
    )

    results = await search("三体", limit=2)

    assert sorted(item["book_id"] for item in results) == ["other", "same-1"]


@pytest.mark.asyncio
async def test_folding_is_scoped_to_one_site(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # Same book on two sites is a *grouping* concern, not a dedupe one.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    results = await search("三体")

    assert sorted(item["site"] for item in results) == ["alpha", "beta"]


# --------------------------------------------------------------------------- #
# dirty-data sanitization (runs before dedupe / rank / group / limit)
# --------------------------------------------------------------------------- #


def test_sanitize_results_drops_unusable_rows_and_clears_unsafe_urls() -> None:
    from novel_downloader.plugins.search import _sanitize_results

    good = _search_result("alpha", "good", "三体")
    no_site = _search_result("", "x", "三体")
    blank_site = _search_result("   ", "x", "三体")
    no_identity = _search_result("alpha", "", "三体", book_url="")
    blank_identity = _search_result("alpha", "  ", "三体", book_url="  ")
    javascript = _search_result("alpha", "js", "三体", book_url="javascript:alert(1)")
    file_url = _search_result("alpha", "file", "三体", book_url="file:///etc/passwd")
    data_url = _search_result("alpha", "data", "三体", book_url="data:text/html,hi")
    relative = _search_result("alpha", "rel", "三体", book_url="/books/1")
    spaced = _search_result(
        "alpha", "spaced", "三体", book_url="  https://example.com/b/1  "
    )

    cleaned = _sanitize_results(
        [
            good,
            no_site,
            blank_site,
            no_identity,
            blank_identity,
            javascript,
            file_url,
            data_url,
            relative,
            spaced,
        ]
    )

    assert [row["book_id"] for row in cleaned] == [
        "good",
        "js",
        "file",
        "data",
        "rel",
        "spaced",
    ]
    assert cleaned[0]["book_url"] == good["book_url"]
    # Non-http(s) URLs are cleared, not passed to a browser/WebView.
    assert cleaned[1]["book_url"] == ""
    assert cleaned[2]["book_url"] == ""
    assert cleaned[3]["book_url"] == ""
    assert cleaned[4]["book_url"] == ""
    # A legitimate URL is stripped but otherwise kept verbatim.
    assert cleaned[5]["book_url"] == "https://example.com/b/1"
    # Rows are never mutated in place (a cache snapshot shares them).
    assert javascript["book_url"] == "javascript:alert(1)"
    assert good["book_url"] == "https://example.com/alpha/good"


def test_sanitize_keeps_a_row_without_a_book_id_and_does_not_guess_one() -> None:
    from novel_downloader.plugins.search import _sanitize_results

    # The URL contains an id, but extracting it would require site-specific
    # rules the aggregator must not invent: the row is kept verbatim with its
    # empty ``book_id`` so the download entry can reject it explicitly.
    row = _search_result("alpha", "", "三体", book_url="https://example.com/novel/42")

    cleaned = _sanitize_results([row])

    assert cleaned == [row]
    assert cleaned[0]["book_id"] == ""
    assert cleaned[0]["book_url"] == "https://example.com/novel/42"


def test_same_site_title_and_url_is_folded_before_the_limit() -> None:
    from novel_downloader.plugins.search import _finalize

    first = _search_result(
        "alpha", "a", "三体", book_url="https://example.com/1", priority=100
    )
    duplicate = _search_result(
        "alpha", "b", "三体", book_url="https://example.com/1", priority=200
    )
    other = _search_result(
        "alpha", "c", "球状闪电", book_url="https://example.com/2", priority=300
    )

    ranked = _finalize(
        [first, duplicate, other],
        keyword="三体",
        sort="priority",
        order="desc",
        limit=2,
        group=False,
    )

    # Rows sharing site + title + URL fold onto the first occurrence; the
    # fold happens before ``limit`` so the surviving duplicate cannot consume
    # a second slot.
    assert [row["book_id"] for row in ranked] == ["c", "a"]


def test_dirty_rows_are_dropped_before_the_limit() -> None:
    from novel_downloader.plugins.search import _finalize

    no_site = _search_result("", "x", "三体")
    no_identity = _search_result("alpha", "", "三体", book_url="javascript:alert(1)")
    first = _search_result("alpha", "a", "三体")
    second = _search_result("alpha", "b", "三体")

    ranked = _finalize(
        [no_site, no_identity, first, second],
        keyword="三体",
        sort="title",
        order="asc",
        limit=2,
        group=False,
    )

    # Both slots go to real rows; a limit applied first would have been spent
    # on the two unusable ones.
    assert [row["book_id"] for row in ranked] == ["a", "b"]


def test_dirty_rows_cannot_become_a_group_primary() -> None:
    from novel_downloader.plugins.search import _finalize

    # The dirtiest row would win on word count; if it survived grouping, the
    # merged entry would carry an empty site and point nowhere.
    ghost = _search_result("", "ghost", "三体", book_url="", word_count="9999万字")
    real = _search_result("alpha", "real", "三体", word_count="1万字")

    merged = _finalize(
        [ghost, real],
        keyword="三体",
        sort="quality",
        order=None,
        limit=None,
        group=True,
        min_relevance=None,
    )

    assert len(merged) == 1
    assert merged[0]["site"] == "alpha"
    assert merged[0]["book_id"] == "real"
    assert merged[0]["sources"] == ["alpha"]


def test_dirty_rows_do_not_feed_the_relevance_floor() -> None:
    from novel_downloader.plugins.search import _finalize

    ghost = _search_result("", "ghost", "斗破苍穹", book_url="")
    real = _search_result("alpha", "real", "三体", author="刘慈欣")

    ranked = _finalize(
        [ghost, real],
        keyword="斗破苍穹",
        sort="quality",
        order=None,
        limit=None,
        group=False,
        min_relevance=0.45,
    )

    # The dirty row is removed before scoring; the lone valid row survives via
    # the never-empty fallback, instead of the exact-title ghost masking it.
    assert [row["book_id"] for row in ranked] == ["real"]


class _DirtyAggregateSearcher(BaseSearcher):
    """Searcher returning a mix of valid and dirty aggregate rows."""

    site_name = "dirty"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            _search_result("dirty", "good", "三体"),
            _search_result("", "ghost", "三体"),
            _search_result("dirty", "unsafe", "三体", book_url="javascript:alert(1)"),
            _search_result("dirty", "", "三体", book_url=""),
        ]


@pytest.mark.asyncio
async def test_sanitization_does_not_rewrite_the_cache_snapshot(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_DirtyAggregateSearcher])
    )

    results = await search("三体", min_relevance=None)

    assert [row["book_id"] for row in results] == ["good", "unsafe"]
    assert results[1]["book_url"] == ""

    # The cache keeps the raw snapshot: sanitization is per call, and a later
    # call (or a stream cache hit) must still see what the site returned.
    snapshot = _cache_snapshot("三体")
    assert len(snapshot) == 4
    unsafe = next(row for row in snapshot if row["book_id"] == "unsafe")
    assert unsafe["book_url"] == "javascript:alert(1)"


# --------------------------------------------------------------------------- #
# filter -> group -> limit ordering
# --------------------------------------------------------------------------- #


class _FilterCopySearcher(_MixedRelevanceSearcher):
    """A second site copy of the mixed catalogue (exact + derivative + noise)."""

    site_name = "mixed2"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            _search_result("mixed2", "exact", "斗破苍穹", author="天蚕土豆"),
            _search_result("mixed2", "deriv", "斗破苍穹之雷霆震碎", author="别人"),
            _search_result("mixed2", "unrelated", "三体", author="刘慈欣"),
        ]


@pytest.mark.asyncio
async def test_filter_runs_before_grouping_and_limit(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_MixedRelevanceSearcher, _FilterCopySearcher]),
    )

    merged = await search("斗破苍穹", group=True, limit=1)

    # The limit counts books after the unrelated row was filtered out; the one
    # surviving book still carries both sites as sources.
    assert len(merged) == 1
    assert merged[0]["title"] == "斗破苍穹"
    assert merged[0]["sources"] == ["mixed", "mixed2"]


@pytest.mark.asyncio
async def test_group_keeps_derivative_separate_from_the_exact_title(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_MixedRelevanceSearcher, _FilterCopySearcher]),
    )

    merged = await search("斗破苍穹", group=True)

    assert [entry["title"] for entry in merged] == ["斗破苍穹", "斗破苍穹之雷霆震碎"]
    assert all(len(entry["sources"]) == 2 for entry in merged)


# --------------------------------------------------------------------------- #
# cancelled site tasks are reaped before returning
# --------------------------------------------------------------------------- #


class _SlowCancelSearcher(_RecordingSearcher):
    """Searcher whose cancellation is delivered with a short delay.

    Mirrors the Windows proactor loop, where a cancel request can stay
    unobserved for a moment while the cancellation is in flight.
    """

    site_name = "slow_cancel"

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise
        return []


class _ImmortalSearcher(_RecordingSearcher):
    """Searcher that ignores cancellation for longer than the grace period."""

    site_name = "immortal"

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(30)
            raise
        return []


def _site_tasks() -> set[asyncio.Task[Any]]:
    """Every still-pending site search task in the running loop."""
    return {
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().endswith(".search")
    }


@pytest.mark.asyncio
async def test_timed_out_site_task_is_reaped_before_search_returns(
    monkeypatch: pytest.MonkeyPatch,
    _fresh_state: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gc
    import logging

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowCancelSearcher]),
    )

    with caplog.at_level(logging.ERROR):
        results = await search(
            "keyword", sites=["recorder", "slow_cancel"], timeout=0.05
        )
        gc.collect()
        await asyncio.sleep(0)

    assert [item["site"] for item in results] == ["recorder"]
    # Nothing may still be running against the session that is now closed.
    assert _site_tasks() == set()
    assert not [record for record in caplog.records if record.name == "asyncio"]


@pytest.mark.asyncio
async def test_timed_out_site_task_is_reaped_before_stream_closes(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import gc
    import logging

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowCancelSearcher]),
    )

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector()
    logging.getLogger().addHandler(handler)
    try:
        chunks = [
            chunk
            async for chunk in search_stream(
                "keyword", sites=["recorder", "slow_cancel"], timeout=0.05
            )
        ]
        gc.collect()
        await asyncio.sleep(0)
    finally:
        logging.getLogger().removeHandler(handler)

    assert [[item["site"] for item in chunk] for chunk in chunks] == [["recorder"]]
    assert _site_tasks() == set()
    assert not [record for record in records if record.name == "asyncio"]


@pytest.mark.asyncio
async def test_abandon_grace_bounds_a_site_that_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    import time

    from novel_downloader.plugins.search import _ABANDON_GRACE

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _ImmortalSearcher]),
    )

    started = time.perf_counter()
    results = await search("keyword", sites=["recorder", "immortal"], timeout=0.05)
    elapsed = time.perf_counter() - started

    # The aggregate may wait for the grace once, never for the 30s sleep.
    assert elapsed < 0.05 + 2 * _ABANDON_GRACE + 0.5
    assert [item["site"] for item in results] == ["recorder"]
    # Clean up the deliberately unkillable task so the loop can shut down.
    for task in _site_tasks():
        task.cancel()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancelling_search_reaps_the_site_tasks(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The caller may abandon a search (a new keyword typed, the window
    # closed).  Cancellation must still leave no site task behind against the
    # session that is about to close.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowCancelSearcher]),
    )

    task = asyncio.create_task(
        search("keyword", sites=["recorder", "slow_cancel"], timeout=5)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert _site_tasks() == set()


@pytest.mark.asyncio
async def test_closing_the_stream_reaps_the_site_tasks(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_RecordingSearcher, _SlowCancelSearcher]),
    )

    stream = search_stream("keyword", sites=["recorder", "slow_cancel"], timeout=5)
    first = await stream.__anext__()
    assert [item["site"] for item in first] == ["recorder"]

    await stream.aclose()
    await asyncio.sleep(0)

    assert _site_tasks() == set()


# --------------------------------------------------------------------------- #
# relevance floor is applied to the book, not to each source row
# --------------------------------------------------------------------------- #


class _AuthorHitSearcher(BaseSearcher):
    """The author's book, found on a site that knows the author."""

    site_name = "authorhit"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            _search_result("authorhit", "b1", "斗破苍穹", author="天蚕土豆"),
        ]


class _AuthorBlankSearcher(BaseSearcher):
    """A mirror of the same book whose author field is missing."""

    site_name = "authorblank"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            _search_result("authorblank", "b1", "斗破苍穹", author=""),
        ]


@pytest.mark.asyncio
async def test_group_filter_keeps_sources_without_the_keyword(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    """The floor decides whether a *book* is relevant, not every source row.

    An author search ("天蚕土豆") must keep the mirror site that has the same
    book but no author field: on its own that row scores 0, yet it is one of
    the book's download sources.
    """
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_AuthorHitSearcher, _AuthorBlankSearcher]),
    )

    merged = await search("天蚕土豆", group=True)

    assert len(merged) == 1
    assert merged[0]["title"] == "斗破苍穹"
    assert set(merged[0]["sources"]) == {"authorhit", "authorblank"}


# --------------------------------------------------------------------------- #
# "书名 + 作者" multi-token queries
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["斗破苍穹 天蚕土豆", "天蚕土豆 斗破苍穹"])
async def test_multi_token_query_keeps_the_matching_book(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None, keyword: str
) -> None:
    # Neither the spaced title nor the spaced author alone occurs verbatim in
    # the row fields; only token-aware relevance keeps the book above the
    # 0.45 floor.  Token order must not matter.
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    results = await search(keyword)

    assert [item["book_id"] for item in results] == ["exact", "deriv"]


# --------------------------------------------------------------------------- #
# ranking telemetry on final results (rank_score / match_tier)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_annotates_final_rows_with_ranking_telemetry(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    results = await search("斗破苍穹")

    assert [item["book_id"] for item in results] == ["exact", "deriv"]
    for item in results:
        assert set(item) >= _DEBUG_KEYS
        assert isinstance(item["rank_score"], float)
        assert 0.0 <= item["rank_score"] <= 1.0
        assert isinstance(item["match_tier"], int)
        assert 0 <= item["match_tier"] <= 4
    # An exact title is tier 4; a title that starts with the keyword is tier 3.
    assert [item["match_tier"] for item in results] == [4, 3]
    # Both rows share the same metadata, so the exact title outranks the
    # derivative and the annotated scores follow the returned order.
    assert results[0]["rank_score"] > results[1]["rank_score"]


@pytest.mark.asyncio
async def test_ranking_telemetry_never_reaches_the_cache_snapshot(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    first = await search("斗破苍穹")
    snapshot = _cache_snapshot("斗破苍穹")

    assert len(first) == 2
    assert len(snapshot) == 3
    for row in snapshot:
        assert _DEBUG_KEYS.isdisjoint(row)

    # Returned rows are copies: mutating them cannot reach the snapshot.
    first[0]["title"] = "tampered"
    first[0]["rank_score"] = 0.0

    again = await search("斗破苍穹")  # cache hit: re-ranked and re-annotated
    assert again[0]["title"] == "斗破苍穹"
    assert again[0]["rank_score"] > 0.0
    assert set(again[0]) >= _DEBUG_KEYS
    refreshed = _cache_snapshot("斗破苍穹")
    assert refreshed[0]["title"] == "斗破苍穹"
    assert all(_DEBUG_KEYS.isdisjoint(row) for row in refreshed)


def test_ranking_telemetry_is_derived_never_an_input() -> None:
    from novel_downloader.plugins.search import _finalize

    strong = _search_result("alpha", "strong", "斗破苍穹", word_count="300万字")
    weak = _search_result("alpha", "weak", "斗破苍穹外传", word_count="1万字")

    baseline = _finalize(
        [dict(weak), dict(strong)],
        keyword="斗破苍穹",
        sort="quality",
        order=None,
        limit=None,
        group=False,
    )
    # Pre-set telemetry that would win if ranking/grouping ever read it.
    poisoned = _finalize(
        [
            {**weak, "rank_score": 1.0, "match_tier": 4},
            {**strong, "rank_score": 0.0, "match_tier": 0},
        ],
        keyword="斗破苍穹",
        sort="quality",
        order=None,
        limit=None,
        group=False,
    )

    assert [row["book_id"] for row in baseline] == ["strong", "weak"]
    assert [row["book_id"] for row in poisoned] == ["strong", "weak"]
    # The annotations are recomputed from the row, not echoed back.
    assert poisoned[0]["rank_score"] > poisoned[1]["rank_score"]
    assert [row["match_tier"] for row in poisoned] == [
        baseline[0]["match_tier"],
        baseline[1]["match_tier"],
    ]


@pytest.mark.asyncio
async def test_grouped_rows_carry_telemetry_and_keep_the_snapshot_clean(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    from novel_downloader.plugins.ranking import rank_score

    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_BookCopySearcher, _StrongCopySearcher]),
    )

    merged = await search("三体", group=True)

    assert len(merged) == 1
    entry = merged[0]
    assert entry["sources"] == ["beta", "alpha"]
    assert set(entry) >= _DEBUG_KEYS
    assert entry["match_tier"] == 4  # exact title
    # The annotated score is the group primary's score under the same keyword.
    alpha = _search_result(
        "alpha", "alpha-1", "三体", word_count="10万字", priority=100
    )
    beta = _search_result("beta", "beta-1", "三体", word_count="300万字", priority=100)
    assert entry["rank_score"] == pytest.approx(
        max(rank_score(alpha, "三体"), rank_score(beta, "三体")), abs=1e-5
    )

    snapshot = _cache_snapshot("三体")
    assert all(_DEBUG_KEYS.isdisjoint(row) for row in snapshot)
    assert all("sources" not in row for row in snapshot)


@pytest.mark.asyncio
async def test_stream_chunks_never_carry_ranking_telemetry(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    monkeypatch.setattr(
        registrar, "get_searcher_classes", _mirror_registry([_MixedRelevanceSearcher])
    )

    fresh = [chunk async for chunk in search_stream("斗破苍穹")]
    assert fresh
    assert all(_DEBUG_KEYS.isdisjoint(item) for chunk in fresh for item in chunk)

    await search("斗破苍穹")  # warm the shared cache
    cached = [chunk async for chunk in search_stream("斗破苍穹")]
    assert cached
    assert all(_DEBUG_KEYS.isdisjoint(item) for chunk in cached for item in chunk)


# --------------------------------------------------------------------------- #
# multi-token queries combined with grouping and the relevance floor
# --------------------------------------------------------------------------- #


class _MultiTokenMirrorSearcher(BaseSearcher):
    """Same book as :class:`_MultiTokenBookSearcher`, with a blank author."""

    site_name = "multimirror"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_search_result("multimirror", "b1", "斗破苍穹", author="")]


class _MultiTokenBookSearcher(BaseSearcher):
    """The title+author book, found on a site that knows both fields."""

    site_name = "multibook"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_search_result("multibook", "b1", "斗破苍穹", author="天蚕土豆")]


class _MultiTokenNoiseSearcher(BaseSearcher):
    """An unrelated book that shares no characters with either token."""

    site_name = "multinoise"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_search_result("multinoise", "n1", "三体", author="刘慈欣")]


class _MultiTokenDerivativeSearcher(BaseSearcher):
    """A genuine derivative: relevant on its own, but below a strict floor."""

    site_name = "multideriv"

    async def _fetch_html(self, keyword: str) -> str:
        return "html"

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return [_search_result("multideriv", "d1", "斗破苍穹之雷霆震碎", author="别人")]


@pytest.mark.asyncio
async def test_multi_token_group_floor_keeps_the_book_with_a_blank_author_source(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # The mirror row has no author and scores 0 on the whole query, yet it is a
    # source of the book whose primary clears the floor: grouping first must
    # keep it, exactly like the single-token author search.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry(
            [
                _MultiTokenBookSearcher,
                _MultiTokenMirrorSearcher,
                _MultiTokenNoiseSearcher,
            ]
        ),
    )

    merged = await search("斗破苍穹 天蚕土豆", group=True)

    assert len(merged) == 1
    assert merged[0]["title"] == "斗破苍穹"
    assert set(merged[0]["sources"]) == {"multibook", "multimirror"}
    assert merged[0]["match_tier"] == 4
    assert merged[0]["rank_score"] > 0.0


@pytest.mark.asyncio
async def test_multi_token_group_floor_falls_back_to_positive_primaries(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None
) -> None:
    # Nothing clears 0.95: the never-empty fallback keeps the derivative book
    # (positive relevance) and still drops the unrelated one, per *book*.
    monkeypatch.setattr(
        registrar,
        "get_searcher_classes",
        _mirror_registry([_MultiTokenDerivativeSearcher, _MultiTokenNoiseSearcher]),
    )

    merged = await search("斗破苍穹 天蚕土豆", group=True, min_relevance=0.95)

    assert [entry["title"] for entry in merged] == ["斗破苍穹之雷霆震碎"]
    assert merged[0]["rank_score"] > 0.0
    assert merged[0]["match_tier"] == 3  # prefix match on the title


# --------------------------------------------------------------------------- #
# arrival-order determinism (shuffled rows, swapped sites, swapped delays)
# --------------------------------------------------------------------------- #
class _ArrivalSearcher(BaseSearcher):
    """Searcher whose rows and completion delay are set per test."""

    site_name = "arrival_a"
    rows: list[SearchResult] = []
    delay = 0.0

    async def search(
        self, keyword: str, limit: int | None = None
    ) -> list[SearchResult]:
        # Completion order is what a concurrent aggregate sees; the rows
        # themselves are returned in the order the class was given.
        await asyncio.sleep(type(self).delay)
        rows = list(type(self).rows)
        return rows if limit is None else rows[:limit]

    async def _fetch_html(self, keyword: str) -> str:
        return ""

    def _parse_html(
        self, html_str: str, limit: int | None = None
    ) -> list[SearchResult]:
        return []


class _ArrivalSearcherB(_ArrivalSearcher):
    site_name = "arrival_b"


def _arrival_rows_a() -> list[SearchResult]:
    return [
        _search_result(
            "arrival_a",
            "a-strong",
            "三体",
            author="刘慈欣",
            word_count="300万字",
            update_date="2024-06-14",
            cover_url="https://example.com/a.jpg",
            latest_chapter="第300章",
        ),
        _search_result(
            "arrival_a",
            "a-weak",
            "三体",
            author="刘慈欣",
            word_count="1万字",
            update_date="2020-01-01",
        ),
        _search_result(
            "arrival_a", "a-jia", "球状闪电", author="甲", word_count="10万字"
        ),
        _search_result(
            "arrival_a", "a-yi", "球状闪电", author="乙", word_count="10万字"
        ),
        # Same site + title, no book id, different authors and URLs: the
        # historical canonical key (site, title, book_id) was identical.
        _search_result(
            "arrival_a",
            "",
            "三体外传",
            author="丙",
            book_url="https://example.com/a-c",
        ),
        _search_result(
            "arrival_a",
            "",
            "三体外传",
            author="丁",
            book_url="https://example.com/a-d",
        ),
    ]


def _arrival_rows_b() -> list[SearchResult]:
    return [
        _search_result(
            "arrival_b",
            "b-strong",
            "三体",
            author="刘慈欣",
            word_count="200万字",
            update_date="2024-05-01",
        ),
        _search_result(
            "arrival_b", "b-jia", "球状闪电", author="甲", word_count="10万字"
        ),
    ]


def _arrival_view(rows: Sequence[SearchResult]) -> list[tuple[Any, ...]]:
    """Observable layout: order, identity, group sources and match tier."""
    return [
        (
            item["site"],
            item["book_id"],
            item["title"],
            item["author"],
            tuple(item.get("sources", ())),
            item["match_tier"],
        )
        for item in rows
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1, 2])
async def test_search_layout_is_independent_of_arrival_order(
    monkeypatch: pytest.MonkeyPatch, _fresh_state: None, limit: int | None
) -> None:
    import novel_downloader.plugins.search as search_module

    a_rows = _arrival_rows_a()
    b_rows = _arrival_rows_b()

    async def run(
        rows_a: list[SearchResult],
        rows_b: list[SearchResult],
        *,
        delay_a: float,
        delay_b: float,
        sites: list[str],
    ) -> list[SearchResult]:
        # Same rows, different arrival: swap site order, completion delays and
        # the within-site row order.
        monkeypatch.setattr(_ArrivalSearcher, "rows", rows_a)
        monkeypatch.setattr(_ArrivalSearcher, "delay", delay_a)
        monkeypatch.setattr(_ArrivalSearcherB, "rows", rows_b)
        monkeypatch.setattr(_ArrivalSearcherB, "delay", delay_b)
        monkeypatch.setattr(
            registrar,
            "get_searcher_classes",
            _mirror_registry([_ArrivalSearcher, _ArrivalSearcherB]),
        )
        # Cache holds the unranked snapshot; each run must build its own.
        search_module._CACHE.clear()
        return await search(
            "三体",
            sites=sites,
            per_site_limit=10,
            group=True,
            limit=limit,
            min_relevance=None,
        )

    first = await run(
        a_rows,
        b_rows,
        delay_a=0.0,
        delay_b=0.05,
        sites=["arrival_a", "arrival_b"],
    )
    second = await run(
        list(reversed(a_rows)),
        list(reversed(b_rows)),
        delay_a=0.05,
        delay_b=0.0,
        sites=["arrival_b", "arrival_a"],
    )

    assert _arrival_view(second) == _arrival_view(first)
    assert [item["rank_score"] for item in second] == pytest.approx(
        [item["rank_score"] for item in first], abs=1e-9
    )
    expected_count = 5 if limit is None else limit
    assert len(first) == expected_count

    # The tie-broken pair is ordered by content, not by which row came first.
    if limit is None:
        twin_authors = [item["author"] for item in first if item["title"] == "三体外传"]
        assert twin_authors == ["丁", "丙"]
