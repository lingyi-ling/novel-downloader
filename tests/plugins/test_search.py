#!/usr/bin/env python3
"""Tests for the cross-site search orchestration."""

import asyncio
from collections.abc import Callable, Iterator, Sequence
from typing import Any

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
    assert results == [_stub_result("recorder", "1")]


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
    assert first == second == [_stub_result("recorder", "1")]


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

    assert results == [_stub_result("recorder", "1")]
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

    assert results == [_stub_result("recorder", "1")]
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

    assert results == [_stub_result("recorder", "1")]
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
    from novel_downloader.plugins.search_cache import build_key

    cached = search_module._CACHE.get(build_key(keyword, None, 5, False, 5.0))
    assert cached is not None
    return cached
