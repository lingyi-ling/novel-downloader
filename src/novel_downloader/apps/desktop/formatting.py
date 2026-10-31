#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.formatting
----------------------------------------

Pure display helpers shared by the pywebview bridge and the tests.

Deliberately free of GUI imports: the browser front end formats most values
itself, so this module only carries the pieces the Python side needs (status
labels, error rendering, retry eligibility and follow-list timestamps).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from novel_downloader.infra.i18n import t

__all__ = [
    "RetryParams",
    "format_error",
    "format_follow_updated",
    "retry_params",
    "status_label",
]

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

#: Terminal states from which a task may be re-queued.
_RETRYABLE_STATES = frozenset({"failed", "cancelled"})


@dataclass(frozen=True)
class RetryParams:
    """The arguments needed to queue a task again."""

    title: str
    site: str
    book_id: str


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
