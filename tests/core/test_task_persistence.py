#!/usr/bin/env python3
"""Queue persistence: a restart must not lose the pending queue."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import TaskManager


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


@pytest.fixture(autouse=True)
def _no_real_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every worker parks instead of touching a site client."""

    async def _idle(self: TaskManager, site: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(TaskManager, "_site_worker", _idle)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "tasks.json"


@pytest.mark.asyncio
async def test_pending_queue_survives_a_restart(tmp_path: Path) -> None:
    path = _path(tmp_path)
    first = TaskManager(state_path=path)
    await first.start()
    await first.add_task(title="Book A", site="demo", book_id="1")
    await first.add_task(title="Book B", site="demo", book_id="2")
    await first._flush_now()
    await first.close()

    assert path.is_file()

    second = TaskManager(state_path=path)
    await second.start()
    snapshot = second.snapshot()
    await second.close()

    assert [task.book_id for task in snapshot["pending"]] == ["1", "2"]
    assert all(task.status is Status.QUEUED for task in snapshot["pending"])


@pytest.mark.asyncio
async def test_an_interrupted_task_comes_back_as_queued(tmp_path: Path) -> None:
    path = _path(tmp_path)
    first = TaskManager(state_path=path)
    await first.start()
    task = await first.add_task(title="Book A", site="demo", book_id="1")
    task.status = Status.RUNNING
    task.chapters_total = 40
    task.chapters_done = 12
    await first._flush_now()
    await first.close()

    second = TaskManager(state_path=path)
    await second.start()
    [restored] = second.snapshot()["pending"]
    await second.close()

    assert restored.status is Status.QUEUED
    assert restored.chapters_total == 40
    assert restored.chapters_done == 12


@pytest.mark.asyncio
async def test_completed_history_survives_a_restart(tmp_path: Path) -> None:
    path = _path(tmp_path)
    first = TaskManager(state_path=path)
    await first.start()
    done = DownloadTask(title="Done", site="demo", book_id="9")
    done.status = Status.COMPLETED
    done.chapters_total = 10
    done.chapters_done = 10
    first._append_completed(done)
    await first._flush_now()
    await first.close()

    second = TaskManager(state_path=path)
    await second.start()
    completed = second.snapshot()["completed"]
    await second.close()

    assert [(task.book_id, task.status) for task in completed] == [
        ("9", Status.COMPLETED)
    ]


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path: Path) -> None:
    path = _path(tmp_path)
    manager = TaskManager(state_path=path)
    await manager.start()
    await manager.add_task(title="A", site="demo", book_id="1")
    await manager._flush_now()

    await manager.start()
    await manager.start()
    pending = manager.snapshot()["pending"]
    await manager.close()

    assert len(pending) == 1


@pytest.mark.asyncio
async def test_a_corrupt_file_is_ignored(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    manager = TaskManager(state_path=path)
    await manager.start()
    assert manager.snapshot()["pending"] == []
    await manager.close()


@pytest.mark.asyncio
async def test_an_unknown_schema_is_ignored(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 999, "pending": [{"site": "x", "book_id": "1"}]}),
        encoding="utf-8",
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    assert manager.snapshot()["pending"] == []
    await manager.close()


@pytest.mark.asyncio
async def test_entries_missing_site_or_book_id_are_skipped(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending": [
                    {"site": "", "book_id": "1"},
                    {"site": "demo"},
                    {"site": "demo", "book_id": "2", "title": "ok"},
                ],
            }
        ),
        encoding="utf-8",
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    pending = manager.snapshot()["pending"]
    await manager.close()

    assert [task.book_id for task in pending] == ["2"]


@pytest.mark.asyncio
async def test_restore_skips_ids_that_would_escape_the_raw_data_tree(
    tmp_path: Path,
) -> None:
    """A hand-edited/corrupted queue must not become an arbitrary-write path.

    ``site``/``book_id`` become directory names under ``raw_data/`` when the
    client runs.  The REST/embedded/CLI entry points validate them, but the
    persisted queue is also read verbatim at startup, so a ``..`` segment or
    an absolute path there must be dropped just like a bad request.
    """
    path = _path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending": [
                    {"site": "demo", "book_id": "../escape"},
                    {"site": "demo", "book_id": "C:\\escape"},
                    {"site": "demo", "book_id": "/etc"},
                    {"site": "..", "book_id": "1"},
                    {"site": "demo", "book_id": "safe-id"},
                ],
            }
        ),
        encoding="utf-8",
    )

    manager = TaskManager(state_path=path)
    await manager.start()
    pending = manager.snapshot()["pending"]
    await manager.close()

    assert [task.book_id for task in pending] == ["safe-id"]


@pytest.mark.asyncio
async def test_add_task_rejects_an_escaping_book_id(tmp_path: Path) -> None:
    """The desktop bridge is not the only caller; the queue itself must check."""
    path = _path(tmp_path)
    manager = TaskManager(state_path=path)
    await manager.start()

    with pytest.raises(ValueError):
        await manager.add_task(title="Bad", site="demo", book_id="../escape")
    with pytest.raises(ValueError):
        await manager.add_task(title="Bad", site="demo", book_id="C:\\escape")
    with pytest.raises(ValueError):
        await manager.add_task(title="Bad", site="../demo", book_id="1")

    assert manager.snapshot()["pending"] == []
    await manager.close()


@pytest.mark.asyncio
async def test_persistence_is_off_without_a_path(tmp_path: Path) -> None:
    manager = TaskManager()
    await manager.start()
    await manager.add_task(title="A", site="demo", book_id="1")
    await manager.close()

    assert list((tmp_path / "home").glob("tasks.json")) == []
    assert manager._state_path is None


@pytest.mark.asyncio
async def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = _path(tmp_path)
    manager = TaskManager(state_path=path)
    await manager.start()
    await manager.add_task(title="A", site="demo", book_id="1")
    await manager._flush_now()
    await manager.close()

    leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
