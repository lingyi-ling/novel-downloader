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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.apps.common.history_files import FileItem, _format_size
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.infra.i18n import t
from novel_downloader.schemas import SearchResult

__all__ = [
    "TaskOutputActions",
    "format_duration",
    "format_error",
    "format_mtime",
    "format_size",
    "format_task_progress",
    "history_row",
    "music_row",
    "parse_sites",
    "search_row",
    "status_label",
    "task_detail_text",
    "task_export_paths",
    "task_output_actions",
    "task_row",
]

_SITE_SEPARATORS = re.compile(r"[,，;；\s]+")

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


def task_export_paths(task: object) -> tuple[Path, ...]:
    """
    Return the exported file paths of a task in display order.

    ``epub`` and ``txt`` are preferred because those are the formats the
    history page lists; any other exported format follows in sorted order.
    Duplicate paths are dropped so the details line does not repeat itself.

    :param task: Task snapshot (duck-typed so tests can pass stand-ins).
    :return: An ordered tuple of paths (empty when nothing was exported).
    """
    exported = getattr(task, "exported_paths", None)
    if not isinstance(exported, Mapping) or not exported:
        return ()

    keys: list[Any] = [key for key in _EXPORT_PRIORITY if key in exported]
    keys.extend(
        sorted((key for key in exported if key not in _EXPORT_PRIORITY), key=str)
    )

    paths: list[Path] = []
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
        paths.append(path)
    return tuple(paths)


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
