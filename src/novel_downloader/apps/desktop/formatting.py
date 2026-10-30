#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.formatting
----------------------------------------

Pure formatting helpers used by the Tkinter desktop UI.

Keeping this module free of Tk imports makes the presentation logic easy to
test without a display.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from novel_downloader.apps.common.history_files import FileItem, _format_size
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.infra.i18n import t
from novel_downloader.plugins.ranking import normalize_text
from novel_downloader.schemas import SearchResult

__all__ = [
    "FILTER_ACTIVE",
    "FILTER_ALL",
    "FILTER_COMPLETED",
    "FILTER_FAILED",
    "TASK_FILTER_CODES",
    "ExportChoice",
    "RetryParams",
    "TaskMenuState",
    "TaskOutputActions",
    "filter_tasks",
    "follow_row",
    "format_duration",
    "format_error",
    "format_follow_updated",
    "format_mtime",
    "format_size",
    "format_task_progress",
    "history_row",
    "match_watch_result",
    "music_row",
    "normalize_title",
    "parse_sites",
    "retry_params",
    "search_row",
    "status_label",
    "task_detail_text",
    "task_export_choices",
    "task_export_paths",
    "task_filter_code",
    "task_filter_label",
    "task_matches_filter",
    "task_menu_state",
    "task_output_actions",
    "task_row",
    "task_status_group",
]

_SITE_SEPARATORS = re.compile(r"[,，;；\s]+")

_T = TypeVar("_T")

#: Status key -> lazily translated label.  The lambdas keep the literal msgids
#: visible to the ``t()`` extraction pass while avoiding import-time catalogs.
_STATUS_LABELS: dict[str, Callable[[], str]] = {
    "queued": lambda: t("Queued"),
    "running": lambda: t("Running"),
    "processing": lambda: t("Processing"),
    "exporting": lambda: t("Exporting"),
    "completed": lambda: t("Completed"),
    "cancelled": lambda: t("Cancelled"),
    "failed": lambda: t("Failed"),
}


def status_label(status: object) -> str:
    """
    Return a translated label for a task status.

    :param status: A :class:`Status` member or its string value.
    :return: The localized status label, or the raw value when unknown.
    """
    raw = getattr(status, "value", status)
    key = str(raw)
    label = _STATUS_LABELS.get(key)
    return label() if label is not None else key


def format_task_progress(chapters_done: int, chapters_total: int) -> str:
    """
    Render a task's chapter progress as ``done/total (percent%)``.

    :param chapters_done: Number of downloaded chapters.
    :param chapters_total: Total number of chapters, or ``0`` when unknown.
    :return: A compact progress string.
    """
    done = max(0, int(chapters_done))
    total = max(0, int(chapters_total))
    if total <= 0:
        return t("{done}/?").format(done=done)
    percent = int(round(done * 100 / total))
    return t("{done}/{total} ({percent}%)").format(
        done=done, total=total, percent=percent
    )


def task_row(task: DownloadTask) -> tuple[str, str, str, str, str]:
    """
    Build the Treeview values for one download task.

    :param task: Task snapshot taken from the task manager.
    :return: ``(status, title, site, progress, error)``.
    """
    return (
        status_label(task.status),
        task.title,
        task.site,
        format_task_progress(task.chapters_done, task.chapters_total),
        task.error or "",
    )


#: Export formats are opened in this order; the first available one is used.
_EXPORT_PRIORITY = ("epub", "txt")

#: Task states whose exported files may be opened from the UI.
_OPENABLE_STATES = frozenset({"completed", "failed"})

#: Task states that may be re-queued from the UI.
_RETRYABLE_STATES = frozenset({"failed", "cancelled"})


@dataclass(frozen=True)
class ExportChoice:
    """One exported file of a task, labelled for the open-file menu."""

    key: str
    path: Path

    @property
    def label(self) -> str:
        """Human-readable menu label such as ``"EPUB: book.epub"``."""
        return f"{self.key.upper()}: {self.path.name}"


@dataclass(frozen=True)
class RetryParams:
    """The arguments needed to queue a task again."""

    title: str
    site: str
    book_id: str


@dataclass(frozen=True)
class TaskMenuState:
    """Which context-menu entries are available for the selected task."""

    can_retry: bool
    can_open_file: bool
    can_open_folder: bool
    can_copy_path: bool
    can_copy_error: bool
    can_show_details: bool
    choices: tuple[ExportChoice, ...]


@dataclass(frozen=True)
class TaskOutputActions:
    """Paths a finished task can open, and whether each action is available."""

    file_path: Path | None = None
    folder_path: Path | None = None

    @property
    def can_open_file(self) -> bool:
        """Whether the "open file" button should be enabled."""
        return self.file_path is not None

    @property
    def can_open_folder(self) -> bool:
        """Whether the "open folder" button should be enabled."""
        return self.folder_path is not None


def task_export_choices(task: object) -> tuple[ExportChoice, ...]:
    """
    Return every exported file of a task, in display order.

    ``epub`` and ``txt`` come first because those are the formats the history
    page lists; any other format follows in sorted order.  Duplicate paths are
    dropped (keeping the first, highest-priority key) so the menu never lists
    the same file twice.

    :param task: Task snapshot (duck-typed so tests can pass stand-ins).
    :return: An ordered tuple of choices (empty when nothing was exported).
    """
    exported = getattr(task, "exported_paths", None)
    if not isinstance(exported, Mapping) or not exported:
        return ()

    keys: list[Any] = [key for key in _EXPORT_PRIORITY if key in exported]
    keys.extend(
        sorted((key for key in exported if key not in _EXPORT_PRIORITY), key=str)
    )

    choices: list[ExportChoice] = []
    seen: set[str] = set()
    for key in keys:
        value = exported.get(key)
        if value is None:
            continue
        path = Path(value)
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        choices.append(ExportChoice(key=str(key), path=path))
    return tuple(choices)


def task_export_paths(task: object) -> tuple[Path, ...]:
    """
    Return the exported file paths of a task in display order.

    :param task: Task snapshot (duck-typed so tests can pass stand-ins).
    :return: An ordered tuple of paths (empty when nothing was exported).
    """
    return tuple(choice.path for choice in task_export_choices(task))


def retry_params(task: object | None) -> RetryParams | None:
    """
    Build the arguments for re-queuing a failed or cancelled task.

    A task is only retryable when it has stopped with ``failed`` or
    ``cancelled`` and still carries the site and book id needed by
    ``TaskManager.add_task``.

    :param task: Task snapshot, or ``None`` when no row is selected.
    :return: The original identifying fields, or ``None`` when not retryable.
    """
    if task is None:
        return None

    status_value = getattr(task, "status", None)
    status = str(getattr(status_value, "value", status_value))
    if status not in _RETRYABLE_STATES:
        return None

    site = str(getattr(task, "site", "") or "").strip()
    book_id = str(getattr(task, "book_id", "") or "").strip()
    if not site or not book_id:
        return None
    title = str(getattr(task, "title", "") or "").strip()
    return RetryParams(title=title, site=site, book_id=book_id)


def task_menu_state(
    task: object | None, *, retry_blocked: bool = False
) -> TaskMenuState:
    """
    Decide which actions the task context menu should offer.

    ``retry_blocked`` lets the UI suppress the retry entry while a retry is in
    flight or after the same row was already re-queued, which prevents
    duplicate submissions from repeated clicks.

    :param task: Task snapshot, or ``None`` when no row is selected.
    :param retry_blocked: Whether retry was already requested for this row.
    :return: The available menu entries and exported-file choices.
    """
    if task is None:
        return TaskMenuState(
            can_retry=False,
            can_open_file=False,
            can_open_folder=False,
            can_copy_path=False,
            can_copy_error=False,
            can_show_details=False,
            choices=(),
        )

    actions = task_output_actions(task)
    choices = task_export_choices(task)
    error = str(getattr(task, "error", "") or "").strip()
    return TaskMenuState(
        can_retry=retry_params(task) is not None and not retry_blocked,
        can_open_file=actions.can_open_file,
        can_open_folder=actions.can_open_folder,
        can_copy_path=bool(choices),
        can_copy_error=bool(error),
        can_show_details=True,
        choices=choices,
    )


def task_output_actions(task: object | None) -> TaskOutputActions:
    """
    Decide which output-opening actions a task supports.

    Only completed or failed tasks with at least one exported file may open a
    file or its containing folder; everything else leaves the buttons disabled.

    :param task: Task snapshot, or ``None`` when no row is selected.
    :return: The available paths.
    """
    if task is None:
        return TaskOutputActions()

    status_value = getattr(task, "status", None)
    status = str(getattr(status_value, "value", status_value))
    if status not in _OPENABLE_STATES:
        return TaskOutputActions()

    paths = task_export_paths(task)
    if not paths:
        return TaskOutputActions()
    return TaskOutputActions(file_path=paths[0], folder_path=paths[0].parent)


# ---------------------------------------------------------------------------
# Task filtering (pure, local-only)
# ---------------------------------------------------------------------------
FILTER_ALL = "all"
FILTER_ACTIVE = "active"
FILTER_COMPLETED = "completed"
FILTER_FAILED = "failed"

#: Selectable filters, in combobox order.
TASK_FILTER_CODES: tuple[str, ...] = (
    FILTER_ALL,
    FILTER_ACTIVE,
    FILTER_COMPLETED,
    FILTER_FAILED,
)

_FILTER_LABELS: dict[str, Callable[[], str]] = {
    FILTER_ALL: lambda: t("All"),
    FILTER_ACTIVE: lambda: t("Active"),
    FILTER_COMPLETED: lambda: t("Completed"),
    FILTER_FAILED: lambda: t("Failed"),
}

_ACTIVE_STATUSES = frozenset({"queued", "running", "processing", "exporting"})
_COMPLETED_STATUSES = frozenset({"completed"})
_FAILED_STATUSES = frozenset({"failed", "cancelled"})


def task_status_group(status: object) -> str:
    """
    Map a task status to the coarse filter group it belongs to.

    :param status: A :class:`Status` member or its string value.
    :return: ``"active"``, ``"completed"``, ``"failed"`` or ``"other"``.
    """
    raw = getattr(status, "value", status)
    value = str(raw)
    if value in _ACTIVE_STATUSES:
        return FILTER_ACTIVE
    if value in _COMPLETED_STATUSES:
        return FILTER_COMPLETED
    if value in _FAILED_STATUSES:
        return FILTER_FAILED
    return "other"


def task_filter_label(code: str) -> str:
    """
    Return the localized combobox label for a filter code.

    :param code: One of :data:`TASK_FILTER_CODES`.
    :return: The display label.
    """
    label = _FILTER_LABELS.get(code)
    return label() if label is not None else _FILTER_LABELS[FILTER_ALL]()


def task_filter_code(value: object) -> str:
    """
    Map a combobox label (or a raw code) back to a filter code.

    :param value: Text selected in the combobox, or a raw code.
    :return: One of :data:`TASK_FILTER_CODES`.
    """
    raw = str(value or "").strip()
    lowered = raw.lower()
    if lowered in _FILTER_LABELS:
        return lowered
    for code in TASK_FILTER_CODES:
        if lowered == code.lower() or raw == task_filter_label(code):
            return code
    return FILTER_ALL


def task_matches_filter(
    task: object,
    *,
    status: str = FILTER_ALL,
    keyword: str = "",
) -> bool:
    """
    Return whether a task passes the local status and keyword filters.

    The keyword is matched case-insensitively against the title only; an empty
    keyword matches every title.  ``cancelled`` tasks count as ``failed`` so
    they stay reachable from the failed filter.

    :param task: Task snapshot (duck-typed so tests can pass stand-ins).
    :param status: Filter code from :data:`TASK_FILTER_CODES`.
    :param keyword: Case-insensitive title substring.
    :return: ``True`` when the task should be shown.
    """
    code = task_filter_code(status)
    if code != FILTER_ALL and task_status_group(getattr(task, "status", None)) != code:
        return False

    needle = str(keyword or "").strip().casefold()
    if not needle:
        return True
    title = str(getattr(task, "title", "") or "").casefold()
    return needle in title


def filter_tasks(
    tasks: Iterable[_T],
    *,
    status: str = FILTER_ALL,
    keyword: str = "",
) -> list[_T]:
    """
    Filter a task sequence while preserving its order.

    :param tasks: Tasks from a manager snapshot.
    :param status: Filter code from :data:`TASK_FILTER_CODES`.
    :param keyword: Case-insensitive title substring.
    :return: The tasks that match both filters.
    """
    return [
        task
        for task in tasks
        if task_matches_filter(task, status=status, keyword=keyword)
    ]


def task_detail_text(task: object | None) -> str:
    """
    Render the detail pane for the selected task.

    The full error and every exported path are included; the Treeview itself
    only has room for a truncated error column.

    :param task: Task snapshot, or ``None`` when no row is selected.
    :return: Multi-line text for the detail label / popup.
    """
    if task is None:
        return t("Select a task to view its details.")

    lines: list[str] = []
    title = str(getattr(task, "title", "") or "")
    site = str(getattr(task, "site", "") or "")
    if title:
        lines.append(f"{title} ({site})" if site else title)
    lines.append(f"{t('Status')}: {status_label(getattr(task, 'status', None))}")

    error = str(getattr(task, "error", "") or "").strip()
    if error:
        lines.append(f"{t('Error')}: {error}")

    paths = task_export_paths(task)
    if paths:
        rendered = ", ".join(str(path) for path in paths)
        lines.append(f"{t('Exported files')}: {rendered}")

    return "\n".join(lines)


def parse_sites(text: str) -> list[str] | None:
    """
    Split a comma/space separated site list.

    Site keys are registry slugs written in lower case, so the input is folded
    to lower case as well; this makes ``"Qidian"`` behave like ``"qidian"`` and
    lets differently-cased duplicates collapse into one entry.

    :param text: Raw input such as ``"qidian, b520"``.
    :return: De-duplicated, lower-cased site keys, or ``None`` when empty.
    """
    parts = [part.lower() for part in _SITE_SEPARATORS.split(text.strip()) if part]
    unique = list(dict.fromkeys(parts))
    return unique or None


def format_error(exc: BaseException) -> str:
    """
    Render an exception for the status bar and the log.

    The type name is kept next to the message because several worker errors
    (network, parsing, configuration) would otherwise be indistinguishable in
    the status line.

    :param exc: The exception to describe.
    :return: A compact ``"TypeName: message"`` string.
    """
    return f"{type(exc).__name__}: {exc}"


def search_row(result: SearchResult) -> tuple[str, str, str, str, str]:
    """
    Build the Treeview values for one search result.

    :param result: Result returned by :func:`novel_downloader.plugins.search.search`.
    :return: ``(title, author, site, word_count, update_date)``.
    """
    return (
        str(result.get("title", "")),
        str(result.get("author", "")),
        str(result.get("site", "")),
        str(result.get("word_count", "")),
        str(result.get("update_date", "")),
    )


def format_size(size: int) -> str:
    """
    Render a byte count in a human-readable unit.

    :param size: File size in bytes.
    :return: A compact size string such as ``"1.2 MB"``.
    """
    return _format_size(int(size))


def format_mtime(mtime: float) -> str:
    """
    Render a POSIX timestamp as local wall-clock time.

    :param mtime: Modification time.
    :return: ``YYYY-MM-DD HH:MM:SS``.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))


def history_row(item: FileItem) -> tuple[str, str, str]:
    """
    Build the Treeview values for one exported file.

    :param item: File metadata from the history scanner.
    :return: ``(name, size, modified)``.
    """
    return (item.name, format_size(item.size), format_mtime(item.mtime))


def format_duration(seconds: int) -> str:
    """
    Render a track duration as ``m:ss`` or ``h:mm:ss``.

    :param seconds: Duration in seconds; values <= 0 mean unknown.
    :return: A duration string, or ``"-"`` when unknown.
    """
    if seconds <= 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def music_row(track: Any) -> tuple[str, str, str, str]:
    """
    Build the Treeview values for one music track.

    ``track`` is a duck-typed ``novel_downloader.apps.music.providers.Track``;
    the music engine is optional, so no hard import is performed here.

    :param track: Track-like object.
    :return: ``(name, artists, album, duration)``.
    """
    return (
        str(getattr(track, "name", "") or ""),
        str(getattr(track, "artists", "") or ""),
        str(getattr(track, "album", "") or ""),
        format_duration(int(getattr(track, "duration_s", 0) or 0)),
    )


# ---------------------------------------------------------------------------
# Follow list ("追更") helpers
# ---------------------------------------------------------------------------


def normalize_title(value: object) -> str:
    """
    Normalize a book title for comparison.

    Case and punctuation/spacing differences are folded away so ``"Hello, "
    "World!"`` matches ``"hello world"``; CJK characters and digits are kept.
    Delegates to :func:`novel_downloader.plugins.ranking.normalize_text` so the
    desktop behaves exactly like ``follow check`` on the CLI.

    :param value: Raw title from the watchlist or a search result.
    :return: The comparison key (``""`` when there is nothing to compare).
    """
    return normalize_text(value)


def match_watch_result(
    site: str,
    book_id: str,
    title: str,
    results: Iterable[SearchResult],
) -> SearchResult | None:
    """
    Pick the search result that belongs to one followed book.

    A result from the same site with the exact book id wins; otherwise the
    first same-site result whose normalized title equals the followed title is
    accepted (a site may expose the same book under a different id). An empty
    title disables the fallback, so a nameless entry is never mis-matched, and
    an empty result list always yields ``None``.

    :param site: Source site key of the followed book.
    :param book_id: Site-specific book id of the followed book.
    :param title: Display title of the followed book (may be empty).
    :param results: Results returned by a same-site search.
    :return: The matching result, or ``None`` when nothing matches.
    """
    wanted_site = str(site or "").strip()
    wanted_id = str(book_id or "").strip()

    for result in results:
        if (
            str(result.get("site", "")) == wanted_site
            and str(result.get("book_id", "")) == wanted_id
        ):
            return result

    wanted_title = normalize_title(title)
    if not wanted_title:
        return None
    for result in results:
        if str(result.get("site", "")) != wanted_site:
            continue
        if normalize_title(result.get("title", "")) == wanted_title:
            return result
    return None


def format_follow_updated(raw: str) -> str:
    """
    Render a watchlist ``updated_at`` timestamp as local wall-clock time.

    The store writes UTC ISO-8601 strings; naive timestamps are assumed UTC.
    Unparsable input is shown as-is so hand-edited files stay visible.

    :param raw: Stored timestamp such as ``"2026-01-01T00:00:00+00:00"``.
    :return: ``YYYY-MM-DD HH:MM:SS`` in the local time zone, or the raw text.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def follow_row(entry: object) -> tuple[str, str, str, str, str]:
    """
    Build the Treeview values for one followed book.

    :param entry: A ``WatchEntry`` (duck-typed so tests can pass stand-ins).
    :return: ``(site, book_id, title, last_chapter, updated)``.
    """
    return (
        str(getattr(entry, "site", "") or ""),
        str(getattr(entry, "book_id", "") or ""),
        str(getattr(entry, "title", "") or ""),
        str(getattr(entry, "last_chapter", "") or ""),
        format_follow_updated(str(getattr(entry, "updated_at", "") or "")),
    )
