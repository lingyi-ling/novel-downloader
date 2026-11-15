#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.search
-----------------------------------------

"""

from __future__ import annotations

import json
import math
import re
from argparse import ArgumentParser, BooleanOptionalAction, Namespace
from collections.abc import Sequence
from datetime import date
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
from .base import (
    EXIT_FAILURE,
    EXIT_USAGE,
    Command,
    expand_path,
    fail,
    stdin_is_interactive,
)

if TYPE_CHECKING:
    from novel_downloader.plugins.search import SearchEvent

#: Upper bound shared with ``plugins.search.MAX_WORD_FILTER`` and the REST
#: schema.  Kept local so the CLI does not import the API layer.
_MAX_WORD_FILTER = 1_000_000_000

#: The only accepted spelling of ``--updated-after``.
_FILTER_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _unknown_search_sites(sites: Sequence[str]) -> list[str]:
    """
    Return the explicitly requested site keys no searcher is registered for.

    Deliberately a module-level hook: tests can replace it to exercise the
    diagnostic without depending on the process-wide plugin registry, while
    the real implementation asks the registry (built-ins, rule sources and any
    local plugin enabled just before this runs).
    """
    from novel_downloader.plugins.search import unknown_search_sites

    return unknown_search_sites(sites)


def _available_search_sites() -> list[str]:
    """Return every site key a searcher is registered for, sorted.

    Only called on the error path, because it makes the registry import every
    plugin; a valid ``--site`` never pays that cost.
    """
    from novel_downloader.plugins.search import registered_search_site_keys

    return registered_search_site_keys()


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
        # ``nargs="+"`` accepts an unquoted "title author" query, which is the
        # form the multi-word ranking feature expects; the parts are joined
        # with single spaces in ``run``.
        parser.add_argument(
            "keyword",
            nargs="+",
            help=t("Search keyword"),
        )
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
            help=t(
                "Result ordering key: quality, priority, site, title, author, "
                "words or update (default: quality)."
            ),
        )
        # ``None`` is meaningful: the ranking layer then applies the key's
        # natural direction (``desc`` for quality/words/update, ``asc`` for
        # priority/site/title/author), exactly like the API's omitted ``order``.
        parser.add_argument(
            "--order",
            choices=["asc", "desc"],
            default=None,
            help=t(
                "Sort direction: asc or desc. Default: the key's natural "
                "direction (desc for quality, words and update; asc for "
                "priority, site, title and author)."
            ),
        )
        parser.add_argument(
            "--min-words",
            type=int,
            default=None,
            metavar="N",
            help=t(
                "Minimum word count (inclusive); results without a word "
                "count are dropped when set."
            ),
        )
        parser.add_argument(
            "--max-words",
            type=int,
            default=None,
            metavar="N",
            help=t(
                "Maximum word count (inclusive); results without a word "
                "count are dropped when set."
            ),
        )
        parser.add_argument(
            "--updated-after",
            default=None,
            metavar="YYYY-MM-DD",
            help=t(
                "Only keep results updated on or after this date; results "
                "without an update date are dropped when set."
            ),
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
        # Shell users may type the keyword unquoted ("斗破苍穹 天蚕土豆"), so
        # the parser produces one item per word; the ranking layer expects a
        # single normalized string. A blank query is a usage error, not a
        # request to search every site for nothing.
        raw_keyword = args.keyword
        if isinstance(raw_keyword, (list, tuple)):
            keyword = " ".join(str(part) for part in raw_keyword)
        else:
            keyword = str(raw_keyword or "")
        keyword = keyword.strip()
        if not keyword:
            fail(t("No keyword entered."), EXIT_USAGE)

        min_words: int | None = args.min_words
        max_words: int | None = args.max_words
        for value in (min_words, max_words):
            if value is not None and not 0 <= value <= _MAX_WORD_FILTER:
                fail(
                    t("--min-words/--max-words must be between 0 and {max}.").format(
                        max=_MAX_WORD_FILTER
                    ),
                    EXIT_USAGE,
                )
        # Contradictory bounds can never match a row.  Without this they ran a
        # full search and returned an empty array with exit 0 -- the same
        # document as "no such book", which hides a typo such as swapped
        # bounds.  The desktop already rejects the pair as a usage error.
        if min_words is not None and max_words is not None and min_words > max_words:
            fail(t("Min words cannot be greater than max words"), EXIT_USAGE)

        # ``argparse`` accepts ``nan``/``inf`` for ``type=float``.  ``inf``
        # reaches ``aiohttp.ClientTimeout``, whose timer calls ``ceil(inf)``
        # and raises ``OverflowError`` on the first request -- every site then
        # looks like an outage (exit 1, empty array) although nothing was
        # wrong with the network.  ``nan`` survives ``max(0.1, nan)`` as
        # ``0.1`` and silently collapses the budget.  Neither is a usable
        # duration, so both are usage errors, exactly like ``follow``.
        # Finite non-positive values keep their historical clamp to 0.1 s.
        timeout_arg = float(args.timeout)
        if not math.isfinite(timeout_arg):
            fail(
                f"{t('Invalid input.')} (--timeout={timeout_arg!r})",
                EXIT_USAGE,
            )

        updated_after: date | None = None
        if args.updated_after is not None:
            raw_date = str(args.updated_after).strip()
            if not _FILTER_DATE_RE.fullmatch(raw_date):
                fail(
                    t(
                        "Invalid --updated-after date: {value} (expected YYYY-MM-DD)."
                    ).format(value=raw_date),
                    EXIT_USAGE,
                )
            try:
                updated_after = date.fromisoformat(raw_date)
            except ValueError:
                fail(
                    t(
                        "Invalid --updated-after date: {value} (expected YYYY-MM-DD)."
                    ).format(value=raw_date),
                    EXIT_USAGE,
                )

        sites: Sequence[str] | None = args.site or None
        formats: list[str] | None = args.format
        as_json: bool = bool(args.as_json)
        interactive = not as_json

        if as_json and args.first:
            fail(t("--json and --first cannot be combined."), EXIT_USAGE)

        overall_limit = None if args.limit is None else max(1, args.limit)
        per_site_limit = max(1, args.site_limit)
        timeout = max(0.1, timeout_arg)
        result_sort: str = args.sort
        # ``None`` keeps the ranking layer's natural direction for the key.
        result_order: str | None = args.order
        group_results_flag: bool = bool(args.group)
        config_path: Path | None = expand_path(args.config) if args.config else None

        config_data = load_or_init_config(
            config_path, non_interactive=as_json or not stdin_is_interactive()
        )
        if config_data is None:
            # ``load_or_init_config`` already reported the reason on stderr.
            raise SystemExit(EXIT_FAILURE)
        adapter = ConfigAdapter(config=config_data)
        # A local plugin can provide searchers that do not exist in the
        # built-in tree; download/export/batch already enable them.  Without
        # this, ``search --site <local-plugin>`` searched nothing at all and
        # reported a successful run with "0 site(s) completed".
        plugins_cfg = adapter.get_plugins_config()
        if plugins_cfg.get("enable_local_plugins"):
            registrar.enable_local_plugins(
                plugins_cfg.get("local_plugins_path"),
                override=plugins_cfg.get("override_builtins", False),
            )
        # A typo in ``--site`` used to resolve to no searcher at all, which the
        # aggregate reported as "0 site(s) completed" and exit 0 -- the same
        # output as "no such book".  Reject the selection as a usage error
        # instead, after local plugins are enabled so their keys count.
        if sites:
            unknown_sites = _unknown_search_sites(sites)
            if unknown_sites:
                available_sites = _available_search_sites()
                # An empty registry cannot tell a typo from a plugin tree that
                # failed to load; keep the historical behaviour rather than
                # rejecting every explicit site.
                if available_sites:
                    fail(
                        t(
                            "Unsupported site(s): {sites}. Available: {available}"
                        ).format(
                            sites=", ".join(repr(site) for site in unknown_sites),
                            available=", ".join(available_sites),
                        ),
                        EXIT_USAGE,
                    )
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
            # while the searchers are prepared.  Human mode prints one line per
            # event on stderr; JSON mode suppresses the per-site lines and
            # keeps only the closing tally on stderr, so stdout stays pure.
            events: list[SearchEvent] = []

            def _on_event(event: SearchEvent) -> None:
                """Record one per-site search event and echo it on stderr."""
                events.append(event)
                if as_json:
                    # JSON consumers get one summary line on stderr; per-site
                    # chatter is suppressed, and stdout stays pure JSON.
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

            def _every_site_failed() -> bool:
                """
                True when every site that reported an outcome failed.

                An empty result set is a successful search only while at least
                one site actually answered: "all sites down" used to exit 0 and
                look exactly like "no match", which hides an outage from every
                script and CI job. A site that was skipped (browser-assisted)
                is not an answer, so it does not count as success either.
                """
                answered = any(e.kind == "completed" for e in events)
                refused = any(e.kind == "failed" for e in events)
                return refused and not answered

            if as_json:
                results = await search(
                    keyword=keyword,
                    sites=sites,
                    limit=overall_limit,
                    per_site_limit=per_site_limit,
                    timeout=timeout,
                    sort=result_sort,
                    order=result_order,
                    on_event=_on_event,
                    group=group_results_flag,
                    min_words=min_words,
                    max_words=max_words,
                    updated_after=updated_after,
                )
                _print_search_summary()
                if not results and not _every_site_failed():
                    # An empty array is a valid document, but it does not say
                    # why it is empty. Echo the (possibly multi-word) keyword
                    # on stderr so "no such book" is distinguishable from an
                    # outage, mirroring the human-readable path below.
                    ui.warn(
                        t("No results found for '{keyword}'.").format(keyword=keyword)
                    )
                # The document is emitted first so a machine consumer still
                # gets its (empty) payload; the non-zero exit reports the outage.
                # Compact separators keep stdout on exactly one line.
                ui.emit(
                    json.dumps(list(results), ensure_ascii=False, separators=(",", ":"))
                )
                if _every_site_failed():
                    raise SystemExit(EXIT_FAILURE)
                return

            with ui.status(t("Searching for '{keyword}'...").format(keyword=keyword)):
                results = await search(
                    keyword=keyword,
                    sites=sites,
                    limit=overall_limit,
                    per_site_limit=per_site_limit,
                    timeout=timeout,
                    sort=result_sort,
                    order=result_order,
                    on_event=_on_event,
                    group=group_results_flag,
                    min_words=min_words,
                    max_words=max_words,
                    updated_after=updated_after,
                )
            _print_search_summary()

            if not results:
                if _every_site_failed():
                    # Every selected site reported an error: that is an outage,
                    # not "no such book". The per-site failure lines were
                    # already printed by ``_on_event`` above.
                    raise SystemExit(EXIT_FAILURE)
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

            # A book that fails to process/export must not cost the user the
            # other completed books, and an export that produced no file is a
            # failure: ``CommonClient.export_book`` reports a rejected write as
            # an empty list rather than raising, so "no exception" would
            # otherwise be mistaken for success and exit 0.
            export_failed: list[str] = []
            for book in download_ui.completed_books:
                try:
                    client.process_book(
                        book,
                        processors=adapter.get_processor_configs(site),
                        ui=process_ui,
                    )
                    export_result = client.export_book(
                        book,
                        cfg=adapter.get_exporter_config(site),
                        formats=formats or adapter.get_export_fmt(site),
                        ui=export_ui,
                    )
                except Exception as e:
                    export_failed.append(book.book_id)
                    ui.error(
                        t("Process/export failed for book {book_id}: {err}").format(
                            book_id=book.book_id, err=e
                        )
                    )
                    continue

                # Older/third-party clients may return ``None``, which carries
                # no verdict; a dict with only empty lists does.
                if isinstance(export_result, dict) and not any(export_result.values()):
                    export_failed.append(book.book_id)

            if export_failed:
                ui.error(t("Export failed: {err}").format(err=", ".join(export_failed)))
                raise SystemExit(EXIT_FAILURE)

        import asyncio

        asyncio.run(_run())
