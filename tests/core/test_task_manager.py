#!/usr/bin/env python3
"""
tests.core.test_task_manager
----------------------------

Offline unit tests for the in-process task manager and its data model.

No worker is allowed to touch the network: the tests either populate the
manager's dictionaries directly (pure bookkeeping) or replace
``TaskManager._site_worker`` with a local coroutine, so ``add_task`` never
reaches a real site client.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import MAX_COMPLETED_TASKS, TaskManager


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any lazy config/state lookup outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


@pytest.fixture
def manager() -> TaskManager:
    return TaskManager()


def _task(site: str, book_id: str) -> DownloadTask:
    return DownloadTask(title=f"Book {book_id}", site=site, book_id=book_id)


# ---------------------------------------------------------------------------
# Status / DownloadTask maths
# ---------------------------------------------------------------------------


def test_status_values_are_stable() -> None:
    assert {status.value for status in Status} == {
        "queued",
        "running",
        "processing",
        "exporting",
        "completed",
        "cancelled",
        "failed",
    }
    # ``str`` mixin keeps the enum serialisable as a plain string.
    assert Status.QUEUED == "queued"
    assert Status.FAILED == "failed"


def test_new_task_defaults() -> None:
    first = _task("demo", "1")
    second = _task("demo", "2")

    assert first.status is Status.QUEUED
    assert first.chapters_total == 0
    assert first.chapters_done == 0
    assert first.exported_paths == {}
    assert first.finished_at is None
    assert first.asyncio_task is None
    assert first.task_id != second.task_id


def test_progress_handles_empty_and_partial() -> None:
    task = _task("demo", "1")

    assert task.progress() == 0.0

    task.chapters_total = 3
    task.chapters_done = 1
    assert task.progress() == 0.33

    task.chapters_done = 3
    assert task.progress() == 1.0

    task.chapters_total = -5
    assert task.progress() == 0.0


def test_eta_returns_none_without_estimate() -> None:
    task = _task("demo", "1")

    # No chapter count at all.
    assert task.eta() is None

    # Count known but no timings recorded yet.
    task.chapters_total = 10
    task.chapters_done = 2
    assert task.eta() is None

    # Already finished.
    task.chapters_total = 2
    task.chapters_done = 2
    assert task.eta() is None


def test_eta_uses_mean_of_recent_timings() -> None:
    task = _task("demo", "1")
    task.chapters_total = 10
    task.chapters_done = 4
    task._recent.extend([1.0, 3.0])

    # mean(1, 3) * remaining(6) == 12
    assert task.eta() == pytest.approx(12.0)


def test_record_chapter_time_ignores_long_gaps() -> None:
    task = _task("demo", "1")

    task._last_ts = time.monotonic() - 2
    task.record_chapter_time()
    assert len(task._recent) == 1
    assert 1.5 <= task._recent[0] < 60

    # Gaps of two minutes or more are treated as idle time, not a chapter.
    task._last_ts = time.monotonic() - 200
    task.record_chapter_time()
    assert len(task._recent) == 1


# ---------------------------------------------------------------------------
# snapshot() bookkeeping
# ---------------------------------------------------------------------------


def test_snapshot_partitions_state_and_preserves_order(
    manager: TaskManager,
) -> None:
    a1, a2, b1 = _task("a", "1"), _task("a", "2"), _task("b", "3")
    r1, r2 = _task("a", "9"), _task("b", "8")
    c1, c2 = _task("b", "7"), _task("a", "6")

    manager.pending["a"].extend([a1, a2])
    manager.pending["b"].append(b1)
    manager.running["a"] = r1
    manager.running["b"] = r2
    manager.completed.extend([c1, c2])

    snapshot = manager.snapshot()

    assert snapshot["running"] == [r1, r2]
    # Sites keep insertion order and each site's queue keeps FIFO order.
    assert snapshot["pending"] == [a1, a2, b1]
    assert snapshot["completed"] == [c1, c2]


def test_snapshot_returns_copies(manager: TaskManager) -> None:
    pending = _task("a", "1")
    running = _task("a", "2")
    completed = _task("a", "3")
    manager.pending["a"].append(pending)
    manager.running["a"] = running
    manager.completed.append(completed)

    snapshot = manager.snapshot()
    snapshot["pending"].clear()
    snapshot["running"].clear()
    snapshot["completed"].clear()

    assert manager.pending["a"] == [pending]
    assert manager.running["a"] is running
    assert list(manager.completed) == [completed]


def test_snapshot_of_empty_manager(manager: TaskManager) -> None:
    assert manager.snapshot() == {"running": [], "pending": [], "completed": []}


def test_completed_queue_is_truncated_to_limit(manager: TaskManager) -> None:
    overflow = 5
    for index in range(MAX_COMPLETED_TASKS + overflow):
        manager.completed.append(_task("s", str(index)))

    assert len(manager.completed) == MAX_COMPLETED_TASKS
    # The oldest entries are dropped, not the newest ones.
    assert manager.completed[0].book_id == str(overflow)
    assert manager.completed[-1].book_id == str(MAX_COMPLETED_TASKS + overflow - 1)
    assert len(manager.snapshot()["completed"]) == MAX_COMPLETED_TASKS


# ---------------------------------------------------------------------------
# add_task / cancel_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_task_queues_without_starting_a_real_worker(
    manager: TaskManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def fake_site_worker(self: TaskManager, site: str) -> None:
        await release.wait()

    monkeypatch.setattr(TaskManager, "_site_worker", fake_site_worker)

    first = await manager.add_task(title="A", site="a", book_id="1")
    second = await manager.add_task(title="B", site="a", book_id="2")
    third = await manager.add_task(title="C", site="b", book_id="3")

    assert manager.pending["a"] == [first, second]
    assert manager.pending["b"] == [third]
    assert {first.status, second.status, third.status} == {Status.QUEUED}
    # One worker per site, reused while it is still alive.
    assert set(manager._worker_tasks) == {"a", "b"}

    release.set()
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_false(manager: TaskManager) -> None:
    task = _task("a", "1")
    manager.pending["a"].append(task)

    assert await manager.cancel_task("does-not-exist") is False
    assert task.status is Status.QUEUED
    assert manager.pending["a"] == [task]


@pytest.mark.asyncio
async def test_cancel_pending_task_moves_it_to_completed(
    manager: TaskManager,
) -> None:
    task = _task("a", "1")
    manager.pending["a"].extend([task, _task("a", "2")])

    assert await manager.cancel_task(task.task_id) is True

    assert [pending.book_id for pending in manager.pending["a"]] == ["2"]
    assert task.status is Status.CANCELLED
    assert isinstance(task.finished_at, float)
    assert list(manager.completed) == [task]


@pytest.mark.asyncio
async def test_cancel_running_task_cancels_asyncio_task(
    manager: TaskManager,
) -> None:
    task = _task("a", "1")

    async def forever() -> None:
        await asyncio.Event().wait()

    handle = asyncio.create_task(forever())
    task.asyncio_task = handle
    manager.running["a"] = task

    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED
    assert isinstance(task.finished_at, float)

    with pytest.raises(asyncio.CancelledError):
        await handle
    assert handle.cancelled()
