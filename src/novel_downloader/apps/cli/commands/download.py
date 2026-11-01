#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.download
-------------------------------------------

"""

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import cast

from novel_downloader.apps.cli import ui
from novel_downloader.apps.utils import load_or_init_config
from novel_downloader.infra.config import ConfigAdapter
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import registrar
from novel_downloader.plugins.mixins.download import IncompleteDownloadError
from novel_downloader.schemas import BookConfig
from novel_downloader.schemas.book import DownloadSummary

from ..ui_adapters import (
    CLIDownloadUI,
    CLIExportUI,
    CLILoginUI,
    CLIProcessUI,
)
from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, parse_book_args

#: Failure lines shown by the default (warning-only) report.
_MAX_LISTED_FAILURES = 5
#: Failure lines shown by ``--strict`` before pointing at the ledger.
_MAX_LISTED_FAILURES_STRICT = 20


def _collect_failures(raw: object, out: list[DownloadSummary]) -> None:
    """
    Append ``raw`` to ``out`` when it is a download summary with failures.

    Clients are typed through :class:`ClientProtocol`, whose ``download_book``
    is declared to return ``None``, and third-party clients may return nothing
    at all; the check is therefore structural rather than an ``isinstance`` on
    a :class:`~typing.TypedDict`.

    :param raw: Whatever ``client.download_book`` returned.
    :param out: Accumulator for summaries that report failures.
    """
    if not isinstance(raw, dict):
        return
    if not raw.get("failed"):
        return
    out.append(cast(DownloadSummary, raw))


def _report_incomplete(summaries: list[DownloadSummary], *, strict: bool) -> None:
    """
    Print the failure report for books that ended an incomplete download.

    ``--strict`` uses the error style and lists up to
    :data:`_MAX_LISTED_FAILURES_STRICT` chapters; the default only warns with
    the first :data:`_MAX_LISTED_FAILURES` chapters.  Both point at the failure
    ledger, which holds every record.

    :param summaries: Summaries with ``failed > 0``.
    :param strict: Whether the caller is running in strict mode.
    """
    limit = _MAX_LISTED_FAILURES_STRICT if strict else _MAX_LISTED_FAILURES
    report = ui.error if strict else ui.warn

    for summary in summaries:
        report(
            t(
                "Download incomplete for book {book_id}: "
                "{failed} of {total} chapter(s) failed."
            ).format(
                book_id=summary["book_id"],
                failed=summary["failed"],
                total=summary["total"],
            )
        )

        failures = summary["failures"]
        for failure in failures[:limit]:
            report(
                t("Failed: {name} ({reason})").format(
                    name=failure["chapter_title"] or failure["chapter_id"],
                    reason=failure["reason"],
                )
            )

        if len(failures) > limit:
            report(
                t("{count} more failure(s) not listed; see {path} for details.").format(
                    count=len(failures) - limit, path=summary["ledger_path"]
                )
            )
        else:
            report(t("Failure ledger: {path}").format(path=summary["ledger_path"]))


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
        parser.add_argument(
            "--strict",
            action="store_true",
            help=t(
                "Exit with a non-zero status if any chapter could not be downloaded."
            ),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        as_json = bool(getattr(args, "as_json", False))
        strict = bool(getattr(args, "strict", False))
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
        # Books whose download ended with missing chapters.  Populated either
        # from the summary (default mode) or from ``IncompleteDownloadError``
        # (``--strict``).
        incomplete: list[DownloadSummary] = []

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
                        # Typed as ``object``: older/third-party clients may
                        # still return ``None`` instead of a summary.
                        raw_summary: object
                        try:
                            # ``strict`` is only forwarded when requested, so
                            # clients without the parameter keep working.
                            if strict:
                                raw_summary = await client.download_book(
                                    book, ui=download_ui, strict=True
                                )
                            else:
                                raw_summary = await client.download_book(
                                    book, ui=download_ui
                                )
                        except IncompleteDownloadError as exc:
                            incomplete.append(exc.summary)
                            continue
                        _collect_failures(raw_summary, incomplete)
            except ValueError as e:
                unsupported_error = t(
                    "'{site}' is currently not supported: {err}"
                ).format(site=site, err=e)
            except Exception as e:
                download_error = t("Site error ({site}): {err}").format(
                    site=site, err=e
                )

        asyncio.run(download_books())

        # Report missing chapters before the (potentially slow) export so the
        # failure list is not buried under export output.
        if incomplete:
            _report_incomplete(incomplete, strict=strict)

        # In strict mode an incomplete book must not be exported: the caller
        # asked to fail rather than to produce a file with holes.
        failed_book_ids = {summary["book_id"] for summary in incomplete}

        # export
        exported: dict[str, dict[str, list[str]]] = {}
        export_failed = False
        if not args.no_export and download_ui.completed_books:
            process_ui = CLIProcessUI()
            export_ui = CLIExportUI()

            for book in download_ui.completed_books:
                if strict and book.book_id in failed_book_ids:
                    continue
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
            # ``downloaded`` means "the run finished", not "the book is whole".
            # Machine consumers need the gap size to tell the two apart, so
            # every book also reports how many chapters are missing.
            failed_by_book = {
                summary["book_id"]: int(summary["failed"]) for summary in incomplete
            }
            ui.emit(
                json.dumps(
                    {
                        "site": site,
                        "books": [
                            {
                                "book_id": book.book_id,
                                "downloaded": book in download_ui.completed_books,
                                "failed": failed_by_book.get(book.book_id, 0),
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
        if strict and incomplete:
            # The failure list was already printed by ``_report_incomplete``.
            raise SystemExit(EXIT_FAILURE)
        if not download_ui.completed_books:
            raise SystemExit(EXIT_FAILURE)
        if export_failed:
            raise SystemExit(EXIT_FAILURE)
