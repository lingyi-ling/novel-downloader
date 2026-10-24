#!/usr/bin/env python3
"""
Site health checker.

Two modes are available:

``search`` (default)
    Runs each site's real ``BaseSearcher.search()`` with a keyword and reports
    ``PASS`` (results found), ``EMPTY`` (no results, no error) or ``ERROR``
    (exception/timeout, or a fetch failure swallowed by the searcher).
    The site pool is ``SEARCH_SUPPORT_SITES`` (57 sites).

``http``
    Legacy URL probe: fetches every URL from ``scripts/data/site_health_config.json``
    with aiohttp, curl_cffi and httpx under one global concurrency limit and
    writes a standalone HTML report (suppressed by ``--json``).

Usage examples::

    # Full search health check, Markdown report, 4 concurrent sites
    python scripts/check_sites_health.py --report dist/site_health.md

    # Machine-readable output for one site
    python scripts/check_sites_health.py --only qidian --json

    # Random sample of 10 sites with a custom keyword
    python scripts/check_sites_health.py --sample 10 --keyword 剑

    # Legacy URL probing
    python scripts/check_sites_health.py --mode http --report dist/http.md

Exit codes
----------
0   no ``ERROR`` result (all PASS, or a mix of PASS and EMPTY)
1   at least one ``ERROR`` result
2   invalid arguments or configuration
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import html
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import aiohttp
import httpx
from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES
from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.plugins.registry import registrar

# ----------------------------
# Configuration
# ----------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "data" / "site_health_config.json"
OUTPUT_PATH = Path("dev/site_health_report.html")

DEFAULT_KEYWORD = "魔法"
DEFAULT_SEARCH_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 4
DEFAULT_LIMIT = 1

STATUS_PASS = "PASS"
STATUS_EMPTY = "EMPTY"
STATUS_ERROR = "ERROR"

HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Site Health Report</title>

<!-- Optional Highlight.js -->
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>

<script>
document.addEventListener('DOMContentLoaded', function() {
  if (typeof hljs !== 'undefined') {
    document.querySelectorAll('#failed-responses pre code').forEach(block => {
      hljs.highlightElement(block);
    });
  }
});
</script>

<style>
body {{
    font-family: monospace;
    background: #1e1e1e;
    color: #e0e0e0;
    padding: 1em 2em;
}}
pre {{
    background: #2a2a2a;
    color: #ccc;
    padding: 0.75em;
    white-space: pre-wrap;
    border-radius: 4px;
    overflow-x: auto;
}}
a {{
    color: #4ea1ff;
}}
h1 {{
    color: #ffd166;
    margin-bottom: 0.2em;
}}
h2 {{
    color: #06d6a0;
    margin-top: 1.5em;
    border-bottom: 1px solid #333;
    padding-bottom: 0.3em;
}}
h3 {{
    color: #ef476f;
    margin-top: 1em;
}}
hr {{
    border: 0;
    border-top: 1px solid #333;
    margin: 2em 0;
}}
</style>
</head>
<body>
<h1>Site Health Summary ({timestamp})</h1>
{summary_html}
<hr>
<h2>Failed Responses</h2>
<div id="failed-responses">
{failed_html}
</div>
</body>
</html>
"""

console = Console()

# ----------------------------
# Data structures
# ----------------------------


@dataclass
class SiteResult:
    """One URL fetched by one HTTP library (legacy http mode)."""

    site: str
    lib: str
    url: str
    status: int
    text: str
    elapsed: float


@dataclass
class SearchHealthResult:
    """Result of exercising one site's search plugin."""

    site: str
    searcher: str
    status: str
    elapsed: float
    result_count: int = 0
    reason: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "searcher": self.searcher,
            "status": self.status,
            "elapsed": round(self.elapsed, 3),
            "result_count": self.result_count,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass
class HttpHealthResult:
    """Flattened result of a legacy HTTP probe."""

    site: str
    lib: str
    url: str
    status_code: int
    status: str
    elapsed: float
    reason: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "lib": self.lib,
            "url": self.url,
            "status_code": self.status_code,
            "status": self.status,
            "elapsed": round(self.elapsed, 3),
            "reason": self.reason,
            "error": self.error,
        }


# ----------------------------
# Utilities
# ----------------------------


def load_config(cfg_path: Path) -> dict[str, Any] | None:
    if not cfg_path.is_file():
        return None
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


def _normalize_site_key(key: str) -> str:
    """Mirror ``PluginRegistry._normalize_key`` for CLI input."""
    normalized = key.strip().lower()
    if normalized and normalized[0].isdigit():
        return f"n{normalized}"
    return normalized


def parse_site_keys(raw: list[str] | None) -> list[str]:
    """Expand repeatable/comma-separated ``--only`` values, preserving order."""
    keys: list[str] = []
    for item in raw or []:
        for part in item.split(","):
            key = _normalize_site_key(part)
            if key and key not in keys:
                keys.append(key)
    return keys


def select_sites(
    pool: list[str],
    only: list[str],
    sample: int | None,
) -> list[str]:
    """Filter/sample the site pool, validating user input."""
    available = set(pool)
    if only:
        unknown = [key for key in only if key not in available]
        if unknown:
            raise ValueError(
                "Unknown site key(s): "
                + ", ".join(unknown)
                + ". Use --list-sites to see valid keys."
            )
        selected = list(only)
    else:
        selected = sorted(available)

    if sample is not None:
        if sample < 1:
            raise ValueError("--sample must be >= 1")
        if sample < len(selected):
            selected = sorted(random.sample(selected, sample))

    return selected


def _short_error(text: object, limit: int = 300) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _md_cell(text: object, limit: int = 160) -> str:
    """Escape a value for a Markdown table cell."""
    value = _short_error(text, limit)
    return value.replace("|", "\\|")


def _status_counts(results: list[SearchHealthResult]) -> dict[str, int]:
    counts = {STATUS_PASS: 0, STATUS_EMPTY: 0, STATUS_ERROR: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def _group_reasons(results: list[SearchHealthResult]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for result in results:
        if result.status == STATUS_PASS:
            continue
        reason = result.reason or (
            "no_results" if result.status == STATUS_EMPTY else "unknown"
        )
        grouped.setdefault(reason, []).append(result.site)
    return grouped


def _classify_exception(exc: BaseException) -> str:
    """Map an exception to a stable, human-readable reason key."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"http_{exc.status}"
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return "tls_error"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return "connection_error"
    if isinstance(exc, aiohttp.ClientPayloadError):
        return "payload_error"
    if isinstance(exc, aiohttp.ClientError):
        return f"client_error:{type(exc).__name__}"
    if isinstance(exc, UnicodeDecodeError):
        return "decode_error"
    if isinstance(exc, (ValueError, KeyError, TypeError, IndexError)):
        return f"parse_error:{type(exc).__name__}"
    return type(exc).__name__


def _classify_http_failure(status_code: int, text: str) -> str:
    if status_code >= 400:
        return f"http_{status_code}"
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return "tls_error"
    if (
        "name or service not known" in lowered
        or "getaddrinfo" in lowered
        or "nodename nor servname" in lowered
        or "dns" in lowered
    ):
        return "dns_error"
    if "connect" in lowered:
        return "connection_error"
    if "reset by peer" in lowered:
        return "connection_reset"
    return "network_error"


# ----------------------------
# Searcher error capture
# ----------------------------
#
# Several searchers swallow fetch failures and simply return an empty list,
# which would make a DNS/TLS/HTTP error look like a healthy "no results" site.
# We collect ERROR-level records emitted by the ``plugins`` loggers while a
# site is being checked (attributed via a ContextVar), and promote a result to
# ERROR when nothing was returned but the searcher logged a failure.

_LOG_SINK: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "site_health_log_sink", default=None
)


class _SearcherErrorCollector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        if not record.name.startswith("novel_downloader.plugins"):
            return
        sink = _LOG_SINK.get()
        if sink is not None:
            sink.append(_short_error(record.getMessage(), 400))


def _install_error_collector() -> _SearcherErrorCollector:
    handler = _SearcherErrorCollector(level=logging.ERROR)
    logging.getLogger().addHandler(handler)
    return handler


# ----------------------------
# Search mode
# ----------------------------


async def check_search_site(
    session: aiohttp.ClientSession,
    site: str,
    keyword: str,
    limit: int,
    timeout: float,
) -> SearchHealthResult:
    sink: list[str] = []
    token = _LOG_SINK.set(sink)
    start = perf_counter()
    searcher_name = ""

    try:
        searcher_cls = registrar.get_searcher_class(site)
        searcher_name = searcher_cls.__name__
        searcher = searcher_cls(session)
        # Leave ``explicitly_selected`` False so slow fallbacks (e.g. the
        # browser-assisted Fanqie search) are skipped during a health check.
        results = await asyncio.wait_for(
            searcher.search(keyword, limit=limit), timeout=timeout
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return SearchHealthResult(
            site=site,
            searcher=searcher_name,
            status=STATUS_ERROR,
            elapsed=perf_counter() - start,
            reason="timeout",
            error=f"timed out after {timeout:g}s",
        )
    except Exception as exc:
        return SearchHealthResult(
            site=site,
            searcher=searcher_name,
            status=STATUS_ERROR,
            elapsed=perf_counter() - start,
            reason=_classify_exception(exc),
            error=_short_error(f"{type(exc).__name__}: {exc}"),
        )
    finally:
        _LOG_SINK.reset(token)

    elapsed = perf_counter() - start
    result_count = len(results) if results else 0
    if result_count:
        return SearchHealthResult(
            site=site,
            searcher=searcher_name,
            status=STATUS_PASS,
            elapsed=elapsed,
            result_count=result_count,
        )
    if sink:
        return SearchHealthResult(
            site=site,
            searcher=searcher_name,
            status=STATUS_ERROR,
            elapsed=elapsed,
            reason="fetch_failed",
            error=_short_error(sink[-1]),
        )
    return SearchHealthResult(
        site=site,
        searcher=searcher_name,
        status=STATUS_EMPTY,
        elapsed=elapsed,
        reason="no_results",
    )


async def run_search_mode(args: argparse.Namespace, sites: list[str]) -> int:
    timeout = args.timeout if args.timeout is not None else DEFAULT_SEARCH_TIMEOUT
    concurrency = args.concurrency
    semaphore = asyncio.Semaphore(concurrency)
    results: list[SearchHealthResult] = []
    started = perf_counter()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        disable=args.no_progress,
    )

    with progress:
        task_id = progress.add_task(
            f"[cyan]search[/cyan] ({concurrency} concurrent)", total=len(sites)
        )

        async def worker(site: str) -> None:
            async with semaphore:
                result = await check_search_site(
                    session, site, args.keyword, args.limit, timeout
                )
            results.append(result)
            progress.advance(task_id)

        async with aiohttp.ClientSession(
            headers=DEFAULT_USER_HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            await asyncio.gather(*(worker(site) for site in sites))

    elapsed = perf_counter() - started
    results.sort(key=lambda item: item.site)
    counts = _status_counts(results)

    generated_at = datetime.now().astimezone()
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "mode": "search",
        "keyword": args.keyword,
        "limit": args.limit,
        "timeout": timeout,
        "concurrency": concurrency,
        "elapsed": round(elapsed, 3),
        "summary": {
            "total": len(results),
            "pass": counts[STATUS_PASS],
            "empty": counts[STATUS_EMPTY],
            "error": counts[STATUS_ERROR],
            "reasons": {
                reason: len(items)
                for reason, items in sorted(
                    _group_reasons(results).items(),
                    key=lambda kv: (-len(kv[1]), kv[0]),
                )
            },
        },
        "results": [result.as_dict() for result in results],
    }

    if args.report:
        write_search_report(
            args.report,
            results,
            keyword=args.keyword,
            limit=args.limit,
            timeout=timeout,
            concurrency=concurrency,
            elapsed=elapsed,
            generated_at=generated_at,
        )
        console.print(f"[green]Markdown report saved to:[/green] {args.report}")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_search_summary(results, elapsed)

    return 1 if counts[STATUS_ERROR] else 0


def print_search_summary(results: list[SearchHealthResult], elapsed: float) -> None:
    counts = _status_counts(results)
    table = Table(title="Search Health Summary", show_header=True)
    table.add_column("Status", justify="center")
    table.add_column("Count", justify="right")
    for status in (STATUS_PASS, STATUS_EMPTY, STATUS_ERROR):
        color = {
            STATUS_PASS: "green",
            STATUS_EMPTY: "yellow",
            STATUS_ERROR: "red",
        }[status]
        table.add_row(Text(status, style=color), str(counts[status]))
    table.add_row("Total time", f"{elapsed:.2f}s")
    console.print(table)

    reasons = _group_reasons(results)
    if reasons:
        reason_table = Table(title="Non-PASS Reasons", show_header=True)
        reason_table.add_column("Reason")
        reason_table.add_column("Count", justify="right")
        reason_table.add_column("Sites")
        for reason, sites in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            reason_table.add_row(reason, str(len(sites)), ", ".join(sites))
        console.print(reason_table)

    failed = [r for r in results if r.status != STATUS_PASS]
    if failed:
        detail = Table(title="Non-PASS Sites", show_header=True)
        detail.add_column("Site")
        detail.add_column("Status")
        detail.add_column("Time", justify="right")
        detail.add_column("Error")
        for result in failed:
            detail.add_row(
                result.site,
                Text(
                    result.status,
                    style="red" if result.status == STATUS_ERROR else "yellow",
                ),
                f"{result.elapsed:.2f}s",
                result.error,
            )
        console.print(detail)


def write_search_report(
    path: Path,
    results: list[SearchHealthResult],
    *,
    keyword: str,
    limit: int,
    timeout: float,
    concurrency: int,
    elapsed: float,
    generated_at: datetime,
) -> None:
    counts = _status_counts(results)
    error_results = [r for r in results if r.status == STATUS_ERROR]
    empty_results = [r for r in results if r.status == STATUS_EMPTY]
    error_reasons = _group_reasons(error_results)
    ranked_reasons = sorted(error_reasons.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    lines: list[str] = [
        "# Site Search Health Report",
        "",
        f"- Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "- Mode: `search`",
        f"- Keyword: `{keyword}`",
        f"- Per-site limit: {limit}",
        f"- Timeout: {timeout:g}s",
        f"- Concurrency: {concurrency}",
        f"- Sites checked: {len(results)}",
        f"- Total time: {elapsed:.2f}s",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "| --- | ---: |",
        f"| PASS | {counts[STATUS_PASS]} |",
        f"| EMPTY | {counts[STATUS_EMPTY]} |",
        f"| ERROR | {counts[STATUS_ERROR]} |",
        "",
    ]

    if counts[STATUS_ERROR]:
        verdict = f"**FAIL** — {counts[STATUS_ERROR]} site(s) returned ERROR"
    else:
        verdict = "**OK** — no ERROR results"
    lines.extend([f"Result: {verdict}", ""])

    if ranked_reasons:
        top = ", ".join(
            f"`{reason}` ({len(sites)})" for reason, sites in ranked_reasons[:3]
        )
        lines.extend([f"Main failure reasons: {top}", ""])

    lines.extend(
        [
            "## Failure Reason Distribution",
            "",
        ]
    )
    if ranked_reasons:
        lines.extend(
            [
                "| Reason | Count | Sites |",
                "| --- | ---: | --- |",
            ]
        )
        for reason, sites in ranked_reasons:
            site_list = _md_cell(", ".join(sites), 300)
            lines.append(f"| {_md_cell(reason)} | {len(sites)} | {site_list} |")
    else:
        lines.append("No failures.")
    lines.append("")

    if empty_results:
        empty_sites = ", ".join(result.site for result in empty_results)
        lines.extend([f"EMPTY (no results): {empty_sites}", ""])

    lines.extend(
        [
            "## Site Results",
            "",
            "| Site | Searcher | Status | Results | Elapsed (s) | Error |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    severity = {STATUS_ERROR: 0, STATUS_EMPTY: 1, STATUS_PASS: 2}
    ordered = sorted(results, key=lambda item: (severity[item.status], item.site))
    for result in ordered:
        lines.append(
            f"| {_md_cell(result.site)} "
            f"| {_md_cell(result.searcher)} "
            f"| {result.status} "
            f"| {result.result_count} "
            f"| {result.elapsed:.2f} "
            f"| {_md_cell(result.error)} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------
# Legacy HTTP mode
# ----------------------------


def save_html_summary(
    grouped: dict[str, dict[str, list[SiteResult]]],
    slow_t: float,
    very_slow_t: float,
    output_path: Path = OUTPUT_PATH,
) -> None:
    """Render the site check summary as a standalone HTML report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = Console(record=True, width=140)
    recorder.print("\n[bold cyan]=== Site Health Summary ===[/bold cyan]\n")
    failed_entries: list[tuple[str, str, str, str]] = []  # (lib, site, url, text)

    for lib_name, sites in grouped.items():
        table = Table(
            title=f"[cyan]{lib_name}[/cyan]",
            show_header=True,
            header_style="bold magenta",
            expand=True,
        )
        table.add_column("Status", justify="center", no_wrap=True)
        table.add_column("Time", justify="right", no_wrap=True)
        table.add_column("Site", justify="left", no_wrap=True)
        table.add_column("URL", justify="left")

        for res_list in sites.values():
            for result in res_list:
                if result.status <= 0 or result.status >= 400:
                    status_text = Text(
                        "ERR" if result.status <= 0 else str(result.status),
                        style="red",
                    )
                elif result.status < 300:
                    status_text = Text(str(result.status), style="green")
                else:
                    status_text = Text(str(result.status), style="yellow")

                if result.elapsed > very_slow_t:
                    time_style = "red"
                elif result.elapsed > slow_t:
                    time_style = "yellow"
                else:
                    time_style = "green"
                time_text = Text(f"{result.elapsed:.2f}s", style=time_style)

                table.add_row(status_text, time_text, result.site, result.url)

                if result.status >= 400 or result.status == 0:
                    failed_entries.append(
                        (lib_name, result.site, result.url, result.text)
                    )

        recorder.print(table)

    summary_html = recorder.export_html(inline_styles=True)

    failed_parts: list[str] = []
    for lib, site, url, text in failed_entries:
        failed_parts.append(
            f"<h3>{lib} :: {site}</h3>"
            f"<p><a href='{url}' target='_blank'>{url}</a></p>"
            f"<pre><code class='language-html'>{html.escape(text or '')}</code></pre>"
        )

    html_output = HTML_TEMPLATE.format(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        summary_html=summary_html,
        failed_html="\n".join(failed_parts),
    )

    output_path.write_text(html_output, encoding="utf-8")
    print(f"[HTML] Report saved to: {output_path.resolve()}")


async def fetch_aiohttp(site: str, urls: list[str], timeout: float) -> list[SiteResult]:
    results = []
    async with aiohttp.ClientSession(
        headers=DEFAULT_USER_HEADERS,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as sess:
        for url in urls:
            start = perf_counter()
            try:
                async with sess.get(url) as resp:
                    text = await resp.text(errors="ignore")
                    results.append(
                        SiteResult(
                            site,
                            "aiohttp",
                            url,
                            resp.status,
                            text,
                            perf_counter() - start,
                        )
                    )
            except Exception as e:
                results.append(
                    SiteResult(site, "aiohttp", url, 0, str(e), perf_counter() - start)
                )
    return results


async def fetch_httpx(site: str, urls: list[str], timeout: float) -> list[SiteResult]:
    results = []
    async with httpx.AsyncClient(
        headers=DEFAULT_USER_HEADERS,
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        for url in urls:
            start = perf_counter()
            try:
                resp = await client.get(url)
                results.append(
                    SiteResult(
                        site,
                        "httpx",
                        url,
                        resp.status_code,
                        resp.text,
                        perf_counter() - start,
                    )
                )
            except Exception as e:
                results.append(
                    SiteResult(site, "httpx", url, 0, str(e), perf_counter() - start)
                )
    return results


async def fetch_curl_cffi(
    site: str, urls: list[str], timeout: float
) -> list[SiteResult]:
    results = []
    async with AsyncSession(
        headers=DEFAULT_USER_HEADERS, timeout=timeout, impersonate="chrome136"
    ) as sess:
        for url in urls:
            start = perf_counter()
            try:
                resp = await sess.get(url)
                results.append(
                    SiteResult(
                        site,
                        "curl_cffi",
                        url,
                        resp.status_code,
                        resp.text,
                        perf_counter() - start,
                    )
                )
            except Exception as e:
                results.append(
                    SiteResult(
                        site, "curl_cffi", url, 0, str(e), perf_counter() - start
                    )
                )
    return results


async def run_check_for_lib(
    lib_name: str,
    sites: dict[str, dict[str, Any]],
    timeout: float,
    semaphore: asyncio.Semaphore,
    site_locks: dict[str, asyncio.Lock],
    progress: Progress,
    task_id: TaskID,
) -> dict[str, list[SiteResult]]:
    results: dict[str, list[SiteResult]] = {}

    async def run_site(site_key: str, urls: list[str]) -> None:
        lock = site_locks[site_key]
        async with semaphore, lock:
            try:
                if lib_name == "aiohttp":
                    site_results = await fetch_aiohttp(site_key, urls, timeout)
                elif lib_name == "httpx":
                    site_results = await fetch_httpx(site_key, urls, timeout)
                else:
                    site_results = await fetch_curl_cffi(site_key, urls, timeout)
            except Exception as e:  # defensive: session setup can fail
                site_results = [
                    SiteResult(site_key, lib_name, url, 0, str(e), 0.0) for url in urls
                ]
            results[site_key] = site_results
            progress.advance(task_id)

    await asyncio.gather(*(run_site(k, v["urls"]) for k, v in sites.items()))
    return results


async def run_http_mode(args: argparse.Namespace, sites: list[str]) -> int:
    config = load_config(args.config)
    if config is None:
        console.print(f"[red]Config file not found: {args.config}[/red]")
        return 2

    all_sites: dict[str, Any] = config["sites"]
    selected_config = {key: all_sites[key] for key in sites}
    timeout = float(
        args.timeout if args.timeout is not None else config.get("timeout", 20)
    )
    concurrency = args.concurrency
    semaphore = asyncio.Semaphore(concurrency)
    site_locks = {key: asyncio.Lock() for key in selected_config}
    slow_t = config.get("speed_levels", {}).get("slow", 2.0)
    very_slow_t = config.get("speed_levels", {}).get("very_slow", 5.0)
    grouped: dict[str, dict[str, list[SiteResult]]] = {}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        disable=args.no_progress,
    )
    with progress:
        tasks = {}
        for lib in ("aiohttp", "curl_cffi", "httpx"):
            task_id = progress.add_task(
                f"[cyan]{lib}[/cyan]", total=len(selected_config)
            )
            coro = run_check_for_lib(
                lib,
                selected_config,
                timeout,
                semaphore,
                site_locks,
                progress,
                task_id,
            )
            tasks[lib] = asyncio.create_task(coro)

        for lib, task in tasks.items():
            grouped[lib] = await task

    if not args.json:
        save_html_summary(grouped, slow_t, very_slow_t, args.html)

    flat: list[HttpHealthResult] = []
    for lib, site_map in grouped.items():
        for site, site_results in site_map.items():
            for result in site_results:
                ok = 0 < result.status < 400
                flat.append(
                    HttpHealthResult(
                        site=site,
                        lib=lib,
                        url=result.url,
                        status_code=result.status,
                        status=STATUS_PASS if ok else STATUS_ERROR,
                        elapsed=result.elapsed,
                        reason=(
                            ""
                            if ok
                            else _classify_http_failure(result.status, result.text)
                        ),
                        error="" if ok else _short_error(result.text),
                    )
                )
    flat.sort(key=lambda item: (item.site, item.lib, item.url))

    counts = {STATUS_PASS: 0, STATUS_ERROR: 0}
    for item in flat:
        counts[item.status] = counts.get(item.status, 0) + 1

    if args.report:
        _write_http_report(
            args.report,
            flat,
            timeout=timeout,
            concurrency=concurrency,
        )
        console.print(f"[green]Markdown report saved to:[/green] {args.report}")

    if args.json:
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "http",
            "config": str(args.config),
            "timeout": timeout,
            "concurrency": concurrency,
            "summary": {
                "total": len(flat),
                "pass": counts[STATUS_PASS],
                "error": counts[STATUS_ERROR],
            },
            "results": [item.as_dict() for item in flat],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 1 if counts[STATUS_ERROR] else 0


def _write_http_report(
    path: Path,
    results: list[HttpHealthResult],
    *,
    timeout: float,
    concurrency: int,
) -> None:
    errors = [item for item in results if item.status == STATUS_ERROR]
    reasons: dict[str, list[str]] = {}
    for item in errors:
        reasons.setdefault(item.reason or "unknown", []).append(
            f"{item.site}/{item.lib}"
        )

    lines = [
        "# Site HTTP Health Report",
        "",
        f"- Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        "- Mode: `http`",
        f"- Timeout: {timeout:g}s",
        f"- Concurrency: {concurrency}",
        f"- Requests: {len(results)}",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "| --- | ---: |",
        f"| PASS | {len(results) - len(errors)} |",
        f"| ERROR | {len(errors)} |",
        "",
    ]
    if reasons:
        lines.extend(
            [
                "## Failure Reason Distribution",
                "",
                "| Reason | Count | Entries |",
                "| --- | ---: | --- |",
            ]
        )
        for reason, entries in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            lines.append(
                f"| {_md_cell(reason)} | {len(entries)} "
                f"| {_md_cell(', '.join(entries), 300)} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Results",
            "",
            "| Site | Library | URL | HTTP | Status | Elapsed (s) | Error |",
            "| --- | --- | --- | ---: | --- | ---: | --- |",
        ]
    )
    for item in results:
        lines.append(
            f"| {_md_cell(item.site)} | {_md_cell(item.lib)} "
            f"| {_md_cell(item.url, 300)} | {item.status_code} "
            f"| {item.status} | {item.elapsed:.2f} | {_md_cell(item.error)} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------
# Entry point
# ----------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Health check for novel site searchers (default) or URLs.",
        epilog=(
            "Exit codes: 0 = no ERROR, 1 = at least one ERROR, "
            "2 = invalid arguments/config."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("search", "http"),
        default="search",
        help="search: exercise site searchers (default); http: probe configured URLs",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="KEY",
        help="Only check these site keys (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="Randomly sample N sites from the selected pool",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Maximum concurrent site checks (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-site timeout in seconds "
            f"(default: {DEFAULT_SEARCH_TIMEOUT:g} for search, config value for http)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON to stdout (progress goes to stderr)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        metavar="PATH",
        help="Write a Markdown report to PATH",
    )
    parser.add_argument(
        "--keyword",
        default=DEFAULT_KEYWORD,
        help=f"Search keyword for search mode (default: {DEFAULT_KEYWORD})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum results requested per site (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Path to the HTTP mode JSON config",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=OUTPUT_PATH,
        help="HTML report path for http mode",
    )
    parser.add_argument(
        "--list-sites",
        action="store_true",
        help="List the available site keys and exit",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress bar",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.mode == "search":
        pool = sorted(SEARCH_SUPPORT_SITES)
    else:
        config = load_config(args.config)
        if config is None:
            console.print(f"[red]Config file not found: {args.config}[/red]")
            return 2
        pool = sorted(config["sites"])

    if args.list_sites:
        print("\n".join(pool))
        return 0

    only = parse_site_keys(args.only)
    try:
        sites = select_sites(pool, only, args.sample)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    if not sites:
        console.print("[red]No sites selected.[/red]")
        return 2

    if args.mode == "search":
        return await run_search_mode(args, sites)
    return await run_http_mode(args, sites)


def main() -> None:
    global console
    parser = build_parser()
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.json:
        console = Console(stderr=True)

    collector = _install_error_collector()
    try:
        exit_code = asyncio.run(async_main(args))
    finally:
        logging.getLogger().removeHandler(collector)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
