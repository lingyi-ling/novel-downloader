#!/usr/bin/env python3
"""
tests.core.test_export_outcome
------------------------------

Offline tests for the final status a download task ends up with after its
export phase.

The export client reports per-format outcomes through the ``ExportUI``
callbacks and returns normally even when every format failed, so the export
worker must translate those outcomes into the final status:

* every format failed               -> FAILED (was wrongly COMPLETED before);
* some formats failed, some exported -> COMPLETED with the failure kept in
  ``task.error``;
* every format unsupported          -> FAILED;
* a no-op export without callbacks  -> COMPLETED (historical contract).

Three layers are exercised without touching the network:

* the real ``WebExportUI`` adapter and its run-level outcome flags;
* the real ``CommonClient.export_book`` per-format error containment;
* the real ``TaskManager`` export worker, driven by client doubles that fire
  the same callbacks (following the convention of
  ``tests.core.test_task_manager_robustness``).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.core.adapters import WebExportUI
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import TaskManager
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.schemas import BookConfig, ExporterConfig

BOOK = BookConfig(book_id="42")
OUT_DIR = Path("downloads") / "demo"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any lazy config/state lookup outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


# ---------------------------------------------------------------------------
# WebExportUI run-level outcome flags
# ---------------------------------------------------------------------------


def _task() -> DownloadTask:
    return DownloadTask(title="Demo Book", site="demo", book_id="42")


def test_export_ui_starts_with_clear_outcome_flags() -> None:
    task = _task()
    task.exported_paths = {"txt": OUT_DIR / "old.txt"}  # stale from a prior run
    ui = WebExportUI(task)

    assert ui.exported_any is False
    assert ui.unsupported_any is False
    assert task.exported_paths == {}


def test_export_ui_flags_track_success_and_unsupported() -> None:
    ui = WebExportUI(_task())

    ui.on_error(BOOK, "txt", RuntimeError("disk full"))
    assert ui.exported_any is False  # an error alone never counts as success
    assert ui.unsupported_any is False

    ui.on_unsupported(BOOK, "mobi")
    assert ui.unsupported_any is True
    assert ui.exported_any is False

    ui.on_success(BOOK, "epub", OUT_DIR / "book.epub")
    assert ui.exported_any is True
    assert ui.unsupported_any is True  # unchanged by the later success


# ---------------------------------------------------------------------------
# CommonClient.export_book: per-format error containment (client contract)
# ---------------------------------------------------------------------------


class _RecorderUI:
    """``ExportUI`` double recording every callback it receives."""

    def __init__(self) -> None:
        self.started: list[str | None] = []
        self.succeeded: list[tuple[str, Path]] = []
        self.errors: list[tuple[str | None, str]] = []
        self.unsupported: list[str] = []

    def on_start(self, book: BookConfig, fmt: str | None = None) -> None:
        self.started.append(fmt)

    def on_success(self, book: BookConfig, fmt: str, path: Path) -> None:
        self.succeeded.append((fmt, path))

    def on_error(self, book: BookConfig, fmt: str | None, error: Exception) -> None:
        self.errors.append((fmt, str(error)))

    def on_unsupported(self, book: BookConfig, fmt: str) -> None:
        self.unsupported.append(fmt)


def _bare_client(
    **format_funcs: Callable[[Any, Any, str | None], list[Path]],
) -> CommonClient:
    """
    Build a ``CommonClient`` without running ``__init__`` and shadow the
    named ``_export_book_<fmt>`` mixin methods with the given fakes, so the
    export loop never touches real data.
    """
    client = object.__new__(CommonClient)
    for name, func in format_funcs.items():
        setattr(client, f"_export_book_{name}", func)
    return client


def test_export_book_contains_per_format_errors_and_keeps_going() -> None:
    """A failing format is reported via on_error; the client never raises."""

    def broken_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        raise RuntimeError("disk full")

    client = _bare_client(txt=broken_txt)
    ui = _RecorderUI()

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["txt", "pdf"], ui=ui
    )

    assert results == {"txt": [], "pdf": []}
    assert ui.errors == [("txt", "disk full")]
    assert ui.unsupported == ["pdf"]
    assert ui.succeeded == []
    # Unsupported formats never start; supported ones do.
    assert ui.started == ["txt"]


def test_export_book_collects_successes_alongside_unsupported() -> None:
    out = OUT_DIR / "book.txt"

    def good_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        return [out]

    client = _bare_client(txt=good_txt)
    ui = _RecorderUI()

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["txt", "pdf"], ui=ui
    )

    assert results == {"txt": [out], "pdf": []}
    assert ui.succeeded == [("txt", out)]
    assert ui.errors == []
    assert ui.unsupported == ["pdf"]


# ---------------------------------------------------------------------------
# TaskManager._export_worker: the final-status decision table
# ---------------------------------------------------------------------------


class _ExportClient:
    """
    Site client double: a no-op download followed by an export that drives
    the real ``WebExportUI`` callbacks through ``export``.
    """

    def __init__(self, export: Callable[[Any, Any], None] | None = None) -> None:
        self._export = export

    async def __aenter__(self) -> _ExportClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        return True

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        return None

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        if self._export is not None:
            self._export(book, ui)
        return {}


class _NoStagesAdapter:
    """Config double: no login, no processors, default exporter."""

    def get_login_required(self, site: str) -> bool:
        return False

    def get_login_config(self, site: str) -> dict[str, Any]:
        return {}

    def get_processor_configs(self, site: str) -> list[Any]:
        return []

    def get_exporter_config(self, site: str) -> None:
        return None

    def get_client_config(self, site: str) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}


def _manager(client: Any) -> TaskManager:
    """Build a manager wired to fakes (never touches registrar or config)."""
    manager = TaskManager()
    manager._adapter = _NoStagesAdapter()  # type: ignore[assignment]
    manager._clients["demo"] = client  # type: ignore[assignment]
    return manager


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until it is true, yielding to the event loop."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


async def _wait_export_done(manager: TaskManager, task: DownloadTask) -> None:
    """
    Wait until the export worker has decided ``task``'s final outcome.

    ``finished_at`` is assigned by the worker exactly when it applies the
    final status, together with all of its bookkeeping. The status alone is
    not a settled signal: a FAILED status can already be observed from
    inside the export thread (the adapter's ``on_error`` fires there), long
    before the worker applies its decision.
    """
    await _wait_until(lambda: task.finished_at is not None)


async def _wait_export_queue_done(manager: TaskManager) -> None:
    """Wait until the export worker has fully processed its queue."""
    await asyncio.wait_for(manager._export_waiting.join(), timeout=2.0)


# -- every format failed -> FAILED -------------------------------------------


@pytest.mark.asyncio
async def test_all_formats_failed_ends_failed() -> None:
    """The regression: an export failing in every format was COMPLETED."""

    def export(book: Any, ui: Any) -> None:
        # Mimic CommonClient.export_book: on_start per supported format,
        # on_error swallowed per format, the run itself returns normally.
        ui.on_start(book, "txt")
        ui.on_error(book, "txt", RuntimeError("disk full"))
        ui.on_start(book, "epub")
        ui.on_error(book, "epub", ValueError("bad metadata"))

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.FAILED
    assert task.error == "bad metadata"  # last failure stays visible
    assert isinstance(task.finished_at, float)
    assert task.exported_paths == {}
    # Normal wrap-up: registered once by the site worker, stage cleaned up.
    assert list(manager.completed) == [task]
    assert task.task_id not in manager._stage_tasks
    assert manager._export_waiting.empty()
    await manager.close()


@pytest.mark.asyncio
async def test_error_then_unsupported_stays_failed() -> None:
    def export(book: Any, ui: Any) -> None:
        ui.on_start(book, "txt")
        ui.on_error(book, "txt", RuntimeError("disk full"))
        ui.on_unsupported(book, "pdf")

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.FAILED
    assert task.error == "Export format 'pdf' is not supported."
    assert isinstance(task.finished_at, float)
    await manager.close()


@pytest.mark.asyncio
async def test_export_book_raising_ends_failed() -> None:
    """A client that raises out of export_book still fails the task."""

    def export(book: Any, ui: Any) -> None:
        raise RuntimeError("export exploded")

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.FAILED
    assert task.error == "export exploded"
    assert isinstance(task.finished_at, float)
    await manager.close()


# -- every format unsupported -> FAILED ---------------------------------------


@pytest.mark.asyncio
async def test_all_formats_unsupported_ends_failed() -> None:
    def export(book: Any, ui: Any) -> None:
        ui.on_unsupported(book, "pdf")
        ui.on_unsupported(book, "mobi")

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.FAILED
    assert task.error == "Export format 'mobi' is not supported."
    assert isinstance(task.finished_at, float)
    assert task.exported_paths == {}
    await manager.close()


# -- partial success -> COMPLETED with the failure retained --------------------


@pytest.mark.asyncio
async def test_partial_failure_then_success_stays_completed_with_error() -> None:
    out = OUT_DIR / "42.epub"

    def export(book: Any, ui: Any) -> None:
        ui.on_start(book, "txt")
        ui.on_error(book, "txt", RuntimeError("disk full"))
        ui.on_start(book, "epub")
        ui.on_success(book, "epub", out)

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.COMPLETED
    assert task.error == "disk full"  # failure detail retained, not cleared
    assert task.exported_paths == {"epub": out}
    assert isinstance(task.finished_at, float)
    await manager.close()


@pytest.mark.asyncio
async def test_success_then_failure_stays_completed_with_error() -> None:
    """A trailing format failure must not mask the overall completion."""
    out = OUT_DIR / "42.txt"

    def export(book: Any, ui: Any) -> None:
        ui.on_start(book, "txt")
        ui.on_success(book, "txt", out)
        ui.on_start(book, "epub")
        ui.on_error(book, "epub", ValueError("no cover"))

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.COMPLETED
    assert task.error == "no cover"
    assert task.exported_paths == {"txt": out}
    await manager.close()


@pytest.mark.asyncio
async def test_success_with_unsupported_format_stays_completed() -> None:
    """An unsupported format next to a success is a detail, not a failure."""
    out = OUT_DIR / "42.txt"

    def export(book: Any, ui: Any) -> None:
        ui.on_unsupported(book, "pdf")
        ui.on_start(book, "txt")
        ui.on_success(book, "txt", out)

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.COMPLETED
    assert task.error == "Export format 'pdf' is not supported."
    assert task.exported_paths == {"txt": out}
    await manager.close()


# -- no-op export keeps its historical outcome ---------------------------------


@pytest.mark.asyncio
async def test_silent_noop_export_stays_completed() -> None:
    """A client reporting nothing at all (no callbacks) still completes."""

    def export(book: Any, ui: Any) -> None:
        return None

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.COMPLETED
    assert task.error is None
    await manager.close()


# -- cancellation during the export phase ---------------------------------------


@pytest.mark.asyncio
async def test_cancelled_during_export_is_not_overridden() -> None:
    """A task cancelled while its export ran keeps CANCELLED."""

    def export(book: Any, ui: Any) -> None:
        ui.on_start(book, "txt")
        ui.on_success(book, "txt", OUT_DIR / "42.txt")
        # Mirror what TaskManager.cancel_task does to a staged task while
        # the export thread is running (it cannot await from in here).
        ui.task.status = Status.CANCELLED
        ui.task.error = "Cancelled by user"

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_until(lambda: task.status is Status.CANCELLED)
    await _wait_export_queue_done(manager)

    assert task.status is Status.CANCELLED  # never flipped to COMPLETED
    assert task.error == "Cancelled by user"
    assert task.task_id not in manager._stage_tasks
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_landing_mid_export_is_not_resurrected() -> None:
    """
    A cancel landing while the export *thread* runs survives the callbacks
    fired afterwards.

    ``cancel_task`` runs on the event loop while ``export_book`` runs in
    ``asyncio.to_thread``; the export UI callbacks therefore race with the
    cancellation. Callbacks fired after the cancel used to flip CANCELLED back
    to EXPORTING/FAILED, and the worker's final decision then completed the
    task despite the user's cancellation.
    """
    export_reached = threading.Event()
    release_export = threading.Event()

    def export(book: Any, ui: Any) -> None:
        ui.on_start(book, "txt")
        ui.on_success(book, "txt", OUT_DIR / "42.txt")
        export_reached.set()
        # Park the export thread until the loop has cancelled the task.
        release_export.wait(timeout=2.0)
        # Everything below runs *after* cancel_task marked the task CANCELLED.
        ui.on_start(book, "epub")
        ui.on_success(book, "epub", OUT_DIR / "42.epub")
        ui.on_error(book, "epub", RuntimeError("late failure"))

    manager = _manager(_ExportClient(export))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_until(export_reached.is_set)
    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED
    release_export.set()

    await _wait_export_queue_done(manager)

    assert task.status is Status.CANCELLED  # never resurrected to COMPLETED
    assert task.error == "Cancelled by user"
    assert task.task_id not in manager._stage_tasks
    await manager.close()


# ---------------------------------------------------------------------------
# Whole chain: real CommonClient -> real WebExportUI -> real export worker
# ---------------------------------------------------------------------------


def _bare_client_with_txt(
    *,
    error: Exception | None = None,
    paths: list[Path] | None = None,
) -> CommonClient:
    """A bare ``CommonClient`` whose ``txt`` export stubs the outcome."""
    client = object.__new__(CommonClient)

    def stub(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        if error is not None:
            raise error
        assert paths is not None
        return paths

    client._export_book_txt = stub  # type: ignore[method-assign]
    return client


class _DelegatingClient:
    """Site client double delegating export to a real ``CommonClient``."""

    def __init__(self, bare: CommonClient, formats: list[str]) -> None:
        self._bare = bare
        self._formats = formats

    async def __aenter__(self) -> _DelegatingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        return True

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        return None

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> dict[str, list[Path]]:
        assert ui is not None
        return CommonClient.export_book(
            self._bare, book, cfg, formats=self._formats, ui=ui
        )


@pytest.mark.asyncio
async def test_real_client_all_formats_failed_ends_failed() -> None:
    """'pdf' is unsupported, 'txt' raises: the task must end FAILED."""
    bare = _bare_client_with_txt(error=RuntimeError("disk full"))
    manager = _manager(_DelegatingClient(bare, formats=["pdf", "txt"]))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.FAILED
    assert task.error == "disk full"  # the txt failure is the last one reported
    assert task.exported_paths == {}
    assert isinstance(task.finished_at, float)
    await manager.close()


@pytest.mark.asyncio
async def test_real_client_partial_success_stays_completed() -> None:
    """'pdf' unsupported, 'txt' succeeds: COMPLETED with the detail kept."""
    out = OUT_DIR / "42.txt"
    bare = _bare_client_with_txt(paths=[out])
    manager = _manager(_DelegatingClient(bare, formats=["pdf", "txt"]))
    task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

    await _wait_export_done(manager, task)

    assert task.status is Status.COMPLETED
    assert task.error == "Export format 'pdf' is not supported."
    assert task.exported_paths == {"txt": out}
    await manager.close()
