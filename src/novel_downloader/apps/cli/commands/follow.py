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
        [--wait] [--wait-timeout SECS]

``check`` deliberately performs searches only: when a new chapter is found it
prints the exact ``novel-cli download`` command instead of starting a download.
A download is long-running, and the CLI has no background scheduler, so
returning the command keeps the command fast, non-blocking and predictable.

The per-book searches run with bounded concurrency (see
:func:`novel_downloader.apps.core.watchlist.check_all`), each book succeeds or
fails on its own, and the report is printed in follow-list order no matter in
which order the searches finished.

Passing ``--download --wait`` opts in to actually download: the books with new
chapters are queued in the shared task manager and awaited one by one inside a
single ``asyncio.run`` (all waiting and ``manager.close()`` happen there, so no
worker task outlives the CLI process).
"""

from __future__ import annotations

import asyncio
import json
from argparse import ArgumentParser, Namespace, _SubParsersAction
from dataclasses import dataclass
from typing import TYPE_CHECKING

from novel_downloader.apps.cli import ui
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.watchlist import (
    DEFAULT_CHECK_TIMEOUT,
    CheckResult,
    WatchEntry,
    WatchlistStore,
    check_all,
)
from novel_downloader.infra.i18n import t

from .base import EXIT_FAILURE, EXIT_USAGE, Command, fail, localize_help

if TYPE_CHECKING:
    from novel_downloader.apps.core.task_manager import TaskManager

#: Default max time (seconds) to wait for one download with ``--wait``.
_DEFAULT_WAIT_TIMEOUT = 30 * 60

#: How often (seconds) the ``--wait`` loop polls the manager snapshot.
_WAIT_POLL_INTERVAL = 0.5

#: Download-task statuses that end the ``--wait`` polling loop.
_TERMINAL_STATUSES: frozenset[Status] = frozenset(
    {Status.COMPLETED, Status.CANCELLED, Status.FAILED}
)


def _make_store() -> WatchlistStore:
    """Create the store for a command run (patch point for tests)."""
    return WatchlistStore()


@dataclass
class _WaitOutcome:
    """Result of waiting for one download task."""

    entry: WatchEntry
    task_id: str = ""
    status: Status | None = None
    error: str | None = None

    @property
    def timed_out(self) -> bool:
        """``True`` when the wait budget expired before a terminal status."""
        return self.status is None


def _get_manager() -> TaskManager:
    """
    Return the shared task manager (patch point for tests).

    Imported lazily so ``follow`` invocations that never download do not pay
    for the task-manager import chain (aiohttp, site adapters, ...).
    """
    from novel_downloader.apps.core.task_manager import manager

    return manager


async def _wait_for_task(
    manager: TaskManager, task_id: str, timeout: float
) -> DownloadTask | None:
    """
    Poll the manager snapshot until ``task_id`` reaches a terminal status.

    :param manager: Task manager owning the task.
    :param task_id: Id of the task to watch.
    :param timeout: Max seconds to wait before giving up.
    :return: The finished task (its ``status``/``error`` are final), or
        ``None`` when ``timeout`` expired first.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        snapshot = manager.snapshot()
        for state in (
            *snapshot["running"],
            *snapshot["pending"],
            *snapshot["completed"],
        ):
            if state.task_id == task_id and state.status in _TERMINAL_STATUSES:
                return state
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_WAIT_POLL_INTERVAL)


async def _download_entries(
    entries: list[WatchEntry], wait_timeout: float
) -> list[_WaitOutcome]:
    """
    Queue one download per entry and wait for each to finish.

    Books are processed strictly one after another so a slow site cannot pile
    up concurrent pressure. A book whose wait budget expires is cancelled
    through the manager (so its site worker can move on) and recorded as a
    timeout; the remaining books still get their own full budget.

    ``manager.close()`` runs in a ``finally`` block, so every worker task is
    stopped before this coroutine returns and the event loop shuts down.
    """
    manager = _get_manager()
    outcomes: list[_WaitOutcome] = []
    try:
        for entry in entries:
            task = await manager.add_task(
                title=entry.title or entry.book_id,
                site=entry.site,
                book_id=entry.book_id,
            )
            state = await _wait_for_task(manager, task.task_id, wait_timeout)
            if state is None:
                await manager.cancel_task(task.task_id)
                outcomes.append(_WaitOutcome(entry=entry, task_id=task.task_id))
            else:
                outcomes.append(
                    _WaitOutcome(
                        entry=entry,
                        task_id=task.task_id,
                        status=state.status,
                        error=state.error,
                    )
                )
    finally:
        await manager.close()
    return outcomes


def _wait_and_report(entries: list[WatchEntry], wait_timeout: float) -> bool:
    """
    Download ``entries`` through the task manager, one at a time, and report.

    Runs the whole add/wait/close cycle inside a single ``asyncio.run`` so no
    worker survives the call. Returns ``True`` only when every book reached
    :attr:`Status.COMPLETED`.
    """
    ui.info(t("Downloading {count} book(s) sequentially...").format(count=len(entries)))
    outcomes = asyncio.run(_download_entries(entries, wait_timeout))

    completed = 0
    cancelled = 0
    failed = 0
    for outcome in outcomes:
        label = f"{outcome.entry.site}/{outcome.entry.book_id}"
        if outcome.timed_out:
            cancelled += 1
            ui.warn(
                t("Timed out waiting for {book} after {timeout:g} seconds.").format(
                    book=label, timeout=wait_timeout
                )
            )
        elif outcome.status is Status.COMPLETED:
            completed += 1
            ui.success(t("Download completed: {book}").format(book=label))
        elif outcome.status is Status.FAILED:
            failed += 1
            ui.error(
                t("Download failed for {book}: {error}").format(
                    book=label, error=outcome.error or "-"
                )
            )
        else:
            cancelled += 1
            ui.warn(t("Download cancelled: {book}").format(book=label))

    ui.info(
        t(
            "Download summary: {completed} completed, {cancelled} cancelled, "
            "{failed} failed."
        ).format(completed=completed, cancelled=cancelled, failed=failed)
    )
    return completed == len(outcomes)


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
            "--wait",
            action="store_true",
            help=t(
                "With --download: queue each new-chapter download in the task "
                "manager and wait for it to finish (one book at a time)."
            ),
        )
        parser.add_argument(
            "--wait-timeout",
            type=float,
            default=_DEFAULT_WAIT_TIMEOUT,
            metavar="SECS",
            help=t(
                "Max seconds to wait for each download before giving up "
                "(default: 1800, used with --wait)."
            ),
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_CHECK_TIMEOUT,
            metavar="SECS",
            help=t("Search timeout per book in seconds (default: 5.0)."),
        )

    @classmethod
    def run(cls, args: Namespace) -> None:
        wait = bool(args.wait)
        if wait and not args.download:
            fail(t("--wait requires --download."), EXIT_USAGE)

        store = _make_store()
        entries = store.entries()
        if not entries:
            ui.info(t("No followed books to check."))
            return

        timeout = max(0.1, float(args.timeout))
        # check_all searches the books with bounded concurrency, persists every
        # new chapter it finds and returns the results in follow-list order.
        outcomes: list[CheckResult] = asyncio.run(check_all(store, timeout))

        show_all = bool(args.all)
        changed = 0
        failed = 0
        hints: list[str] = []
        download_targets: list[WatchEntry] = []

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

            if not outcome.changed:
                if show_all:
                    ui.info(
                        t("Up to date: {book} ({latest})").format(
                            book=label, latest=outcome.latest
                        )
                    )
                continue

            was_known = bool(entry.last_chapter)
            changed += 1
            message = (
                t("New chapter for {book}: {latest}")
                if was_known
                else t("First chapter recorded for {book}: {latest}")
            )
            ui.success(message.format(book=label, latest=outcome.latest))
            if args.download:
                if wait:
                    download_targets.append(entry)
                else:
                    hints.append(
                        f"novel-cli download --site {entry.site} {entry.book_id}"
                    )

        downloads_ok = True
        if args.download and wait:
            if download_targets:
                wait_timeout = max(0.1, float(args.wait_timeout))
                downloads_ok = _wait_and_report(download_targets, wait_timeout)
        elif args.download:
            for hint in hints:
                ui.info(t("Run: {command}").format(command=hint))

            if hints:
                ui.info(
                    t(
                        "'follow check' does not start downloads; run the "
                        "command(s) above to fetch the new chapters."
                    )
                )

        if changed == 0 and failed == 0:
            if show_all:
                ui.info(t("All followed books are up to date."))
            else:
                ui.info(t("No new chapters found."))

        if failed or not downloads_ok:
            raise SystemExit(EXIT_FAILURE)
