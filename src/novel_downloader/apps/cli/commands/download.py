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
from novel_downloader.plugins.protocols import ClientProtocol
from novel_downloader.schemas import BookConfig
from novel_downloader.schemas.book import DownloadSummary

from ..ui_adapters import (
    CLIDownloadUI,
    CLIExportUI,
    CLILoginUI,
    CLIProcessUI,
)
from .base import (
    EXIT_FAILURE,
    EXIT_USAGE,
    Command,
    expand_config_paths,
    expand_path,
    fail,
    parse_book_args,
    require_safe_book_id,
    require_safe_site,
)

#: Failure lines shown by the default (warning-only) report.
_MAX_LISTED_FAILURES = 5
#: Failure lines shown by ``--strict`` before pointing at the ledger.
_MAX_LISTED_FAILURES_STRICT = 20


def _clean_range(value: str | None, option: str) -> str | None:
    """
    Strip a ``--start`` / ``--end`` chapter ID and reject an empty one.

    Chapter IDs are strings, not numbers (a site may use ``8_8187`` or a
    UUID), so there is nothing numeric to validate here.  What must not slip
    through is a blank value: ``--start ""`` would otherwise be forwarded as
    an ID that can never match, and the book would silently fall back to an
    empty plan ("No chapters downloaded") instead of a usage error.

    :param value: Raw option value, or ``None`` when the option was omitted.
    :param option: Option name used in the diagnostic (``--start``/``--end``).
    :return: The trimmed chapter ID, or ``None`` when the option was omitted.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        # Reuse an existing translated message; the option name is raw
        # diagnostic detail, matching ``require_safe_book_id``.
        fail(f"{t('Invalid input.')} ({option} must not be empty)", EXIT_USAGE)
    return cleaned


def _failed_chapters(raw: object) -> int:
    """
    Return the positive failed-chapter count of a summary-like value.

    Clients are typed through :class:`ClientProtocol`, but third-party clients
    may return ``None`` or a dict whose ``failed`` is not an integer. The check
    is structural so a malformed summary cannot crash the report.

    :param raw: Whatever ``client.download_book`` returned.
    :return: ``failed`` when it is a positive ``int``, else ``0``.
    """
    if not isinstance(raw, dict):
        return 0
    failed = raw.get("failed")
    if isinstance(failed, bool) or not isinstance(failed, int):
        return 0
    return failed if failed > 0 else 0


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
    if _failed_chapters(raw) <= 0:
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
        # The summary is typed, but third-party clients are only checked
        # structurally (see ``_collect_failures``): a dict that merely has a
        # truthy ``failed`` must not crash the report with a ``KeyError``.
        book_id = summary.get("book_id") or "?"
        report(
            t(
                "Download incomplete for book {book_id}: "
                "{failed} of {total} chapter(s) failed."
            ).format(
                book_id=book_id,
                failed=summary.get("failed") or 0,
                total=summary.get("total") or 0,
            )
        )

        failures = summary.get("failures") or []
        if not isinstance(failures, list):
            failures = []
        for failure in failures[:limit]:
            if not isinstance(failure, dict):
                continue
            report(
                t("Failed: {name} ({reason})").format(
                    name=failure.get("chapter_title")
                    or failure.get("chapter_id")
                    or "?",
                    reason=failure.get("reason") or "-",
                )
            )

        ledger_path = summary.get("ledger_path") or "-"
        if len(failures) > limit:
            report(
                t("{count} more failure(s) not listed; see {path} for details.").format(
                    count=len(failures) - limit, path=ledger_path
                )
            )
        else:
            report(t("Failure ledger: {path}").format(path=ledger_path))


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
        config_path: Path | None = expand_path(args.config) if args.config else None
        site: str | None = args.site.strip() if args.site is not None else None
        formats: list[str] | None = args.format
        start = _clean_range(args.start, "--start")
        end = _clean_range(args.end, "--end")
        # IDs are joined onto the data directory exactly as typed. ``ConfigAdapter``
        # already strips IDs read from the configuration file, so ``" 123"`` on the
        # command line must name the same book (and directory) as ``123`` instead
        # of starting a second, duplicate download.
        book_args = [value.strip() for value in args.book_ids]

        # book_ids
        if site is not None:  # SITE MODE
            if not site:
                fail(t("Unsupported site: {site!r}").format(site=site), EXIT_USAGE)
            books = parse_book_args(book_args, start, end)
        else:  # URL MODE
            if not book_args:
                fail(
                    t(
                        "No book ID or URL provided. Pass a book URL, "
                        "or use --site SITE [book_id ...]."
                    ),
                    EXIT_USAGE,
                )

            if len(book_args) != 1:
                fail(
                    t(
                        "Expected exactly one URL argument when --site is omitted (got {n})."  # noqa: E501
                    ).format(n=len(book_args)),
                    EXIT_USAGE,
                )

            from novel_downloader.infra.book_url_resolver import resolve_book_url

            ui.info(t("No --site provided; detecting site from URL..."))

            raw_url = book_args[0]
            resolved = resolve_book_url(raw_url)
            if not resolved:
                fail(
                    t("Could not resolve site and book from URL: {url}").format(
                        url=raw_url
                    ),
                    EXIT_USAGE,
                )

            site = resolved["site_key"].strip()
            book_id = resolved.get("book_id")

            if not book_id:
                fail(
                    t("The provided URL does not contain a valid book ID."),
                    EXIT_USAGE,
                )

            books = [
                BookConfig(
                    book_id=str(book_id).strip(),
                    start_id=start,
                    end_id=end,
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

        # Config directories go through the same ``~``/env expansion as the CLI
        # path options, so ``output_dir = "~/books"`` means the home directory
        # on every platform (and matches ``batch --out``).
        config_data = expand_config_paths(config_data)

        ui.info(t("Using site: {site}").format(site=site))
        adapter = ConfigAdapter(config=config_data)

        if not books and site:
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

        # ``site`` and ``book_id`` become directory names under the raw-data
        # root (and the client is built before any download starts), so an
        # escaping value must be refused as a usage error.
        require_safe_site(site)
        for book in books:
            require_safe_book_id(book.book_id)

        # The same book requested twice (a copy-pasted ID, a config entry that
        # also appears on the command line) would otherwise be downloaded
        # twice: the second pass finds every chapter cached, but it still costs
        # a metadata round-trip, a second progress report and a duplicate JSON
        # entry. Only *identical* configs collapse; a second request for the
        # same book with a different range stays a separate job.
        books = list(dict.fromkeys(books))

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

        # Errors are collected instead of raised so a ``--json`` caller still
        # gets a machine-readable summary before the non-zero exit code.
        download_error: str | None = None
        unsupported_error: str | None = None
        # Books whose download ended with missing chapters.  Populated either
        # from the summary (default mode) or from ``IncompleteDownloadError``
        # (``--strict``).
        incomplete: list[DownloadSummary] = []
        # Books whose download raised.  Kept separate from ``download_error``
        # (a site/session-level failure) so one broken book does not abort the
        # remaining books -- the same isolation ``batch --download`` provides.
        download_failed_books: list[str] = []

        # Client construction can fail (an unloadable plugin, a broken
        # client config). That is a runtime failure like any other: record it
        # and keep going to the report instead of dying with a traceback
        # before ``--json`` has emitted anything.
        client: ClientProtocol | None = None
        try:
            client = registrar.get_client(site, adapter.get_client_config(site))
        except ValueError as e:
            unsupported_error = t("'{site}' is currently not supported: {err}").format(
                site=site, err=e
            )
        except Exception as e:
            download_error = t("Site error ({site}): {err}").format(site=site, err=e)

        async def download_books(active_client: ClientProtocol) -> None:
            nonlocal download_error, unsupported_error
            try:
                async with active_client:
                    if adapter.get_login_required(site):
                        succ = await active_client.login(
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
                                raw_summary = await active_client.download_book(
                                    book, ui=download_ui, strict=True
                                )
                            else:
                                raw_summary = await active_client.download_book(
                                    book, ui=download_ui
                                )
                        except IncompleteDownloadError as exc:
                            incomplete.append(exc.summary)
                            continue
                        except ValueError:
                            # Site-level (the parser/fetcher for ``site`` is
                            # missing): no other book can succeed either, so
                            # let the outer handler report it once.
                            raise
                        except Exception as e:
                            download_failed_books.append(book.book_id)
                            ui.error(
                                t("Download failed for book {book_id}: {err}").format(
                                    book_id=book.book_id, err=e
                                )
                            )
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

        if client is not None:
            asyncio.run(download_books(client))

        # A download that returned without ``ui.on_complete`` did not produce
        # a book (``CommonClient`` skips that call when the chapter plan is
        # empty, e.g. an out-of-range --start/--end). If another book
        # succeeded, that used to be skipped silently and the run exited 0.
        if client is not None and download_error is None and unsupported_error is None:
            incomplete_book_ids = {
                summary.get("book_id")
                for summary in incomplete
                if summary.get("book_id")
            }
            completed_ids = {book.book_id for book in download_ui.completed_books}
            for book in books:
                if (
                    book.book_id in completed_ids
                    or book.book_id in incomplete_book_ids
                    or book.book_id in download_failed_books
                ):
                    continue
                download_failed_books.append(book.book_id)
                ui.error(
                    t("No chapters downloaded for book {book_id}.").format(
                        book_id=book.book_id
                    )
                )

        # Report missing chapters before the (potentially slow) export so the
        # failure list is not buried under export output.
        if incomplete:
            _report_incomplete(incomplete, strict=strict)

        # In strict mode an incomplete book must not be exported: the caller
        # asked to fail rather than to produce a file with holes.  A summary
        # that omits ``book_id`` (a third-party client) cannot name a book to
        # skip, so it is ignored here and reported by ``_report_incomplete``.
        failed_book_ids = {
            summary["book_id"] for summary in incomplete if summary.get("book_id")
        }

        # export
        exported: dict[str, dict[str, list[str]]] = {}
        failed_books: list[str] = []
        if client is not None and not args.no_export and download_ui.completed_books:
            process_ui = CLIProcessUI()
            export_ui = CLIExportUI()

            # ``completed_books`` is a set, so iterating it would export in
            # hash order (and make the export summary differ between runs).
            # Walk the requested books instead: downloads happened in this
            # order, so exports, ``failed_books`` and the JSON report agree.
            # ``dict.fromkeys`` keeps the previous de-duplication of a book
            # id passed twice.
            for book in dict.fromkeys(
                item for item in books if item in download_ui.completed_books
            ):
                if strict and book.book_id in failed_book_ids:
                    continue
                # One book whose processing/export blows up must not cost the
                # remaining books their export (this loop used to abort the
                # whole command, JSON report included). Record it per book and
                # keep going, exactly like the download loop above.
                try:
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
                    if not isinstance(results, dict):
                        # Older/third-party clients may return ``None``.
                        raise TypeError(
                            f"unexpected export result: {type(results).__name__}"
                        )
                    paths_by_fmt = {
                        fmt: [str(path) for path in paths]
                        for fmt, paths in results.items()
                    }
                except Exception as err:
                    exported[book.book_id] = {}
                    failed_books.append(book.book_id)
                    ui.error(
                        t("Process/export failed for book {book_id}: {err}").format(
                            book_id=book.book_id, err=err
                        )
                    )
                    continue
                exported[book.book_id] = paths_by_fmt
                # ``CommonClient.export_book`` catches per-format errors and
                # returns an empty list, so "no exception" is not "wrote a
                # file": an export that produced nothing is a failure.
                if not any(paths_by_fmt.values()):
                    failed_books.append(book.book_id)
        elif args.no_export:
            ui.info(t("Export skipped (--no-export)"))

        if as_json:
            # ``downloaded`` means "the run finished", not "the book is whole".
            # Machine consumers need the gap size to tell the two apart, so
            # every book also reports how many chapters are missing.
            failed_by_book: dict[str, int] = {}
            for summary in incomplete:
                book_id = summary.get("book_id")
                if book_id:
                    failed_by_book[book_id] = _failed_chapters(summary)
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
                    separators=(",", ":"),
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
        if failed_books:
            # JSON (if requested) was already emitted above; name the books on
            # stderr so a human sees *what* failed, not just the exit code.
            ui.error(t("Export failed: {err}").format(err=", ".join(failed_books)))
            raise SystemExit(EXIT_FAILURE)
        if download_failed_books:
            # Each failed book was reported in the loop above; the remaining
            # books were still downloaded and exported.
            raise SystemExit(EXIT_FAILURE)
