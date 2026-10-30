#!/usr/bin/env python3
"""
novel_downloader.apps.api.client
--------------------------------

Lightweight synchronous client for the local Novel Downloader REST API.

The client can be used directly by other software::

    from novel_downloader.apps.api.client import NovelApiClient

    with NovelApiClient("http://127.0.0.1:8081", token="secret") as api:
        print(api.health())

Every non-2xx response (and any transport failure) raises :class:`ApiError`,
which carries the HTTP status, the server's ``detail`` field and the parsed
response payload.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from urllib.parse import quote

import httpx

__all__ = ["ApiError", "NovelApiClient"]


class ApiError(Exception):
    """
    Error raised for failed API calls.

    :param status_code: HTTP status returned by the server, or ``0`` when the
        request could not be completed (connection error, timeout, ...).
    :param detail: The server's ``detail`` field when present, otherwise the
        raw response body or a description of the transport error.
    :param payload: The parsed response body, or the raw text when the body is
        not JSON. ``None`` for transport failures.
    """

    def __init__(self, status_code: int, detail: Any, payload: Any = None) -> None:
        message = detail if isinstance(detail, str) else str(detail)
        if status_code:
            message = f"HTTP {status_code}: {message}"
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.payload = payload


def _response_payload(response: httpx.Response) -> Any:
    """Return the response body as JSON when possible, else as text."""
    try:
        return response.json()
    except ValueError:
        return response.text


def _detail_from_payload(payload: Any) -> Any:
    """Extract ``detail`` from a JSON error body, falling back to the body."""
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


def _error_from_response(response: httpx.Response) -> ApiError:
    """Build an :class:`ApiError` from a non-success response."""
    payload = _response_payload(response)
    detail = _detail_from_payload(payload)
    if detail is None or detail == "":
        detail = f"HTTP {response.status_code}"
    return ApiError(response.status_code, detail, payload)


def _decode_sse_data(raw: str) -> Any:
    """Decode an SSE ``data`` payload, falling back to the raw string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _iter_sse_events(response: httpx.Response) -> Iterator[tuple[str, Any]]:
    """
    Yield ``(event, data)`` pairs from a streaming SSE response.

    Implements the subset of the SSE grammar the API emits: ``event:`` and
    ``data:`` fields, comment lines beginning with ``:`` and blank-line event
    separators. Multiple ``data:`` lines are joined with ``\\n`` before JSON
    decoding. The response must be opened in streaming mode.
    """
    event = "message"
    data_lines: list[str] = []

    for line in response.iter_lines():
        if line == "":
            if data_lines:
                yield event, _decode_sse_data("\n".join(data_lines))
            event = "message"
            data_lines = []
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)

    # A well-behaved server terminates every event with a blank line, but be
    # forgiving when the connection closes right after the final data line.
    if data_lines:
        yield event, _decode_sse_data("\n".join(data_lines))


class NovelApiClient:
    """
    Synchronous client for the local REST API.

    :param base_url: Server root, for example ``http://127.0.0.1:8081``. A URL
        that already ends with ``/api/v1`` is accepted as well.
    :param token: API token sent as ``Authorization: Bearer <token>``.
    :param timeout: Per-request timeout in seconds.
    :param api_key: API token sent as ``X-API-Key`` instead of the Bearer
        header. Mutually exclusive with ``token``.
    :param transport: Optional ``httpx`` transport; mainly useful for tests.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8081",
        token: str | None = None,
        timeout: float = 60.0,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if token and api_key:
            raise ValueError("pass either token or api_key, not both")

        root = base_url.rstrip("/")
        if root.endswith("/api/v1"):
            self._api_base = root
            self._root_url = root[: -len("/api/v1")]
        else:
            self._api_base = f"{root}/api/v1"
            self._root_url = root

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif api_key:
            headers["X-API-Key"] = api_key

        self._client = httpx.Client(
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> NovelApiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_value(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an API request and return the decoded JSON value."""
        url = f"{self._api_base}/{path.lstrip('/')}"
        try:
            response = self._client.request(method, url, json=json, params=params)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc

        if not response.is_success:
            raise _error_from_response(response)
        if not response.content:
            return None
        return _response_payload(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform an API request and return the decoded JSON object."""
        payload = self._request_value(method, path, json=json, params=params)
        if payload is None:
            return {}
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
        return {"data": payload}

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return the server health probe."""
        return self._request("GET", "health")

    def sites(self) -> dict[str, Any]:
        """Return the sites supported for searching and downloading."""
        return self._request("GET", "sites")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        keyword: str,
        sites: list[str] | None = None,
        per_site_limit: int = 5,
        timeout: float = 5.0,
        sort: str = "quality",
        order: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Search all (or the selected) sites.

        :return: A payload with ``total`` and ``results`` keys.
        """
        body: dict[str, Any] = {
            "keyword": keyword,
            "per_site_limit": per_site_limit,
            "timeout": timeout,
            "sort": sort,
        }
        if sites is not None:
            body["sites"] = sites
        if order is not None:
            body["order"] = order
        if limit is not None:
            body["limit"] = limit
        return self._request("POST", "search", json=body)

    # ------------------------------------------------------------------
    # Download tasks
    # ------------------------------------------------------------------

    def add_task(
        self, site: str, book_id: str, title: str | None = None
    ) -> dict[str, Any]:
        """Queue a download task and return the created task."""
        body: dict[str, Any] = {"site": site, "book_id": book_id}
        if title is not None:
            body["title"] = title
        return self._request("POST", "tasks", json=body)

    def tasks(self) -> dict[str, Any]:
        """Return ``pending``, ``running`` and ``completed`` task lists."""
        return self._request("GET", "tasks")

    def tasks_page(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """
        Return one page of the task snapshot.

        The page spans ``pending + running + completed`` and the payload
        carries ``total``, ``offset`` and ``limit`` in addition to the three
        lists. ``limit`` is capped at 200 by the server.

        :param offset: Number of tasks to skip (``>= 0``).
        :param limit: Maximum number of tasks to return (``1..200``).
        """
        return self._request(
            "GET",
            "tasks",
            params={"offset": offset, "limit": limit},
        )

    def iter_tasks(self) -> Iterator[dict[str, Any]]:
        """
        Yield task snapshots from ``GET /api/v1/tasks/stream``.

        The server sends a ``snapshot`` event immediately and then once per
        second. Each yielded value is the event's decoded ``data`` object, with
        the same ``pending`` / ``running`` / ``completed`` shape as
        :meth:`tasks`. Iteration ends when the server closes the stream; break
        out of the loop (or close the generator) to disconnect::

            for snapshot in api.iter_tasks():
                print(len(snapshot["running"]))
                if done:
                    break

        Transport failures and non-2xx responses raise :class:`ApiError`.
        """
        url = f"{self._api_base}/tasks/stream"
        try:
            with self._client.stream("GET", url) as response:
                if not response.is_success:
                    response.read()
                    raise _error_from_response(response)
                for event, payload in _iter_sse_events(response):
                    if event != "snapshot" or not isinstance(payload, dict):
                        continue
                    yield cast(dict[str, Any], payload)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc

    def task(self, task_id: str) -> dict[str, Any]:
        """
        Return a single task.

        Servers that expose ``GET /api/v1/tasks/{task_id}`` are queried
        directly; older servers are supported by filtering :meth:`tasks`.
        """
        try:
            return self._request("GET", f"tasks/{quote(task_id, safe='')}")
        except ApiError as exc:
            if exc.status_code not in (404, 405):
                raise

        listing = self.tasks()
        for group in ("pending", "running", "completed"):
            items = listing.get(group)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("task_id") == task_id:
                    return cast(dict[str, Any], item)
        raise ApiError(404, f"Task not found: {task_id}")

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a pending or running task."""
        return self._request("POST", f"tasks/{quote(task_id, safe='')}/cancel")

    # ------------------------------------------------------------------
    # Login credential requests
    # ------------------------------------------------------------------

    def login_requests(self) -> dict[str, Any]:
        """
        Return the pending login credential requests.

        :return: A payload with ``total`` and a ``requests`` list. Each entry
            has ``req_id``, ``task_id``, ``title``, ``fields`` (name / label /
            type / required / default / placeholder / description) and
            ``prefill``. Poll this while a task is stuck waiting for login
            credentials, then call :meth:`submit_login`.
        """
        return self._request("GET", "login/requests")

    def submit_login(
        self, req_id: str, values: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Complete a pending login credential request.

        :param req_id: Request id as returned by :meth:`login_requests`.
        :param values: Field values keyed by field name; ``type=cookie``
            fields take the raw cookie string. Omitted values fall back to the
            request's ``prefill``.
        :return: ``{"ok": true}`` on success. Unknown ``req_id`` raises
            :class:`ApiError` with status 404; an already-completed request
            raises 409.
        """
        body: dict[str, Any] = {"values": dict(values or {})}
        return self._request(
            "POST",
            f"login/requests/{quote(req_id, safe='')}",
            json=body,
        )

    # ------------------------------------------------------------------
    # Watchlist (追更)
    # ------------------------------------------------------------------

    def watchlist(self) -> dict[str, Any]:
        """Return the followed books (``total`` plus a ``books`` list)."""
        return self._request("GET", "watchlist")

    def add_watch(
        self, site: str, book_id: str, title: str | None = None
    ) -> dict[str, Any]:
        """
        Follow a book.

        :return: ``{"created": bool, "entry": {...}}``. ``created`` is
            ``false`` when the book was already followed.
        """
        body: dict[str, Any] = {"site": site, "book_id": book_id}
        if title is not None:
            body["title"] = title
        return self._request("POST", "watchlist", json=body)

    def remove_watch(self, site: str, book_id: str) -> dict[str, Any]:
        """Stop following a book (``404`` when it is not followed)."""
        return self._request(
            "DELETE",
            f"watchlist/{quote(site, safe='')}/{quote(book_id, safe='')}",
        )

    def check_watchlist(self, download: bool = False) -> dict[str, Any]:
        """
        Search every followed book's site for new chapters.

        No download is started. ``download=True`` adds a ready-to-post
        ``task`` body to each entry that needs an update.

        :return: ``checked`` / ``updated`` / ``failed`` counts, the
            ``needs_download`` entries and every per-book ``items`` outcome.
        """
        return self._request("POST", "watchlist/check", json={"download": download})

    # ------------------------------------------------------------------
    # Exported files
    # ------------------------------------------------------------------

    def history(self) -> dict[str, Any]:
        """Return the exported ``txt`` / ``epub`` files."""
        return self._request("GET", "history")

    def history_page(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """
        Return one page of exported files, newest first.

        The payload carries ``total`` (the full match count before slicing),
        ``offset``, ``limit`` and the ``files`` list. ``limit`` is capped at
        200 by the server.

        :param offset: Number of files to skip (``>= 0``).
        :param limit: Maximum number of files to return (``1..200``).
        """
        return self._request(
            "GET",
            "history",
            params={"offset": offset, "limit": limit},
        )

    def download_file(self, name: str, dest_dir: str | Path) -> Path:
        """
        Download an exported file from ``/downloads/<name>``.

        :param name: File name as listed by :meth:`history`. Only the basename
            is used, so ``../`` components cannot escape ``dest_dir``.
        :param dest_dir: Local directory; created when missing.
        :return: Path of the written file.
        """
        filename = Path(name).name
        if not filename:
            raise ValueError("name must contain a file name")

        url = f"{self._root_url}/downloads/{quote(filename, safe='')}"
        target_dir = Path(dest_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename

        try:
            with self._client.stream("GET", url) as response:
                if not response.is_success:
                    payload = _response_payload(response)
                    detail = _detail_from_payload(payload)
                    if detail is None or detail == "":
                        detail = f"HTTP {response.status_code}"
                    raise ApiError(response.status_code, detail, payload)
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            target.unlink(missing_ok=True)
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
        return target

    # ------------------------------------------------------------------
    # Music
    # ------------------------------------------------------------------

    def music_platforms(self) -> dict[str, Any]:
        """Return the registered music platforms."""
        return self._request("GET", "music/platforms")

    def music_search(
        self, platform: str, keyword: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search a music platform."""
        return self._request(
            "POST",
            "music/search",
            json={"platform": platform, "keyword": keyword, "limit": limit},
        )

    def music_download(
        self,
        platform: str,
        tracks: list[dict[str, Any]],
        level: str | None = None,
        out_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Download one or more tracks returned by :meth:`music_search`."""
        body: dict[str, Any] = {"platform": platform, "tracks": tracks}
        if level is not None:
            body["level"] = level
        if out_dir is not None:
            body["out_dir"] = str(out_dir)
        return self._request("POST", "music/download", json=body)

    def music_export(
        self,
        platform: str,
        tracks: list[dict[str, Any]],
        out_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """
        Write an M3U8 playlist for downloaded tracks.

        Each track must carry a local ``path``; tracks without one are counted
        in ``skipped``.

        :return: ``{"path", "tracks_written", "skipped"}``.
        """
        body: dict[str, Any] = {"platform": platform, "tracks": tracks}
        if out_path is not None:
            body["out_path"] = str(out_path)
        return self._request("POST", "music/export", json=body)

    def music_health(self, platform: str | None = None) -> list[dict[str, Any]]:
        """
        Probe one or all music platforms.

        :return: One dict per platform with ``platform``, ``ok``,
            ``elapsed_ms`` and ``error``.
        """
        params = {"platform": platform} if platform is not None else None
        payload = self._request_value("GET", "music/health", params=params)
        if not isinstance(payload, list):
            return []
        return [
            cast(dict[str, Any], item) for item in payload if isinstance(item, dict)
        ]
