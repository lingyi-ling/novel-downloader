#!/usr/bin/env python3
"""Tests for the cross-site search orchestration."""

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

    chunks = [chunk async for chunk in search_stream(
        "keyword", sites=["recorder", "recorder"]
    )]

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

    chunks = [
        chunk async for chunk in search_stream("keyword", on_event=events.append)
    ]

    assert chunks == [[_stub_result("recorder", "1")]]
    kinds = {(event.site, event.kind) for event in events}
    assert ("recorder", "completed") in kinds
    assert ("nonepayload", "failed") in kinds
    assert ("raising", "failed") in kinds


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
