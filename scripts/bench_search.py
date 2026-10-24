#!/usr/bin/env python3
"""
bench_search.py
---------------

Micro-benchmark for the search aggregation layer.

Two independent modes:

* ``--offline`` (default) builds synthetic :class:`SearchResult` records and
  measures :func:`rank_score`, :func:`sort_results` and
  :func:`group_results`.  No network access is required, so the numbers are
  stable enough to compare before/after a change.
* ``--online`` calls the real :func:`novel_downloader.plugins.search.search`
  and reports wall-clock timings plus the per-site latency distribution.

Every scenario reports the total, p50 and p95 over ``--times`` iterations.

Examples::

    python scripts/bench_search.py --keyword 斗破苍穹 --times 7
    python scripts/bench_search.py --online --keyword 斗破苍穹 --times 3
    python scripts/bench_search.py --json > bench.json

Only the standard library and the project's existing runtime dependencies
are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Running ``python scripts/bench_search.py`` puts ``scripts/`` on sys.path,
# not ``src/``; make the package importable without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import novel_downloader.plugins.search as search_module  # noqa: E402
from novel_downloader.plugins.grouping import group_results  # noqa: E402
from novel_downloader.plugins.ranking import (  # noqa: E402
    rank_score,
    sort_results,
)
from novel_downloader.plugins.registry import registrar  # noqa: E402
from novel_downloader.plugins.search import search  # noqa: E402
from novel_downloader.schemas import SearchResult  # noqa: E402

_DEFAULT_KEYWORD = "斗破苍穹"

_SITES = tuple(f"site{i:02d}" for i in range(12))

_WORD_COUNTS = (
    "",
    "未知",
    "1.2万字",
    "12.3万字",
    "88万字",
    "300万字",
    "1234",
    "3千字",
)

_UPDATE_DATES = (
    "",
    "刚刚",
    "昨天",
    "3天前",
    "2个月前",
    "2024-05-01",
    "2023/12/31",
    "2024年5月1日",
    "2019-01-01",
)

_AUTHORS = tuple(f"作者{i}" for i in range(37))


# --------------------------------------------------------------------------- #
# statistics helpers
# --------------------------------------------------------------------------- #
def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (0..100) of a non-empty sequence."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


@dataclass
class ScenarioResult:
    """Timing summary of one benchmark scenario."""

    name: str
    items: int
    iterations: int
    durations_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        total = sum(self.durations_ms)
        p50 = percentile(self.durations_ms, 50)
        p95 = percentile(self.durations_ms, 95)
        return {
            "name": self.name,
            "items": self.items,
            "iterations": self.iterations,
            "total_ms": round(total, 3),
            "p50_ms": round(p50, 3),
            "p95_ms": round(p95, 3),
            "per_item_us": round(p50 * 1000.0 / self.items, 3) if self.items else 0.0,
        }


def _bench(
    name: str,
    items: int,
    times: int,
    action: Callable[[], object],
) -> ScenarioResult:
    result = ScenarioResult(name=name, items=items, iterations=times)
    action()  # warm-up (imports, caches, branch predictors)
    for _ in range(times):
        start = time.perf_counter()
        action()
        result.durations_ms.append((time.perf_counter() - start) * 1000.0)
    return result


# --------------------------------------------------------------------------- #
# synthetic data
# --------------------------------------------------------------------------- #
def make_results(
    count: int,
    *,
    keyword: str,
    seed: int = 42,
    conflict: bool = False,
) -> list[SearchResult]:
    """
    Build ``count`` deterministic synthetic results.

    Normally titles are drawn from a pool of ``count // 4`` books (roughly
    four site copies per book).  With ``conflict=True`` every record shares
    one title but has a unique author, which is the worst case for
    same-title clustering.
    """
    rng = random.Random(seed)
    if conflict:
        titles = ["同书名"] * count
        authors = [f"作者{i}" for i in range(count)]
    else:
        pool_size = max(1, count // 4)
        titles = [keyword] + [f"作品{i:05d}" for i in range(1, pool_size)]
        author_pool = list(_AUTHORS)
        authors = [author_pool[i % len(author_pool)] for i in range(pool_size)]

    results: list[SearchResult] = []
    for index in range(count):
        book = rng.randrange(len(titles))
        title = titles[book]
        author = authors[book]
        if not conflict and rng.random() < 0.05:
            author = ""  # unknown author still merges
        results.append(
            {
                "site": _SITES[index % len(_SITES)],
                "book_id": f"{index:06d}",
                "book_url": f"https://example.com/book/{index}",
                "cover_url": "https://example.com/cover.jpg"
                if rng.random() < 0.6
                else "",
                "title": title,
                "author": author,
                "latest_chapter": f"第{index % 400 + 1}章"
                if rng.random() < 0.6
                else "",
                "update_date": rng.choice(_UPDATE_DATES),
                "word_count": rng.choice(_WORD_COUNTS),
                "priority": rng.choice((5, 10, 30, 50, 100, 200, 500, 1000)),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# offline benchmark
# --------------------------------------------------------------------------- #
def run_offline(keyword: str, times: int, seed: int) -> list[ScenarioResult]:
    rank_data = make_results(2000, keyword=keyword, seed=seed)
    group_1000 = make_results(1000, keyword=keyword, seed=seed + 1)
    group_5000 = make_results(5000, keyword=keyword, seed=seed + 2)
    conflict_2000 = make_results(2000, keyword=keyword, seed=seed + 3, conflict=True)

    def score_all() -> None:
        for item in rank_data:
            rank_score(item, keyword)

    def sort_copy() -> None:
        data = list(rank_data)
        sort_results(data, keyword=keyword)

    return [
        _bench("rank_score", len(rank_data), times, score_all),
        _bench("sort_results", len(rank_data), times, sort_copy),
        _bench(
            "group_results",
            len(group_1000),
            times,
            lambda: group_results(group_1000, keyword),
        ),
        _bench(
            "group_results",
            len(group_5000),
            times,
            lambda: group_results(group_5000, keyword),
        ),
        _bench(
            "group_results_conflict",
            len(conflict_2000),
            times,
            lambda: group_results(conflict_2000, keyword),
        ),
    ]


# --------------------------------------------------------------------------- #
# online benchmark
# --------------------------------------------------------------------------- #
def _install_timers(
    classes: Sequence[type[Any]],
) -> tuple[
    dict[type[Any], Any],
    dict[str, list[float]],
    dict[str, int],
]:
    """Wrap every searcher's ``search`` to record per-site wall-clock time."""
    originals: dict[type[Any], Any] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)

    for cls in classes:
        original = cls.search
        site = cls.site_name

        async def timed(
            self: Any,
            kw: str,
            limit: int | None = None,
            *,
            _original: Any = original,
            _site: str = site,
        ) -> Any:
            start = time.perf_counter()
            try:
                return await _original(self, kw, limit)
            except BaseException:
                errors[_site] += 1
                raise
            finally:
                durations[_site].append(time.perf_counter() - start)

        originals[cls] = original
        cls.search = timed  # type: ignore[method-assign]
    return originals, durations, errors


def _restore_timers(originals: dict[type[Any], Any]) -> None:
    for cls, original in originals.items():
        cls.search = original  # type: ignore[method-assign]


async def _online_runs(
    keyword: str,
    times: int,
    sites: Sequence[str] | None,
    per_site_limit: int,
    timeout: float,
    sort: str,
) -> tuple[list[float], int, dict[str, list[float]], dict[str, int]]:
    classes = registrar.get_searcher_classes(sites, load_all_if_none=True)
    originals, durations, errors = _install_timers(classes)
    totals: list[float] = []
    result_count = 0
    try:
        for _ in range(times):
            # The real search() serves repeat queries from a process-wide TTL
            # cache; drop it so every iteration measures actual network work.
            search_module._CACHE.clear()
            start = time.perf_counter()
            results = await search(
                keyword,
                sites=sites,
                per_site_limit=per_site_limit,
                timeout=timeout,
                sort=sort,
            )
            totals.append((time.perf_counter() - start) * 1000.0)
            result_count = len(results)
    finally:
        _restore_timers(originals)
    return totals, result_count, dict(durations), dict(errors)


def run_online(
    keyword: str,
    times: int,
    sites: Sequence[str] | None,
    per_site_limit: int,
    timeout: float,
    sort: str,
) -> dict[str, Any]:
    totals, result_count, durations, errors = asyncio.run(
        _online_runs(keyword, times, sites, per_site_limit, timeout, sort)
    )

    per_site: list[dict[str, Any]] = []
    for site, samples in durations.items():
        per_site.append(
            {
                "site": site,
                "calls": len(samples),
                "errors": errors.get(site, 0),
                "mean_ms": round(sum(samples) / len(samples) * 1000.0, 3)
                if samples
                else 0.0,
                "p50_ms": round(percentile(samples, 50) * 1000.0, 3),
                "p95_ms": round(percentile(samples, 95) * 1000.0, 3),
            }
        )
    per_site.sort(key=lambda row: row["mean_ms"], reverse=True)

    return {
        "keyword": keyword,
        "iterations": times,
        "results_returned": result_count,
        "total_ms": round(sum(totals), 3),
        "p50_ms": round(percentile(totals, 50), 3),
        "p95_ms": round(percentile(totals, 95), 3),
        "site_count": len(per_site),
        "slowest_sites": per_site[:5],
        "all_sites": per_site,
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _join_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(
        text.ljust(width) for text, width in zip(cells, widths, strict=False)
    )


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = ("scenario", "items", "iters", "total ms", "p50 ms", "p95 ms", "us/item")
    body = [
        (
            str(row["name"]),
            str(row["items"]),
            str(row["iterations"]),
            f"{row['total_ms']:.2f}",
            f"{row['p50_ms']:.2f}",
            f"{row['p95_ms']:.2f}",
            f"{row['per_item_us']:.2f}",
        )
        for row in rows
    ]
    widths = [
        max(len(headers[i]), *(len(line[i]) for line in body))
        if body
        else len(headers[i])
        for i in range(len(headers))
    ]
    print(_join_row(headers, widths))
    print("  ".join("-" * width for width in widths))
    for row in body:
        print(_join_row(row, widths))


def _print_online(report: dict[str, Any]) -> None:
    print(
        f"online search: keyword={report['keyword']!r} "
        f"iterations={report['iterations']} results={report['results_returned']}"
    )
    print(
        f"  total={report['total_ms']:.2f} ms  "
        f"p50={report['p50_ms']:.2f} ms  p95={report['p95_ms']:.2f} ms"
    )
    if not report["slowest_sites"]:
        print("  no site timings collected (no searchers ran)")
        return
    print()
    headers = ("site", "calls", "errors", "mean ms", "p50 ms", "p95 ms")
    widths = (24, 7, 7, 10, 10, 10)
    print(_join_row(headers, widths))
    print("  ".join("-" * width for width in widths))
    for row in report["slowest_sites"]:
        cells = (
            str(row["site"]),
            str(row["calls"]),
            str(row["errors"]),
            f"{row['mean_ms']:.2f}",
            f"{row['p50_ms']:.2f}",
            f"{row['p95_ms']:.2f}",
        )
        print(_join_row(cells, widths))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark search ranking, grouping and online aggregation.",
    )
    parser.add_argument(
        "--keyword",
        default=_DEFAULT_KEYWORD,
        help=f"search keyword (default: {_DEFAULT_KEYWORD})",
    )
    parser.add_argument(
        "--times",
        type=int,
        default=5,
        help="recorded iterations per scenario (default: 5)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        dest="mode",
        action="store_const",
        const="offline",
        help="synthetic ranking/grouping benchmark (default)",
    )
    mode.add_argument(
        "--online",
        dest="mode",
        action="store_const",
        const="online",
        help="real search() benchmark against registered sites",
    )
    parser.set_defaults(mode="offline")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of tables",
    )
    parser.add_argument("--seed", type=int, default=42, help="offline RNG seed")
    parser.add_argument(
        "--sites",
        default=None,
        help="online mode: comma-separated site keys (default: all)",
    )
    parser.add_argument(
        "--per-site-limit",
        type=int,
        default=5,
        help="online mode: results per site (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="online mode: per-request timeout in seconds (default: 5.0)",
    )
    parser.add_argument(
        "--sort",
        default="quality",
        help="online mode: sort key (default: quality)",
    )
    args = parser.parse_args(argv)
    if args.times < 1:
        parser.error("--times must be >= 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    payload: dict[str, Any] = {
        "mode": args.mode,
        "keyword": args.keyword,
        "times": args.times,
    }

    offline_rows: list[dict[str, Any]] = []
    online_report: dict[str, Any] | None = None

    if args.mode == "offline":
        scenarios = run_offline(args.keyword, args.times, args.seed)
        offline_rows = [scenario.summary() for scenario in scenarios]
        payload["offline"] = offline_rows
    else:
        sites = (
            [part.strip() for part in args.sites.split(",") if part.strip()]
            if args.sites
            else None
        )
        online_report = run_online(
            args.keyword,
            args.times,
            sites,
            args.per_site_limit,
            args.timeout,
            args.sort,
        )
        payload["online"] = online_report

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if offline_rows:
        _print_table(offline_rows)
    if online_report is not None:
        _print_online(online_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
