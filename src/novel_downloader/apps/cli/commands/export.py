#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.export
-----------------------------------------

"""

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

from novel_downloader.apps.cli import prompts, ui
from novel_downloader.apps.constants import DOWNLOAD_SUPPORT_SITES
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar

from ..ui_adapters import CLIExportUI
from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, parse_book_args


class ExportCmd(Command):
    name = "export"
    help = t("Export previously downloaded novels.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "book_ids",
            nargs="*",
            help=t("Book ID(s) to export (optional; choose interactively if omitted)"),
        )
        parser.add_argument(
            "--format",
            nargs="+",
            help=t("Output format(s): txt, epub, html. Default: from config."),
        )
        parser.add_argument(
            "--site",
            help=t("Source site key (optional; choose interactively if omitted)"),
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
            "--stage",
            type=str,
            help=t("Export stage (e.g. raw, cleaner). Defaults to last stage."),
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
        site: str | None = args.site
        stage: str | None = args.stage
        book_ids: list[str] = list(args.book_ids or [])
        config_path: Path | None = Path(args.config) if args.config else None
        formats: list[str] | None = args.format

        config_data = load_or_init_config(config_path)
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)

        raw_cfg = config_data.get("general") or {}
        raw_dir = Path(raw_cfg.get("raw_data_dir", "./raw_data"))

        # site selection
        if not site:
            book_ids = []  # ignore passed-in ids when site is not specified
            if as_json or not sys.stdin.isatty():
                # --json must never enter interactive selection (documented
                # contract); non-TTY stdin could not answer it either way.
                fail(
                    t(
                        "Interactive selection requires a terminal; "
                        "pass the required arguments explicitly."
                    ),
                    EXIT_USAGE,
                )
            site = prompts.select_site(raw_dir)
            if site is None:
                # A missing raw-data directory is a failure; an explicit
                # cancel (empty selection) is not.
                if not raw_dir.exists():
                    raise SystemExit(EXIT_FAILURE)
                ui.warn(t("No site selected."))
                return

        ui.info(
            t("Using site: {site}").format(
                site=DOWNLOAD_SUPPORT_SITES.get(site, site),
            )
        )

        # book selection
        if not book_ids:
            if as_json or not sys.stdin.isatty():
                fail(
                    t(
                        "Interactive selection requires a terminal; "
                        "pass the required arguments explicitly."
                    ),
                    EXIT_USAGE,
                )
            selected = prompts.select_books(raw_dir, site)
            if not selected:
                if not (raw_dir / site).exists():
                    raise SystemExit(EXIT_FAILURE)
                ui.warn(t("No books selected."))
                return
            book_ids = selected

        adapter = ConfigAdapter(config=config_data)
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
        books = parse_book_args(book_ids, args.start, args.end)

        client = registrar.get_client(site, adapter.get_client_config(site))

        export_ui = CLIExportUI()

        exported: dict[str, dict[str, list[str]]] = {}
        export_failed = False
        for book in books:
            results = client.export_book(
                book,
                cfg=adapter.get_exporter_config(site),
                formats=formats,
                stage=stage,
                ui=export_ui,
            )
            paths_by_fmt = {
                fmt: [str(path) for path in paths] for fmt, paths in results.items()
            }
            exported[book.book_id] = paths_by_fmt
            if not any(paths_by_fmt.values()):
                export_failed = True

        if as_json:
            ui.emit(
                json.dumps(
                    {
                        "site": site,
                        "books": [
                            {
                                "book_id": book.book_id,
                                "exported": exported.get(book.book_id, {}),
                            }
                            for book in books
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

        if export_failed:
            raise SystemExit(EXIT_FAILURE)
