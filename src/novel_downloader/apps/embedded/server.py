#!/usr/bin/env python3
"""
novel_downloader.apps.embedded.server
-------------------------------------

A stdlib-only HTTP server that exposes the novel-downloader service layer on
``127.0.0.1`` using the same JSON contract as the FastAPI REST API
(:mod:`novel_downloader.apps.api.routes`).

Why stdlib: the Android runtime (Chaquopy) has no glibc wheels for the
FastAPI stack (pydantic-core) and the server must never pull optional GUI
extras.

Called from Kotlin via Chaquopy::

    Python.start(AndroidPlatform(context))
    Python.getInstance()
        .getModule("novel_downloader.apps.embedded.server")
        .callAttr("start_server", filesDir.absolutePath, 8081)

Contract parity with the REST API
---------------------------------
* Success bodies mirror ``routes.py`` field for field: ``GET /tasks``
  paginates and carries ``total`` / ``offset`` / ``limit``, task dicts carry
  ``export_stage`` and a nullable ``error``, watchlist entries are exactly
  ``WatchEntry.to_dict()``, search responses have ``total`` / ``results``
  only.
* Every error is ``{"detail": ...}`` with the REST status: ``404`` for
  unknown routes / tasks / login requests / watchlist entries, ``405`` plus
  ``Allow`` for wrong methods, ``409`` for consumed login requests, ``422``
  with an ``errors`` list for validation failures, ``413`` over the body
  cap, ``503`` while the music extras are absent, ``504`` when an engine
  call exceeds its budget, and a stack-trace-free ``500``.
* ``HEAD`` is answered like ``GET`` (headers, no body) so probes work.

Lifecycle
---------
* ``start_server`` is idempotent and thread-safe.  A busy port raises
  ``OSError`` instead of silently drifting to a neighbour port: the Kotlin
  client speaks to a fixed port and must learn the engine is unavailable
  rather than talk to whatever occupies it.
* ``stop_server`` shuts the HTTP server down but leaves the engine event
  loop running: the task manager and the credential broker hold asyncio
  state bound to that loop, so a restart reuses it instead of abandoning
  it (and any request still waiting inside ``_run``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

__all__ = ["start_server", "stop_server"]

logger = logging.getLogger(__name__)

#: Loopback only — the embedded engine must never be reachable from LAN.
_HOST = "127.0.0.1"

#: Fallback wall-clock budget (seconds) for one coroutine bridged onto the
#: engine loop.
_RUN_TIMEOUT = 600.0

#: Wall-clock budget for ``POST /api/v1/search`` — mirrors
#: ``BLOCKING_TIMEOUTS["search"]`` in apps.api.routes.
_SEARCH_TIMEOUT = 120.0

#: Concurrent request-handler threads.  ThreadingHTTPServer spawns one
#: thread per connection with no built-in ceiling; requests beyond this cap
#: are answered with ``503`` instead of exhausting the phone's memory.
_MAX_CONCURRENT_REQUESTS = 32

#: Request-body ceiling for the JSON endpoints, mirroring the REST API's
#: ``DEFAULT_MAX_BODY_BYTES``.
_MAX_BODY_BYTES = 1024 * 1024

#: JSON endpoints whose request bodies are capped, mirroring the REST API's
#: ``BODY_LIMIT_ENDPOINTS``.
_BODY_LIMIT_ENDPOINTS = frozenset(
    {
        ("POST", "/api/v1/search"),
        ("POST", "/api/v1/watchlist"),
        ("POST", "/api/v1/watchlist/check"),
        ("POST", "/api/v1/music/search"),
        ("POST", "/api/v1/music/download"),
        ("POST", "/api/v1/music/export"),
        ("POST", "/api/v1/music/jobs"),
    }
)

#: Page size used when only one of ``offset`` / ``limit`` is supplied
#: (mirrors ``routes.DEFAULT_PAGE_LIMIT``).
_DEFAULT_PAGE_LIMIT = 50

#: Hard upper bound for ``limit`` on the paginated endpoints (mirrors
#: ``routes.MAX_PAGE_LIMIT``).
_MAX_PAGE_LIMIT = 200

#: Task snapshot groups, in the order the REST API concatenates them.
_TASK_GROUPS = ("pending", "running", "completed")

#: Valid ``sort`` keys, mirroring ``schemas.SearchSortKey``.
_SEARCH_SORT_KEYS = (
    "quality",
    "priority",
    "site",
    "title",
    "author",
    "words",
    "update",
)

# Message templates mirrored from apps.api.constants.  That module lives in
# the FastAPI app package (importing it would drag the whole FastAPI stack
# into the embedded runtime), so the strings are duplicated here; the tests
# pin them against the originals so a wording change cannot drift.
_MSG_TASK_NOT_FOUND = "Task not found: {task_id}"
_MSG_LOGIN_REQUEST_NOT_FOUND = "Login request not found: {req_id}"
_MSG_LOGIN_REQUEST_ALREADY_COMPLETED = "Login request already completed: {req_id}"
_MSG_BOOK_NOT_IN_WATCHLIST = "Book not in watchlist: {site}/{book_id}"
_MSG_MUSIC_UNAVAILABLE = (
    "Music support is not available: {error}. "
    'Install it with: pip install "novel-downloader[music]"'
)
_MSG_TIMED_OUT = "{label} timed out after {timeout:g}s"

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)
_store_lock = threading.Lock()
_watchlist_stores: dict[Path, Any] = {}

#: Handler signature: ``(body, path params, query params) -> JSON payload``.
Handler = Callable[
    [dict[str, Any], dict[str, str], dict[str, list[str]]], dict[str, Any]
]


# ---------------------------------------------------------------------------
# errors (mirror of the REST API's wire shapes)
# ---------------------------------------------------------------------------


class _ApiError(Exception):
    """An error carrying the REST API's wire shape: status + ``detail``."""

    def __init__(
        self, status: int, detail: str, errors: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors


def _err(name: str, msg: str, type_: str, source: str = "body") -> dict[str, Any]:
    """One validation entry; ``name`` may be dotted (``sites.0``)."""
    return {"type": type_, "loc": [source, *name.split(".")], "msg": msg}


def _validation_error(entries: list[dict[str, Any]]) -> _ApiError:
    """
    Build the 422 the REST API emits for a request-validation failure.

    Mirrors ``apps.api.middleware._validation_detail`` /
    ``_validation_errors``: a readable ``detail`` sentence plus the raw
    entries under ``errors``.
    """
    parts: list[str] = []
    for error in entries:
        location = [
            str(part)
            for part in error.get("loc", ())
            if part not in ("body", "query", "path", "header")
        ]
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{'.'.join(location)}: {message}" if location else message)
    detail = "Request validation failed"
    if parts:
        detail = "Request validation failed: " + "; ".join(parts)
    return _ApiError(422, detail, errors=entries)


def _required_str(
    body: dict[str, Any], name: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate one required ``min_length=1`` string field like pydantic."""
    if name not in body:
        return None, _err(name, "Field required", "missing")
    value = body[name]
    if value is None or not isinstance(value, str):
        return None, _err(name, "Input should be a valid string", "string_type")
    if len(value) < 1:
        return None, _err(
            name, "String should have at least 1 character", "string_too_short"
        )
    return value, None


def _int_field(
    body: dict[str, Any],
    name: str,
    default: int | None,
    ge: int | None,
    entries: list[dict[str, Any]],
) -> int | None:
    if name not in body or body[name] is None:
        return default
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int):
        entries.append(_err(name, "Input should be a valid integer", "int_type"))
        return default
    if ge is not None and value < ge:
        entries.append(
            _err(
                name,
                f"Input should be greater than or equal to {ge}",
                "greater_than_equal",
            )
        )
        return default
    return value


def _float_field(
    body: dict[str, Any],
    name: str,
    default: float,
    gt: float,
    entries: list[dict[str, Any]],
) -> float:
    if name not in body or body[name] is None:
        return default
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        entries.append(_err(name, "Input should be a valid number", "float_type"))
        return default
    result = float(value)
    if result <= gt:
        entries.append(
            _err(name, f"Input should be greater than {gt:g}", "greater_than")
        )
        return default
    return result


def _literal_field(
    body: dict[str, Any],
    name: str,
    default: str | None,
    choices: tuple[str, ...],
    entries: list[dict[str, Any]],
) -> str | None:
    if name not in body or body[name] is None:
        return default
    value = body[name]
    if not isinstance(value, str):
        entries.append(_err(name, "Input should be a valid string", "string_type"))
        return default
    if value not in choices:
        quoted = ", ".join(f"'{choice}'" for choice in choices[:-1])
        quoted += f" or '{choices[-1]}'"
        entries.append(_err(name, f"Input should be {quoted}", "literal_error"))
        return default
    return value


def _parse_paging(query: dict[str, list[str]]) -> tuple[int | None, int | None]:
    """Parse ``offset`` / ``limit`` with the REST API's query validation."""
    parsed: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    for name, ge, le in (("offset", 0, None), ("limit", 1, _MAX_PAGE_LIMIT)):
        values = query.get(name)
        if not values:
            continue
        try:
            value = int(values[-1])
        except ValueError:
            entries.append(
                _err(
                    name,
                    "Input should be a valid integer, unable to parse string "
                    "as an integer",
                    "int_parsing",
                    "query",
                )
            )
            continue
        if value < ge:
            entries.append(
                _err(
                    name,
                    f"Input should be greater than or equal to {ge}",
                    "greater_than_equal",
                    "query",
                )
            )
        elif le is not None and value > le:
            entries.append(
                _err(
                    name,
                    f"Input should be less than or equal to {le}",
                    "less_than_equal",
                    "query",
                )
            )
        else:
            parsed[name] = value
    if entries:
        raise _validation_error(entries)
    return parsed.get("offset"), parsed.get("limit")


def _parse_bool(body: dict[str, Any], name: str) -> bool:
    """Parse an optional boolean the way pydantic's lax mode does."""
    value = body.get(name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "on", "yes"}:
            return True
        if lowered in {"false", "0", "off", "no"}:
            return False
    raise _validation_error(
        [
            _err(
                name,
                "Input should be a valid boolean, unable to interpret input "
                "as a boolean",
                "bool_parsing",
            )
        ]
    )


# ---------------------------------------------------------------------------
# async bridge (mirrors apps.desktop.engine but self-contained)
# ---------------------------------------------------------------------------


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()
        threading.Thread(
            target=loop.run_forever, name="noveldl-embedded", daemon=True
        ).start()
        _loop = loop
        return loop


def _run(coro: Any, timeout: float = _RUN_TIMEOUT, label: str = "Request") -> Any:
    """
    Bridge a coroutine onto the engine loop and block until it finishes.

    Exceeding ``timeout`` raises a 504 carrying the REST API's timed-out
    message (a ``TimeoutError`` raised by the operation itself is reported
    the same way, exactly like ``routes._with_timeout``).
    """
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise _ApiError(
            504, _MSG_TIMED_OUT.format(label=label, timeout=timeout)
        ) from None


# ---------------------------------------------------------------------------
# serialisation (mirrors apps.api.routes)
# ---------------------------------------------------------------------------


def _status_value(task: Any) -> str:
    return str(getattr(getattr(task, "status", None), "value", "") or "")


def _task_dict(task: Any) -> dict[str, Any]:
    """Mirror ``routes.task_to_dict`` (same fields, same nullability)."""
    try:
        exports = {
            key: str(path)
            for key, path in dict(getattr(task, "exported_paths", {})).items()
        }
    except Exception:  # noqa: BLE001 - dict may mutate concurrently
        exports = {}
    try:
        progress = float(task.progress())
    except Exception:  # noqa: BLE001 - defensive
        progress = 0.0
    error = getattr(task, "error", None)
    finished_at = getattr(task, "finished_at", None)
    export_stage = getattr(task, "export_stage", None)
    return {
        "task_id": str(getattr(task, "task_id", "") or ""),
        "title": str(getattr(task, "title", "") or ""),
        "site": str(getattr(task, "site", "") or ""),
        "book_id": str(getattr(task, "book_id", "") or ""),
        "status": _status_value(task),
        "chapters_done": int(getattr(task, "chapters_done", 0) or 0),
        "chapters_total": int(getattr(task, "chapters_total", 0) or 0),
        "progress": progress,
        "error": None if error is None else str(error),
        "exported_paths": exports,
        "created_at": float(getattr(task, "created_at", 0.0) or 0.0),
        "finished_at": None if finished_at is None else float(finished_at),
        "export_stage": None if export_stage is None else str(export_stage),
    }


def _entry_dict(entry: Any) -> dict[str, str]:
    """Mirror ``WatchEntry.to_dict`` (the REST watchlist entry shape)."""
    return {
        "site": str(entry.site),
        "book_id": str(entry.book_id),
        "title": str(entry.title),
        "last_chapter": str(entry.last_chapter),
        "updated_at": str(entry.updated_at),
    }


def _page_bounds(
    total: int, offset: int | None, limit: int | None
) -> tuple[int, int, int]:
    """Mirror ``routes._page_bounds``."""
    if offset is None and limit is None:
        return 0, total, total
    start = 0 if offset is None else offset
    applied = _DEFAULT_PAGE_LIMIT if limit is None else limit
    return start, start + applied, applied


def _paginate_tasks(
    snapshot: dict[str, list[Any]], offset: int | None, limit: int | None
) -> tuple[dict[str, list[Any]], int, int, int]:
    """Mirror ``routes._paginate_tasks``."""
    total = sum(len(snapshot.get(name, [])) for name in _TASK_GROUPS)
    start, stop, applied = _page_bounds(total, offset, limit)

    groups: dict[str, list[Any]] = {name: [] for name in _TASK_GROUPS}
    cursor = 0
    for name in _TASK_GROUPS:
        items = snapshot.get(name, [])
        end = cursor + len(items)
        low = max(start, cursor)
        high = min(stop, end)
        if low < high:
            groups[name] = items[low - cursor : high - cursor]
        cursor = end
    return groups, total, start, applied


def _find_task(snapshot: dict[str, list[Any]], task_id: str) -> Any:
    for name in _TASK_GROUPS:
        for task in snapshot.get(name, []):
            if getattr(task, "task_id", None) == task_id:
                return task
    return None


def _watchlist_store() -> Any:
    """
    One shared :class:`WatchlistStore` per resolved watchlist file.

    The REST API builds a store per request; the embedded server funnels all
    requests through a single instance per file so the store's internal
    lock serialises concurrent read-modify-write cycles (the Kotlin app can
    fire several adds at once).  Keyed by path because tests and portable
    installs change ``NOVEL_HOME`` between starts.
    """
    from novel_downloader.apps.core.watchlist import (
        WatchlistStore,
        default_watchlist_path,
    )

    with _store_lock:
        path = default_watchlist_path()
        store = _watchlist_stores.get(path)
        if store is None:
            store = WatchlistStore(path)
            _watchlist_stores[path] = store
        return store


# ---------------------------------------------------------------------------
# handlers — (method, path-pattern) -> callable(body, params, query)
# ---------------------------------------------------------------------------


def _handle_health(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader import __version__

    return {"status": "ok", "version": __version__}


def _handle_sites(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.constants import (
        DOWNLOAD_SUPPORT_SITES,
        SEARCH_SUPPORT_SITES,
    )

    return {
        "search": dict(SEARCH_SUPPORT_SITES),
        "download": dict(DOWNLOAD_SUPPORT_SITES),
    }


def _handle_search(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.plugins.search import search

    keyword, keyword_err = _required_str(body, "keyword")
    entries: list[dict[str, Any]] = []

    sites_raw = body.get("sites")
    sites: list[str] | None = None
    if sites_raw is not None:
        if not isinstance(sites_raw, list):
            entries.append(_err("sites", "Input should be a valid list", "list_type"))
        else:
            sites = []
            for index, item in enumerate(sites_raw):
                if isinstance(item, str):
                    sites.append(item)
                else:
                    entries.append(
                        _err(
                            f"sites.{index}",
                            "Input should be a valid string",
                            "string_type",
                        )
                    )

    per_site_limit = _int_field(body, "per_site_limit", 5, 1, entries)
    timeout = _float_field(body, "timeout", 5.0, 0.0, entries)
    sort = _literal_field(body, "sort", "quality", _SEARCH_SORT_KEYS, entries)
    order = _literal_field(body, "order", None, ("asc", "desc"), entries)
    limit = _int_field(body, "limit", None, 1, entries)
    if keyword_err is not None:
        entries.append(keyword_err)
    if entries:
        raise _validation_error(entries)
    # keyword / per_site_limit / sort always resolve: their defaults are
    # non-None and any invalid input appended a validation entry above.
    assert keyword is not None
    assert per_site_limit is not None
    assert sort is not None

    results = _run(
        search(
            keyword,
            sites=sites,
            limit=limit,
            per_site_limit=per_site_limit,
            timeout=timeout,
            sort=sort,
            order=order,
        ),
        timeout=_SEARCH_TIMEOUT,
        label="Search",
    )
    return {"total": len(results), "results": [dict(r) for r in results]}


def _handle_tasks_get(
    _body: dict[str, Any], _params: dict[str, str], query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    offset, limit = _parse_paging(query)
    groups, total, start, applied = _paginate_tasks(manager.snapshot(), offset, limit)
    payload: dict[str, Any] = {
        name: [_task_dict(task) for task in groups[name]] for name in _TASK_GROUPS
    }
    payload.update({"total": total, "offset": start, "limit": applied})
    return payload


def _handle_task_get(
    _body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    task_id = params.get("task_id", "")
    task = _find_task(manager.snapshot(), task_id)
    if task is None:
        raise _ApiError(404, _MSG_TASK_NOT_FOUND.format(task_id=task_id))
    return _task_dict(task)


def _handle_tasks_post(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    site, site_err = _required_str(body, "site")
    book_id, book_id_err = _required_str(body, "book_id")
    title = body.get("title")
    entries: list[dict[str, Any]] = []
    if site_err is not None:
        entries.append(site_err)
    if book_id_err is not None:
        entries.append(book_id_err)
    if title is not None and not isinstance(title, str):
        entries.append(_err("title", "Input should be a valid string", "string_type"))
    if entries:
        raise _validation_error(entries)
    assert site is not None and book_id is not None

    resolved_title = (title or "").strip() or f"{site} (id = {book_id})"
    task = _run(manager.add_task(title=resolved_title, site=site, book_id=book_id))
    return _task_dict(task)


def _handle_task_cancel(
    _body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    task_id = params.get("task_id", "")
    if not _run(manager.cancel_task(task_id)):
        raise _ApiError(404, _MSG_TASK_NOT_FOUND.format(task_id=task_id))
    return {"ok": True}


def _handle_login_requests(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core import cred_broker

    requests = _run(cred_broker.list_requests())
    return {"total": len(requests), "requests": requests}


def _handle_login_submit(
    body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core import cred_broker

    req_id = params.get("req_id", "")
    values = body.get("values")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise _validation_error(
            [_err("values", "Input should be a valid dictionary", "dict_type")]
        )

    exists, done = _run(cred_broker.get_req_state(req_id))
    if not exists:
        raise _ApiError(404, _MSG_LOGIN_REQUEST_NOT_FOUND.format(req_id=req_id))
    if done:
        raise _ApiError(409, _MSG_LOGIN_REQUEST_ALREADY_COMPLETED.format(req_id=req_id))
    _run(
        cred_broker.complete_request(
            req_id, {str(key): str(value) for key, value in values.items()}
        )
    )
    return {"ok": True}


def _handle_history(
    _body: dict[str, Any], _params: dict[str, str], query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.common.history_files import _apply_sort, _scan_files

    offset, limit = _parse_paging(query)
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
    return {"total": total, "offset": start, "limit": applied, "files": files}


def _handle_watchlist_get(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    entries = _watchlist_store().entries()
    return {"total": len(entries), "books": [_entry_dict(entry) for entry in entries]}


def _handle_watchlist_post(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    site, site_err = _required_str(body, "site")
    book_id, book_id_err = _required_str(body, "book_id")
    title = body.get("title")
    entries: list[dict[str, Any]] = []
    if site_err is not None:
        entries.append(site_err)
    if book_id_err is not None:
        entries.append(book_id_err)
    if title is not None and not isinstance(title, str):
        entries.append(_err("title", "Input should be a valid string", "string_type"))
    if entries:
        raise _validation_error(entries)
    assert site is not None and book_id is not None

    entry, created = _watchlist_store().add(site, book_id, title=title)
    return {"created": created, "entry": _entry_dict(entry)}


def _handle_watchlist_delete(
    _body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    site = params.get("site", "")
    book_id = params.get("book_id", "")
    if not _watchlist_store().remove(site, book_id):
        raise _ApiError(
            404, _MSG_BOOK_NOT_IN_WATCHLIST.format(site=site, book_id=book_id)
        )
    return {"ok": True}


def _handle_watchlist_check(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.watchlist import check_all

    download = _parse_bool(body, "download")
    results = _run(check_all(_watchlist_store()))

    items: list[dict[str, Any]] = []
    for result in results:
        entry = result.entry
        changed = bool(result.changed)
        title = str(entry.title or "")
        matched_title = str(getattr(result, "matched_title", "") or "")
        if matched_title and not title:
            title = matched_title
        latest = str(result.latest or "")
        item: dict[str, Any] = {
            "site": entry.site,
            "book_id": entry.book_id,
            "title": title,
            "previous_chapter": entry.last_chapter,
            "last_chapter": latest or entry.last_chapter,
            "found": bool(result.found),
            "changed": changed,
            "needs_download": changed,
            "error": result.error,
            "task": None,
        }
        if changed and download:
            item["task"] = {
                "site": entry.site,
                "book_id": entry.book_id,
                "title": title or None,
            }
        items.append(item)

    return {
        "checked": len(results),
        "updated": sum(1 for item in items if item["changed"]),
        "failed": sum(1 for item in items if item["error"]),
        "download": download,
        "needs_download": [item for item in items if item["needs_download"]],
        "items": items,
    }


def _handle_music_platforms(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    # Music extras (mutagen/qrcode/websocket/Pillow) are not bundled on
    # Android in v1; an empty platform list makes the Compose screens render
    # their empty state and gate every other music call.
    return {"platforms": []}


def _handle_music_unavailable(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    # Unreachable while the platform list is empty (the client gates on it),
    # but answer exactly like the REST API does without the music extras.
    raise _ApiError(503, _MSG_MUSIC_UNAVAILABLE.format(error="ModuleNotFoundError"))


ROUTES: dict[tuple[str, str], Handler] = {
    ("GET", "/api/v1/health"): _handle_health,
    ("GET", "/api/v1/sites"): _handle_sites,
    ("POST", "/api/v1/search"): _handle_search,
    ("GET", "/api/v1/tasks"): _handle_tasks_get,
    ("GET", "/api/v1/tasks/{task_id}"): _handle_task_get,
    ("POST", "/api/v1/tasks"): _handle_tasks_post,
    ("POST", "/api/v1/tasks/{task_id}/cancel"): _handle_task_cancel,
    ("GET", "/api/v1/login/requests"): _handle_login_requests,
    ("POST", "/api/v1/login/requests/{req_id}"): _handle_login_submit,
    ("GET", "/api/v1/history"): _handle_history,
    ("GET", "/api/v1/watchlist"): _handle_watchlist_get,
    ("POST", "/api/v1/watchlist"): _handle_watchlist_post,
    ("DELETE", "/api/v1/watchlist/{site}/{book_id}"): _handle_watchlist_delete,
    ("POST", "/api/v1/watchlist/check"): _handle_watchlist_check,
    ("GET", "/api/v1/music/platforms"): _handle_music_platforms,
    ("POST", "/api/v1/music/search"): _handle_music_unavailable,
    ("POST", "/api/v1/music/jobs"): _handle_music_unavailable,
}


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def _pattern_matches(pattern: str, path: str) -> bool:
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    for want, got in zip(pattern_parts, path_parts, strict=True):
        if want.startswith("{") and want.endswith("}"):
            continue
        if want != got:
            return False
    return True


def _match_route(method: str, path: str) -> tuple[Handler | None, dict[str, str]]:
    """Match ``path`` against the route table; return (handler, params)."""
    for (route_method, pattern), handler in ROUTES.items():
        if route_method != method or not _pattern_matches(pattern, path):
            continue
        params: dict[str, str] = {}
        for want, got in zip(
            pattern.strip("/").split("/"), path.strip("/").split("/"), strict=True
        ):
            if want.startswith("{") and want.endswith("}"):
                params[want[1:-1]] = unquote(got)
        return handler, params
    return None, {}


def _allowed_methods(path: str) -> list[str]:
    """Methods the REST API would allow on ``path`` (for 405 responses)."""
    if path == "/downloads" or path.startswith("/downloads/"):
        return ["GET", "HEAD"]
    allowed: list[str] = []
    for route_method, pattern in ROUTES:
        if route_method not in allowed and _pattern_matches(pattern, path):
            allowed.append(route_method)
    return allowed


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: Idle or stalled connections are dropped after this many seconds so a
    #: silent client cannot pin a handler thread forever.
    timeout = 60

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _send_json(
        self,
        payload: dict[str, Any],
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_api_error(self, error: _ApiError) -> None:
        payload: dict[str, Any] = {"detail": error.detail}
        if error.errors:
            payload["errors"] = error.errors
        self._send_json(payload, error.status)

    def _content_length(self) -> int | None:
        """The declared Content-Length, or ``None`` when unusable."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            length = int(raw)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _drain_body(self) -> None:
        """Consume an unread request body so keep-alive stays in sync."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        remaining = self._content_length()
        if remaining is None:
            if self.headers.get("Content-Length") is not None:
                self.close_connection = True
            return
        if remaining > _MAX_BODY_BYTES:
            self.close_connection = True
            return
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _read_body(self, path: str) -> dict[str, Any]:
        """Read and decode the JSON request body (validation errors → 422)."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise _ApiError(501, "Chunked request bodies are not supported")
        length = self._content_length()
        if length is None:
            if self.headers.get("Content-Length") is not None:
                self.close_connection = True
                raise _ApiError(400, "Invalid Content-Length header")
            return {}
        if length == 0:
            return {}
        if (
            self.command,
            path.rstrip("/") or "/",
        ) in _BODY_LIMIT_ENDPOINTS and length > _MAX_BODY_BYTES:
            self.close_connection = True
            raise _ApiError(
                413,
                "Request body too large: the limit for this endpoint is "
                f"{_MAX_BODY_BYTES} bytes",
            )
        data = self.rfile.read(length)
        if len(data) < length:
            self.close_connection = True
            raise _ApiError(400, "Incomplete request body")
        try:
            loaded = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _validation_error(
                [_err("body", "JSON decode error", "json_invalid")]
            ) from None
        if not isinstance(loaded, dict):
            raise _validation_error(
                [
                    _err(
                        "body",
                        "Input should be a valid dictionary or object to "
                        "extract fields from",
                        "model_attributes_type",
                    )
                ]
            )
        return loaded

    def _serve_download(self, name: str) -> None:
        from novel_downloader.apps.common.history_files import _EXTS, _output_dir

        safe = Path(unquote(name)).name
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if not safe or ext not in _EXTS:
            self._send_json({"detail": "Not Found"}, 404)
            return
        path = _output_dir() / safe
        try:
            size = path.stat().st_size
        except OSError:
            self._send_json({"detail": "Not Found"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{quote(safe, safe="")}"'
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with path.open("rb") as handle:
                remaining = size
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if remaining > 0:
                    # The file shrank mid-transfer: never leave a short body
                    # on a keep-alive connection.
                    self.close_connection = True
        except OSError:
            logger.exception("embedded download failed: %s", safe)
            self.close_connection = True

    # -- dispatch ---------------------------------------------------------

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        if method == "GET" and path.startswith("/downloads/"):
            self._serve_download(path[len("/downloads/") :])
            return

        handler, params = _match_route(method, path)
        if handler is not None:
            if method == "POST":
                body = self._read_body(path)
            else:
                self._drain_body()
                body = {}
            self._send_json(handler(body, params, query))
            return

        # No handler for this method on this path: mirror the REST API's
        # 404 / 405 (+ Allow).  The body, if any, is drained so the
        # keep-alive connection stays usable.
        allowed = _allowed_methods(path)
        self._drain_body()
        if not allowed:
            self._send_json({"detail": "Not Found"}, 404)
            return
        self._send_json(
            {"detail": "Method Not Allowed"}, 405, {"Allow": ", ".join(allowed)}
        )

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if not _request_slots.acquire(blocking=False):
                self.close_connection = True
                self._send_json({"detail": "Embedded engine busy"}, 503)
                return
            try:
                self._route(method, path, query)
            finally:
                _request_slots.release()
        except _ApiError as error:
            self._send_api_error(error)
        except Exception:  # noqa: BLE001 - mirror the REST API's opaque 500
            logger.exception("embedded handler failed: %s", path)
            self._send_json({"detail": "Internal server error"}, 500)

    # -- verbs ------------------------------------------------------------
    # HEAD is dispatched like GET (the response body is skipped in
    # _send_json / _serve_download), every other unsupported verb falls
    # through to the 405 branch of _route.

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that logs handler-thread errors, not stderr."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Client disconnects mid-request are routine; keep them out of
        # stderr (on Android that is logcat) and out of test output.
        logger.debug("embedded connection error from %s", client_address, exc_info=True)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def _prepare_home(home_dir: str) -> None:
    """Point the whole config stack at the app-private home and seed it."""
    from novel_downloader.infra.config import copy_default_config
    from novel_downloader.infra.paths import DEFAULT_CONFIG_FILENAME

    os.environ["NOVEL_HOME"] = str(home_dir)
    home = Path(home_dir)
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    config_path = home / DEFAULT_CONFIG_FILENAME
    if not config_path.is_file():
        copy_default_config(config_path)


def start_server(home_dir: str, port: int = 8081) -> int:
    """
    Prepare the app home and start serving on ``127.0.0.1:<port>``.

    Safe to call repeatedly and from any thread: a running server is left
    untouched.

    A port that cannot be bound raises :class:`OSError` instead of silently
    drifting to ``port + 1``: the Kotlin client talks to a fixed port and
    must learn the engine is unavailable rather than send its requests to
    whatever else occupies it.

    :return: The bound port.
    """
    global _server
    with _server_lock:
        if _server is not None:
            return int(_server.server_address[1])
        _prepare_home(home_dir)
        server = _QuietThreadingHTTPServer((_HOST, port), _Handler)
        _server = server
    threading.Thread(
        target=server.serve_forever,
        name="noveldl-embedded-http",
        daemon=True,
    ).start()
    logger.info("embedded engine listening on %s:%s", _HOST, port)
    return port


def stop_server() -> None:
    """
    Shut the HTTP server down (tests / teardown).

    The engine event loop is deliberately left running for the lifetime of
    the process: the task manager and the credential broker hold asyncio
    state bound to that loop, and a restart on a fresh loop would abandon
    in-flight ``_run`` calls (and any loop-bound waiter).  ``start_server``
    after this reuses the same loop.
    """
    global _server
    with _server_lock:
        server = _server
        _server = None
    if server is not None:
        server.shutdown()
        server.server_close()
