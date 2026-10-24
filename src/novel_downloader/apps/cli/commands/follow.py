#!/usr/bin/env python3
"""
novel_downloader.apps.cli.commands.follow
-----------------------------------------

Watch a list of books and check whether new chapters have been published.

Subcommands::

    add SITE BOOK_ID [--title T]   follow a book
    remove SITE BOOK_ID            stop following a book
    list [--json]                  show the follow list
    check [--all] [--download]     search the sites for newer chapters

``check`` deliberately performs searches only: when a new chapter is found it
prints the exact ``novel-cli download`` command instead of starting a download.
A download is long-running, and the CLI has no background scheduler, so
returning the command keeps the command fast, non-blocking and predictable.
"""

from __future__ import annotations

import asyncio
import json
from argparse import ArgumentParser, Namespace, _SubParsersAction
from dataclasses import dataclass

from novel_downloader.apps.cli import ui
from novel_downloader.apps.core.watchlist import WatchEntry, WatchlistStore
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import search as search_module
from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import SearchResult

from .base import EXIT_FAILURE, Command, localize_help

#: Results requested per site while looking for the latest chapter.
_PER_SITE_LIMIT = 10

#: Default per-search timeout (seconds), matching the ``search`` command.
_DEFAULT_TIMEOUT = 5.0


@dataclass
class _CheckOutcome:
    """Result of checking one followed book."""

    entry: WatchEntry
    latest: str = ""
    matched_title: str = ""
    error: str | None = None

    @property
    def found(self) -> bool:
        return self.error is None and bool(self.latest)


def _make_store() -> WatchlistStore:
    """Create the store for a command run (patch point for tests)."""
    return WatchlistStore()


def _match_result(
    entry: WatchEntry, results: list[SearchResult]
) -> SearchResult | None:
    """
    Pick the search result that belongs to ``entry``.

    A result from the same site with the exact same book id wins; otherwise a
    result whose normalized title equals the followed title is accepted (a site
    may expose the same book under a different id).
    """
    for result in results:
        if result.get("site") != entry.site:
            continue
        if str(result.get("book_id", "")) == entry.book_id:
            return result

    if not entry.title:
        return None

    wanted = normalize_text(entry.title)
    if not wanted:
        return None
    for result in results:
        if result.get("site") != entry.site:
            continue
        if normalize_text(result.get("title", "")) == wanted:
            return result
    return None


async def _check_entry(entry: WatchEntry, timeout: float) -> _CheckOutcome:
    """Search one site and return the newest chapter for ``entry``."""
    keyword = entry.title or entry.book_id
    try:
        results = await search_module.search(
            keyword=keyword,
            sites=[entry.site],
            limit=None,
            per_site_limit=_PER_SITE_LIMIT,
            timeout=timeout,
            sort="quality",
        )
    except Exception as exc:  # network/plugin failures are per-book, not fatal
        return _CheckOutcome(entry=entry, error=str(exc))

    matched = _match_result(entry, results)
    if matched is None:
        return _CheckOutcome(entry=entry)
    return _CheckOutcome(
        entry=entry,
        latest=str(matched.get("latest_chapter") or "").strip(),
        matched_title=str(matched.get("title") or "").strip(),
    )


async def _check_all(entries: list[WatchEntry], timeout: float) -> list[_CheckOutcome]:
    """Check every entry sequentially (one site is usually queried per book)."""
    outcomes: list[_CheckOutcome] = []
    for entry in entries:
        outcomes.append(await _check_entry(entry, timeout))
    return outcomes


class FollowCmd(Command):
    name = "follow"
    help = t("Follow books and check them for new chapters.")

    @classmethod
    def register(cls, subparsers: _SubParsersAction[ArgumentParser]) -> None:
        parser = subparsers.add_parser(
            cls.name, help=cls.help, description=cls.help, add_help=False
        )
        localize_help(parser)
        sub = parser.add_subparsers(
            dest="subcommand", required=True, title=t("Subcommands")
        )

        for subcmd in (
            FollowAddCmd,
            FollowRemoveCmd,
            FollowListCmd,
            FollowCheckCmd,
        ):
            subcmd.register(sub)

    @classmethod
    def run(cls, args: Namespace) -> None:
        raise NotImplementedError("FollowCmd should not be executed directly")


class FollowAddCmd(Command):
    name = "add"
    help = t("Add a book to the follow list.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("site", help=t("Source site key"))
        parser.add_argument("book_id", help=t("Book ID"))
        parser.add_argument(
            "--title",
            metavar="TEXT",
            help=t("Display title used when checking for new chapters."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        store = _make_store()
        entry, created = store.add(str(args.site), str(args.book_id), title=args.title)
        message = (
            t("Added to the follow list: {site}/{book_id}")
            if created
            else t("Updated follow entry: {site}/{book_id}")
        )
        ui.success(message.format(site=entry.site, book_id=entry.book_id))
        if not entry.title:
            ui.warn(
                t(
                    "No title set for this book. Run 'follow add {site} {book_id} "
                    "--title TEXT' so 'follow check' can find it by name."
                ).format(site=entry.site, book_id=entry.book_id)
            )


class FollowRemoveCmd(Command):
    name = "remove"
    help = t("Remove a book from the follow list.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument("site", help=t("Source site key"))
        parser.add_argument("book_id", help=t("Book ID"))

    @classmethod
    def run(cls, args: Namespace) -> None:
        store = _make_store()
        site = str(args.site)
        book_id = str(args.book_id)
        if store.remove(site, book_id):
            ui.success(
                t("Removed from the follow list: {site}/{book_id}").format(
                    site=site, book_id=book_id
                )
            )
        else:
            ui.warn(
                t("Not in the follow list: {site}/{book_id}").format(
                    site=site, book_id=book_id
                )
            )


class FollowListCmd(Command):
    name = "list"
    help = t("List followed books.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help=t("Print machine-readable JSON on stdout and exit."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        entries = _make_store().entries()

        if args.as_json:
            ui.emit(
                json.dumps(
                    [entry.to_dict() for entry in entries],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if not entries:
            ui.info(t("No followed books."))
            return

        rows = [
            [
                entry.site,
                entry.book_id,
                entry.title or "-",
                entry.last_chapter or "-",
                entry.updated_at or "-",
            ]
            for entry in entries
        ]
        ui.render_table(
            t("Followed books"),
            [t("Site"), t("Book ID"), t("Title"), t("Latest chapter"), t("Updated")],
            rows,
        )


class FollowCheckCmd(Command):
    name = "check"
    help = t("Check followed books for new chapters.")

    @classmethod
    def add_arguments(cls, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--all",
            action="store_true",
            help=t("Show every followed book, including ones that are up to date."),
        )
        parser.add_argument(
            "--download",
            action="store_true",
            help=t("Print the download command for books with new chapters."),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=_DEFAULT_TIMEOUT,
            metavar="SECS",
            help=t("Search timeout per book in seconds (default: 5.0)."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        store = _make_store()
        entries = store.entries()
        if not entries:
            ui.info(t("No followed books to check."))
            return

        timeout = max(0.1, float(args.timeout))
        outcomes = asyncio.run(_check_all(entries, timeout))

        show_all = bool(args.all)
        changed = 0
        failed = 0
        hints: list[str] = []

        for outcome in outcomes:
            entry = outcome.entry
            label = f"{entry.site}/{entry.book_id}"

            if outcome.error is not None:
                failed += 1
                ui.error(
                    t("Check failed for {book}: {error}").format(
                        book=label, error=outcome.error
                    )
                )
                continue

            if not outcome.found:
                if show_all:
                    ui.info(t("No result found for {book}.").format(book=label))
                continue

            if outcome.latest == entry.last_chapter:
                if show_all:
                    ui.info(
                        t("Up to date: {book} ({latest})").format(
                            book=label, latest=outcome.latest
                        )
                    )
                continue

            was_known = bool(entry.last_chapter)
            store.update_latest(
                entry.site,
                entry.book_id,
                outcome.latest,
                title=outcome.matched_title,
            )
            changed += 1
            message = (
                t("New chapter for {book}: {latest}")
                if was_known
                else t("First chapter recorded for {book}: {latest}")
            )
            ui.success(message.format(book=label, latest=outcome.latest))
            if args.download:
                hints.append(f"novel-cli download --site {entry.site} {entry.book_id}")

        for hint in hints:
            ui.info(t("Run: {command}").format(command=hint))

        if args.download and hints:
            ui.info(
                t(
                    "'follow check' does not start downloads; run the command(s) "
                    "above to fetch the new chapters."
                )
            )

        if changed == 0 and failed == 0:
            if show_all:
                ui.info(t("All followed books are up to date."))
            else:
                ui.info(t("No new chapters found."))

        if failed:
            raise SystemExit(EXIT_FAILURE)
