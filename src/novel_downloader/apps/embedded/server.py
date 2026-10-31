#!/usr/bin/env python3
"""
novel_downloader.apps.embedded.server
-------------------------------------

A stdlib-only HTTP server that exposes the novel-downloader service layer on
``127.0.0.1`` using the same JSON contract as the FastAPI REST API
(:mod:`novel_downloader.apps.api.routes`).

Why stdlib: the Android runtime (Chaquopy) has no glibc wheels for the
FastAPI stack (pydantic-core) and the server must never pull optional GUI
extras.  Endpoints not supported on the phone (music) answer with the same
shapes as the real API, just empty.

Called from Kotlin via Chaquopy::

    Python.start(AndroidPlatform(context))
    Python.getInstance()
        .getModule("novel_downloader.apps.embedded.server")
        .callAttr("start_server", filesDir.absolutePath, 8081)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

__all__ = ["start_server", "stop_server"]

logger = logging.getLogger(__name__)

#: Loopback only — the embedded engine must never be reachable from LAN.
_HOST = "127.0.0.1"
_server: ThreadingHTTPServer | None = None
_loop: asyncio.AbstractEventLoop | None = None


# ---------------------------------------------------------------------------
# async bridge (mirrors apps.desktop.engine but self-contained)
# ---------------------------------------------------------------------------


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None and _loop.is_running():
        return _loop
    loop = asyncio.new_event_loop()
    threading.Thread(
        target=loop.run_forever, name="noveldl-embedded", daemon=True
    ).start()
    _loop = loop
    return loop


def _run(coro: Any) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result(timeout=600)


# ---------------------------------------------------------------------------
# serialisation (mirrors apps.api.routes)
# ---------------------------------------------------------------------------


def _status_value(task: Any) -> str:
    return str(getattr(getattr(task, "status", None), "value", "") or "")


def _task_dict(task: Any) -> dict[str, Any]:
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
    return {
        "task_id": str(getattr(task, "task_id", "")),
        "title": str(getattr(task, "title", "") or ""),
        "site": str(getattr(task, "site", "") or ""),
        "book_id": str(getattr(task, "book_id", "") or ""),
        "status": _status_value(task),
        "chapters_done": int(getattr(task, "chapters_done", 0) or 0),
        "chapters_total": int(getattr(task, "chapters_total", 0) or 0),
        "progress": progress,
        "error": str(getattr(task, "error", None) or ""),
        "exported_paths": exports,
        "created_at": float(getattr(task, "created_at", 0.0) or 0.0),
        "finished_at": getattr(task, "finished_at", None),
    }


def _entry_dict(entry: Any) -> dict[str, Any]:
    from novel_downloader.apps.desktop.formatting import format_follow_updated

    return {
        "site": entry.site,
        "book_id": entry.book_id,
        "title": entry.title,
        "last_chapter": entry.last_chapter,
        "updated_at": entry.updated_at,
        "updated_text": format_follow_updated(entry.updated_at),
    }


# ---------------------------------------------------------------------------
# handlers — (method, path-pattern) -> callable(body: dict, **params)
# ---------------------------------------------------------------------------


def _handle_health(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader import __version__

    return {"status": "ok", "version": __version__}


def _handle_sites(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.constants import (
        DOWNLOAD_SUPPORT_SITES,
        SEARCH_SUPPORT_SITES,
    )

    return {
        "search": dict(SEARCH_SUPPORT_SITES),
        "download": dict(DOWNLOAD_SUPPORT_SITES),
    }


def _handle_search(body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.plugins.search import search

    keyword = str(body.get("keyword") or "").strip()
    if not keyword:
        return {"total": 0, "results": []}
    sites = body.get("sites") or None
    limit = body.get("limit")
    started = time.perf_counter()
    results = _run(
        search(
            keyword,
            sites=sites,
            per_site_limit=int(body.get("per_site_limit") or 4),
            timeout=float(body.get("timeout") or 6.0),
            sort=str(body.get("sort") or "quality"),
            order=body.get("order"),
            limit=int(limit) if limit else None,
        )
    )
    return {
        "total": len(results),
        "results": [dict(r) for r in results],
        "elapsed": round(time.perf_counter() - started, 2),
    }


def _handle_tasks_get(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    snap = manager.snapshot()
    return {
        "pending": [_task_dict(t) for t in snap.get("pending", [])],
        "running": [_task_dict(t) for t in snap.get("running", [])],
        "completed": [_task_dict(t) for t in snap.get("completed", [])],
    }


def _handle_tasks_post(body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    site = str(body.get("site") or "").strip()
    book_id = str(body.get("book_id") or "").strip()
    title = str(body.get("title") or "").strip() or book_id
    if not site or not book_id:
        return {"error": "site and book_id are required"}
    task = _run(manager.add_task(title=title, site=site, book_id=book_id))
    return _task_dict(task)


def _handle_task_cancel(
    _body: dict[str, Any], task_id: str, **_p: Any
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    ok = _run(manager.cancel_task(task_id))
    return {"ok": bool(ok)}


def _handle_login_requests(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core import cred_broker

    requests = _run(cred_broker.list_requests())
    return {"total": len(requests), "requests": requests}


def _handle_login_submit(
    body: dict[str, Any], req_id: str, **_p: Any
) -> dict[str, Any]:
    from novel_downloader.apps.core import cred_broker

    values = body.get("values") or {}
    _run(
        cred_broker.complete_request(
            req_id, {str(k): str(v) for k, v in values.items()}
        )
    )
    return {"ok": True}


def _handle_history(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.common.history_files import _apply_sort, _scan_files

    items = _apply_sort(_scan_files(), "mtime", "desc")
    return {
        "total": len(items),
        "offset": 0,
        "limit": len(items),
        "files": [
            {
                "name": item.name,
                "ext": item.ext,
                "size": item.size,
                "mtime": item.mtime,
                "download_url": f"/downloads/{quote(item.name, safe='')}",
            }
            for item in items
        ],
    }


def _handle_watchlist_get(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core.watchlist import WatchlistStore

    entries = WatchlistStore().entries()
    return {"total": len(entries), "books": [_entry_dict(e) for e in entries]}


def _handle_watchlist_post(body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core.watchlist import WatchlistStore

    site = str(body.get("site") or "").strip()
    book_id = str(body.get("book_id") or "").strip()
    title = str(body.get("title") or "").strip() or None
    if not site or not book_id:
        return {"error": "site and book_id are required"}
    entry, created = WatchlistStore().add(site, book_id, title=title)
    return {"created": created, "entry": _entry_dict(entry)}


def _handle_watchlist_delete(
    _body: dict[str, Any], site: str, book_id: str, **_p: Any
) -> dict[str, Any]:
    from novel_downloader.apps.core.watchlist import WatchlistStore

    return {"ok": WatchlistStore().remove(site, book_id)}


def _handle_watchlist_check(body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    from novel_downloader.apps.core.watchlist import WatchlistStore, check_all

    results = _run(check_all(WatchlistStore()))
    items = []
    for result in results:
        entry = result.entry
        item = {
            "site": entry.site,
            "book_id": entry.book_id,
            "title": entry.title,
            "previous_chapter": entry.last_chapter,
            "last_chapter": result.latest or entry.last_chapter,
            "found": bool(result.found),
            "changed": bool(result.changed),
            "needs_download": False,
            "error": result.error,
            "task": None,
        }
        if body.get("download") and result.changed and result.found and entry.title:
            item["task"] = {
                "site": entry.site,
                "book_id": entry.book_id,
                "title": entry.title,
            }
        items.append(item)
    updated = sum(1 for r in results if r.changed)
    return {
        "checked": len(results),
        "updated": updated,
        "failed": sum(1 for r in results if r.error),
        "download": bool(body.get("download")),
        "needs_download": [i for i in items if i["task"]],
        "items": items,
    }


def _handle_music_platforms(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    # Music extras (mutagen/qrcode/websocket/Pillow) are not bundled on
    # Android in v1; the screens render an empty state from this.
    return {"platforms": []}


def _handle_music_stub(_body: dict[str, Any], **_p: Any) -> dict[str, Any]:
    return {"tracks": [], "error": "music engine is not available in standalone mode"}


ROUTES: dict[tuple[str, str], Any] = {
    ("GET", "/api/v1/health"): _handle_health,
    ("GET", "/api/v1/sites"): _handle_sites,
    ("POST", "/api/v1/search"): _handle_search,
    ("GET", "/api/v1/tasks"): _handle_tasks_get,
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
    ("POST", "/api/v1/music/search"): _handle_music_stub,
    ("POST", "/api/v1/music/jobs"): _handle_music_stub,
}


def _match_route(method: str, path: str) -> tuple[Any, dict[str, str]]:
    """Match ``path`` against the route table; return (handler, params)."""
    for (route_method, pattern), handler in ROUTES.items():
        if method != route_method:
            continue
        pattern_parts = pattern.strip("/").split("/")
        path_parts = path.strip("/").split("/")
        if len(pattern_parts) != len(path_parts):
            continue
        params: dict[str, str] = {}
        for want, got in zip(pattern_parts, path_parts, strict=True):
            if want.startswith("{") and want.endswith("}"):
                params[want[1:-1]] = unquote(got)
            elif want != got:
                break
        else:
            return handler, params
    return None, {}


def _serve_download(handler: BaseHTTPRequestHandler, name: str) -> None:
    from novel_downloader.apps.common.history_files import _EXTS, _output_dir

    safe = Path(name).name
    ext = safe.rsplit(".", 1)[-1].lower()
    if ext not in _EXTS:
        handler.send_error(404)
        return
    path = _output_dir() / safe
    if not path.is_file():
        handler.send_error(404)
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header(
        "Content-Disposition", f'attachment; filename="{quote(safe, safe="")}"'
    )
    handler.end_headers()
    handler.wfile.write(data)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            loaded = json.loads(self.rfile.read(length).decode("utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- verbs ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/downloads/"):
            _serve_download(self, path[len("/downloads/") :])
            return
        handler, params = _match_route("GET", path)
        if handler is None:
            self._send_json({"error": f"not found: {path}"}, 404)
            return
        try:
            self._send_json(handler({}))
        except Exception as exc:  # noqa: BLE001 - report to the client
            logger.exception("embedded handler failed: %s", path)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        handler, params = _match_route("POST", path)
        if handler is None:
            self._send_json({"error": f"not found: {path}"}, 404)
            return
        try:
            self._send_json(handler(self._read_body(), **params))
        except Exception as exc:  # noqa: BLE001
            logger.exception("embedded handler failed: %s", path)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        handler, params = _match_route("DELETE", path)
        if handler is None:
            self._send_json({"error": f"not found: {path}"}, 404)
            return
        try:
            self._send_json(handler({}, **params))
        except Exception as exc:  # noqa: BLE001
            logger.exception("embedded handler failed: %s", path)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


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

    Safe to call repeatedly: a running server is left untouched.

    :return: The bound port (same as ``port`` unless binding failed and the
        fallback succeeded).
    """
    global _server
    if _server is not None:
        return _server.server_address[1]

    _prepare_home(home_dir)
    try:
        server = ThreadingHTTPServer((_HOST, port), _Handler)
    except OSError:
        port += 1
        server = ThreadingHTTPServer((_HOST, port), _Handler)
    _server = server
    threading.Thread(
        target=server.serve_forever,
        name="noveldl-embedded-http",
        daemon=True,
    ).start()
    logger.info("embedded engine listening on %s:%s", _HOST, port)
    return port


def stop_server() -> None:
    """Shut the HTTP server and the engine loop down (tests / teardown)."""
    global _server, _loop
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
        _loop = None
