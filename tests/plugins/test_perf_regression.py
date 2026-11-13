#!/usr/bin/env python3
"""
Performance regression locks for the audited hot paths.

These tests deliberately avoid tight wall-clock assertions and instead pin the
*algorithmic shape* of the hot paths via call counters, cold-cache miss counts
and linear-memory checks, so they cannot flake on a loaded machine:

* :func:`sort_results` must score each entry exactly once (``O(n)`` scoring on
  top of the ``O(n log n)`` sort).  A regression to comparator-based sorting or
  re-scoring inside loops inflates the counter to ``O(n log n)`` or worse and
  fails immediately.
* :func:`group_results` scores each entry of a multi-source group exactly once
  and skips scoring for one-entry groups, whose primary cannot change.  The
  shortcut is verified for output equivalence against a direct re-computation.
* :func:`normalize_text` must stay behind its LRU cache.
* The text-cleaning and relevance helpers use allocation-free fast paths whose
  output must match the straightforward reference implementations exactly.
* A cache key is constant in the result count (rows are never part of the key),
  and the memory used by ``_finalize`` / ``SearchCache.put`` grows linearly.
* One generous 20k-row tripwire catches an accidental return to quadratic
  behaviour; its budgets are ~8-10x the measured medians.

The numbers behind the budgets (measured 2026-11 on the dev machine, Python
3.12, time.perf_counter medians of cold module caches): sort_results 20k rows
~0.35 s, group_results 20k ~0.35 s, _finalize(flat) 20k ~1.5-2.5 s,
_finalize(group=True) 20k ~1.3-2.3 s; EPUB build+export of 500 chapters
~65 ms.
"""

from __future__ import annotations

import gc
import json
import math
import random
import statistics
import time
import tracemalloc
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.grouping as grouping_module
import novel_downloader.plugins.ranking as ranking_module
from novel_downloader.plugins import search as search_module
from novel_downloader.plugins.grouping import group_results
from novel_downloader.plugins.ranking import (
    match_tier_of,
    normalize_text,
    rank_score,
    sort_results,
)
from novel_downloader.plugins.search import _finalize
from novel_downloader.plugins.search_cache import (
    SearchCache,
    _key_canonical,
    _key_digest,
    build_key,
)
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


_SCALED_BASES = (
    "斗破苍穹",
    "凡人修仙传",
    "遮天",
    "完美世界",
    "盗墓笔记",
    "大奉打更人",
    "诡秘之主",
    "赘婿",
    "庆余年",
    "剑来",
)
_SCALED_DECORATIONS = (
    ("[网游]", "", ""),
    ("", "（全本）", ""),
    ("【精校】", "", ""),
    ("", "", "（第1卷）"),
    ("", "", ""),
)


def _make_scaled_results(n: int) -> list[SearchResult]:
    """
    Deterministic result mix used by the scale tests.

    It contains Chinese titles, decorative publication markers (the
    ``_core_title`` whitelist path), unique and repeated titles, exact
    same-site duplicates (every 5th row) and a multi-word query target, so the
    measured shape matches a real cross-site aggregate rather than a
    best-case list of unique strings.
    """
    results: list[SearchResult] = []
    for i in range(n):
        if i >= 5 and i % 5 == 0:
            results.append(SearchResult(**results[i - 5]))
            continue
        lead, trail, volume = _SCALED_DECORATIONS[i % len(_SCALED_DECORATIONS)]
        suffix = "" if i % 3 else f" 第{i // 3}部"
        results.append(
            SearchResult(
                site=f"site{i % 25}",
                book_id=f"b{i}",
                book_url=f"https://example.com/book/{i}",
                cover_url="https://img.example.com/c.jpg" if i % 3 else "",
                title=f"{lead}{_SCALED_BASES[i % 10]}{suffix}{trail}{volume}",
                author=_AUTHORS[i % len(_AUTHORS)],
                latest_chapter=f"第{i}章 标题" if i % 2 else "",
                update_date=_DATES[i % len(_DATES)],
                word_count=_WORDS[i % len(_WORDS)],
                priority=(i % 7) * 30 + 10,
            )
        )
    return results


KEYWORD = "斗破苍穹 天蚕土豆"


def _reset_caches() -> None:
    """Clear the module-level LRU caches (cold-cache measurements)."""
    ranking_module._normalize_cached.cache_clear()
    ranking_module._similarity_cached.cache_clear()
    ranking_module._query_tokens_cached.cache_clear()
    ranking_module._word_count_cached.cache_clear()
    ranking_module._update_date_cached.cache_clear()
    grouping_module._core_title_cached.cache_clear()


def _median_ms(
    fn: Any,
    *,
    repeats: int = 3,
    reset: bool = True,
    warmup: bool = True,
) -> float:
    """
    Return the median wall-clock milliseconds of ``fn``.

    The cyclic collector is stopped inside the timed region: its pauses are
    bursty and would otherwise dominate the difference between repeats.  The
    LRU caches are cleared before every sample, so each sample is the cost of
    one call against cold module caches.
    """
    if reset:
        _reset_caches()
    if warmup:
        fn()
    samples: list[float] = []
    for _ in range(repeats):
        if reset:
            _reset_caches()
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            fn()
        finally:
            gc.enable()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples)


def _peak_mb(fn: Any) -> float:
    """Return the peak traced memory (MiB) of one call, with cold caches."""
    _reset_caches()
    gc.collect()
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1] / (1024 * 1024)
    finally:
        tracemalloc.stop()


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


def test_group_results_scores_each_entry_of_multi_source_groups_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This dataset has both repeated titles/authors (multi-source groups) and
    # unique ones (singletons).  A singleton group cannot change its primary,
    # so it must not be scored; every entry of a multi-source group is scored
    # exactly once.
    n = 300
    results = _scaled_group_fixture(n)
    counter = {"calls": 0}

    monkeypatch.setattr(grouping_module, "rank_score", _counting_rank_score(counter))
    groups = group_results(results, keyword="斗破")

    expected = sum(len(group.sources) for group in groups if len(group.sources) > 1)
    assert 0 < expected < n
    assert counter["calls"] == expected
    assert sum(len(group.sources) for group in groups) == n


@pytest.mark.parametrize("intent", ["either", "author", "title"])
def test_group_shortcut_matches_explicit_scoring(intent: str) -> None:
    """
    Output equivalence for the singleton fast path.

    Every group is re-derived by explicitly scoring all of its entries with the
    exact ordering ``group_results`` documents (tier, then score, then input
    position).  A one-entry group must still report that entry as its primary
    and its only source; a multi-entry group must be unchanged.
    """
    results = _scaled_group_fixture(120)
    groups = group_results(results, keyword="斗破", now=NOW, intent=intent)
    positions = {id(row): index for index, row in enumerate(results)}

    for group in groups:
        entries = [(positions[id(row)], row) for row in group.sources]
        expected = sorted(
            entries,
            key=lambda item: (
                -match_tier_of(item[1], "斗破", intent=intent),
                -rank_score(item[1], "斗破", intent=intent, now=NOW),
                item[0],
            ),
        )
        assert [id(row) for _, row in expected] == [id(row) for row in group.sources]
        assert group.primary is expected[0][1]


def _scaled_group_fixture(n: int) -> list[SearchResult]:
    """Rows that deliberately form both multi-source groups and singletons."""
    results: list[SearchResult] = []
    for i in range(n):
        # Every 7th row gets a unique title (cannot group with anything else);
        # the rest split into three titles x two authors = six groups.
        title = f"独本{i}" if i % 7 == 6 else _TITLES[i % 3]
        results.append(
            SearchResult(
                site=f"site{i % 4}",
                book_id=f"g{i}",
                book_url=f"https://example.com/g/{i}",
                cover_url="",
                title=title,
                author=_AUTHORS[i % 2],
                latest_chapter="",
                update_date="",
                word_count="1万字",
                priority=10,
            )
        )
    return results


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
# parse_word_count / parse_update_date: memoized without changing the result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "1234",
        "1,234",
        "12.3万字",
        "１．２万字",
        "1.2M",
        "3千字",
        "未知",
        "-",
        "总字数：12.3万字",
    ],
)
def test_parse_word_count_cache_matches_the_uncached_parser(value: object) -> None:
    """The memoized public call must equal one cold parse of the same label."""
    core = ranking_module._word_count_cached.__wrapped__
    raw = "" if value is None else str(value)
    ranking_module._word_count_cached.cache_clear()

    expected = core(raw)
    assert ranking_module.parse_word_count(value) == expected
    # A second call is served from the cache and must still agree.
    assert ranking_module.parse_word_count(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "2024-05-01",
        "2024.05.01 10:00",
        "2024年5月1日",
        "２０２４年５月１日",
        "3天前",
        "昨天",
        "刚刚",
        "2个月前",
        "-",
        "12:30",
    ],
)
def test_parse_update_date_cache_matches_the_uncached_parser(value: object) -> None:
    """The memoized public call must equal one cold parse against the clock."""
    core = ranking_module._update_date_cached.__wrapped__
    raw = "" if value is None else str(value)
    ranking_module._update_date_cached.cache_clear()

    expected = core(raw, NOW)
    assert ranking_module.parse_update_date(value, now=NOW) == expected
    assert ranking_module.parse_update_date(value, now=NOW) == expected


def test_parse_update_date_cache_is_keyed_by_the_clock() -> None:
    """Relative labels must not leak a result parsed against an older clock."""
    ranking_module._update_date_cached.cache_clear()
    later = NOW + timedelta(days=30)

    assert ranking_module.parse_update_date("3天前", now=NOW) == NOW - timedelta(days=3)
    assert ranking_module.parse_update_date("3天前", now=later) == later - timedelta(
        days=3
    )


def test_parse_update_date_without_a_clock_is_not_frozen() -> None:
    """``now=None`` must keep reading the current time instead of a cache entry."""
    ranking_module._update_date_cached.cache_clear()
    before = datetime.now()

    first = ranking_module.parse_update_date("刚刚")
    second = ranking_module.parse_update_date("刚刚")
    after = datetime.now()

    assert first is not None and second is not None
    assert before <= first <= after
    assert before <= second <= after


def test_finalize_filters_parse_each_distinct_label_once() -> None:
    """
    A filtered ``_finalize`` call must not re-parse a label once per row.

    Ranking scores every row, the filter then reads the same two fields, and
    the annotation scores the surviving rows again.  With memoization each
    *distinct* raw label is parsed once per call; without it the miss count
    would grow with the row count.
    """
    word_labels = ("1万字", "2万字", "300万字")
    date_labels = ("2024-01-01", "2024-02-01", "2024-03-01")
    rows = [
        SearchResult(
            site=f"site{i % 3}",
            book_id=f"f{i}",
            book_url=f"https://example.com/f/{i}",
            cover_url="",
            title=f"独本{i}",
            author="作者",
            latest_chapter="",
            update_date=date_labels[i % len(date_labels)],
            word_count=word_labels[i % len(word_labels)],
            priority=10,
        )
        for i in range(300)
    ]
    ranking_module._word_count_cached.cache_clear()
    ranking_module._update_date_cached.cache_clear()

    _finalize(
        [dict(row) for row in rows],
        keyword="独本",
        sort="quality",
        order=None,
        limit=None,
        group=False,
        min_relevance=None,
        min_words=0,
        max_words=1_000_000,
        updated_after=date(2024, 1, 1),
    )

    words = ranking_module._word_count_cached.cache_info()
    dates = ranking_module._update_date_cached.cache_info()
    assert words.misses == len(word_labels)
    assert dates.misses == len(date_labels)
    # Every row asked again during the filter/annotation steps.
    assert words.hits >= len(rows) - len(word_labels)
    assert dates.hits >= len(rows) - len(date_labels)


# --------------------------------------------------------------------------- #
# allocation-free helpers: exact equivalence with the reference implementations
# --------------------------------------------------------------------------- #
def _reference_plain_text(value: object) -> str:
    """The pre-optimisation per-character loop, verbatim."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    text = search_module._ANSI_ESCAPE_RE.sub("", text)
    chars: list[str] = []
    for char in text:
        if unicodedata.category(char) in search_module._CONTROL_CATEGORIES:
            if char in "\t\n\r\f\v":
                chars.append(" ")
            continue
        chars.append(char)
    return "".join(chars).strip()


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "三体",
        " 三体 ",
        "第1章\t标题",
        "a\nb",
        "a\rb",
        "\x00",
        "\x1b[31m红\x1b[0m",
        "\x1b]0;title\x07三体",
        "\u202eRLO",
        "\u200dZWJ",
        "\u00a0NBSP",
        "\ue000private",
        "\u0378unassigned",
        chr(0xD800),
        "<b>刘慈欣</b>",
        "ＡＢＣ",
        "😀",
        "\u3000全角空格\u3000",
    ],
)
def test_plain_text_fast_path_matches_the_reference(value: object) -> None:
    assert search_module._plain_text(value) == _reference_plain_text(value)


def test_plain_text_fast_path_matches_the_reference_on_random_text() -> None:
    rng = random.Random(20240615)
    pool = "三体 abc\t\n\r\f\v\x00\x1b[];0123\u202e\u200d\u00a0\ue000\u0301😀"
    for _ in range(200):
        value = "".join(rng.choice(pool) for _ in range(rng.randint(0, 32)))
        assert search_module._plain_text(value) == _reference_plain_text(value)


def _reference_coverage(needle: str, haystack: str) -> float:
    """The pre-optimisation set-based coverage, verbatim."""
    if not needle:
        return 0.0
    haystack_chars = set(haystack)
    hits = sum(1 for ch in needle if ch in haystack_chars)
    return hits / len(needle)


@pytest.mark.parametrize(
    ("needle", "haystack"),
    [
        ("", ""),
        ("", "三体"),
        ("斗破苍穹", ""),
        ("斗破苍穹", "斗破苍穹"),
        ("斗破苍穹", "斗破"),
        ("aaaa", "a"),
        ("阿阿阿", "阿"),
        ("abc", "xyz"),
    ],
)
def test_coverage_fast_path_matches_the_reference(needle: str, haystack: str) -> None:
    assert ranking_module._coverage(needle, haystack) == _reference_coverage(
        needle, haystack
    )


def test_coverage_fast_path_matches_the_reference_on_random_text() -> None:
    rng = random.Random(20240616)
    alphabet = "斗破苍穹天蚕土豆abcAB 123"
    for _ in range(500):
        needle = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        haystack = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert ranking_module._coverage(needle, haystack) == _reference_coverage(
            needle, haystack
        )


# --------------------------------------------------------------------------- #
# cache key and memory: constant key, linear storage
# --------------------------------------------------------------------------- #
def test_cache_key_is_constant_in_result_count(tmp_path: Path) -> None:
    key = build_key(KEYWORD, None, per_site_limit=5, config_fingerprint="fingerprint")
    canonical = _key_canonical(key)
    digest = _key_digest(canonical)
    # Large enough that every snapshot below is actually persisted, so the
    # payload's key/row count can be inspected.
    cache = SearchCache(disk_dir=tmp_path, max_disk_bytes=64 * 1024 * 1024)

    for n in (50, 1_000, 5_000):
        cache.put(key, _make_scaled_results(n))
        # The key encodes the query, not the rows: its canonical form and
        # file name never change with the result count.
        assert _key_canonical(key) == canonical
        assert _key_digest(canonical) == digest
        payload = json.loads(cache._disk_path(key).read_text(encoding="utf-8"))
        assert payload["key"] == canonical
        assert isinstance(payload["results"], list)
        assert len(payload["results"]) == n


def test_finalize_memory_grows_linearly_with_result_count() -> None:
    # 4x the rows; tracemalloc makes large runs expensive, so this uses the
    # smallest pair that still exposes a non-linear buffer (a quadratic
    # allocation at 4k rows would already exceed 8x the 1k peak).
    small = _make_scaled_results(1_000)
    large = _make_scaled_results(4_000)

    def finalize(rows: list[SearchResult], group: bool) -> None:
        _finalize(
            list(rows),
            keyword=KEYWORD,
            sort="quality",
            order=None,
            limit=None,
            group=group,
            min_relevance=None,
        )

    flat_small = _peak_mb(lambda: finalize(small, False))
    flat_large = _peak_mb(lambda: finalize(large, False))
    grouped_small = _peak_mb(lambda: finalize(small, True))
    grouped_large = _peak_mb(lambda: finalize(large, True))

    assert 0 < flat_small <= flat_large < flat_small * 8, (flat_small, flat_large)
    assert 0 < grouped_small <= grouped_large < grouped_small * 8, (
        grouped_small,
        grouped_large,
    )


def test_cache_storage_grows_linearly_with_result_count(tmp_path: Path) -> None:
    key = build_key(KEYWORD, ["site1"], per_site_limit=5)
    cache = SearchCache(disk_dir=tmp_path)
    small = _make_scaled_results(1_000)
    large = _make_scaled_results(4_000)

    small_mb = _peak_mb(lambda: cache.put(key, small))
    large_mb = _peak_mb(lambda: cache.put(key, large))
    assert 0 < small_mb <= large_mb < small_mb * 8, (small_mb, large_mb)

    cached = cache.get(key)
    assert cached is not None
    # Only the list is copied; the row dicts are shared, so memory stays O(n).
    assert cached is not large
    assert cached[0] is large[0]
    assert len(cached) == 4_000


# --------------------------------------------------------------------------- #
# end-to-end tripwire at the largest audited scale
# --------------------------------------------------------------------------- #
def test_finalize_at_20k_stays_within_a_generous_budget() -> None:
    """
    Tripwire only: measured ~0.2-0.4 s sort/group and ~1.2-2.6 s
    ``_finalize`` at 20k rows.

    The budgets are ~10x those medians.  A return to quadratic behaviour
    (e.g. re-scanning every row per result) exceeds them by a wide margin,
    while a loaded CI machine has ample room left.
    """
    rows = _make_scaled_results(20_000)

    def finalize(group: bool) -> None:
        _finalize(
            list(rows),
            keyword=KEYWORD,
            sort="quality",
            order=None,
            limit=None,
            group=group,
            min_relevance=None,
        )

    sort_ms = _median_ms(
        lambda: sort_results(list(rows), by="quality", keyword=KEYWORD, now=NOW),
        repeats=1,
        warmup=False,
    )
    group_ms = _median_ms(
        lambda: group_results(rows, keyword=KEYWORD, now=NOW),
        repeats=1,
        warmup=False,
    )
    flat_ms = _median_ms(lambda: finalize(False), repeats=2, warmup=False)
    grouped_ms = _median_ms(lambda: finalize(True), repeats=1, warmup=False)

    assert math.isfinite(flat_ms)
    assert sort_ms < 4_000, f"sort_results took {sort_ms:.0f} ms at 20k rows"
    assert group_ms < 4_000, f"group_results took {group_ms:.0f} ms at 20k rows"
    assert flat_ms < 20_000, f"_finalize(flat) took {flat_ms:.0f} ms at 20k rows"
    assert grouped_ms < 20_000, (
        f"_finalize(group=True) took {grouped_ms:.0f} ms at 20k rows"
    )


def test_finalize_filters_at_20k_stay_within_a_generous_budget() -> None:
    """
    Filter tripwire: one or three local filters must stay within ~10x the
    measured no-filter median.

    Measured 2026-11 on the dev machine (Python 3.12, cold module caches):
    no filter ~1.4-1.9 s, ``min_words`` ~1.8-2.2 s, three filters ~1.1-1.6 s
    at 20k rows (the three-filter run scores fewer surviving rows during
    annotation, so it can be *faster* than the unfiltered one).  The budgets
    below are deliberately loose; a return to per-row re-parsing or a second
    sort would blow well past them.
    """
    rows = _make_scaled_results(20_000)

    def finalize(**filters: Any) -> None:
        _finalize(
            list(rows),
            keyword=KEYWORD,
            sort="quality",
            order=None,
            limit=None,
            group=False,
            min_relevance=None,
            **filters,
        )

    flat_ms = _median_ms(lambda: finalize(), repeats=2, warmup=False)
    min_words_ms = _median_ms(
        lambda: finalize(min_words=100_000), repeats=2, warmup=False
    )
    three_ms = _median_ms(
        lambda: finalize(
            min_words=100_000,
            max_words=2_000_000,
            updated_after=date(2024, 1, 1),
        ),
        repeats=2,
        warmup=False,
    )

    assert math.isfinite(flat_ms)
    assert min_words_ms < 25_000, (
        f"_finalize(min_words) took {min_words_ms:.0f} ms at 20k rows"
    )
    assert three_ms < 25_000, (
        f"_finalize(three filters) took {three_ms:.0f} ms at 20k rows"
    )
    # Relative guard: filtering may not add more than one order of magnitude.
    assert min_words_ms < flat_ms * 10, (flat_ms, min_words_ms)
    assert three_ms < flat_ms * 10, (flat_ms, three_ms)


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
