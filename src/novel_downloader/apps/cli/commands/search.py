#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.search
-----------------------------------------

"""

from __future__ import annotations

import json
from argparse import ArgumentParser, BooleanOptionalAction, Namespace
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from novel_downloader.apps.cli import prompts, ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar
from novel_downloader.plugins.ranking import SORT_KEYS
from novel_downloader.schemas import BookConfig

from ..ui_adapters import (
    CLIDownloadUI,
    CLIExportUI,
    CLILoginUI,
    CLIProcessUI,
)
from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail

if TYPE_CHECKING:
    from novel_downloader.plugins.search import SearchEvent


class SearchCmd(Command):
    name = "search"
    help = t("Search for books across one or more sites.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--site",
            "-s",
            action="append",
            metavar="SITE",
            help=t("Restrict search to specific site key(s). Default: all sites."),
        )
        parser.add_argument("keyword", help=t("Search keyword"))
        parser.add_argument(
            "--config", type=str, help=t("Path to the configuration file")
        )
        parser.add_argument(
            "--limit",
            "-l",
            type=int,
            default=None,
            metavar="N",
            help=t("Maximum number of total results (default: no limit)"),
        )
        parser.add_argument(
            "--site-limit",
            type=int,
            default=10,
            metavar="M",
            help=t("Maximum number of results per site (default: 10)"),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=5.0,
            metavar="SECS",
            help=t("Request timeout in seconds (default: 5.0)"),
        )
        parser.add_argument(
            "--format",
            nargs="+",
            help=t("Output format(s): txt, epub, html. Default: from config."),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print every search result as JSON on stdout and exit."),
        )
        parser.add_argument(
            "--first",
            action="store_true",
            help=t("Download the top result without asking (non-interactive)."),
        )
        parser.add_argument(
            "--sort",
            choices=list(SORT_KEYS),
            default="quality",
            help=t("Result ordering (default: quality)."),
        )
        parser.add_argument(
            "--group",
            action=BooleanOptionalAction,
            default=False,
            # Plain literal on purpose: wrapping it in t() requires a zh_CN
            # entry in messages.po, which is being re-synced by a concurrent
            # change -- localize this msgid when the catalog is regenerated.
            help=(
                "Merge same-book results across sites: one row per book, the "
                "site column lists every source (* marks the best source). "
                "Default: no grouping."
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        sites: Sequence[str] | None = args.site or None
        keyword: str = args.keyword
        formats: list[str] | None = args.format
        as_json: bool = bool(args.as_json)
        interactive = not as_json

        if as_json and args.first:
            fail(t("--json and --first cannot be combined."), EXIT_USAGE)

        overall_limit = None if args.limit is None else max(1, args.limit)
        per_site_limit = max(1, args.site_limit)
        timeout = max(0.1, float(args.timeout))
        result_sort: str = args.sort
        group_results_flag: bool = bool(args.group)
        config_path: Path | None = Path(args.config) if args.config else None

        config_data = load_or_init_config(config_path)
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)
        adapter = ConfigAdapter(config=config_data)
        ui.setup_logging(
            log_dir=adapter.get_log_dir(),
            console_level=adapter.get_log_level(),
            console=interactive,
        )

        async def _run() -> None:
            from novel_downloader.plugins.search import search

            # ``search()`` is the aggregate (non-streaming) call, so its
            # ``completed``/``failed`` events arrive in one burst when every
            # site has finished; the browser-assistance events arrive earlier,
            # while the searchers are prepared.  Either way each event is a
            # per-site status line on stderr, keeping stdout machine readable.
            events: list[SearchEvent] = []

            def _on_event(event: SearchEvent) -> None:
                """Record one per-site search event and echo it on stderr."""
                events.append(event)
                if as_json:
                    # Keep stdout parseable: no per-site lines in JSON mode.
                    return
                if event.kind == "completed":
                    count = 0 if event.result_count is None else event.result_count
                    ui.success(
                        f"✓ {event.site} · "
                        + t("{count} result(s)").format(count=count)
                    )
                elif event.kind == "failed":
                    ui.warn(
                        f"✗ {event.site} · "
                        + t("Search failed: {err}").format(err=event.message)
                    )
                elif event.kind == "browser_assisted":
                    ui.info(f"↷ {event.site} · " + t("browser-assisted"))
                elif event.kind == "browser_skipped":
                    ui.info(f"↷ {event.site} · " + t("skipped (browser-assisted)"))

            def _print_search_summary() -> None:
                """Print the per-site outcome tally (stderr in every mode)."""
                done = sum(1 for e in events if e.kind == "completed")
                failed = sum(1 for e in events if e.kind == "failed")
                skipped = sum(1 for e in events if e.kind == "browser_skipped")
                summary = t(
                    "{done} site(s) completed / {failed} failed / {skipped} skipped"
                )
                ui.info(summary.format(done=done, failed=failed, skipped=skipped))

            if as_json:
                results = await search(
                    keyword=keyword,
                    sites=sites,
                    limit=overall_limit,
                    per_site_limit=per_site_limit,
                    timeout=timeout,
                    sort=result_sort,
                    on_event=_on_event,
                    group=group_results_flag,
                )
                _print_search_summary()
                ui.emit(json.dumps(list(results), ensure_ascii=False, indent=2))
                return

            with ui.status(t("Searching for '{keyword}'...").format(keyword=keyword)):
                results = await search(
                    keyword=keyword,
                    sites=sites,
                    limit=overall_limit,
                    per_site_limit=per_site_limit,
                    timeout=timeout,
                    sort=result_sort,
                    on_event=_on_event,
                    group=group_results_flag,
                )
            _print_search_summary()

            if not results:
                ui.warn(t("No results found for '{keyword}'.").format(keyword=keyword))
                return

            ui.info(
                t("Found {count} result(s) for '{keyword}'.").format(
                    count=len(results), keyword=keyword
                )
            )

            chosen = results[0] if args.first else prompts.select_search_result(results)
            if chosen is None:
                return

            site = chosen["site"]
            books: list[BookConfig] = [BookConfig(book_id=chosen["book_id"])]

            login_ui = CLILoginUI()
            download_ui = CLIDownloadUI()
            client = registrar.get_client(site, adapter.get_client_config(site))

            try:
                async with client:
                    if adapter.get_login_required(site):
                        succ = await client.login(
                            ui=login_ui,
                            login_cfg=adapter.get_login_config(site),
                        )
                        if not succ:
                            ui.error(
                                t(
                                    "Login failed: please check your cookies "
                                    "or run 'novel-cli login {site}' first."
                                ).format(site=site)
                            )
                            raise SystemExit(EXIT_FAILURE)

                    for book in books:
                        await client.download_book(book, ui=download_ui)
            except ValueError as e:
                ui.warn(
                    t("'{site}' is currently not supported: {err}").format(
                        site=site, err=e
                    )
                )
                raise SystemExit(EXIT_FAILURE) from e
            except Exception as e:
                ui.error(t("Site error ({site}): {err}").format(site=site, err=e))
                raise SystemExit(EXIT_FAILURE) from e

            if not download_ui.completed_books:
                raise SystemExit(EXIT_FAILURE)

            process_ui = CLIProcessUI()
            export_ui = CLIExportUI()

            for book in download_ui.completed_books:
                client.process_book(
                    book,
                    processors=adapter.get_processor_configs(site),
                    ui=process_ui,
                )
                client.export_book(
                    book,
                    cfg=adapter.get_exporter_config(site),
                    formats=formats or adapter.get_export_fmt(site),
                    ui=export_ui,
                )

        import asyncio

        asyncio.run(_run())
