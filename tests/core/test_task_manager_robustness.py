#!/usr/bin/env python3
"""
tests.core.test_task_manager_robustness
---------------------------------------

Offline robustness tests for the in-process task manager.

Every test runs without touching the network or the real config: a fake
adapter is injected into ``TaskManager._adapter`` and fake clients are seeded
into ``TaskManager._clients`` (``_get_client`` returns a cached client without
ever calling the plugin registrar). No real download is performed.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from novel_downloader.apps.core.models import (
    DownloadTask,
    NonRetryableTaskError,
    RetryableTaskError,
    Status,
)
from novel_downloader.apps.core.task_manager import (
    DEFAULT_MAX_DOWNLOAD_RETRIES,
    MAX_COMPLETED_TASKS,
    TaskManager,
    is_retryable_error,
)


# A transport-looking exception from an optional backend (httpx/curl_cffi).
class ConnectError(Exception):
    """Name-matched by ``is_retryable_error`` even though it is not a ClientError."""


class _WithRetryFlag(Exception):
    retryable = True


class _WithoutRetryFlag(Exception):
    retryable = False


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any lazy config/state lookup outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeAdapter:
    """Minimal stand-in for :class:`ConfigAdapter` used by the workers."""

    def __init__(
        self,
        *,
        login_required: bool = False,
        processors: list[Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.login_required = login_required
        self.processors = processors or []
        self._config = config or {}

    def get_login_required(self, site: str) -> bool:
        return self.login_required

    def get_login_config(self, site: str) -> dict[str, str]:
        return {}

    def get_processor_configs(self, site: str) -> list[Any]:
        return self.processors

    def get_exporter_config(self, site: str) -> None:
        return None

    def get_client_config(self, site: str) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return self._config


class FakeClient:
    """
    Async client double.

    ``download`` is an optional async callable invoked by ``download_book``;
    ``export`` / ``process`` are optional sync callables invoked by
    ``export_book`` / ``process_book``.
    """

    def __init__(
        self,
        *,
        download: Any = None,
        export: Any = None,
        process: Any = None,
        login_result: bool = True,
    ) -> None:
        self._download = download
        self._export = export
        self._process = process
        self.login_result = login_result
        self.download_calls: list[str] = []
        self.export_calls: list[str] = []
        self.process_calls: list[str] = []
        self.login_calls = 0

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        self.login_calls += 1
        return self.login_result

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        self.download_calls.append(book.book_id)
        if self._download is not None:
            await self._download(book, ui)

    def process_book(
        self, book: Any, processors: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> None:
        self.process_calls.append(book.book_id)
        if self._process is not None:
            self._process(book, ui)

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.export_calls.append(book.book_id)
        if self._export is not None:
            self._export(book, ui)
        return {}


def _manager(
    client: FakeClient | None = None,
    *,
    site: str = "a",
    adapter: FakeAdapter | None = None,
    **kwargs: Any,
) -> TaskManager:
    """Build a manager wired to fakes (never touches the registrar or config)."""
    manager = TaskManager(**kwargs)
    manager._adapter = adapter or FakeAdapter()  # type: ignore[assignment]
    if client is not None:
        manager._clients[site] = client  # type: ignore[assignment]
    return manager


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until it is true, yielding to the event loop."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _task(site: str, book_id: str) -> DownloadTask:
    return DownloadTask(title=f"Book {book_id}", site=site, book_id=book_id)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_stdlib_transport_errors_are_retryable() -> None:
    assert is_retryable_error(ConnectionError("net"))
    assert is_retryable_error(TimeoutError("slow"))
    assert is_retryable_error(ConnectionResetError("reset"))
    assert is_retryable_error(OSError("generic os error"))


def test_permission_and_business_errors_are_not_retryable() -> None:
    assert not is_retryable_error(PermissionError("login expired"))
    assert not is_retryable_error(FileNotFoundError("missing"))
    assert not is_retryable_error(ValueError("no copyright"))
    assert not is_retryable_error(LookupError("no metadata"))
    assert not is_retryable_error(NotImplementedError("unsupported"))


def test_explicit_task_error_markers_win() -> None:
    assert is_retryable_error(RetryableTaskError("transient"))
    assert not is_retryable_error(NonRetryableTaskError("permanent"))
    # The explicit non-retryable marker beats the generic retryable type.
    assert not is_retryable_error(PermissionError("denied"))


def test_retryable_attribute_convention() -> None:
    assert is_retryable_error(_WithRetryFlag("maybe"))
    assert not is_retryable_error(_WithoutRetryFlag("never"))


def test_unknown_exceptions_are_not_retried() -> None:
    assert not is_retryable_error(Exception("who knows"))
    assert not is_retryable_error(RuntimeError("logic bug"))


def test_name_matched_backend_errors_are_retryable() -> None:
    assert is_retryable_error(ConnectError("optional backend"))


def test_aiohttp_transport_errors_are_retryable() -> None:
    assert is_retryable_error(aiohttp.ServerDisconnectedError())
    assert is_retryable_error(aiohttp.ClientPayloadError("truncated"))


def test_aiohttp_status_errors_retry_only_server_side() -> None:
    server_error = aiohttp.ClientResponseError(
        request_info=None, history=(), status=503, message="unavailable"
    )
    rate_limited = aiohttp.ClientResponseError(
        request_info=None, history=(), status=429, message="slow down"
    )
    forbidden = aiohttp.ClientResponseError(
        request_info=None, history=(), status=403, message="forbidden"
    )
    unauthorized = aiohttp.ClientResponseError(
        request_info=None, history=(), status=401, message="nope"
    )

    assert is_retryable_error(server_error)
    assert is_retryable_error(rate_limited)
    assert not is_retryable_error(forbidden)
    assert not is_retryable_error(unauthorized)


# ---------------------------------------------------------------------------
# Retry budget configuration
# ---------------------------------------------------------------------------


def test_default_retry_budget_is_used_without_config() -> None:
    manager = _manager(adapter=FakeAdapter(config={}))
    assert manager.max_download_retries == DEFAULT_MAX_DOWNLOAD_RETRIES


def test_retry_budget_reads_from_config() -> None:
    adapter = FakeAdapter(config={"general": {"download_retry_times": 1}})
    manager = _manager(adapter=adapter)
    assert manager.max_download_retries == 1


def test_explicit_retry_budget_overrides_config() -> None:
    adapter = FakeAdapter(config={"general": {"download_retry_times": 1}})
    manager = _manager(adapter=adapter, max_download_retries=5)
    assert manager.max_download_retries == 5


@pytest.mark.parametrize("bad_value", [True, False, "3", None, -1.5, {}])
def test_invalid_config_retry_budget_falls_back_to_default(bad_value: Any) -> None:
    adapter = FakeAdapter(config={"general": {"download_retry_times": bad_value}})
    manager = _manager(adapter=adapter)
    assert manager.max_download_retries == DEFAULT_MAX_DOWNLOAD_RETRIES


# ---------------------------------------------------------------------------
# Retry behaviour through the real per-site worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_is_retried_up_to_budget_then_fails() -> None:
    attempts = 0

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("net down")

    client = FakeClient(download=download)
    manager = _manager(client, max_download_retries=2, retry_backoff=0.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.FAILED)

    assert attempts == 3  # first attempt + two retries
    assert task.retry_count == 2
    assert task.error == "net down (after 2 retries)"
    assert isinstance(task.finished_at, float)
    await manager.close()


@pytest.mark.asyncio
async def test_transient_failure_recovers_within_budget() -> None:
    attempts = 0

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("slow")

    client = FakeClient(download=download)
    manager = _manager(client, max_download_retries=2, retry_backoff=0.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.COMPLETED)

    assert attempts == 2  # one transient failure, one success
    assert task.retry_count == 1
    assert task.error is None
    await manager.close()


@pytest.mark.asyncio
async def test_permission_error_is_not_retried() -> None:
    attempts = 0

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("login expired")

    client = FakeClient(download=download)
    manager = _manager(client, max_download_retries=3, retry_backoff=0.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.FAILED)

    assert attempts == 1
    assert task.retry_count == 0
    assert task.error == "login expired"
    await manager.close()


@pytest.mark.asyncio
async def test_login_failure_is_not_retried() -> None:
    client = FakeClient(login_result=False)
    adapter = FakeAdapter(login_required=True)
    manager = _manager(
        client, adapter=adapter, max_download_retries=3, retry_backoff=0.0
    )

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.FAILED)

    assert client.login_calls == 1
    assert client.download_calls == []
    assert task.retry_count == 0
    assert task.error == "Login failed."
    await manager.close()


@pytest.mark.asyncio
async def test_zero_retry_budget_makes_a_single_attempt() -> None:
    attempts = 0

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("net down")

    client = FakeClient(download=download)
    manager = _manager(client, max_download_retries=0, retry_backoff=0.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.FAILED)

    assert attempts == 1
    assert task.retry_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_retry_budget_can_be_driven_from_config() -> None:
    attempts = 0

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("net down")

    client = FakeClient(download=download)
    adapter = FakeAdapter(config={"general": {"download_retry_times": 1}})
    manager = _manager(client, adapter=adapter, retry_backoff=0.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.status is Status.FAILED)

    assert attempts == 2  # one retry from config
    assert task.retry_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_stops_retrying() -> None:
    attempts = 0
    started = asyncio.Event()

    async def download(book: Any, ui: Any) -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        raise ConnectionError("net down")

    client = FakeClient(download=download)
    # A long backoff keeps the task parked in the retry sleep.
    manager = _manager(client, max_download_retries=5, retry_backoff=30.0)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.01)  # let the worker settle into the backoff sleep

    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED

    await asyncio.sleep(0.02)
    assert attempts == 1  # never retried after cancellation
    await manager.close()


# ---------------------------------------------------------------------------
# Per-site concurrency semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_sites_download_in_parallel() -> None:
    entered = 0
    gate = asyncio.Event()
    both_running = asyncio.Event()
    lock = asyncio.Lock()

    async def download(book: Any, ui: Any) -> None:
        nonlocal entered
        async with lock:
            entered += 1
            if entered >= 2:
                both_running.set()
        await gate.wait()

    manager = _manager()
    first = FakeClient(download=download)
    second = FakeClient(download=download)
    manager._clients["a"] = first  # type: ignore[assignment]
    manager._clients["b"] = second  # type: ignore[assignment]

    task_a = await manager.add_task(title="A", site="a", book_id="1")
    task_b = await manager.add_task(title="B", site="b", book_id="2")

    # If sites were serialised this would time out: both must be in-flight.
    await asyncio.wait_for(both_running.wait(), timeout=2.0)
    gate.set()

    await _wait_until(
        lambda: task_a.status is Status.COMPLETED and task_b.status is Status.COMPLETED
    )
    await manager.close()


@pytest.mark.asyncio
async def test_same_site_is_strictly_sequential_fifo() -> None:
    order: list[str] = []
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def download(book: Any, ui: Any) -> None:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        order.append(book.book_id)
        await asyncio.sleep(0.01)  # would overlap if the site were parallel
        async with lock:
            active -= 1

    client = FakeClient(download=download)
    manager = _manager(client)

    tasks = [
        await manager.add_task(title=f"T{i}", site="a", book_id=str(i))
        for i in range(1, 4)
    ]
    await _wait_until(lambda: all(task.status is Status.COMPLETED for task in tasks))

    assert order == ["1", "2", "3"]  # FIFO
    assert peak == 1  # never two downloads for the same site at once
    await manager.close()


# ---------------------------------------------------------------------------
# Cancellation semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_cancel_of_pending_task_is_idempotent() -> None:
    manager = _manager()
    task = _task("a", "1")
    other = _task("a", "2")
    manager.pending["a"].extend([task, other])

    assert await manager.cancel_task(task.task_id) is True
    assert await manager.cancel_task(task.task_id) is False  # already gone

    assert task.status is Status.CANCELLED
    assert manager.pending["a"] == [other]
    assert list(manager.completed) == [task]  # recorded exactly once


@pytest.mark.asyncio
async def test_cancel_landing_after_download_is_not_overwritten() -> None:
    """
    A cancel that lands between download completion and staging wins.

    The fake download schedules ``cancel_task`` right before returning, so it
    runs while the download asyncio task is already finished but the site
    worker has not resumed yet -- exactly the gap where the staging code used
    to overwrite CANCELLED with PROCESSING/EXPORTING and the task ended up
    COMPLETED despite the user's cancellation.
    """
    manager = _manager()

    async def download(book: Any, ui: Any) -> None:
        # The created coroutine is queued before the download task completes,
        # so it runs before the site worker resumes from ``await task.asyncio_task``.
        asyncio.create_task(manager.cancel_task(ui.task.task_id))

    client = FakeClient(download=download)
    manager._clients["a"] = client  # type: ignore[assignment]

    task = await manager.add_task(title="T", site="a", book_id="1")

    # Settle: the site worker must exit first (the regression enqueues the
    # export before it does); joining the export queue afterwards guarantees
    # the export worker -- if it ever got the item -- has applied its decision.
    await _wait_until(lambda: not manager._worker_tasks)
    await asyncio.wait_for(manager._export_waiting.join(), timeout=1.0)

    assert task.status is Status.CANCELLED
    assert task.error == "Cancelled by user"
    assert isinstance(task.finished_at, float)
    assert client.export_calls == []  # never handed to the export stage
    assert manager._export_waiting.empty()
    assert task.task_id not in manager._stage_tasks
    assert list(manager.completed) == [task]
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_task_waiting_for_export_is_idempotent() -> None:
    manager = _manager()
    task = _task("a", "1")
    task.status = Status.EXPORTING
    manager._stage_tasks[task.task_id] = task
    manager._export_waiting.put_nowait(task)

    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED
    assert task.error == "Cancelled by user"
    assert isinstance(task.finished_at, float)
    assert task.task_id not in manager._stage_tasks

    # Second cancel is a no-op: the task is neither queued nor registered.
    assert await manager.cancel_task(task.task_id) is False

    # The export worker skips the cancelled entry and finishes its accounting.
    worker = asyncio.create_task(manager._export_worker())
    await asyncio.wait_for(manager._export_waiting.join(), timeout=1.0)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert task.status is Status.CANCELLED  # never flipped to COMPLETED
    assert list(manager.completed) == [task]
    assert manager._export_waiting.empty()


@pytest.mark.asyncio
async def test_cancel_task_queued_for_processing() -> None:
    manager = _manager()
    task = _task("a", "1")
    task.status = Status.PROCESSING
    manager._stage_tasks[task.task_id] = task
    manager._process_waiting.put_nowait(task)

    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED
    assert task.task_id not in manager._stage_tasks
    assert await manager.cancel_task(task.task_id) is False

    worker = asyncio.create_task(manager._process_worker())
    await asyncio.wait_for(manager._process_waiting.join(), timeout=1.0)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert task.status is Status.CANCELLED
    assert manager._process_waiting.empty()


@pytest.mark.asyncio
async def test_cancel_unknown_task_is_false() -> None:
    manager = _manager()
    assert await manager.cancel_task("nope") is False


# ---------------------------------------------------------------------------
# Processing stage: failures reported through the UI must not be masked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_stage_failure_is_not_masked_by_export() -> None:
    """
    A processing failure (``on_missing``) fails the task instead of being
    exported away.

    ``CommonClient.process_book`` swallows stage exceptions and reports missing
    input only through ``ProcessUI.on_missing`` (which marks the task FAILED).
    The process worker used to hand the task to the export stage anyway, which
    reset the status to EXPORTING and could end the task COMPLETED.
    """
    missing = Path("raw_data") / "1" / "chapter.raw.sqlite"

    def process(book: Any, ui: Any) -> None:
        ui.on_missing(book, "raw chapters", missing)

    client = FakeClient(process=process)
    adapter = FakeAdapter(processors=[object()])
    manager = _manager(client, adapter=adapter)

    task = await manager.add_task(title="T", site="a", book_id="1")
    await _wait_until(lambda: task.finished_at is not None)
    await asyncio.wait_for(manager._process_waiting.join(), timeout=1.0)

    assert task.status is Status.FAILED
    assert task.error == f"Missing required data (raw chapters): {missing}"
    assert client.process_calls == ["1"]
    assert client.export_calls == []  # never handed to the export stage
    assert manager._export_waiting.empty()
    assert task.task_id not in manager._stage_tasks
    assert list(manager.completed) == [task]
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_landing_mid_process_is_not_exported() -> None:
    """
    A cancel landing while the process *thread* runs wins over the stage.

    ``cancel_task`` runs on the event loop while ``process_book`` runs in
    ``asyncio.to_thread``; the process UI callbacks therefore race with the
    cancellation, exactly like the export ones. Callbacks fired after the
    cancel must not flip CANCELLED back to PROCESSING/FAILED, and the
    worker's post-thread decision must not hand the cancelled book to the
    export stage.
    """
    process_reached = threading.Event()
    release_process = threading.Event()

    def process(book: Any, ui: Any) -> None:
        ui.on_stage_start(book, "merge")
        process_reached.set()
        # Park the process thread until the loop has cancelled the task.
        release_process.wait(timeout=2.0)
        # Everything below runs *after* cancel_task marked the task CANCELLED.
        ui.on_stage_start(book, "merge")
        ui.on_missing(book, "raw chapters", Path("raw_data") / "1")

    client = FakeClient(process=process)
    adapter = FakeAdapter(processors=[object()])
    manager = _manager(client, adapter=adapter)

    task = await manager.add_task(title="T", site="a", book_id="1")

    await _wait_until(process_reached.is_set)
    assert await manager.cancel_task(task.task_id) is True
    assert task.status is Status.CANCELLED
    release_process.set()

    # The process worker consumes the entry and applies its decision.
    await asyncio.wait_for(manager._process_waiting.join(), timeout=1.0)

    assert task.status is Status.CANCELLED  # never resurrected
    assert task.error == "Cancelled by user"
    assert client.export_calls == []  # never handed to the export stage
    assert manager._export_waiting.empty()
    assert task.task_id not in manager._stage_tasks
    assert list(manager.completed) == [task]
    await manager.close()


@pytest.mark.asyncio
async def test_queue_for_export_refuses_cancelled_task() -> None:
    """
    The export queue helper refuses a cancelled task outright.

    Callers check the cancellation themselves before staging, but the helper
    is the choke point every hand-off goes through, so the invariant is
    enforced (and locked in) here: a refused task keeps its CANCELLED
    status, is not registered for any stage and never reaches the queue.
    """
    manager = _manager()
    cancelled = _task("a", "1")
    cancelled.status = Status.CANCELLED

    manager._queue_for_export(cancelled)

    assert cancelled.status is Status.CANCELLED  # not flipped to EXPORTING
    assert manager._export_waiting.empty()
    assert cancelled.task_id not in manager._stage_tasks
    assert manager._export_worker_task is None  # the worker never started

    # A healthy task staged right after it is still queued normally.
    healthy = _task("a", "2")
    manager._queue_for_export(healthy)
    assert healthy.status is Status.EXPORTING
    assert manager._export_waiting.qsize() == 1
    await manager.close()


# ---------------------------------------------------------------------------
# Completed queue upper bound
# ---------------------------------------------------------------------------


def test_completed_truncation_never_touches_running_or_pending() -> None:
    manager = _manager()
    running = _task("a", "run")
    pending = _task("a", "pend")
    manager.running["a"] = running
    manager.pending["a"].append(pending)

    for index in range(MAX_COMPLETED_TASKS + 25):
        manager.completed.append(_task("s", str(index)))

    assert len(manager.completed) == MAX_COMPLETED_TASKS
    # Running / pending live outside the bounded deque and stay intact.
    assert manager.running["a"] is running
    assert manager.pending["a"] == [pending]
    snapshot = manager.snapshot()
    assert running in snapshot["running"]
    assert pending in snapshot["pending"]
    assert len(snapshot["completed"]) == MAX_COMPLETED_TASKS


# ---------------------------------------------------------------------------
# _append_completed: dedup semantics with O(1) membership tracking
# ---------------------------------------------------------------------------


def test_append_completed_dedups_repeated_objects() -> None:
    """Recording the same task twice keeps a single history entry."""
    manager = _manager()
    task = _task("a", "1")
    other = _task("a", "2")

    manager._append_completed(task)
    manager._append_completed(task)
    assert list(manager.completed) == [task]

    manager._append_completed(other)
    assert list(manager.completed) == [task, other]
    manager._append_completed(other)
    assert list(manager.completed) == [task, other]


def test_append_completed_survives_direct_deque_mutation() -> None:
    """A deque mutated outside the helper self-heals instead of duplicating."""
    manager = _manager()
    direct = _task("a", "1")

    manager.completed.append(direct)  # bypasses the helper entirely
    manager._append_completed(direct)  # must not record it a second time

    assert list(manager.completed) == [direct]


def test_append_completed_re_records_after_eviction() -> None:
    """
    An object evicted by the bounded deque is recorded again on re-append,
    while an object still inside the deque is deduped (is-scan semantics).
    """
    manager = _manager()
    first = _task("a", "first")
    manager._append_completed(first)
    for index in range(MAX_COMPLETED_TASKS + 10):
        manager._append_completed(_task("a", str(index)))

    assert len(manager.completed) == MAX_COMPLETED_TASKS
    assert first not in manager.completed  # evicted by the overflow

    manager._append_completed(first)
    assert manager.completed[-1] is first
    assert len(manager.completed) == MAX_COMPLETED_TASKS

    manager._append_completed(first)  # inside the deque again: deduped
    assert list(manager.completed).count(first) == 1


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_survives_unexpected_client_exception() -> None:
    async def download(book: Any, ui: Any) -> None:
        if book.book_id == "1":
            raise RuntimeError("boom")

    client = FakeClient(download=download)
    manager = _manager(client, max_download_retries=0)

    first = await manager.add_task(title="T1", site="a", book_id="1")
    second = await manager.add_task(title="T2", site="a", book_id="2")

    await _wait_until(
        lambda: first.status is Status.FAILED and second.status is Status.COMPLETED
    )

    # The uncaught error was recorded and the next task still ran.
    assert first.status is Status.FAILED
    assert first.error == "boom"
    assert second.status is Status.COMPLETED
    assert client.download_calls == ["1", "2"]
    await manager.close()


@pytest.mark.asyncio
async def test_close_stops_busy_worker_without_draining_queue() -> None:
    started = 0
    gate = asyncio.Event()

    async def download(book: Any, ui: Any) -> None:
        nonlocal started
        started += 1
        await gate.wait()

    client = FakeClient(download=download)
    manager = _manager(client)

    tasks = [
        await manager.add_task(title=f"T{i}", site="a", book_id=str(i))
        for i in range(3)
    ]
    await _wait_until(lambda: started == 1)
    assert len(manager.pending["a"]) == 2

    # Shutdown must terminate the busy worker instead of draining the queue.
    await asyncio.wait_for(manager.close(), timeout=2.0)

    assert started == 1
    assert len(manager.pending["a"]) == 2
    assert tasks[0].status is Status.CANCELLED
    assert manager._worker_tasks == {}
