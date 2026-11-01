#!/usr/bin/env python3
"""
novel_downloader.apps.core.models
--------------------------------
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from statistics import fmean
from uuid import uuid4

from novel_downloader.schemas import LoginField


class Status(str, Enum):  # noqa: UP042 - keep the explicit ``str`` mixin
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


def error_text(error: BaseException) -> str:
    """
    Return ``str(error)``, falling back to the exception type name.

    Several transport errors (a bare ``TimeoutError``,
    ``ConnectionResetError`` without arguments, ...) stringify to ``""``;
    recording that on a task would blank out the failure detail shown to
    the user.
    """
    text = str(error)
    return text if text else type(error).__name__


class TaskError(Exception):
    """Base class for a failure surfaced while running a download task."""


class RetryableTaskError(TaskError):
    """
    A transient failure (network/timeout) worth another attempt.

    Clients or adapters may raise this to opt an otherwise unclassified error
    into the task manager's retry policy.
    """


class NonRetryableTaskError(TaskError):
    """
    A permanent failure (auth/permission/business rule) that must not be retried.

    Raising this always stops the retry loop, regardless of the configured
    retry budget.
    """


@dataclass
class CredRequest:
    task_id: str
    title: str
    fields: list[LoginField]
    prefill: dict[str, str] = field(default_factory=dict)

    # runtime fields
    req_id: str = field(default_factory=lambda: uuid4().hex)
    event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    result: dict[str, str] | None = None

    # claim info (times use time.monotonic() seconds)
    claimed_by: str | None = None
    claimed_at: float | None = None

    # lifecycle
    done: bool = False


@dataclass
class DownloadTask:
    title: str
    site: str
    book_id: str

    # runtime state
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: Status = Status.QUEUED
    chapters_total: int = 0
    chapters_done: int = 0
    error: str | None = None
    #: Number of extra attempts already spent on transient failures (0 means the
    #: task succeeded or failed on its first attempt).
    retry_count: int = 0
    #: Export format currently being written (``"txt"``, ``"epub"``, ...), or
    #: ``None`` while no export format has started. Updated by the export UI
    #: adapter's ``on_start``; informational only -- a consumer may render it
    #: (e.g. ``Exporting (txt) ...``) while the status is ``EXPORTING``.
    export_stage: str | None = None
    exported_paths: dict[str, Path] = field(default_factory=dict)

    # lifecycle (wall-clock times)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)

    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=20), repr=False)
    _last_ts: float = field(default_factory=time.monotonic, repr=False)

    def progress(self) -> float:
        return (
            0.0
            if self.chapters_total <= 0
            else round(self.chapters_done / self.chapters_total, 2)
        )

    def record_chapter_time(self) -> None:
        """Record elapsed time for one finished chapter."""
        now = time.monotonic()
        dt = now - self._last_ts
        self._last_ts = now
        if 0 < dt < 120:
            self._recent.append(dt)

    def eta(self) -> float | None:
        """Return ETA in seconds if estimable, else None."""
        if self.chapters_total <= 0 or self.chapters_done >= self.chapters_total:
            return None
        if not self._recent:
            return None
        remaining = self.chapters_total - self.chapters_done
        return fmean(self._recent) * remaining
