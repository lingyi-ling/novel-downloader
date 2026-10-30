#!/usr/bin/env python3
"""
novel_downloader.apps.core.task_manager
-----------------------------------------------

"""

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

from novel_downloader.infra.config import ConfigAdapter, load_config
from novel_downloader.plugins import ClientProtocol, registrar
from novel_downloader.schemas import BookConfig

from .adapters import WebDownloadUI, WebExportUI, WebLoginUI, WebProcessUI
from .models import (
    DownloadTask,
    NonRetryableTaskError,
    RetryableTaskError,
    Status,
)

MAX_COMPLETED_TASKS = 100

#: Extra attempts made after a *transient* (network/timeout) download failure.
#: ``2`` means at most three attempts in total: the first one plus two retries.
DEFAULT_MAX_DOWNLOAD_RETRIES = 2

#: Fixed pause (seconds) between retry attempts. Kept small and jitter-free so
#: the behaviour stays deterministic; the per-site client already rate-limits
#: its own requests.
DEFAULT_RETRY_BACKOFF = 1.0

#: Failures we never retry: authentication/permission problems, missing or
#: corrupt data and programming errors. ``ValueError``/``LookupError`` cover the
#: parser and the metadata loader, which signal unrecoverable data issues.
_PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    PermissionError,
    FileNotFoundError,
    NotADirectoryError,
    IsADirectoryError,
    LookupError,
    ValueError,
    NotImplementedError,
)

#: Exception class *names* that identify a transient transport failure from the
#: optional httpx / curl_cffi backends without importing them.
_RETRYABLE_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionClosed",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "TransportError",
        "WriteError",
        "WriteTimeout",
    }
)


def is_retryable_error(error: BaseException) -> bool:
    """
    Return ``True`` when a failed download attempt is a transient transport error.

    Permission-class failures (login invalid, no copyright, missing data,
    programming errors) and anything explicitly marked as non-retryable return
    ``False``. Unknown exceptions also return ``False``: only failures we can
    positively identify as "try again later" are retried.
    """
    if isinstance(error, NonRetryableTaskError):
        return False
    if isinstance(error, _PERMANENT_ERRORS):
        return False
    if isinstance(error, RetryableTaskError):
        return True
    if isinstance(error, aiohttp.ClientResponseError):
        # 4xx means the request itself is refused (auth/paywall); only
        # server-side errors and rate limiting are worth another attempt.
        return error.status >= 500 or error.status == 429
    if isinstance(error, (aiohttp.ClientError, ConnectionError, TimeoutError, OSError)):
        return True
    declared = getattr(error, "retryable", None)
    if isinstance(declared, bool):
        return declared
    return type(error).__name__ in _RETRYABLE_ERROR_NAMES


def _is_cancelled(task: DownloadTask) -> bool:
    """
    Return whether ``task`` has been cancelled.

    Reading the status through a helper (instead of comparing the attribute
    inline) is intentional: a task's status is mutated concurrently by
    :meth:`TaskManager.cancel_task`, so a direct comparison would let the type
    checker narrow the attribute to "never cancelled" and reject the checks
    further down the worker loop.
    """
    return task.status is Status.CANCELLED


def _retry_message(error: BaseException, spent: int, budget: int) -> str:
    """Describe an attempt that is about to be retried."""
    return f"[retry {spent}/{budget}] {error}"


def _failure_message(error: BaseException, retries: int) -> str:
    """Build the final task error, preserving ``str(error)`` as a prefix."""
    message = str(error)
    if retries > 0:
        noun = "retry" if retries == 1 else "retries"
        message = f"{message} (after {retries} {noun})"
    return message


class TaskManager:
    """
    A multi-site task manager:
      * Each site has its own queue and a single worker.
      * Tasks from the same site run sequentially (strict FIFO).
      * Tasks from different sites can run in parallel.
      * Workers automatically exit when their site's queue becomes empty.
      * A dedicated export worker runs synchronous export tasks sequentially.
      * Transient (network/timeout) download failures are retried a bounded
        number of times; permission-class failures are not retried.
    """

    def __init__(
        self,
        *,
        max_download_retries: int | None = None,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        self.pending: dict[str, list[DownloadTask]] = defaultdict(list)
        self.running: dict[str, DownloadTask] = {}
        self.completed: deque[DownloadTask] = deque(maxlen=MAX_COMPLETED_TASKS)

        self._worker_tasks: dict[str, asyncio.Task[None]] = {}

        self._process_waiting: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self._export_waiting: asyncio.Queue[DownloadTask] = asyncio.Queue()

        self._process_worker_task: asyncio.Task[None] | None = None
        self._export_worker_task: asyncio.Task[None] | None = None

        # Tasks that left the per-site download phase and are queued for (or
        # being handled by) the process/export workers, keyed by task id.
        self._stage_tasks: dict[str, DownloadTask] = {}

        self._clients: dict[str, ClientProtocol] = {}

        self._lock = asyncio.Lock()
        self._adapter: ConfigAdapter | None = None

        # ``None`` means "resolve from config, fall back to the default".
        self._max_download_retries = max_download_retries
        self._retry_backoff = max(0.0, float(retry_backoff))

    @property
    def adapter(self) -> ConfigAdapter:
        """
        Lazily build the config adapter.

        Loading the config reads from disk, which must not happen at import
        time (the app can be imported from outside the project root).
        """
        if self._adapter is None:
            self._adapter = ConfigAdapter(load_config())
        return self._adapter

    @property
    def max_download_retries(self) -> int:
        """
        Extra attempts allowed after a transient download failure.

        An explicit constructor value wins; otherwise ``general.download_retry_times``
        is used when present, falling back to :data:`DEFAULT_MAX_DOWNLOAD_RETRIES`.
        """
        if self._max_download_retries is not None:
            return max(0, int(self._max_download_retries))
        general = self.adapter.get_config().get("general")
        if isinstance(general, dict):
            value = general.get("download_retry_times")
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, value)
        return DEFAULT_MAX_DOWNLOAD_RETRIES

    # ---------- public API ----------
    async def add_task(self, *, title: str, site: str, book_id: str) -> DownloadTask:
        """
        Add a new task and ensure a worker for its site is running.
        """
        task = DownloadTask(title=title, site=site, book_id=book_id)
        async with self._lock:
            self.pending[site].append(task)
            # start a new worker if needed
            if site not in self._worker_tasks or self._worker_tasks[site].done():
                self._worker_tasks[site] = asyncio.create_task(self._site_worker(site))
        return task

    async def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a task by id.

        Handles every lifecycle stage: a pending task is dropped from its queue,
        a running download has its asyncio task cancelled (the site worker then
        records the cancellation), and a task already queued for processing or
        export is marked cancelled so its stage worker skips it.

        Cancelling an unknown or already finished task is a no-op and returns
        ``False``, making repeated cancellation idempotent.
        """
        async with self._lock:
            # cancel pending
            for queue in self.pending.values():
                for i, pending_task in enumerate(queue):
                    if pending_task.task_id == task_id:
                        pending_task.status = Status.CANCELLED
                        pending_task.error = "Cancelled by user"
                        pending_task.finished_at = time.time()
                        queue.pop(i)
                        self._append_completed(pending_task)
                        return True

            # cancel running
            for running_task in self.running.values():
                if running_task.task_id == task_id:
                    running_task.status = Status.CANCELLED
                    running_task.error = "Cancelled by user"
                    running_task.finished_at = time.time()
                    if running_task.asyncio_task:
                        running_task.asyncio_task.cancel()
                    return True

            # cancel a task queued/being handled by the process/export stage
            staged = self._stage_tasks.get(task_id)
            if staged is not None:
                staged.status = Status.CANCELLED
                staged.error = "Cancelled by user"
                staged.finished_at = time.time()
                # Drop the registration so a repeated cancel is a no-op and the
                # stage worker's own cleanup stays consistent.
                self._stage_tasks.pop(task_id, None)
                self._append_completed(staged)
                return True
        return False

    def snapshot(self) -> dict[str, list[DownloadTask]]:
        """
        Return a shallow copy of the current queue state (running, pending, completed).
        """
        return {
            "running": list(self.running.values()),
            "pending": [task_item for q in self.pending.values() for task_item in q],
            "completed": list(self.completed),
        }

    async def close(self) -> None:
        """Cancel or gracefully finish all workers before shutdown."""
        all_tasks = [*self._worker_tasks.values()]
        if self._export_worker_task:
            all_tasks.append(self._export_worker_task)
        if self._process_worker_task:
            all_tasks.append(self._process_worker_task)

        for worker_task in all_tasks:
            worker_task.cancel()

        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                print(f"Worker error during shutdown: {result!r}")

        self._worker_tasks.clear()
        self._export_worker_task = self._process_worker_task = None

    # ---------- internals ----------
    def _append_completed(self, task: DownloadTask) -> None:
        """Record a finished task once, without duplicating a re-appended object."""
        if not any(existing is task for existing in self.completed):
            self.completed.append(task)

    def _get_client(self, site: str) -> ClientProtocol:
        """Get or create a client instance for a site."""
        if site not in self._clients:
            self._clients[site] = registrar.get_client(
                site, self.adapter.get_client_config(site)
            )

        return self._clients[site]

    def _ensure_worker(
        self, name: str, worker_fn: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Ensure a background worker is running."""
        worker_task = getattr(self, name)
        if not worker_task or worker_task.done():
            setattr(self, name, asyncio.create_task(worker_fn()))

    def _queue_for_export(self, task: DownloadTask) -> None:
        """Enqueue a task for export and start the export worker if needed."""
        task.status = Status.EXPORTING
        self._stage_tasks[task.task_id] = task
        self._export_waiting.put_nowait(task)
        self._ensure_worker("_export_worker_task", self._export_worker)

    async def _site_worker(self, site: str) -> None:
        """
        Sequentially run tasks for a specific site until its queue is empty.

        Exactly one worker exists per site, so a site's tasks run strictly in
        FIFO order while different sites progress in parallel. An unexpected
        error in one task is recorded on that task and never stops the worker:
        the next pending task is picked up immediately.
        """
        self_task = asyncio.current_task()
        try:
            while True:
                async with self._lock:
                    if not self.pending[site]:
                        self.running.pop(site, None)
                        self._worker_tasks.pop(site, None)
                        return
                    current_task = self.pending[site].pop(0)
                    self.running[site] = current_task

                try:
                    await self._run_task(current_task)
                except asyncio.CancelledError:
                    if not _is_cancelled(current_task):
                        current_task.status = Status.CANCELLED
                        current_task.error = "Cancelled by user"
                    current_task.finished_at = time.time()
                    if self_task is not None and self_task.cancelling() > 0:
                        # The worker itself is shutting down: cancel the
                        # in-flight download and stop processing this site.
                        inner = current_task.asyncio_task
                        if inner is not None and not inner.done():
                            inner.cancel()
                        raise
                except Exception as e:
                    current_task.status = Status.FAILED
                    current_task.finished_at = time.time()
                    current_task.error = _failure_message(e, current_task.retry_count)
                finally:
                    async with self._lock:
                        self._append_completed(current_task)
                        self.running.pop(site, None)
        finally:
            async with self._lock:
                # Only clean up the registration we own; a replacement worker
                # may already have taken over this site.
                if self._worker_tasks.get(site) is self_task:
                    self._worker_tasks.pop(site, None)
                    self.running.pop(site, None)

    async def _run_task(self, task: DownloadTask) -> None:
        """Run a single download task and dispatch to processing or export."""
        task.status = Status.RUNNING
        adapter = self.adapter
        client = self._get_client(task.site)

        login_ui = WebLoginUI(task)
        download_ui = WebDownloadUI(task)

        async def download_once() -> None:
            async with client:
                if adapter.get_login_required(task.site):
                    success = await client.login(
                        ui=login_ui, login_cfg=adapter.get_login_config(task.site)
                    )
                    if not success:
                        # Auth failure is permanent: never retry it.
                        raise NonRetryableTaskError("Login failed.")
                await client.download_book(
                    BookConfig(book_id=task.book_id), ui=download_ui
                )

        task.asyncio_task = asyncio.create_task(
            self._download_with_retry(task, download_once)
        )
        await task.asyncio_task

        if adapter.get_processor_configs(task.site):
            task.status = Status.PROCESSING
            self._stage_tasks[task.task_id] = task
            await self._process_waiting.put(task)
            self._ensure_worker("_process_worker_task", self._process_worker)
        else:
            self._queue_for_export(task)

    async def _download_with_retry(
        self,
        task: DownloadTask,
        download_once: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Attempt ``download_once`` until it succeeds, a permanent error occurs,
        or the retry budget is exhausted.

        Cancellation always propagates immediately. The number of retries spent
        is recorded on ``task.retry_count``; the intermediate failure is left in
        ``task.error`` so a snapshot shows the retry in progress.
        """
        budget = self.max_download_retries
        attempt = 0
        while True:
            attempt += 1
            try:
                await download_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                spent = attempt - 1
                if spent >= budget or not is_retryable_error(error):
                    task.retry_count = spent
                    raise
                task.retry_count = attempt
                task.error = _retry_message(error, attempt, budget)
                if self._retry_backoff > 0:
                    await asyncio.sleep(self._retry_backoff)
            else:
                task.retry_count = attempt - 1
                task.error = None
                return

    async def _process_worker(self) -> None:
        """Worker to run synchronous processing tasks sequentially."""
        while True:
            current_task = await self._process_waiting.get()
            if _is_cancelled(current_task):
                self._stage_tasks.pop(current_task.task_id, None)
                self._process_waiting.task_done()
                continue
            try:
                client = self._get_client(current_task.site)
                processors = self.adapter.get_processor_configs(current_task.site)
                if not processors:
                    self._queue_for_export(current_task)
                    continue

                await asyncio.to_thread(
                    client.process_book,
                    BookConfig(book_id=current_task.book_id),
                    processors=processors,
                    ui=WebProcessUI(current_task),
                )
                if not _is_cancelled(current_task):
                    self._queue_for_export(current_task)

            except asyncio.CancelledError:
                current_task.status = Status.CANCELLED
                current_task.finished_at = time.time()
                break
            except Exception as e:
                current_task.status = Status.FAILED
                current_task.finished_at = time.time()
                current_task.error = str(e)
            finally:
                # A task handed off to the export stage stays registered there.
                if current_task.status is not Status.EXPORTING:
                    self._stage_tasks.pop(current_task.task_id, None)
                self._process_waiting.task_done()

    async def _export_worker(self) -> None:
        """
        Dedicated worker for synchronous export tasks.

        A client reports per-format outcomes through the export UI callbacks
        and returns normally even when every format failed, so the final
        status is decided here from those outcomes instead of being
        unconditionally set to COMPLETED:

        * at least one format produced a file -> COMPLETED; failures of the
          other formats were already recorded on ``task.error`` and stay
          visible (partial success);
        * nothing was produced and some format failed or was reported
          unsupported -> FAILED (kept or set, never masked);
        * nothing was produced and no per-format callback fired at all (a
          no-op export) -> COMPLETED, the historical outcome for that case.
        """
        while True:
            current_task = await self._export_waiting.get()
            if _is_cancelled(current_task):
                self._stage_tasks.pop(current_task.task_id, None)
                self._export_waiting.task_done()
                continue
            try:
                client = self._get_client(current_task.site)
                export_ui = WebExportUI(current_task)
                await asyncio.to_thread(
                    client.export_book,
                    BookConfig(book_id=current_task.book_id),
                    cfg=self.adapter.get_exporter_config(current_task.site),
                    ui=export_ui,
                )
                if not _is_cancelled(current_task):
                    current_task.finished_at = time.time()
                    if export_ui.exported_any:
                        current_task.status = Status.COMPLETED
                    elif (
                        current_task.status is Status.FAILED
                        or export_ui.unsupported_any
                    ):
                        # Not a single format produced a file while at least
                        # one failed or was unsupported: keep the task FAILED
                        # instead of masking the failure as COMPLETED.
                        current_task.status = Status.FAILED
                    else:
                        current_task.status = Status.COMPLETED
            except asyncio.CancelledError:
                current_task.status = Status.CANCELLED
                current_task.finished_at = time.time()
                break
            except Exception as e:
                current_task.status = Status.FAILED
                current_task.finished_at = time.time()
                current_task.error = str(e)
            finally:
                self._stage_tasks.pop(current_task.task_id, None)
                self._export_waiting.task_done()


manager = TaskManager()
