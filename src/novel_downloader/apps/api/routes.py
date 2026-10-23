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
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
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

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_token)],
)


def get_manager() -> TaskManager:
    """FastAPI dependency returning the process-wide task manager."""
    return manager


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
async def search_books(body: SearchRequest) -> dict[str, Any]:
    """Search all (or the selected) sites and return ranked results."""
    results = await search(
        body.keyword,
        sites=body.sites,
        limit=body.limit,
        per_site_limit=body.per_site_limit,
        timeout=body.timeout,
        sort=body.sort,
        order=body.order,
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


@router.get("/tasks", tags=["tasks"])
async def list_tasks(
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> dict[str, list[dict[str, Any]]]:
    """Return pending, running and recently completed tasks."""
    snapshot = mgr.snapshot()
    return {
        "pending": [_task_to_dict(task) for task in snapshot["pending"]],
        "running": [_task_to_dict(task) for task in snapshot["running"]],
        "completed": [_task_to_dict(task) for task in snapshot["completed"]],
    }


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
async def history() -> dict[str, Any]:
    """List exported ``txt`` / ``epub`` files in the configured output dir."""
    items = _apply_sort(_scan_files(), "mtime", "desc")
    files = [
        {
            "name": item.name,
            "ext": item.ext,
            "size": item.size,
            "mtime": item.mtime,
            "download_url": f"/downloads/{quote(item.name)}",
        }
        for item in items
    ]
    return {"total": len(files), "files": files}


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
async def music_search(body: MusicSearchRequest) -> dict[str, Any]:
    """Search a music platform. The blocking provider call runs in a thread."""
    components = _load_music_components()
    provider = _create_provider(components, body.platform)

    def _run() -> tuple[int, list[dict[str, Any]]]:
        tracks, total = provider.search(body.keyword, limit=body.limit)
        payload = [asdict(track) for track in tracks]
        return int(total or len(payload)), payload

    total, tracks = await run_in_threadpool(_run)
    return {"total": total, "tracks": tracks}


@router.post("/music/download", tags=["music"])
async def music_download(body: MusicDownloadRequest) -> dict[str, Any]:
    """
    Download one or more tracks.

    Note: the underlying engine is synchronous, so this request blocks until
    every track has finished; for large batches prefer one track per request.
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
        ok, skip, fail, _failed_pairs, _failed_tracks = await run_in_threadpool(
            components.download_many,
            provider,
            tracks,
            options,
            on_result=on_result,
        )
    except Exception as exc:  # noqa: BLE001 - download engine errors
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
) -> None:
    """Execute one queued music job; never raises into the event loop."""
    _music_job_update(job_id, status="running")

    results: list[tuple[int, dict[str, Any]]] = []
    results_lock = threading.Lock()

    def on_result(index: int, _track: Any, result: Any) -> None:
        item = _music_result_item(result)
        with results_lock:
            results.append((index, item))
            done = len(results)
        _music_job_set_progress(job_id, done)

    try:
        await run_in_threadpool(
            components.download_many,
            provider,
            tracks,
            options,
            on_result=on_result,
        )
    except Exception as exc:  # noqa: BLE001 - download engine errors
        ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
        _music_job_update(
            job_id,
            status="failed",
            results=ordered,
            error=f"{type(exc).__name__}: {exc}",
            finished_at=time.time(),
        )
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
async def create_music_job(body: MusicDownloadRequest) -> dict[str, str]:
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
        _run_music_job(job_id, components, provider, tracks, options)
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
