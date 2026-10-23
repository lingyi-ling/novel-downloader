#!/usr/bin/env python3
"""
novel_downloader.apps.api.routes
--------------------------------

All ``/api/v1`` endpoints.

The endpoints are thin adapters over the same service layer the NiceGUI app
uses: search comes from :mod:`novel_downloader.plugins.search`, download tasks
from :mod:`novel_downloader.apps.web.services.task_manager`, and exported files
from :mod:`novel_downloader.apps.common.history_files`.

The task manager is provided through :func:`get_manager` so tests can replace
it with a fake via ``app.dependency_overrides`` and never touch the network.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from novel_downloader import __version__
from novel_downloader.apps.common.history_files import _apply_sort, _scan_files
from novel_downloader.apps.constants import (
    DOWNLOAD_SUPPORT_SITES,
    SEARCH_SUPPORT_SITES,
)
from novel_downloader.apps.web.models import DownloadTask
from novel_downloader.apps.web.services.task_manager import TaskManager, manager
from novel_downloader.plugins.search import search

from .auth import require_token
from .schemas import (
    MusicDownloadRequest,
    MusicSearchRequest,
    SearchRequest,
    TaskCreateRequest,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["api"],
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


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@router.get("/sites")
async def sites() -> dict[str, dict[str, str]]:
    """List the sites supported for searching and downloading."""
    return {
        "search": dict(SEARCH_SUPPORT_SITES),
        "download": dict(DOWNLOAD_SUPPORT_SITES),
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.post("/search")
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


# ---------------------------------------------------------------------------
# Download tasks
# ---------------------------------------------------------------------------


@router.get("/tasks")
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


@router.post("/tasks")
async def create_task(
    body: TaskCreateRequest,
    mgr: Annotated[TaskManager, Depends(get_manager)],
) -> dict[str, Any]:
    """Queue a download task."""
    title = (body.title or "").strip() or f"{body.site} (id = {body.book_id})"
    task = await mgr.add_task(title=title, site=body.site, book_id=body.book_id)
    return _task_to_dict(task)


@router.post("/tasks/{task_id}/cancel")
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


@router.get("/history")
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


@router.get("/music/platforms")
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


@router.post("/music/search")
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


@router.post("/music/download")
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
        item = {
            "status": str(getattr(result, "status", "fail")),
            "reason": str(getattr(result, "reason", "") or ""),
            "path": str(getattr(result, "path", "") or ""),
            "bytes": int(getattr(result, "bytes_", 0) or 0),
        }
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


__all__ = ["get_manager", "router"]
