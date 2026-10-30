#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.notifications
-------------------------------------------

Task-completion notification rules for the Tkinter desktop client.

The transition rules are pure so they can be exercised without Tk or a sound
device, and they mirror the Android client's ``TaskEvents`` contract:

* a task notifies only when it moves from a non-terminal status to
  ``completed`` or ``failed``,
* the first time a task is observed it only becomes the baseline (its earlier
  history is unknown, so a task that was already finished when the window
  opened must not notify),
* ``cancelled`` is terminal but never notifies by itself,
* terminal -> terminal transitions never notify twice,
* a task listed twice in one snapshot notifies once.

The audible part is deliberately low-tech: :func:`play_notification_sound`
uses ``winsound.MessageBeep`` (standard library, asynchronous, never blocks
the Tk event loop) instead of shelling out to a PowerShell Windows-Runtime
toast, which would spawn a process per notification, need WinRT type loading
and fail in unpredictable ways across Windows builds.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.infra.i18n import t

__all__ = [
    "TaskNotification",
    "TaskOutcome",
    "detect_task_notifications",
    "flatten_task_snapshot",
    "is_terminal_status",
    "play_notification_sound",
    "snapshot_task_states",
    "task_event_status_message",
]

logger = logging.getLogger(__name__)

#: Statuses a download task cannot leave on its own.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class TaskOutcome(Enum):
    """How a notified task finished."""

    COMPLETED = "completed"
    FAILED = "failed"


#: Status values that deserve a notification, mapped to their outcome.
_NOTIFIED_OUTCOMES: dict[str, TaskOutcome] = {
    "completed": TaskOutcome.COMPLETED,
    "failed": TaskOutcome.FAILED,
}


@dataclass(frozen=True)
class TaskNotification:
    """One task that just reached a notifiable terminal status."""

    task_id: str
    title: str
    outcome: TaskOutcome
    error: str = ""


def _status_value(status: object) -> str:
    """Read the raw status string from a ``Status`` member or a plain value."""
    return str(getattr(status, "value", status))


def is_terminal_status(status: object) -> bool:
    """
    Whether a status is terminal (the task will not run again).

    :param status: A :class:`Status` member or its string value.
    :return: ``True`` for ``completed``, ``failed`` and ``cancelled``.
    """
    return _status_value(status).strip().lower() in _TERMINAL_STATUSES


def flatten_task_snapshot(
    snapshot: Mapping[str, Iterable[DownloadTask]],
) -> list[DownloadTask]:
    """
    Flatten the manager snapshot's sections into one de-duplicated task list.

    The manager reports ``running``, ``pending`` and ``completed`` separately;
    a task object may only appear once, but the de-duplication keeps the
    detection below safe even if that invariant ever changes.

    :param snapshot: A :meth:`TaskManager.snapshot` result.
    :return: Tasks in section order, first occurrence wins per task id.
    """
    tasks: dict[str, DownloadTask] = {}
    for section in ("running", "pending", "completed"):
        section_tasks: Iterable[DownloadTask] = snapshot.get(section, ())
        for task in section_tasks:
            tasks.setdefault(str(task.task_id), task)
    return list(tasks.values())


def snapshot_task_states(tasks: Iterable[DownloadTask]) -> dict[str, str]:
    """
    Build the ``task_id -> status`` baseline used by the next poll.

    :param tasks: Tasks from a manager snapshot.
    :return: The mapping to keep between two snapshots.
    """
    return {str(task.task_id): _status_value(task.status) for task in tasks}


def detect_task_notifications(
    previous: Mapping[str, str],
    tasks: Iterable[DownloadTask],
) -> list[TaskNotification]:
    """
    Return the transitions from ``previous`` to ``tasks`` that should notify.

    A task notifies when its previous status is known, non-terminal, and its
    current status is ``completed`` or ``failed``.  Unknown ids, terminal
    previous statuses and ``cancelled`` completions never notify.

    :param previous: The ``task_id -> status`` mapping of the previous poll.
    :param tasks: Tasks from the current snapshot (de-duplicated by id).
    :return: One notification per transition, in task order.
    """
    events: list[TaskNotification] = []
    seen: set[str] = set()
    for task in tasks:
        task_id = str(task.task_id)
        if task_id in seen:
            continue
        seen.add(task_id)
        outcome = _NOTIFIED_OUTCOMES.get(_status_value(task.status))
        if outcome is None:
            continue
        before = previous.get(task_id)
        # A first observation only establishes the baseline: the task may have
        # finished before the window opened, so its history is unknown.
        if before is None or is_terminal_status(before):
            continue
        events.append(
            TaskNotification(
                task_id=task_id,
                title=str(task.title),
                outcome=outcome,
                error=str(task.error or ""),
            )
        )
    return events


def task_event_status_message(events: Sequence[TaskNotification]) -> str:
    """
    Render the status-bar line for one batch of task notifications.

    A single event renders its own line; several events are joined with a
    language-neutral separator, so no untranslated sentence is ever needed.

    :param events: The notifications raised by one snapshot transition.
    :return: A localized one-line summary (``""`` for no events).
    """
    return " | ".join(_notification_line(event) for event in events)


def _notification_line(event: TaskNotification) -> str:
    """The localized line for a single task notification."""
    if event.outcome is TaskOutcome.COMPLETED:
        return t("Download completed: {book}").format(book=event.title)
    return t("Download failed for {book}: {error}").format(
        book=event.title, error=event.error.strip() or t("Failed")
    )


def play_notification_sound() -> None:
    """
    Play the system notification sound (Windows; a no-op elsewhere).

    ``winsound`` ships with CPython on Windows and ``MessageBeep`` plays the
    system sound asynchronously, so the Tk event loop is never blocked.  Any
    failure (missing audio device, ...) is swallowed: a silent machine must
    not break the polling loop.
    """
    try:
        import winsound
    except ImportError:  # pragma: no cover - non-Windows platforms
        return
    try:
        winsound.MessageBeep()
    except Exception:  # noqa: BLE001 - a sound problem is never fatal
        logger.exception("Failed to play the task notification sound")
