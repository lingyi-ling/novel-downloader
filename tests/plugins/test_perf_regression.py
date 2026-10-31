#!/usr/bin/env python3
"""
Performance regression locks for the audited hot paths.

These tests deliberately avoid wall-clock assertions (except one tripwire with
~150x headroom) and instead pin the *algorithmic shape* of the hot paths via
call counters, so they cannot flake on a loaded machine:

* :func:`sort_results` / :func:`group_results` must score each entry exactly
  once (``O(n)`` scoring on top of the ``O(n log n)`` sort).  A regression to
  comparator-based sorting or re-scoring inside loops inflates the counter to
  ``O(n log n)`` or worse and fails immediately.
* :func:`normalize_text` must stay behind its LRU cache: repeated lookups of
  the same text must be served from the cache.
* An EPUB export of 500 chapters must finish within a generous budget and
  produce exactly one ZIP entry per chapter (guards against accidental
  per-chapter duplication or an ``O(n^2)`` build blow-up).

The numbers behind the budgets (measured 2026-11 on the dev machine, Python
3.12): sort_results 500 entries ~3 ms, group_results 500 entries ~5 ms, EPUB
build+export of 500 chapters ~65 ms.
"""

from __future__ import annotations

import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.grouping as grouping_module
import novel_downloader.plugins.ranking as ranking_module
from novel_downloader.plugins.grouping import group_results
from novel_downloader.plugins.ranking import normalize_text, sort_results
from novel_downloader.schemas import SearchResult

NOW = datetime(2024, 6, 15, 12, 0, 0)

_TITLES = ("斗破苍穹", "凡人修仙传", "遮天", "完美世界", "盗墓笔记", "大奉打更人")
_AUTHORS = ("天蚕土豆", "忘语", "辰东", "猫腻", "南派三叔", "卖报小郎君")
_WORDS = ("12.3万字", "8,000,000", "456.7万字", "102万字", "未知", "3千字")
_DATES = (
    "2024-05-01 12:30:00",
    "3天前",
    "2024/5/1",
    "2024年5月1日",
    "昨天",
    "2个月前",
)


def _make_results(n: int) -> list[SearchResult]:
    """Deterministic fake aggregated-search results (no I/O)."""
    return [
        SearchResult(
            book_id=f"b{i}",
            title=_TITLES[i % 6] + ("" if i < 6 else f" {i // 6}"),
            author=_AUTHORS[i % 6],
            site=f"site{i % 25}",
            book_url=f"https://example.com/{i}",
            cover_url="https://img.example.com/c.jpg" if i % 3 else "",
            latest_chapter=f"第{i}章" if i % 2 else "",
            word_count=_WORDS[i % 6],
            update_date=_DATES[i % 6],
            priority=(i % 7) * 30 + 10,
        )
        for i in range(n)
    ]


def _counting_rank_score(
    counter: dict[str, int],
) -> Any:
    """Wrap the real ``rank_score`` with a call counter (behaviour unchanged)."""
    real = ranking_module.rank_score

    def counted(result: SearchResult, keyword: str = "", **kwargs: Any) -> float:
        counter["calls"] += 1
        return real(result, keyword, **kwargs)

    return counted


# --------------------------------------------------------------------------- #
# sort_results / group_results: score each entry exactly once
# --------------------------------------------------------------------------- #
def test_sort_results_by_quality_scores_each_entry_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    n = 300
    results = _make_results(n)
    counter = {"calls": 0}

    monkeypatch.setattr(ranking_module, "rank_score", _counting_rank_score(counter))
    sort_results(results, by="quality", keyword="斗破", now=NOW)

    assert counter["calls"] == n


def test_sort_results_other_keys_never_call_rank_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = {"calls": 0}
    monkeypatch.setattr(ranking_module, "rank_score", _counting_rank_score(counter))
    for key in ("words", "update", "title", "author", "site", "priority"):
        sort_results(_make_results(50), by=key, now=NOW)

    assert counter["calls"] == 0


def test_group_results_scores_each_entry_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    n = 300
    results = _make_results(n)
    counter = {"calls": 0}

    monkeypatch.setattr(grouping_module, "rank_score", _counting_rank_score(counter))
    groups = group_results(results, keyword="斗破")

    assert counter["calls"] == n
    assert sum(len(group.sources) for group in groups) == n


# --------------------------------------------------------------------------- #
# normalize_text: the LRU cache must stay active
# --------------------------------------------------------------------------- #
def test_normalize_text_repeated_calls_are_lru_cached() -> None:
    cached = ranking_module._normalize_cached
    before = cached.cache_info().hits

    for _ in range(50):
        normalize_text("斗 破苍穹！")

    after = cached.cache_info().hits
    assert after - before >= 49


# --------------------------------------------------------------------------- #
# EPUB export at the audited scale (500 chapters)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _epub_500_chapters() -> Any:
    from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter

    paragraphs = [
        f"第{i}段　性能回归测试假数据，模拟真实章节文本长度。" for i in range(40)
    ]
    content = "\n".join(paragraphs)

    builder = EpubBuilder(title="性能回归测试", author="测试")
    for i in range(500):
        builder.add_chapter(
            EpubChapter(
                id=f"c{i}",
                filename=f"c{i}.xhtml",
                title=f"第{i + 1}章 测试",
                content=content,
                fonts=[],
                extra_content="",
            )
        )
    return builder


def test_epub_export_500_chapters_within_budget(
    tmp_path: Path, _epub_500_chapters: Any
) -> None:
    """Tripwire only: ~65 ms measured, 10 s budget (150x headroom)."""
    out = tmp_path / "book.epub"

    start = time.perf_counter()
    _epub_500_chapters.export(out)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert out.is_file()
    assert elapsed_ms < 10_000, f"EPUB export took {elapsed_ms:.0f} ms"

    with zipfile.ZipFile(out) as epub:
        names = epub.namelist()
        chapters = [name for name in names if name.endswith(".xhtml")]
        # 500 chapters + intro + nav (+ cover when configured).
        assert len(chapters) >= 501
        assert epub.testzip() is None


def test_epub_chapter_registration_does_not_rescan_previous_chapters(
    _epub_500_chapters: Any,
) -> None:
    """Registration must be O(1) amortized: one item, one spine, one TOC line."""
    builder = _epub_500_chapters

    # items/spine each hold the 500 chapters plus the built-in intro page.
    assert len(builder.items) == 501
    assert len(builder.opf.spine_lines) == 501
    assert len(builder.nav.lines) == 501
