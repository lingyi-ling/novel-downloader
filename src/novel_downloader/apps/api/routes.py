#!/usr/bin/env python3
"""
novel_downloader.apps.api.routes
--------------------------------

All ``/api/v1`` endpoints.

The endpoints are thin adapters over the same service layer the NiceGUI app
uses: search comes from :mod:`novel_downloader.plugins.search`, download tasks
from :mod:`novel_downloader.apps.core.task_manager`, and exported files
from :mod:`novel_downloader.apps.common.history_files`.

The task manager is provided through :func:`get_manager` so tests can replace
it with a fake via ``app.dependency_overrides`` and never touch the network.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Annotated, Any, TypeVar
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from novel_downloader import __version__
from novel_downloader.apps.common.history_files import _apply_sort, _scan_files
from novel_downloader.apps.constants import (
    DOWNLOAD_SUPPORT_SITES,
    SEARCH_SUPPORT_SITES,
)
from novel_downloader.apps.core.models import DownloadTask
from novel_downloader.apps.core.task_manager import TaskManager, manager
from novel_downloader.plugins.search import search, search_stream

from .auth import require_token
from .schemas import (
    MusicDownloadRequest,
    MusicSearchRequest,
    SearchRequest,
    TaskCreateRequest,
)

#: OpenAPI groups shown in ``/docs``.
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "system", "description": "Health checks and supported-site metadata."},
    {"name": "search", "description": "Aggregated book search across sites."},
    {"name": "tasks", "description": "Download task queue management."},
    {"name": "history", "description": "Exported files served under /downloads."},
    {"name": "music", "description": "Optional music search and download."},
]

#: Page size used when the caller supplies only one of ``offset`` / ``limit``.
DEFAULT_PAGE_LIMIT = 50

#: Hard upper bound for ``limit`` on the paginated list endpoints.
MAX_PAGE_LIMIT = 200

#: Seconds between two ``snapshot`` events on ``GET /tasks/stream``.
TASK_STREAM_INTERVAL = 1.0

#: Queue groups in the order they are concatenated when paginating ``/tasks``.
_TASK_GROUPS = ("pending", "running", "completed")

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_token)],
)


def get_manager() -> TaskManager:
    """FastAPI dependency returning the process-wide task manager."""
    return manager


# ---------------------------------------------------------------------------
# Timeouts for blocking endpoints
# ---------------------------------------------------------------------------

#: Default wall-clock budgets (seconds) for endpoints that wait on a slow,
#: potentially network-bound operation. They can be overridden per app through
#: ``create_app(timeouts={...})`` (see ``app.state.api_timeouts``).
BLOCKING_TIMEOUTS: dict[str, float] = {
    "search": 120.0,
    "music_search": 120.0,
    "music_download": 1800.0,
}

_T = TypeVar("_T")


def _blocking_timeout(request: Request, key: str) -> float:
    """Return the configured budget for ``key``, falling back to the default."""
    overrides = getattr(request.app.state, "api_timeouts", None)
    if isinstance(overrides, dict) and key in overrides:
        try:
            return float(overrides[key])
        except (TypeError, ValueError):
            pass
    return BLOCKING_TIMEOUTS[key]


async def _with_timeout(awaitable: Awaitable[_T], timeout: float, label: str) -> _T:
    """
    Await ``awaitable`` with a hard deadline.

    :raises HTTPException: 504 with a readable ``detail`` when the deadline is
        exceeded. A ``TimeoutError`` raised by the operation itself is reported
        the same way. Note that cancelling a threadpool job only stops the
        *request* from waiting: the worker thread itself is not interrupted, so
        a stuck provider call keeps running in the background until it returns.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"{label} timed out after {timeout:g}s",
        ) from exc


def _task_to_dict(task: DownloadTask) -> dict[str, Any]:
    """Serialise a task for the JSON API."""
    return {
        "task_id": task.task_id,
        "title": task.title,
        "site": task.site,
        "book_id": task.book_id,
        "status": task.status.value,
        "chapters_done": task.chapters_done,
        "chapters_total": task.chapters_total,
        "progress": task.progress(),
        "error": task.error,
        "exported_paths": {key: str(path) for key, path in task.exported_paths.items()},
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@router.get("/sites", tags=["system"])
async def sites() -> dict[str, dict[str, str]]:
    """List the sites supported for searching and downloading."""
    return {
        "search": dict(SEARCH_SUPPORT_SITES),
        "download": dict(DOWNLOAD_SUPPORT_SITES),
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.post("/search", tags=["search"])
async def search_books(body: SearchRequest, request: Request) -> dict[str, Any]:
    """
    Search all (or the selected) sites and return ranked results.

    The aggregate search is bounded by an overall deadline (in addition to the
    per-site ``timeout``); exceeding it returns ``504``.
    """
    timeout = _blocking_timeout(request, "search")
    results = await _with_timeout(
        search(
            body.keyword,
            sites=body.sites,
            limit=body.limit,
            per_site_limit=body.per_site_limit,
            timeout=body.timeout,
            sort=body.sort,
            order=body.order,
        ),
        timeout,
        "Search",
    )
    return {
        "total": len(results),
        "results": [dict(item) for item in results],
    }


def _sse_frame(event: str, payload: Any) -> str:
    """Encode one Server-Sent Events frame."""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


async def _search_event_stream(
    keyword: str,
    sites: list[str] | None,
    per_site_limit: int,
    timeout: float,
) -> AsyncIterator[str]:
    """
    Adapt :func:`search_stream` to SSE frames.

    ``aclosing`` guarantees the source generator is closed as soon as this
    wrapper is closed or cancelled. Starlette cancels the response generator
    when the client disconnects, so the ``finally`` block inside
    ``search_stream`` runs and its per-site tasks are cancelled too.
    """
    total = 0
    async with aclosing(
        search_stream(
            keyword,
            sites=sites,
            per_site_limit=per_site_limit,
            timeout=timeout,
        )
    ) as batches:
        async for batch in batches:
            items = [dict(item) for item in batch]
            total += len(items)
            yield _sse_frame("results", items)
    yield _sse_frame("done", {"total": total})


@router.get("/search/stream", tags=["search"])
async def stream_search(
    keyword: Annotated[str, Query(min_length=1)],
    sites: Annotated[str | None, Query()] = None,
    per_site_limit: Annotated[int, Query(ge=1)] = 5,
    timeout: Annotated[float, Query(gt=0)] = 5.0,
) -> StreamingResponse:
    """
    Stream search results as Server-Sent Events.

    One ``results`` event is emitted per site as soon as that site's batch is
    ready, followed by a final ``done`` event carrying the total count. An
    omitted ``sites`` parameter searches every enabled site; ``sites=`` (an
    empty value) searches none, matching the JSON endpoint's empty-array
    semantics.
    """
    selected: list[str] | None = None
    if sites is not None:
        selected = [part.strip() for part in sites.split(",") if part.strip()]

    return StreamingResponse(
        _search_event_stream(keyword, selected, per_site_limit, timeout),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Download tasks
# ---------------------------------------------------------------------------


def _page_bounds(
    total: int,
    offset: int | None,
    limit: int | None,
) -> tuple[int, int, int]:
    """
    Resolve ``offset`` / ``limit`` into a half-open slice and the applied limit.

    Omitting both parameters means "no pagination": the whole collection is
    returned (the pre-pagination behaviour) and the reported ``limit`` equals
    ``total``. Supplying only one of them fills the other from its default
    (``offset=0``, ``limit=50``). ``limit`` is already bounded by FastAPI's
    query validation; this helper only computes the slice.

    :return: ``(start, stop, applied_limit)``.
    """
    if offset is None and limit is None:
        return 0, total, total
    start = 0 if offset is None else offset
    applied = DEFAULT_PAGE_LIMIT if limit is None else limit
    return start, start + applied, applied


def _paginate_tasks(
    snapshot: dict[str, list[DownloadTask]],
    offset: int | None,
    limit: int | None,
) -> tuple[dict[str, list[DownloadTask]], int, int, int]:
    """
    Paginate the concatenation of ``pending``, ``running`` and ``completed``.

    The three lists keep their individual ordering; a page simply spans that
    combined sequence. Grouping is restored afterwards, so the response shape
    never changes.

    :return: ``(groups, total, start, applied_limit)``.
    """
    total = sum(len(snapshot[name]) for name in _TASK_GROUPS)
    start, stop, applied = _page_bounds(total, offset, limit)

    groups: dict[str, list[DownloadTask]] = {name: [] for name in _TASK_GROUPS}
    cursor = 0
    for name in _TASK_GROUPS:
        items = snapshot[name]
        end = cursor + len(items)
        low = max(start, cursor)
        high = min(stop, end)
        if low < high:
            groups[name] = items[low - cursor : high - cursor]
        cursor = end

    return groups, total, start, applied


def _tasks_snapshot(mgr: TaskManager) -> dict[str, list[dict[str, Any]]]:
    """Return the full, serialised task snapshot (the ``/tasks`` shape)."""
    snapshot = mgr.snapshot()
    return {
        name: [_task_to_dict(task) for task in snapshot[name]] for name in _TASK_GROUPS
    }


@router.get("/tasks", tags=["tasks"])
async def list_tasks(
    mgr: Annotated[TaskManager, Depends(get_manager)],
    offset: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_LIMIT)] = None,
) -> dict[str, Any]:
    """
    Return pending, running and recently completed tasks.

    ``offset`` / ``limit`` paginate the combined sequence
    ``pending + running + completed``. With neither parameter the full
    snapshot is returned, exactly as before pagination existed (the response
    still carries ``total``, ``offset`` and ``limit`` alongside the three
    lists). Supplying one parameter defaults the other to ``0`` / ``50``.
    """
    groups, total, start, applied = _paginate_tasks(mgr.snapshot(), offset, limit)
    payload: dict[str, Any] = {
        name: [_task_to_dict(task) for task in groups[name]] for name in _TASK_GROUPS
    }
    payload.update({"total": total, "offset": start, "limit": applied})
    return payload


async def _task_event_stream(
    mgr: TaskManager,
    interval: float,
) -> AsyncIterator[str]:
    """
    Emit one ``snapshot`` SSE frame every ``interval`` seconds.

    The loop lives for exactly as long as the request: when the client
    disconnects Starlette closes this generator, the pending ``sleep`` is
    cancelled and no further snapshots are produced. There is deliberately no
    process-wide broadcaster.
    """
    while True:
        yield _sse_frame("snapshot", _tasks_snapshot(mgr))
        await asyncio.sleep(interval)


@router.get("/tasks/stream", tags=["tasks"])
async def stream_tasks(
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> StreamingResponse:
    """
    Stream the current task snapshot as Server-Sent Events.

    A ``snapshot`` event is emitted immediately and then once per second; its
    ``data`` is a JSON object with the same ``pending`` / ``running`` /
    ``completed`` structure as ``GET /api/v1/tasks``. The stream ends when the
    client disconnects.
    """
    return StreamingResponse(
        _task_event_stream(mgr, TASK_STREAM_INTERVAL),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _find_task(mgr: TaskManager, task_id: str) -> DownloadTask | None:
    """Look a task up across every queue group."""
    snapshot = mgr.snapshot()
    for group in ("pending", "running", "completed"):
        for task in snapshot[group]:
            if task.task_id == task_id:
                return task
    return None


@router.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(
    task_id: str,
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> dict[str, Any]:
    """Return a single task by id."""
    task = _find_task(mgr, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}",
        )
    return _task_to_dict(task)


@router.post("/tasks", tags=["tasks"])
async def create_task(
    body: TaskCreateRequest,
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> dict[str, Any]:
    """Queue a download task."""
    title = (body.title or "").strip() or f"{body.site} (id = {body.book_id})"
    task = await mgr.add_task(title=title, site=body.site, book_id=body.book_id)
    return _task_to_dict(task)


@router.post("/tasks/{task_id}/cancel", tags=["tasks"])
async def cancel_task(
    task_id: str,
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> dict[str, bool]:
    """Cancel a pending or running task."""
    if not await mgr.cancel_task(task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}",
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Exported files
# ---------------------------------------------------------------------------


@router.get("/history", tags=["history"])
async def history(
    offset: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_LIMIT)] = None,
) -> dict[str, Any]:
    """
    List exported ``txt`` / ``epub`` files in the configured output dir.

    Files are sorted newest-first before ``offset`` / ``limit`` are applied;
    ``total`` always reports the number of matching files before slicing.
    Omitting both parameters returns every file (the pre-pagination behaviour).

    ``download_url`` percent-encodes the file name (``safe=""``), so names with
    spaces, ``#``, ``?`` or non-ASCII characters produce a directly usable URL.
    """
    items = _apply_sort(_scan_files(), "mtime", "desc")
    total = len(items)
    start, stop, applied = _page_bounds(total, offset, limit)
    files = [
        {
            "name": item.name,
            "ext": item.ext,
            "size": item.size,
            "mtime": item.mtime,
            "download_url": f"/downloads/{quote(item.name, safe='')}",
        }
        for item in items[start:stop]
    ]
    return {
        "total": total,
        "offset": start,
        "limit": applied,
        "files": files,
    }


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MusicComponents:
    """Lazy-imported pieces of the optional music engine."""

    paths: Any
    download_options: Any
    download_many: Any
    available_labels: Any
    create: Any
    platform_note: Any
    track_cls: Any


def _load_music_components() -> _MusicComponents:
    """
    Import the optional music engine.

    :raises HTTPException: 503 with an install hint when the ``music`` extras
        are missing, so clients get a clear error instead of a 500.
    """
    try:
        import mutagen  # noqa: F401
        import qrcode  # noqa: F401
        import websocket  # noqa: F401

        from novel_downloader.apps.music import paths
        from novel_downloader.apps.music.downloader import (
            DownloadOptions,
            download_many,
        )
        from novel_downloader.apps.music.providers import (
            available_labels,
            create,
            platform_note,
        )
        from novel_downloader.apps.music.providers.base import Track
    except Exception as exc:  # noqa: BLE001 - optional dependency guard
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Music support is not available: "
                f"{type(exc).__name__}: {exc}. "
                'Install it with: pip install "novel-downloader[music]"'
            ),
        ) from exc

    return _MusicComponents(
        paths=paths,
        download_options=DownloadOptions,
        download_many=download_many,
        available_labels=available_labels,
        create=create,
        platform_note=platform_note,
        track_cls=Track,
    )


def _create_provider(components: _MusicComponents, platform: str) -> Any:
    """Instantiate a provider, mapping unknown platforms to HTTP 400."""
    try:
        return components.create(platform)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown music platform: {platform}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - provider construction
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not initialise music platform {platform!r}: {exc}",
        ) from exc


@router.get("/music/platforms", tags=["music"])
async def music_platforms() -> dict[str, Any]:
    """List the registered music platforms and their usage notes."""
    components = _load_music_components()
    platforms = [
        {
            "key": str(key),
            "label": str(label),
            "note": str(components.platform_note(key)),
        }
        for key, label in components.available_labels()
    ]
    return {"platforms": platforms}


@router.post("/music/search", tags=["music"])
async def music_search(body: MusicSearchRequest, request: Request) -> dict[str, Any]:
    """
    Search a music platform. The blocking provider call runs in a thread.

    Exceeding the configured budget returns ``504``.
    """
    components = _load_music_components()
    provider = _create_provider(components, body.platform)

    def _run() -> tuple[int, list[dict[str, Any]]]:
        tracks, total = provider.search(body.keyword, limit=body.limit)
        payload = [asdict(track) for track in tracks]
        return int(total or len(payload)), payload

    timeout = _blocking_timeout(request, "music_search")
    total, tracks = await _with_timeout(
        run_in_threadpool(_run), timeout, "Music search"
    )
    return {"total": total, "tracks": tracks}


@router.post("/music/download", tags=["music"])
async def music_download(
    body: MusicDownloadRequest, request: Request
) -> dict[str, Any]:
    """
    Download one or more tracks.

    Note: the underlying engine is synchronous, so this request blocks until
    every track has finished; for large batches prefer one track per request or
    the asynchronous ``/music/jobs`` endpoint. The call is bounded by a
    generous wall-clock budget (default 30 minutes) and returns ``504`` when it
    is exceeded.
    """
    components = _load_music_components()
    provider = _create_provider(components, body.platform)

    tracks = [components.track_cls(**track.model_dump()) for track in body.tracks]
    out_dir = (body.out_dir or "").strip() or str(
        components.paths.default_download_dir()
    )
    options = components.download_options(
        out_dir=out_dir,
        level=body.level or "exhigh",
    )

    results: list[tuple[int, dict[str, Any]]] = []
    lock = threading.Lock()

    def on_result(index: int, track: Any, result: Any) -> None:
        item = _music_result_item(result)
        with lock:
            results.append((index, item))

    try:
        ok, skip, fail, _failed_pairs, _failed_tracks = await _with_timeout(
            run_in_threadpool(
                components.download_many,
                provider,
                tracks,
                options,
                on_result=on_result,
            ),
            _blocking_timeout(request, "music_download"),
            "Music download",
        )
    except Exception as exc:  # noqa: BLE001 - download engine errors
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Music download failed: {exc}",
        ) from exc

    ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
    return {
        "ok": int(ok),
        "skip": int(skip),
        "fail": int(fail),
        "results": ordered,
    }


def _music_result_item(result: Any) -> dict[str, Any]:
    """Serialise one per-track download result."""
    return {
        "status": str(getattr(result, "status", "fail")),
        "reason": str(getattr(result, "reason", "") or ""),
        "path": str(getattr(result, "path", "") or ""),
        "bytes": int(getattr(result, "bytes_", 0) or 0),
    }


#: How many music jobs (running or finished) are kept in memory.
MAX_MUSIC_JOBS = 100

_TERMINAL_MUSIC_STATUSES = frozenset({"done", "failed"})

#: In-process job table. Access is guarded by ``_MUSIC_JOBS_LOCK`` because the
#: download callback runs in a worker thread and updates job progress.
_MUSIC_JOBS: dict[str, dict[str, Any]] = {}
_MUSIC_JOBS_LOCK = threading.Lock()
#: Strong references to background tasks (the event loop only keeps weak ones).
_MUSIC_JOB_TASKS: set[asyncio.Task[None]] = set()


def _prune_music_jobs_locked() -> None:
    """
    Keep the job table bounded.

    Caller must hold ``_MUSIC_JOBS_LOCK``. Finished jobs are evicted first;
    if every retained job is still active, the oldest one is dropped.
    """
    while len(_MUSIC_JOBS) >= MAX_MUSIC_JOBS:
        for job_id, job in _MUSIC_JOBS.items():
            if job["status"] in _TERMINAL_MUSIC_STATUSES:
                del _MUSIC_JOBS[job_id]
                break
        else:
            _MUSIC_JOBS.pop(next(iter(_MUSIC_JOBS)), None)


def _music_job_update(job_id: str, **fields: Any) -> None:
    """Update job fields atomically, ignoring jobs that were pruned."""
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _music_job_set_progress(job_id: str, done: int) -> None:
    """Record how many tracks have finished so far."""
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is not None:
            job["progress"]["done"] = done


async def _run_music_job(
    job_id: str,
    components: _MusicComponents,
    provider: Any,
    tracks: list[Any],
    options: Any,
    timeout: float,
) -> None:
    """
    Execute one queued music job; never raises into the event loop.

    ``timeout`` bounds how long the job may stay active; a job that exceeds it
    is marked ``failed`` (the worker thread is left to finish on its own).
    """
    _music_job_update(job_id, status="running")

    results: list[tuple[int, dict[str, Any]]] = []
    results_lock = threading.Lock()

    def on_result(index: int, _track: Any, result: Any) -> None:
        item = _music_result_item(result)
        with results_lock:
            results.append((index, item))
            done = len(results)
        _music_job_set_progress(job_id, done)

    def _finish_failed(reason: str) -> None:
        ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
        _music_job_update(
            job_id,
            status="failed",
            results=ordered,
            error=reason,
            finished_at=time.time(),
        )

    try:
        await asyncio.wait_for(
            run_in_threadpool(
                components.download_many,
                provider,
                tracks,
                options,
                on_result=on_result,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        _finish_failed(f"Music download timed out after {timeout:g}s")
        return
    except Exception as exc:  # noqa: BLE001 - download engine errors
        _finish_failed(f"{type(exc).__name__}: {exc}")
        return

    ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
    _music_job_update(
        job_id,
        status="done",
        results=ordered,
        error=None,
        finished_at=time.time(),
    )


@router.post(
    "/music/jobs",
    tags=["music"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_music_job(
    body: MusicDownloadRequest, request: Request
) -> dict[str, str]:
    """Queue a music download and return immediately with a job id."""
    components = _load_music_components()
    provider = _create_provider(components, body.platform)

    tracks = [components.track_cls(**track.model_dump()) for track in body.tracks]
    out_dir = (body.out_dir or "").strip() or str(
        components.paths.default_download_dir()
    )
    options = components.download_options(
        out_dir=out_dir,
        level=body.level or "exhigh",
    )

    job_id = uuid4().hex
    with _MUSIC_JOBS_LOCK:
        _prune_music_jobs_locked()
        _MUSIC_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": {"done": 0, "total": len(tracks)},
            "results": [],
            "error": None,
            "created_at": time.time(),
            "finished_at": None,
        }

    task = asyncio.create_task(
        _run_music_job(
            job_id,
            components,
            provider,
            tracks,
            options,
            _blocking_timeout(request, "music_download"),
        )
    )
    _MUSIC_JOB_TASKS.add(task)
    task.add_done_callback(_MUSIC_JOB_TASKS.discard)
    return {"job_id": job_id}


@router.get("/music/jobs/{job_id}", tags=["music"])
async def music_job_status(job_id: str) -> dict[str, Any]:
    """Return the status of a queued, running or finished music job."""
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Music job not found: {job_id}",
            )
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": dict(job["progress"]),
            "results": list(job["results"]),
            "error": job["error"],
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
        }


__all__ = ["OPENAPI_TAGS", "get_manager", "router"]
