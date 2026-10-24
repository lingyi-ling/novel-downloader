#!/usr/bin/env python3
"""
bench_api.py
------------

In-process performance benchmark for the local REST API.

Requests are served through :class:`httpx.ASGITransport`, so no TCP port is
opened and no server process is needed. Every endpoint runs five warm-up
requests followed by thirty measured requests (``--iterations``); p50 / p95 /
p99 latency and throughput (RPS) are reported, and ``--json`` emits the same
data as machine-readable JSON.

All expensive collaborators are faked so the numbers describe the API layer,
not the network:

* ``POST /search`` returns ``--search-results`` synthetic results;
* ``GET /tasks`` serves an in-memory manager with ``--task-count`` tasks;
* ``GET /history`` scans a temporary directory with ``--history-files`` files;
* ``GET /music/platforms`` uses the real (lazily imported) music registry.

The script also estimates how much of each request is JSON serialisation:
FastAPI's ``serialize_response`` (validation / ``jsonable_encoder``) and
Starlette's ``JSONResponse.render`` (``json.dumps`` + UTF-8 encode) are timed
around every measured request.

Examples::

    ./.venv/Scripts/python.exe scripts/bench_api.py
    ./.venv/Scripts/python.exe scripts/bench_api.py --json
    ./.venv/Scripts/python.exe scripts/bench_api.py --concurrency 4
    ./.venv/Scripts/python.exe scripts/bench_api.py --iterations 100

The benchmark never modifies the API. Run it with ``--concurrency`` above 1
to observe how the single-event-loop in-process transport behaves under
overlapping requests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Running ``python scripts/bench_api.py`` puts ``scripts/`` on sys.path, not
# ``src/``; make the package importable without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

DEFAULT_WARMUP = 5
DEFAULT_ITERATIONS = 30
DEFAULT_CONCURRENCY = 1
DEFAULT_SEARCH_RESULTS = 100
DEFAULT_HISTORY_FILES = 200
DEFAULT_TASK_COUNT = 100
DEFAULT_SEED = 42

_SEARCH_SITES = (
    "qidian",
    "fanqienovel",
    "b520",
    "ciweimao",
    "linovel",
    "sfacg",
    "n37yq",
    "esjzone",
)
_WORD_COUNTS = ("", "12.3万字", "88万字", "300万字")
_UPDATE_DATES = ("", "昨天", "3天前", "2024-05-01", "2024年5月1日")
_AUTHORS = tuple(f"作者{i:02d}" for i in range(20))


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------
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


def _median(values: Sequence[float]) -> float:
    return percentile(values, 50)


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------
def make_search_results(count: int, *, keyword: str, seed: int) -> list[dict[str, Any]]:
    """Build ``count`` deterministic, realistic-looking search results."""
    rng = random.Random(seed)
    results: list[dict[str, Any]] = []
    for index in range(count):
        site = _SEARCH_SITES[index % len(_SEARCH_SITES)]
        results.append(
            {
                "site": site,
                "book_id": f"{index:06d}",
                "book_url": f"https://example.com/{site}/book/{index}",
                "cover_url": (
                    f"https://example.com/covers/{index}.jpg" if index % 3 else ""
                ),
                "title": f"{keyword} 第{index // 8 + 1}部",
                "author": _AUTHORS[index % len(_AUTHORS)],
                "latest_chapter": f"第{index % 400 + 1}章 新的章节标题",
                "update_date": rng.choice(_UPDATE_DATES),
                "word_count": rng.choice(_WORD_COUNTS),
                "priority": rng.choice((5, 10, 30, 50, 100, 200, 500, 1000)),
            }
        )
    return results


def make_history_files(directory: Path, count: int) -> None:
    """Create ``count`` txt/epub files with Chinese names and varied sizes."""
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        ext = "txt" if index % 3 else "epub"
        name = f"作品{index:04d} - 第{index // 10 + 1}卷 - 标题.{ext}"
        payload = "x" * (1024 * (index % 50 + 1))
        (directory / name).write_text(payload, encoding="utf-8")


def _make_task(
    index: int,
    *,
    status: Any,
    chapters_total: int,
    chapters_done: int,
    finished: bool,
) -> Any:
    """Build one ``DownloadTask`` for the ``/tasks`` benchmark."""
    from novel_downloader.apps.core.models import DownloadTask

    task = DownloadTask(
        title=f"合成作品 {index:03d} 的长标题",
        site=_SEARCH_SITES[index % len(_SEARCH_SITES)],
        book_id=f"{index:06d}",
        status=status,
        chapters_total=chapters_total,
        chapters_done=chapters_done,
        error=None if finished else "",
        created_at=time.time() - index * 60.0,
        finished_at=time.time() - index * 10.0 if finished else None,
    )
    if finished:
        task.exported_paths = {
            "txt": Path(f"/tmp/export/{index:03d}.txt"),
            "epub": Path(f"/tmp/export/{index:03d}.epub"),
        }
    return task


def make_task_manager(count: int) -> Any:
    """Populate an isolated ``TaskManager`` without starting any worker."""
    from novel_downloader.apps.core.models import Status
    from novel_downloader.apps.core.task_manager import TaskManager

    manager = TaskManager(max_download_retries=0)
    pending = count // 2
    running = count // 5
    completed = count - pending - running

    for index in range(pending):
        task = _make_task(
            index,
            status=Status.QUEUED,
            chapters_total=120,
            chapters_done=0,
            finished=False,
        )
        manager.pending[f"site{index % 4}"].append(task)

    for index in range(running):
        task = _make_task(
            pending + index,
            status=Status.RUNNING,
            chapters_total=120,
            chapters_done=60,
            finished=False,
        )
        # ``running`` is keyed by site and holds one task per site, so use a
        # unique key to keep every synthetic running task visible.
        manager.running[f"running-site{index}"] = task

    for index in range(completed):
        task = _make_task(
            pending + running + index,
            status=Status.COMPLETED,
            chapters_total=120,
            chapters_done=120,
            finished=True,
        )
        manager.completed.append(task)

    return manager


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------
@dataclass
class SerializationProbe:
    """Samples collected by the FastAPI / Starlette wrappers."""

    prep_ms: list[float] = field(default_factory=list)
    encode_ms: list[float] = field(default_factory=list)

    def clear(self) -> None:
        self.prep_ms.clear()
        self.encode_ms.clear()


def install_serialization_probe(probe: SerializationProbe) -> tuple[Any, Any]:
    """
    Wrap response preparation and JSON rendering, returning originals.

    ``serialize_response`` covers pydantic validation and
    ``jsonable_encoder``; ``JSONResponse.render`` covers ``json.dumps`` and
    the UTF-8 encode. Both run on the event loop, so list appends are safe.
    """
    import fastapi.routing as fastapi_routing
    from starlette.responses import JSONResponse

    original_serialize = fastapi_routing.serialize_response
    original_render = JSONResponse.render

    async def timed_serialize_response(**kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return await original_serialize(**kwargs)
        finally:
            probe.prep_ms.append((time.perf_counter() - start) * 1000.0)

    def timed_render(self: Any, content: Any) -> bytes:
        start = time.perf_counter()
        try:
            return original_render(self, content)
        finally:
            probe.encode_ms.append((time.perf_counter() - start) * 1000.0)

    fastapi_routing.serialize_response = timed_serialize_response
    JSONResponse.render = timed_render
    return original_serialize, original_render


def restore_serialization_probe(originals: tuple[Any, Any]) -> None:
    import fastapi.routing as fastapi_routing
    from starlette.responses import JSONResponse

    original_serialize, original_render = originals
    fastapi_routing.serialize_response = original_serialize
    JSONResponse.render = original_render


# ---------------------------------------------------------------------------
# benchmark core
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoint:
    """One endpoint under test."""

    name: str
    method: str
    path: str
    json_body: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class EndpointResult:
    """Raw samples for one endpoint."""

    endpoint: Endpoint
    latencies_ms: list[float] = field(default_factory=list)
    payload_bytes: list[int] = field(default_factory=list)
    items: int | None = None
    wall_s: float = 0.0
    error: str | None = None


async def _one_request(
    client: Any,
    endpoint: Endpoint,
) -> tuple[float, bytes]:
    """Perform one fully-read request and return ``(elapsed_ms, body)``."""
    kwargs: dict[str, Any] = {}
    if endpoint.json_body is not None:
        kwargs["json"] = endpoint.json_body

    start = time.perf_counter()
    response = await client.request(endpoint.method, endpoint.path, **kwargs)
    await response.aread()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    body = response.content

    if not 200 <= response.status_code < 300:
        snippet = body[:200].decode("utf-8", "replace")
        raise RuntimeError(
            f"{endpoint.label} returned {response.status_code}: {snippet!r}"
        )
    return elapsed_ms, body


def _count_items(name: str, payload: Any) -> int | None:
    """Extract a human-meaningful item count from a response body."""
    if name == "sites":
        return len(payload.get("search", {})) + len(payload.get("download", {}))
    if name in {"history", "tasks", "search"}:
        return int(payload.get("total", 0))
    if name == "music/platforms":
        return len(payload.get("platforms", []))
    return None


async def _bench_endpoint(
    client: Any,
    endpoint: Endpoint,
    *,
    warmup: int,
    iterations: int,
    concurrency: int,
    probe: SerializationProbe,
) -> EndpointResult:
    """Warm up, then measure ``iterations`` requests with bounded overlap."""
    result = EndpointResult(endpoint=endpoint)
    try:
        for _ in range(warmup):
            await _one_request(client, endpoint)

        probe.clear()
        first_body: bytes | None = None
        start = time.perf_counter()
        remaining = iterations
        while remaining > 0:
            batch = min(concurrency, remaining)
            samples = await asyncio.gather(
                *(_one_request(client, endpoint) for _ in range(batch))
            )
            for elapsed_ms, body in samples:
                result.latencies_ms.append(elapsed_ms)
                result.payload_bytes.append(len(body))
                if first_body is None:
                    first_body = body
            remaining -= batch
        result.wall_s = time.perf_counter() - start

        if first_body is not None:
            try:
                result.items = _count_items(endpoint.name, json.loads(first_body))
            except (ValueError, AttributeError):
                result.items = None
    except Exception as exc:  # noqa: BLE001 - report, do not abort the run
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _result_payload(
    result: EndpointResult, probe: SerializationProbe
) -> dict[str, Any]:
    """Turn raw samples into the report row."""
    base: dict[str, Any] = {
        "endpoint": result.endpoint.name,
        "label": result.endpoint.label,
        "method": result.endpoint.method,
        "path": result.endpoint.path,
        "requests": len(result.latencies_ms),
        "items": result.items,
    }
    if result.error is not None:
        base.update({"error": result.error})
        return base

    latencies = result.latencies_ms
    prep = probe.prep_ms
    encode = probe.encode_ms
    p50 = percentile(latencies, 50)
    prep_p50 = _median(prep) if prep else 0.0
    encode_p50 = _median(encode) if encode else 0.0
    serialization = prep_p50 + encode_p50
    base.update(
        {
            "p50_ms": round(p50, 3),
            "p95_ms": round(percentile(latencies, 95), 3),
            "p99_ms": round(percentile(latencies, 99), 3),
            "mean_ms": round(sum(latencies) / len(latencies), 3),
            "rps": round(len(latencies) / result.wall_s, 2)
            if result.wall_s > 0
            else 0.0,
            "serialize_prep_ms": round(prep_p50, 3),
            "serialize_encode_ms": round(encode_p50, 3),
            "serialization_ms": round(serialization, 3),
            "serialization_share_pct": round(serialization / p50 * 100.0, 1)
            if p50 > 0
            else 0.0,
            "estimated_handler_ms": round(max(p50 - serialization, 0.0), 3),
            "payload_bytes_median": int(_median(result.payload_bytes))
            if result.payload_bytes
            else 0,
        }
    )
    return base


def _describe(name: str, items: int | None, args: argparse.Namespace) -> str:
    if name == "history":
        return f"{args.history_files} files in a temporary output directory"
    if name == "tasks":
        return f"{args.task_count} tasks (pending/running/completed)"
    if name == "search":
        return f"{args.search_results} synthetic search results"
    if name == "sites":
        return f"{items if items is not None else '?'} supported site entries"
    if name == "music/platforms":
        return f"{items if items is not None else '?'} registered platforms"
    return "static JSON response"


def _build_observations(rows: list[dict[str, Any]], *, concurrency: int) -> list[str]:
    """Derive short, data-driven bottleneck notes from the measured rows."""
    by_name = {row["endpoint"]: row for row in rows}
    observations: list[str] = []

    for row in rows:
        if row.get("error"):
            observations.append(f"{row['label']} failed: {row['error']}")

    health = by_name.get("health")
    if health and not health.get("error"):
        observations.append(
            f"{health['label']} p50 {health['p50_ms']:.3f} ms is the in-process "
            "floor (request building + ASGI + JSON response); compare other "
            "endpoints against it to separate handler cost from transport "
            "overhead."
        )

    history = by_name.get("history")
    if history and not history.get("error"):
        observations.append(
            "history: _scan_files() runs os.scandir + stat over "
            f"{history['items']} files on every request, then sorts the whole "
            f"list before slicing (p50 {history['p50_ms']:.3f} ms). There is no "
            "cache, so cost grows with the output directory."
        )

    tasks = by_name.get("tasks")
    if tasks and not tasks.get("error"):
        observations.append(
            "tasks: snapshot() copies every queue and the default response "
            f"serialises all {tasks['items']} tasks; serialisation is "
            f"~{tasks['serialization_share_pct']:.0f}% of p50. Pagination "
            "params only limit task_to_dict(), not the snapshot copy."
        )

    search = by_name.get("search")
    if search and not search.get("error"):
        observations.append(
            f"search: the {search['items']}-result payload adds "
            f"{search['serialization_ms']:.3f} ms of serialisation "
            f"(~{search['serialization_share_pct']:.0f}% of p50); in production "
            "real network latency dominates this layer."
        )

    music = by_name.get("music/platforms")
    if music and not music.get("error"):
        observations.append(
            "music/platforms: _load_music_components() re-runs the optional "
            "imports (module objects are cached by sys.modules) and "
            "available_labels() re-sorts the registry on every request "
            f"(p50 {music['p50_ms']:.3f} ms after warm-up, "
            f"{music['items']} platforms)."
        )

    heavy = [
        row
        for row in rows
        if not row.get("error") and row.get("serialization_share_pct", 0.0) >= 30.0
    ]
    for row in heavy:
        observations.append(
            f"{row['label']}: serialisation is {row['serialization_share_pct']:.0f}% "
            f"of p50 ({row['serialization_ms']:.3f} ms); reducing payload size "
            "or paginating would help more than micro-optimising the handler."
        )

    if concurrency > 1:
        observations.append(
            f"concurrency={concurrency}: in-process ASGI runs handlers and JSON "
            "encoding on one event loop, so CPU-bound serialisation is not "
            "parallelised. RPS therefore reflects overlap of I/O only."
        )
    return observations


# ---------------------------------------------------------------------------
# CLI / report rendering
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the local Novel Downloader REST API in-process.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"measured requests per endpoint (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP,
        help=f"warm-up requests per endpoint (default: {DEFAULT_WARMUP})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="max in-flight requests via asyncio.gather (default: 1)",
    )
    parser.add_argument(
        "--search-results",
        type=int,
        default=DEFAULT_SEARCH_RESULTS,
        help=f"synthetic /search results (default: {DEFAULT_SEARCH_RESULTS})",
    )
    parser.add_argument(
        "--history-files",
        type=int,
        default=DEFAULT_HISTORY_FILES,
        help=f"files in the /history temp dir (default: {DEFAULT_HISTORY_FILES})",
    )
    parser.add_argument(
        "--task-count",
        type=int,
        default=DEFAULT_TASK_COUNT,
        help=f"tasks exposed by /tasks (default: {DEFAULT_TASK_COUNT})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"synthetic search RNG seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.search_results < 1:
        parser.error("--search-results must be >= 1")
    if args.history_files < 0:
        parser.error("--history-files must be >= 0")
    if args.task_count < 1:
        parser.error("--task-count must be >= 1")
    return args


async def _run(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    import httpx

    from novel_downloader import __version__
    from novel_downloader.apps.api import create_app
    from novel_downloader.apps.api import routes as api_routes
    from novel_downloader.apps.common import history_files
    from novel_downloader.plugins import search as search_module

    history_dir = data_dir / "history"
    make_history_files(history_dir, args.history_files)

    synthetic = make_search_results(
        args.search_results,
        keyword="斗破苍穹",
        seed=args.seed,
    )

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in synthetic]

    # ``routes`` binds the function at import time, so patching the plugin
    # module alone would not affect POST /search. Patch both for completeness.
    original_route_search = api_routes.search
    original_plugin_search = search_module.search
    api_routes.search = fake_search  # type: ignore[assignment]
    search_module.search = fake_search  # type: ignore[assignment]

    original_output_dir = history_files._output_dir
    history_files._output_dir = lambda: history_dir  # type: ignore[assignment]

    manager = make_task_manager(args.task_count)
    app = create_app(
        token="",
        output_dir=data_dir / "downloads",
        webhook_url="",
    )
    app.dependency_overrides[api_routes.get_manager] = lambda: manager

    probe = SerializationProbe()
    serialization_originals = install_serialization_probe(probe)

    endpoints = [
        Endpoint("health", "GET", "/api/v1/health"),
        Endpoint("sites", "GET", "/api/v1/sites"),
        Endpoint("history", "GET", "/api/v1/history"),
        Endpoint("tasks", "GET", "/api/v1/tasks"),
        Endpoint(
            "search",
            "POST",
            "/api/v1/search",
            {
                "keyword": "斗破苍穹",
                "sites": ["demo"],
                "per_site_limit": args.search_results,
            },
        ),
        Endpoint("music/platforms", "GET", "/api/v1/music/platforms"),
    ]

    rows: list[dict[str, Any]] = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://bench.local",
            timeout=30.0,
        ) as client:
            for endpoint in endpoints:
                probe.clear()
                result = await _bench_endpoint(
                    client,
                    endpoint,
                    warmup=args.warmup,
                    iterations=args.iterations,
                    concurrency=args.concurrency,
                    probe=probe,
                )
                row = _result_payload(result, probe)
                row["synthetic"] = _describe(endpoint.name, result.items, args)
                rows.append(row)
    finally:
        restore_serialization_probe(serialization_originals)
        api_routes.search = original_route_search
        search_module.search = original_plugin_search
        history_files._output_dir = original_output_dir
        app.dependency_overrides.clear()

    return {
        "meta": {
            "transport": "httpx.ASGITransport (in-process, no socket)",
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            "app_version": __version__,
            "warmup": args.warmup,
            "iterations": args.iterations,
            "concurrency": args.concurrency,
            "search_results": args.search_results,
            "history_files": args.history_files,
            "task_count": args.task_count,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "endpoints": rows,
        "observations": _build_observations(rows, concurrency=args.concurrency),
    }


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _join_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(
        text.ljust(width) for text, width in zip(cells, widths, strict=False)
    )


def _print_report(report: dict[str, Any]) -> None:
    meta = report["meta"]
    print(
        "API benchmark: "
        f"{meta['iterations']} requests/endpoint, {meta['warmup']} warm-ups, "
        f"concurrency={meta['concurrency']}"
    )
    print(
        f"  python {meta['python']} | {meta['platform']} | "
        f"app {meta['app_version']} | {meta['transport']}"
    )
    print()

    headers = (
        "endpoint",
        "req",
        "p50 ms",
        "p95 ms",
        "p99 ms",
        "rps",
        "ser ms",
        "ser %",
        "payload",
        "items",
    )
    body: list[tuple[str, ...]] = []
    for row in report["endpoints"]:
        if row.get("error"):
            body.append(
                (
                    row["label"],
                    "0",
                    "ERROR",
                    row["error"][:40],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
            continue
        body.append(
            (
                row["label"],
                str(row["requests"]),
                f"{row['p50_ms']:.3f}",
                f"{row['p95_ms']:.3f}",
                f"{row['p99_ms']:.3f}",
                f"{row['rps']:.1f}",
                f"{row['serialization_ms']:.3f}",
                f"{row['serialization_share_pct']:.0f}%",
                _format_bytes(row["payload_bytes_median"]),
                str(row["items"] if row["items"] is not None else "-"),
            )
        )

    widths = [
        max(len(headers[index]), *(len(line[index]) for line in body))
        if body
        else len(headers[index])
        for index in range(len(headers))
    ]
    print(_join_row(headers, widths))
    print("  ".join("-" * width for width in widths))
    for line in body:
        print(_join_row(line, widths))

    print()
    print("Synthetic data:")
    for row in report["endpoints"]:
        print(f"  {row['label']}: {row['synthetic']}")

    print()
    print("Observations:")
    for note in report["observations"]:
        print(f"  - {note}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    # Keep configuration, caches and any accidental writes out of the real
    # user profile; the API modules are imported after this point.
    with (
        tempfile.TemporaryDirectory(prefix="novel-bench-home-") as home,
        tempfile.TemporaryDirectory(prefix="novel-bench-data-") as data,
    ):
        os.environ["NOVEL_HOME"] = home
        os.environ.pop("NOVEL_API_TOKEN", None)
        os.environ.pop("NOVEL_API_WEBHOOK", None)
        report = asyncio.run(_run(args, Path(data)))

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
