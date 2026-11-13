#!/usr/bin/env python3
"""
bench_search_pipeline.py
------------------------

Offline performance baseline for the search aggregation pipeline.

The script builds deterministic synthetic result sets (Chinese titles,
decorative bracket markers, same-book duplicates and a multi-word query) and
measures the hot public entry points with ``time.perf_counter``:

* ``sort_results``            -- ranking only
* ``group_results``           -- same-book folding only
* ``_finalize`` (flat)        -- sanitize + dedupe + intent + rank + annotate
* ``_finalize`` (grouped)     -- the same plus grouping
* ``_finalize`` + local filters -- the same flat path with one or all three
  of ``min_words``/``max_words``/``updated_after`` active, so the filter
  overhead at 1k/5k/20k rows is reported as a ratio against the no-filter run
* ``SearchCache.put`` / ``get`` -- memory layer
* disk ``put`` / ``get``      -- optional persistent layer (tmpdir)

Every number is the median of several repeats after one warm-up run.  The
script is deliberately dependency-free (standard library only) and never
touches the network, so it can run on CI and in a sandbox.

Usage::

    .venv/Scripts/python.exe scripts/bench_search_pipeline.py
    .venv/Scripts/python.exe scripts/bench_search_pipeline.py --sizes 1000 20000
    .venv/Scripts/python.exe scripts/bench_search_pipeline.py --repeats 9 --json

The table is written to stdout; ``--json`` appends a machine-readable block for
before/after comparison.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Make ``src`` importable when the script is run directly from the repo root.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import novel_downloader.plugins.grouping as grouping_module  # noqa: E402
import novel_downloader.plugins.ranking as ranking_module  # noqa: E402
from novel_downloader.plugins.grouping import group_results  # noqa: E402
from novel_downloader.plugins.ranking import sort_results  # noqa: E402
from novel_downloader.plugins.search import _finalize  # noqa: E402
from novel_downloader.plugins.search_cache import (  # noqa: E402
    SearchCache,
    build_key,
)
from novel_downloader.schemas import SearchResult  # noqa: E402

NOW = datetime(2024, 6, 15, 12, 0, 0)

#: Filter combinations measured by the benchmark.  The first entry is the
#: unfiltered baseline; the others exercise one and all three local filters so
#: their incremental cost can be reported as a ratio against it.  The values
#: match the synthetic mix above (``_WORDS``/``_DATES``), so every scenario
#: keeps a non-trivial share of the rows instead of filtering everything out.
FILTER_SCENARIOS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("no-filter", {}),
    ("min_words", {"min_words": 100_000}),
    (
        "three-filters",
        {
            "min_words": 100_000,
            "max_words": 2_000_000,
            "updated_after": date(2024, 1, 1),
        },
    ),
)

#: Query used for the multi-token path: two tokens exercising the per-token
#: title/author accumulation in ``detect_query_intent`` / ``_relevance``.
KEYWORD = "斗破苍穹 天蚕土豆"

_BASE_TITLES = (
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
_AUTHORS = (
    "天蚕土豆",
    "忘语",
    "辰东",
    "猫腻",
    "南派三叔",
    "卖报小郎君",
    "爱潜水的乌贼",
    "愤怒的香蕉",
    "",
)
_DECORATIONS = (
    ("[网游]", "", ""),
    ("", "（全本）", ""),
    ("【精校】", "", ""),
    ("", "", "（第1卷）"),
    ("", "", ""),
)
_WORDS = (
    "12.3万字",
    "8,000,000",
    "456.7万字",
    "102万字",
    "未知",
    "3千字",
    "1.2M",
    "",
)
_DATES = (
    "2024-05-01 12:30:00",
    "3天前",
    "2024/5/1",
    "2024年5月1日",
    "昨天",
    "2个月前",
    "2023-01-01",
    "",
)


def make_results(n: int) -> list[SearchResult]:
    """
    Return ``n`` deterministic synthetic aggregate rows.

    The mix matters for the measured shape:

    * every 5th row is a byte-identical copy of the row five back, so
      ``_dedupe_exact`` drops ``n // 5`` rows and ``group_results`` sees real
      cross-site duplicates instead of only unique titles;
    * titles rotate through ten bases with leading/trailing decorative markers
      (``_core_title`` / ``group_results`` exercise the whitelist path) and a
      numeric suffix every few bases, so titles are both duplicated and
      unique;
    * the multi-word keyword gives ``detect_query_intent`` a two-token path.
    """
    results: list[SearchResult] = []
    for i in range(n):
        if i >= 5 and i % 5 == 0:
            results.append(SearchResult(**results[i - 5]))
            continue
        base = _BASE_TITLES[i % len(_BASE_TITLES)]
        lead, trail, volume = _DECORATIONS[i % len(_DECORATIONS)]
        suffix = "" if i % 3 else f" 第{i // 3}部"
        title = f"{lead}{base}{suffix}{trail}{volume}"
        results.append(
            SearchResult(
                site=f"site{i % 25}",
                book_id=f"b{i}",
                book_url=f"https://example.com/book/{i}",
                cover_url="https://img.example.com/c.jpg" if i % 3 else "",
                title=title,
                author=_AUTHORS[i % len(_AUTHORS)],
                latest_chapter=f"第{i}章 标题" if i % 2 else "",
                update_date=_DATES[i % len(_DATES)],
                word_count=_WORDS[i % len(_WORDS)],
                priority=(i % 7) * 30 + 10,
            )
        )
    return results


def reset_caches() -> None:
    """Drop the process-wide LRU caches so every sample is a cold run."""
    ranking_module._normalize_cached.cache_clear()
    ranking_module._similarity_cached.cache_clear()
    ranking_module._query_tokens_cached.cache_clear()
    ranking_module._word_count_cached.cache_clear()
    ranking_module._update_date_cached.cache_clear()
    grouping_module._core_title_cached.cache_clear()


def median_time(
    fn: Callable[[], Any],
    *,
    repeats: int,
    warmup: bool = True,
    reset: Callable[[], None] | None = None,
) -> tuple[float, list[float]]:
    """
    Run ``fn`` ``repeats`` times and return ``(median_ms, samples_ms)``.

    ``reset`` runs before every sample (warm-up included), so the LRU caches
    of one sample cannot make the next one look faster.  Every number is
    therefore the cost of one full call against cold module caches, which is
    what a fresh process pays.
    """
    if reset is not None:
        reset()
    if warmup:
        fn()
    samples: list[float] = []
    for _ in range(repeats):
        if reset is not None:
            reset()
        # Cyclic-GC pauses are bursty: a 20k-row call sometimes pays several
        # full collections inside the timed region and sometimes none, which
        # made repeated medians differ by >1.5x.  Collect first, then disable
        # GC for the measured call and re-enable afterwards.  The number is
        # therefore CPU + allocation time, not "CPU + this run's GC luck";
        # tests apply 5-10x headroom on top of it.
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            fn()
        finally:
            gc.enable()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples), samples


@contextmanager
def _peak_memory() -> Any:
    """Yield a callable returning the peak traced bytes inside the block."""
    gc.collect()
    tracemalloc.start()
    try:
        yield tracemalloc.get_traced_memory
    finally:
        tracemalloc.stop()


def measure_all(n: int, *, repeats: int, include_disk: bool) -> dict[str, Any]:
    """Measure every hot path for one dataset size."""
    rows = make_results(n)
    key = build_key(KEYWORD, ["site1", "site2"], per_site_limit=5)
    cache = SearchCache(max_entries=8)
    cache.put(key, rows[: n // 2])

    def _finalize_run(*, group: bool, **filters: Any) -> list[SearchResult]:
        return _finalize(
            list(rows),
            keyword=KEYWORD,
            sort="quality",
            order=None,
            limit=None,
            group=group,
            min_relevance=None,
            **filters,
        )

    def _finalize_flat() -> list[SearchResult]:
        return _finalize_run(group=False)

    def _finalize_grouped() -> list[SearchResult]:
        return _finalize_run(group=True)

    def _filtered_flat(filters: dict[str, Any]) -> Callable[[], list[SearchResult]]:
        def run() -> list[SearchResult]:
            return _finalize_run(group=False, **filters)

        return run

    def _sort_only() -> None:
        work = list(rows)
        sort_results(work, by="quality", keyword=KEYWORD, now=NOW)

    def _group_only() -> None:
        group_results(rows, keyword=KEYWORD, now=NOW)

    def _cache_miss() -> None:
        cache.get(build_key("no-such-key", ["site1"], per_site_limit=5))

    def _cache_put() -> None:
        cache.put(key, rows)

    filtered_runs = {
        label: _filtered_flat(filters) for label, filters in FILTER_SCENARIOS
    }
    three_filters = dict(FILTER_SCENARIOS)["three-filters"]

    timings: dict[str, Any] = {}
    for name, fn in (
        ("sort_results", _sort_only),
        ("group_results", _group_only),
        ("_finalize_flat", _finalize_flat),
        ("_finalize_grouped", _finalize_grouped),
        ("_finalize_flat[min_words]", filtered_runs["min_words"]),
        ("_finalize_flat[three-filters]", filtered_runs["three-filters"]),
        (
            "_finalize_grouped[three-filters]",
            lambda: _finalize_run(group=True, **three_filters),
        ),
    ):
        timings[name] = median_time(fn, repeats=repeats, reset=reset_caches)
    # The cache layer copies only the list (row dicts are shared) and does no
    # text normalization, so it is measured without touching the LRU caches.
    timings["cache_get(hit)"] = median_time(lambda: cache.get(key), repeats=repeats)
    timings["cache_put"] = median_time(_cache_put, repeats=repeats)
    timings["cache_get(miss)"] = median_time(_cache_miss, repeats=repeats)

    memory = {
        "finalize_flat_peak": _measure_peak(_finalize_flat) / (1024 * 1024),
        "finalize_grouped_peak": _measure_peak(_finalize_grouped) / (1024 * 1024),
        "finalize_flat[min_words]_peak": _measure_peak(filtered_runs["min_words"])
        / (1024 * 1024),
        "finalize_flat[three-filters]_peak": _measure_peak(
            filtered_runs["three-filters"]
        )
        / (1024 * 1024),
        "cache_put_peak": _measure_peak(_cache_put) / (1024 * 1024),
    }

    baseline_ms = timings["_finalize_flat"][0]
    filter_overhead = {
        label: {
            "ratio": timings[f"_finalize_flat[{label}]"][0] / baseline_ms,
            "extra_ms": timings[f"_finalize_flat[{label}]"][0] - baseline_ms,
        }
        for label in ("min_words", "three-filters")
    }

    if include_disk:
        with tempfile.TemporaryDirectory(prefix="noveldl-bench-") as tmp:
            # The production default (2 MiB) is smaller than a 20k-row
            # snapshot (~5 MiB), so such a snapshot is deliberately kept in
            # memory only.  Lift the cap here so the disk layer itself is
            # measured at every size; the no-op-at-the-default-cap behaviour
            # is documented in the module and covered by its own tests.
            disk_cache = SearchCache(
                disk_dir=tmp,
                max_disk_entries=8,
                max_disk_bytes=64 * 1024 * 1024,
            )
            disk_cache.put(key, [])

            def _disk_put() -> None:
                disk_cache._disk_put(key, rows)

            def _put_then_get() -> None:
                disk_cache._disk_put(key, rows)
                disk_cache._disk_get(key)

            timings["disk_put"] = median_time(_disk_put, repeats=repeats)
            timings["disk_put+get"] = median_time(_put_then_get, repeats=repeats)
            # Peak memory of one disk write includes the serialized payload.
            memory["disk_put_peak"] = _measure_peak(
                lambda: disk_cache._disk_put(key, rows)
            ) / (1024 * 1024)

    return {
        "n": n,
        "timings_ms": {name: value[0] for name, value in timings.items()},
        "samples_ms": {name: value[1] for name, value in timings.items()},
        "memory_peak_mb": memory,
        "filter_overhead": filter_overhead,
    }


def _measure_peak(fn: Callable[[], Any]) -> float:
    """Return the peak traced bytes (not MB) of one call."""
    with _peak_memory() as peak:
        fn()
        return float(peak()[1])


def measure_cache_key_scaling(
    sizes: Sequence[int], *, repeats: int
) -> list[dict[str, Any]]:
    """Show that ``build_key`` cost depends on the site list, not result count."""
    out: list[dict[str, Any]] = []
    for n in sizes:
        rows = make_results(n)
        sites = [f"site{i % 25}" for i in range(min(n, 25))]
        ms, _ = median_time(
            lambda sites=sites: build_key(KEYWORD, sites, per_site_limit=5),
            repeats=repeats,
        )
        out.append({"n": n, "build_key_ms": ms, "rows": len(rows)})
    return out


def print_table(
    results: Sequence[dict[str, Any]], cache_scale: Sequence[dict[str, Any]]
) -> None:
    """Render a fixed-width table plus the key-scaling check."""
    width = 32
    names = list(results[0]["timings_ms"])
    header = f"{'operation':<{width}}" + "".join(f"{r['n']:>12}" for r in results)
    print("Median of N repeats (ms) -- lower is better")
    print(header)
    print("-" * len(header))
    for name in names:
        row = f"{name:<{width}}" + "".join(
            f"{r['timings_ms'][name]:>12.2f}" for r in results
        )
        print(row)
    print()
    print("Peak traced memory (MB)")
    mem_names = list(results[0]["memory_peak_mb"])
    header = f"{'operation':<{width}}" + "".join(f"{r['n']:>12}" for r in results)
    print(header)
    print("-" * len(header))
    for name in mem_names:
        row = f"{name:<{width}}" + "".join(
            f"{r['memory_peak_mb'][name]:>12.2f}" for r in results
        )
        print(row)
    print()
    print("Filter overhead vs no filter -- ratio (x) and extra milliseconds")
    print(f"{'scenario':<{width}}" + "".join(f"{r['n']:>12}" for r in results))
    print("-" * len(header))
    for label in ("min_words", "three-filters"):
        cells = "".join(
            f"{r['filter_overhead'][label]['ratio']:>7.2f}x"
            f"{r['filter_overhead'][label]['extra_ms']:>+7.0f}ms"
            for r in results
        )
        print(f"{label:<{width}}{cells}")
    print()
    print("build_key cost vs result count (rows are never part of the key)")
    print(f"{'n':>8}{'rows':>8}{'build_key_ms':>16}")
    for item in cache_scale:
        print(f"{item['n']:>8}{item['rows']:>8}{item['build_key_ms']:>16.4f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[1000, 5000, 20000],
        help="result counts to measure (default: 1000 5000 20000)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="timed repetitions per operation, median reported (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="also print a JSON block for before/after comparison",
    )
    parser.add_argument(
        "--no-disk",
        action="store_true",
        help="skip the persistent cache layer",
    )
    args = parser.parse_args(argv)

    results = [
        measure_all(n, repeats=args.repeats, include_disk=not args.no_disk)
        for n in args.sizes
    ]
    cache_scale = measure_cache_key_scaling(args.sizes, repeats=args.repeats)
    print_table(results, cache_scale)
    if args.json:
        print()
        print(
            json.dumps(
                {"results": results, "cache_key_scaling": cache_scale},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
