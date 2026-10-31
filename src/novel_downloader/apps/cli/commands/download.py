#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.download
-------------------------------------------

"""

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path

from novel_downloader.apps.cli import ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar
from novel_downloader.schemas import BookConfig

from ..ui_adapters import (
    CLIDownloadUI,
    CLIExportUI,
    CLILoginUI,
    CLIProcessUI,
)
from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, parse_book_args


class DownloadCmd(Command):
    name = "download"
    help = t("Download novels by book ID or URL.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "book_ids", nargs="*", help=t("Book ID(s) or URL to download")
        )
        parser.add_argument(
            "--site",
            help=t("Source site key (auto-detected if omitted and URL is provided)"),
        )
        parser.add_argument(
            "--config", type=str, help=t("Path to the configuration file")
        )
        parser.add_argument(
            "--start",
            type=str,
            help=t("Start chapter ID (applies only to the first book)"),
        )
        parser.add_argument(
            "--end",
            type=str,
            help=t("End chapter ID (applies only to the first book)"),
        )
        parser.add_argument(
            "--no-export",
            action="store_true",
            help=t("Skip export step (download only)"),
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
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        config_path: Path | None = Path(args.config) if args.config else None
        site: str | None = args.site
        formats: list[str] | None = args.format

        # book_ids
        if site:  # SITE MODE
            books = parse_book_args(args.book_ids, args.start, args.end)
        else:  # URL MODE
            if not args.book_ids:
                fail(
                    t(
                        "No book ID or URL provided. Pass a book URL, "
                        "or use --site SITE [book_id ...]."
                    ),
                    EXIT_USAGE,
                )

            if len(args.book_ids) != 1:
                fail(
                    t(
                        "Expected exactly one URL argument when --site is omitted (got {n})."  # noqa: E501
                    ).format(n=len(args.book_ids)),
                    EXIT_USAGE,
                )

            from novel_downloader.infra.book_url_resolver import resolve_book_url

            ui.info(t("No --site provided; detecting site from URL..."))

            raw_url = args.book_ids[0]
            resolved = resolve_book_url(raw_url)
            if not resolved:
                fail(
                    t("Could not resolve site and book from URL: {url}").format(
                        url=raw_url
                    ),
                    EXIT_USAGE,
                )

            site = resolved["site_key"]
            book_id = resolved.get("book_id")

            if not book_id:
                fail(
                    t("The provided URL does not contain a valid book ID."),
                    EXIT_USAGE,
                )

            books = [
                BookConfig(
                    book_id=book_id,
                    start_id=args.start,
                    end_id=args.end,
                )
            ]
            ui.info(
                t("Resolved URL to site '{site}' with book ID '{book_id}'.").format(
                    site=site, book_id=book_id
                )
            )

        config_data = load_or_init_config(config_path)
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)

        ui.info(t("Using site: {site}").format(site=site))
        adapter = ConfigAdapter(config=config_data)

        if not books and args.site:
            try:
                books = adapter.get_book_ids(site)
            except Exception as e:
                fail(
                    t("Failed to read book IDs from configuration: {err}").format(
                        err=str(e)
                    )
                )

        if not books:
            # Documented exit-code contract: no URL/book ID supplied at all
            # is a usage error, not a runtime failure.
            fail(t("No book IDs provided. Exiting."), EXIT_USAGE)

        ui.setup_logging(
            log_dir=adapter.get_log_dir(),
            console_level=adapter.get_log_level(),
            console=not as_json,
        )

        plugins_cfg = adapter.get_plugins_config()
        if plugins_cfg.get("enable_local_plugins"):
            registrar.enable_local_plugins(
                plugins_cfg.get("local_plugins_path"),
                override=plugins_cfg.get("override_builtins", False),
            )

        formats = formats or adapter.get_export_fmt(site)

        # download
        import asyncio

        login_ui = CLILoginUI()
        download_ui = CLIDownloadUI()

        client = registrar.get_client(site, adapter.get_client_config(site))

        # Errors are collected instead of raised so a ``--json`` caller still
        # gets a machine-readable summary before the non-zero exit code.
        download_error: str | None = None
        unsupported_error: str | None = None

        async def download_books() -> None:
            nonlocal download_error, unsupported_error
            try:
                async with client:
                    if adapter.get_login_required(site):
                        succ = await client.login(
                            ui=login_ui,
                            login_cfg=adapter.get_login_config(site),
                        )
                        if not succ:
                            download_error = t("Login failed.")
                            return

                    for book in books:
                        await client.download_book(book, ui=download_ui)
            except ValueError as e:
                unsupported_error = t(
                    "'{site}' is currently not supported: {err}"
                ).format(site=site, err=e)
            except Exception as e:
                download_error = t("Site error ({site}): {err}").format(
                    site=site, err=e
                )

        asyncio.run(download_books())

        # export
        exported: dict[str, dict[str, list[str]]] = {}
        export_failed = False
        if not args.no_export and download_ui.completed_books:
            process_ui = CLIProcessUI()
            export_ui = CLIExportUI()

            for book in download_ui.completed_books:
                client.process_book(
                    book,
                    processors=adapter.get_processor_configs(site),
                    ui=process_ui,
                )
                results = client.export_book(
                    book,
                    cfg=adapter.get_exporter_config(site),
                    formats=formats,
                    ui=export_ui,
                )
                paths_by_fmt = {
                    fmt: [str(path) for path in paths] for fmt, paths in results.items()
                }
                exported[book.book_id] = paths_by_fmt
                if not any(paths_by_fmt.values()):
                    export_failed = True
        elif args.no_export:
            ui.info(t("Export skipped (--no-export)"))

        if as_json:
            ui.emit(
                json.dumps(
                    {
                        "site": site,
                        "books": [
                            {
                                "book_id": book.book_id,
                                "downloaded": book in download_ui.completed_books,
                                "exported": (
                                    None
                                    if args.no_export
                                    else exported.get(book.book_id, {})
                                ),
                            }
                            for book in books
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

        # Exit codes: usage errors were handled above; here anything that
        # prevented the requested work from completing is a runtime failure.
        if unsupported_error:
            ui.warn(unsupported_error)
            raise SystemExit(EXIT_FAILURE)
        if download_error:
            ui.error(download_error)
            raise SystemExit(EXIT_FAILURE)
        if not download_ui.completed_books:
            raise SystemExit(EXIT_FAILURE)
        if export_failed:
            raise SystemExit(EXIT_FAILURE)
