#!/usr/bin/env python3
"""Crash-consistency tests for the persisted task queue.

These tests inject the failures a real shutdown produces:

* ``close()`` running before (or concurrently with) ``start()`` -- the final
  flush must never replace a persisted queue with an empty snapshot, and a
  worker spawned by the concurrent start must not outlive ``close()``;
* two flushes in flight at once (the background flusher plus ``close()``) --
  an older snapshot must never overwrite a newer one;
* a half-written / hand-edited ``tasks.json`` -- it must not crash ``start()``,
  duplicate tasks, or re-run chapters that already finished;
* two enqueues of the same book at the same time -- the queue must stay
  idempotent instead of downloading the book twice.

No worker reaches the network: ``_site_worker`` is replaced with a coroutine
that parks until the manager is closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import TaskManager


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


@pytest.fixture(autouse=True)
def _idle_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every worker parks instead of touching a site client."""

    async def _idle(self: TaskManager, site: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(TaskManager, "_site_worker", _idle)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "tasks.json"


def _seed(path: Path, pending: list[dict[str, Any]], **extra: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "pending": pending, **extra}, ensure_ascii=False),
        encoding="utf-8",
    )


def _entry(book_id: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "task_id": f"t-{book_id}",
        "title": f"Book {book_id}",
        "site": "demo",
        "book_id": book_id,
        "status": "running",
    }
    entry.update(overrides)
    return entry


def _pending_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [entry["book_id"] for entry in payload.get("pending", [])]


async def _wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# close() / start() ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_without_start_keeps_the_persisted_queue(tmp_path: Path) -> None:
    """Shutting down before start() must not wipe the saved queue."""
    path = _path(tmp_path)
    _seed(path, [_entry("1")])

    manager = TaskManager(state_path=path)
    await manager.close()

    assert _pending_ids(path) == ["1"]


@pytest.mark.asyncio
async def test_a_start_racing_close_cannot_leave_a_worker_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() and start() are serialised; the final write is the newest one."""
    path = _path(tmp_path)
    _seed(path, [_entry("1")])

    gate = threading.Event()
    real_write = TaskManager._write_payload

    def _blocked_write(self: TaskManager, payload: dict[str, Any], *args: Any) -> None:
        # Hold the first (earliest) write until the concurrent start() ran.
        gate.wait(5)
        real_write(self, payload, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskManager, "_write_payload", _blocked_write)

    manager = TaskManager(state_path=path)
    closer = asyncio.create_task(manager.close())
    await asyncio.sleep(0.05)  # close() is now inside the file write

    await manager.start()
    gate.set()
    await asyncio.wait_for(closer, timeout=5)

    assert manager._worker_tasks == {}
    assert manager._flush_task is None
    assert manager._closed is True
    assert _pending_ids(path) == ["1"]


# ---------------------------------------------------------------------------
# concurrent flushes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_snapshot_cannot_overwrite_a_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping flushes must leave the newest queue on disk."""
    path = _path(tmp_path)
    manager = TaskManager(state_path=path)
    await manager.start()

    first_started = threading.Event()
    release_first = threading.Event()
    real_write = TaskManager._write_payload

    def _gated_write(self: TaskManager, payload: dict[str, Any], *args: Any) -> None:
        if not first_started.is_set():
            first_started.set()
            assert release_first.wait(5)
        real_write(self, payload, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(TaskManager, "_write_payload", _gated_write)

    await manager.add_task(title="A", site="demo", book_id="1")
    older = asyncio.create_task(manager._flush_now())
    await _wait_until(first_started.is_set)

    await manager.add_task(title="B", site="demo", book_id="2")
    await manager._flush_now()

    release_first.set()
    await asyncio.wait_for(older, timeout=5)

    assert sorted(_pending_ids(path)) == ["1", "2"]
    leftovers = [
        item.name
        for item in path.parent.iterdir()
        if item.name not in {path.name, path.name + ".corrupt"}
    ]
    assert leftovers == []
    await manager.close()


@pytest.mark.asyncio
async def test_a_transient_write_error_keeps_the_queue_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed flush must be retried instead of silently forgetting the queue."""
    path = _path(tmp_path)
    manager = TaskManager(state_path=path)
    await manager.start()
    await manager.add_task(title="A", site="demo", book_id="1")

    def _boom(self: TaskManager, payload: dict[str, Any], *args: Any) -> None:
        raise OSError("file is locked by another process")

    monkeypatch.setattr(TaskManager, "_write_payload", _boom)
    await manager._flush_now()

    assert not path.exists()
    assert manager._dirty is True

    await manager.close()


# ---------------------------------------------------------------------------
# restore hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_task_id_is_restored_once(tmp_path: Path) -> None:
    path = _path(tmp_path)
    entry = _entry("1", task_id="dup")
    _seed(path, [entry, dict(entry)])

    manager = TaskManager(state_path=path)
    await manager.start()
    pending = manager.snapshot()["pending"]
    await manager.close()

    assert [task.book_id for task in pending] == ["1"]


@pytest.mark.asyncio
async def test_a_terminal_status_in_pending_is_not_requeued(tmp_path: Path) -> None:
    """A crash after a task finished must not download the book again."""
    path = _path(tmp_path)
    _seed(
        path,
        [
            _entry("1", task_id="done", status="completed"),
            _entry("2", task_id="bad", status="failed"),
            _entry("3", task_id="gone", status="cancelled"),
            _entry("4", task_id="running", status="running"),
        ],
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    snapshot = manager.snapshot()
    await manager.close()

    assert [task.book_id for task in snapshot["pending"]] == ["4"]
    assert {(task.book_id, task.status) for task in snapshot["completed"]} == {
        ("1", Status.COMPLETED),
        ("2", Status.FAILED),
        ("3", Status.CANCELLED),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "completed"),
    [(7, "nope"), ("oops", {"x": 1}), ({"a": 1}, 3)],
)
async def test_a_non_list_section_does_not_crash_start(
    tmp_path: Path, pending: Any, completed: Any
) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 1, "pending": pending, "completed": completed},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    assert manager.snapshot()["pending"] == []
    await manager.close()


@pytest.mark.asyncio
async def test_a_garbled_entry_field_is_repaired_not_fatal(tmp_path: Path) -> None:
    path = _path(tmp_path)
    _seed(path, [_entry("1", retry_count="lots", chapters_total="x", chapters_done=4)])

    manager = TaskManager(state_path=path)
    await manager.start()
    [restored] = manager.snapshot()["pending"]
    await manager.close()

    assert restored.retry_count == 0
    assert restored.chapters_total == 0
    assert restored.chapters_done == 4


@pytest.mark.asyncio
async def test_a_corrupt_file_is_quarantined(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    manager = TaskManager(state_path=path)
    await manager.start()
    assert manager.snapshot()["pending"] == []

    quarantine = path.with_name(path.name + ".corrupt")
    assert quarantine.is_file()
    assert quarantine.read_text(encoding="utf-8") == "{not json"
    await manager.close()


@pytest.mark.asyncio
async def test_an_unknown_schema_is_quarantined(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 999, "pending": [_entry("1")]}), encoding="utf-8"
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    assert manager.snapshot()["pending"] == []

    assert path.with_name(path.name + ".corrupt").is_file()
    await manager.close()


# ---------------------------------------------------------------------------
# duplicate enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueuing_the_same_active_book_twice_is_idempotent() -> None:
    manager = TaskManager()
    await manager.start()

    first = await manager.add_task(title="A", site="demo", book_id="1")
    again = await manager.add_task(title="A (again)", site="demo", book_id="1")

    assert again is first
    assert manager.pending["demo"] == [first]
    assert len(manager.snapshot()["pending"]) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_concurrent_enqueues_of_one_book_create_one_task() -> None:
    manager = TaskManager()
    await manager.start()

    first, second = await asyncio.gather(
        manager.add_task(title="A", site="demo", book_id="1"),
        manager.add_task(title="A", site="demo", book_id="1"),
    )

    assert first is second
    assert len(manager.snapshot()["pending"]) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_a_finished_task_can_be_enqueued_again() -> None:
    """Idempotence only covers *active* tasks: a re-download stays possible."""
    manager = TaskManager()
    await manager.start()

    done = DownloadTask(title="A", site="demo", book_id="1")
    done.status = Status.COMPLETED
    manager._append_completed(done)

    fresh = await manager.add_task(title="A", site="demo", book_id="1")
    assert fresh is not done
    assert manager.pending["demo"] == [fresh]
    await manager.close()
