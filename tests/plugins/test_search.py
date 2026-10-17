#!/usr/bin/env python3
"""Tests for the cross-site search orchestration."""

from typing import Any

import pytest

from novel_downloader.plugins.base.searcher import BaseSearcher
from novel_downloader.plugins.registry import registrar
from novel_downloader.plugins.search import search, search_stream
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
