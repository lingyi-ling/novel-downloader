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
  only, and a watchlist check only carries ``task`` when ``download=true``
  and the book changed.
* Request validation mirrors ``apps.api.schemas`` even though this module is
  stdlib-only: field bounds (keyword/title/site/book_id/platform, search and
  music paging, track text/URL/headers), stripping/deduplication and the
  redaction rules are re-implemented to the same ``422`` contract.
* Every error is ``{"detail": ...}`` with the REST status: ``404`` for
  unknown routes / tasks / login requests / watchlist entries, ``405`` plus
  ``Allow`` for wrong methods, ``409`` for consumed login requests, ``422``
  with an ``errors`` list for validation failures, ``413`` over the body
  cap, ``415`` for a compressed body, ``502`` when the download engine
  fails, ``503`` while the music extras are absent, ``504`` when an engine
  call exceeds its budget, and a stack-trace-free ``500``.
* Every state-changing request below ``/api/v1`` shares the REST body cap
  (``413``) and refuses non-identity ``Content-Encoding`` (``415``).
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
import dataclasses
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

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

#: Wall-clock budget for the browse endpoints — mirrors
#: ``BLOCKING_TIMEOUTS["browse"]`` in apps.api.routes.
_BROWSE_TIMEOUT = 60.0

#: Wall-clock budget (seconds) for one music download job
#: (mirrors the REST API's ``music_download`` blocking timeout).
_MUSIC_JOB_TIMEOUT = 1800.0

#: Concurrent request-handler threads.  ThreadingHTTPServer spawns one
#: thread per connection with no built-in ceiling; requests beyond this cap
#: are answered with ``503`` instead of exhausting the phone's memory.
_MAX_CONCURRENT_REQUESTS = 32

#: Request-body ceiling for the JSON endpoints, mirroring the REST API's
#: ``DEFAULT_MAX_BODY_BYTES``.
_MAX_BODY_BYTES = 1024 * 1024

#: Page size used when only one of ``offset`` / ``limit`` is supplied
#: (mirrors ``routes.DEFAULT_PAGE_LIMIT``).
_DEFAULT_PAGE_LIMIT = 50

#: Hard upper bound for ``limit`` on the paginated endpoints (mirrors
#: ``routes.MAX_PAGE_LIMIT``).
_MAX_PAGE_LIMIT = 200

#: Maximum ``limit`` for music search (mirrors
#: ``schemas.MAX_MUSIC_SEARCH_LIMIT``): a larger value is a ``422`` here too,
#: so the embedded engine never forwards an unbounded page size to a provider.
_MAX_MUSIC_SEARCH_LIMIT = 100

#: Shape of ``search_id`` / ``job_id`` (mirrors ``schemas.MUSIC_ID_PATTERN``).
#: The REST API validates the same pattern; ids are short opaque tokens, so
#: anything else is a ``422`` before a cache/table lookup or a log line.
_MUSIC_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_MUSIC_ID_RE = re.compile(_MUSIC_ID_PATTERN)

# Field bounds mirrored from ``apps.api.schemas``.  The REST request models
# reject values outside them with ``422``; the embedded engine must answer the
# same way instead of forwarding a hostile/unbounded value to a provider or the
# filesystem layer.
_MAX_KEYWORD_LENGTH = 200
_MAX_TITLE_LENGTH = 512
_MAX_CATEGORY_ID_LENGTH = 128
_MAX_SITE_KEY_LENGTH = 64
_MAX_SEARCH_SITES = 100
_MAX_PER_SITE_LIMIT = 100
_MAX_SEARCH_LIMIT = 500
_MAX_SEARCH_TIMEOUT = 120.0
_MAX_MUSIC_OFFSET = 10_000
_MAX_PLATFORM_LENGTH = 64
_MAX_LEVEL_LENGTH = 64
_MAX_OPTIONAL_PATH_LENGTH = 1024
_MAX_MUSIC_TRACK_TEXT_LENGTH = 512
_MAX_MUSIC_LYRIC_LENGTH = 100_000
_MAX_MEDIA_URL_LENGTH = 2048
_MAX_HEADER_COUNT = 32
_MAX_HEADER_NAME_LENGTH = 64
_MAX_HEADER_VALUE_LENGTH = 2048
_MAX_TRACK_SOURCES = 32
_MAX_LOGIN_FIELDS = 50
_MAX_LOGIN_FIELD_NAME_LENGTH = 64
_MAX_LOGIN_VALUE_LENGTH = 65_536

#: C0 control characters plus DEL: legitimate names/ids/URLs never contain
#: them, and they enable log/header injection (mirrors ``schemas``).
_CONTROL_CHARS = re.compile(r"[\x00-\x1F\x7F]")

#: Shape of a site key (mirrors ``schemas._SITE_KEY_CHARS``).
_SITE_KEY_CHARS = re.compile(r"[A-Za-z0-9_.-]+")

#: Header-name shape accepted for client-supplied per-track headers.
_HEADER_NAME_CHARS = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

#: Methods whose request bodies the REST middleware caps (and whose compressed
#: bodies it refuses), mirroring ``api.middleware.BODY_LIMIT_METHODS``.
_BODY_LIMIT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Prefix under which every state-changing body is capped, mirroring
#: ``api.middleware.BODY_LIMIT_PREFIX``.
_BODY_LIMIT_PREFIX = "/api/v1"

_MSG_BODY_TOO_LARGE = (
    f"Request body too large: the limit for this endpoint is {_MAX_BODY_BYTES} bytes"
)
_MSG_UNSUPPORTED_ENCODING = (
    "Unsupported Content-Encoding: send the request body uncompressed (identity)"
)
_MSG_MUSIC_PLATFORM_INIT_FAILED = (
    "Could not initialise music platform {platform!r}: {error}"
)

#: ``Retry-After`` (seconds) on the music-job concurrency 429 (mirrors
#: ``routes.MUSIC_JOBS_RETRY_AFTER_SECONDS``).
_MUSIC_JOBS_RETRY_AFTER_SECONDS = 5

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
_MSG_MUSIC_JOB_NOT_FOUND = "Music job not found: {job_id}"
_MSG_UNKNOWN_MUSIC_PLATFORM = "Unknown music platform: {platform}. Available: {keys}"
_MSG_MUSIC_DOWNLOAD_FAILED = "Music download failed: {error}"
_MSG_MUSIC_JOBS_BUSY = (
    "Too many music jobs are already running (limit {limit}); retry later"
)
_MSG_TIMED_OUT = "{label} timed out after {timeout:g}s"
_MSG_BROWSE_UNSUPPORTED = "Site '{site}' does not support browsing."

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)
_store_lock = threading.Lock()
_watchlist_stores: dict[Path, Any] = {}

#: Handler signature: ``(body, path params, query params) -> JSON payload``.
#: Handlers return a JSON-serialisable payload; ``/music/health`` answers a
#: bare list like the REST API, so the return type is open.
Handler = Callable[
    [dict[str, Any], dict[str, str], dict[str, list[str]]],
    dict[str, Any] | list[dict[str, Any]],
]


# ---------------------------------------------------------------------------
# errors (mirror of the REST API's wire shapes)
# ---------------------------------------------------------------------------


class _ApiError(Exception):
    """An error carrying the REST API's wire shape: status + ``detail``."""

    def __init__(
        self,
        status: int,
        detail: str,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors
        #: Extra response headers (e.g. ``Retry-After`` on the music-job 429).
        self.headers = dict(headers or {})


def _err(name: str, msg: str, type_: str, source: str = "body") -> dict[str, Any]:
    """One validation entry; ``name`` may be dotted (``sites.0``).

    All-digit segments become integers, matching Pydantic's list indices
    (``["body", "sites", 0]``), while the detail renderer stringifies them.
    """
    location: list[Any] = [source]
    for part in name.split("."):
        location.append(int(part) if part.isdigit() else part)
    return {"type": type_, "loc": location, "msg": msg}


def _value_error(name: str, message: str, source: str = "body") -> dict[str, Any]:
    """One ``AfterValidator``-style entry (Pydantic prefixes the message)."""
    return _err(name, f"Value error, {message}", "value_error", source)


def _has_surrogate(value: str) -> bool:
    """Whether ``value`` holds an unpaired surrogate (mirrors ``schemas``)."""
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _clean_string(
    raw: str,
    name: str,
    max_length: int,
    *,
    source: str = "body",
    label: str | None = None,
    allow_blank: bool = False,
    allow_controls: bool = False,
    strip: bool = True,
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Strip and bound one short, single-line text value.

    Mirrors ``schemas._clean_text``: blank values fail unless ``allow_blank``,
    then the surrogate, length and control-character checks run in that order.
    """
    label = label or name
    cleaned = raw.strip() if strip else raw
    if not allow_blank and not cleaned.strip():
        return None, _value_error(
            name, f"{label} must not be empty or whitespace-only", source
        )
    if _has_surrogate(cleaned):
        return None, _value_error(
            name,
            f"{label} must not contain unpaired surrogate characters",
            source,
        )
    if len(cleaned) > max_length:
        return None, _value_error(
            name, f"{label} must be at most {max_length} characters long", source
        )
    if not allow_controls and _CONTROL_CHARS.search(cleaned):
        return None, _value_error(
            name, f"{label} must not contain control characters", source
        )
    return cleaned, None


def _typed_string(
    body: dict[str, Any], name: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Return an optional string field, or a ``string_type`` error."""
    if name not in body or body[name] is None:
        return None, None
    value = body[name]
    if not isinstance(value, str):
        return None, _err(name, "Input should be a valid string", "string_type")
    return value, None


def _site_value(
    raw: str, *, name: str = "site", source: str = "body"
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate one site key exactly like ``schemas.validate_site_key``."""
    cleaned, error = _clean_string(
        raw, name, _MAX_SITE_KEY_LENGTH, source=source, label="site"
    )
    if error is not None or cleaned is None:
        return None, error
    if _SITE_KEY_CHARS.fullmatch(cleaned) is None:
        return None, _value_error(
            name,
            "site must contain only letters, digits, '_', '-' and '.'",
            source,
        )
    return cleaned, None


def _keyword_value(
    raw: str, *, name: str = "keyword", source: str = "body"
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate one search keyword like ``schemas.validate_keyword``."""
    return _clean_string(
        raw,
        name,
        _MAX_KEYWORD_LENGTH,
        source=source,
        label="keyword",
        strip=False,
    )


def _book_id_value(
    raw: str, *, name: str = "book_id", source: str = "body"
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Validate a ``book_id`` like the REST API's ``BookId`` field type.

    ``book_id`` becomes the cache directory ``raw_data/<site>/<book_id>``:
    path separators, a drive-letter colon, other Windows-reserved characters,
    control characters and unpaired surrogates would let a crafted request
    place the chapter cache and media files outside that tree.
    """
    cleaned, error = _clean_string(raw, name, _MAX_BOOK_ID_LENGTH, source=source)
    if error is not None or cleaned is None:
        return None, error
    if _BOOK_ID_INVALID_CHARS.search(cleaned):
        return None, _value_error(
            name,
            "book_id must not contain path separators, ':' or other "
            "Windows-reserved characters",
            source,
        )
    if cleaned in {".", ".."} or cleaned.endswith("."):
        return None, _value_error(
            name, "book_id must be a usable directory name", source
        )
    return cleaned, None


def _missing_body_error() -> _ApiError:
    """The 422 FastAPI emits when a required JSON body is absent or ``null``."""
    return _ApiError(
        422,
        "Request validation failed: Field required",
        errors=[{"type": "missing", "loc": ["body"], "msg": "Field required"}],
    )


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


def _required_text(
    body: dict[str, Any], name: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate presence/type only; the field's own validator checks content.

    ``SiteKey``/``Keyword`` carry no ``min_length``, so an empty string is
    reported by ``validate_site_key``/``validate_keyword`` as a ``value_error``
    rather than as Pydantic's ``string_too_short``.
    """
    if name not in body:
        return None, _err(name, "Field required", "missing")
    value = body[name]
    if not isinstance(value, str):
        return None, _err(name, "Input should be a valid string", "string_type")
    return value, None


def _site_field(body: dict[str, Any], entries: list[dict[str, Any]]) -> str | None:
    """Validate the required ``site`` field like ``schemas.SiteKey``."""
    raw, error = _required_text(body, "site")
    if error is not None:
        entries.append(error)
        return None
    assert raw is not None
    site, error = _site_value(raw)
    if error is not None:
        entries.append(error)
        return None
    return site


#: ``book_id`` becomes the cache directory ``raw_data/<site>/<book_id>``.
#: Path separators, a drive-letter colon, other Windows-reserved characters
#: and control characters would let a crafted request place the chapter cache
#: and media files outside that tree, so such values are rejected. Mirrors
#: ``apps.api.schemas.validate_book_id`` — keep the two in sync (this module
#: is stdlib-only and cannot import the pydantic layer).
_BOOK_ID_INVALID_CHARS = re.compile(r'[/\\<>:"|?*\x00-\x1F\x7F]')

#: Length ceiling, mirroring ``apps.api.schemas``.
_MAX_BOOK_ID_LENGTH = 128


def _book_id_field(body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Validate ``book_id`` like the REST API's ``BookId`` field type."""
    book_id, error = _required_str(body, "book_id")
    if error is not None or book_id is None:
        return book_id, error
    return _book_id_value(book_id)


def _int_field(
    body: dict[str, Any],
    name: str,
    default: int | None,
    ge: int | None,
    entries: list[dict[str, Any]],
    le: int | None = None,
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
    if le is not None and value > le:
        entries.append(
            _err(
                name,
                f"Input should be less than or equal to {le}",
                "less_than_equal",
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
    le: float | None = None,
    *,
    finite: bool = False,
) -> float:
    if name not in body or body[name] is None:
        return default
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        entries.append(_err(name, "Input should be a valid number", "float_type"))
        return default
    result = float(value)
    # ``json`` loads bare NaN/Infinity, which Pydantic rejects when
    # ``allow_inf_nan=False`` (REST's timeout fields all set it).
    if finite and not math.isfinite(result):
        entries.append(_err(name, "Input should be a finite number", "finite_number"))
        return default
    if result <= gt:
        entries.append(
            _err(name, f"Input should be greater than {gt:g}", "greater_than")
        )
        return default
    if le is not None and result > le:
        entries.append(
            _err(
                name,
                f"Input should be less than or equal to {le:g}",
                "less_than_equal",
            )
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


#: Truthy/falsy string literals pydantic's lax bool mode accepts verbatim
#: (case-insensitive, no whitespace stripping — " true " is a 422 there).
_BOOL_TRUTHY = frozenset({"true", "1", "on", "yes", "t", "y"})
_BOOL_FALSY = frozenset({"false", "0", "off", "no", "f", "n"})


def _parse_bool(body: dict[str, Any], name: str) -> bool:
    """Parse an optional boolean the way pydantic's lax mode does.

    Mirrors the probed FastAPI behaviour exactly: missing key defaults
    False; an explicit JSON null is a type error (not the default); ints
    and floats accept only 0/1 (and 0.0/1.0); strings are matched without
    stripping, so " true " is invalid here just as it is there.
    """
    if name not in body:
        return False
    value = body[name]
    if value is None:
        # Explicit null: pydantic reports bool_type, matching a missing key
        # is a silent contract change the guard tests forbid.
        raise _validation_error(
            [
                _err(
                    name,
                    "Input should be a valid boolean, unable to interpret input",
                    "bool_type",
                )
            ]
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return value == 1.0
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _BOOL_TRUTHY:
            return True
        if lowered in _BOOL_FALSY:
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


def _sites_field(
    body: dict[str, Any], entries: list[dict[str, Any]]
) -> list[str] | None:
    """
    Validate ``sites`` like ``SearchRequest.sites`` (``list[SiteKey]``).

    Items are stripped and bounded, invalid entries keep their list index in
    ``loc``, and duplicates are dropped case-insensitively (the registry is
    case-insensitive), keeping the first spelling — exactly like the REST
    model's ``_dedupe_sites`` validator.
    """
    raw = body.get("sites")
    if raw is None:
        return None
    if not isinstance(raw, list):
        entries.append(_err("sites", "Input should be a valid list", "list_type"))
        return None
    if len(raw) > _MAX_SEARCH_SITES:
        entries.append(
            _err(
                "sites",
                f"List should have at most {_MAX_SEARCH_SITES} items after "
                f"validation, not {len(raw)}",
                "too_long",
            )
        )
        return None
    selected: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            entries.append(
                _err(f"sites.{index}", "Input should be a valid string", "string_type")
            )
            continue
        site, error = _site_value(item)
        if error is not None:
            error["loc"] = ["body", "sites", index]
            entries.append(error)
            continue
        assert site is not None
        key = site.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(site)
    return selected


def _handle_search(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.plugins.search import search

    entries: list[dict[str, Any]] = []
    keyword, keyword_err = _required_text(body, "keyword")
    if keyword_err is not None:
        entries.append(keyword_err)
    elif keyword is not None:
        keyword, keyword_err = _keyword_value(keyword)
        if keyword_err is not None:
            entries.append(keyword_err)

    sites = _sites_field(body, entries)
    per_site_limit = _int_field(
        body, "per_site_limit", 5, 1, entries, le=_MAX_PER_SITE_LIMIT
    )
    timeout = _float_field(
        body, "timeout", 5.0, 0.0, entries, le=_MAX_SEARCH_TIMEOUT, finite=True
    )
    sort = _literal_field(body, "sort", "quality", _SEARCH_SORT_KEYS, entries)
    order = _literal_field(body, "order", None, ("asc", "desc"), entries)
    limit = _int_field(body, "limit", None, 1, entries, le=_MAX_SEARCH_LIMIT)
    if entries:
        raise _validation_error(entries)
    # ``group`` mirrors ``SearchRequest.group`` (validated last, like the
    # pydantic field order, so field errors above take precedence) and is
    # forwarded so ``group=true`` folds results here exactly like the REST API.
    group = _parse_bool(body, "group")
    # keyword / per_site_limit / sort always resolve: their defaults are
    # non-None and any invalid input appended a validation entry above.
    assert keyword is not None
    assert per_site_limit is not None
    assert sort is not None

    if sites is not None and not sites:
        # An explicit empty selection means "search no site": the REST handler
        # returns the same empty envelope without starting a search.
        return {"total": 0, "results": []}

    results = _run(
        search(
            keyword,
            sites=sites,
            limit=limit,
            per_site_limit=per_site_limit,
            timeout=timeout,
            sort=sort,
            order=order,
            group=group,
        ),
        timeout=_SEARCH_TIMEOUT,
        label="Search",
    )
    return {"total": len(results), "results": [dict(r) for r in results]}


_BROWSE_GENDERS = ("male", "female", "1", "2")


def _browse_searcher(site: str, method: str) -> Any:
    """Return the searcher class for ``site``, validated for ``method``."""
    from novel_downloader.plugins import registrar

    try:
        searcher_cls = registrar.get_searcher_class(site)
    except ValueError as exc:
        raise _ApiError(400, str(exc)) from exc
    if not callable(getattr(searcher_cls, method, None)):
        raise _ApiError(400, _MSG_BROWSE_UNSUPPORTED.format(site=site))
    return searcher_cls


async def _with_browse_searcher(searcher_cls: Any, call: Any) -> Any:
    """Run ``call`` with a searcher bound to its own HTTP session."""
    import aiohttp

    from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30), headers=DEFAULT_USER_HEADERS
    ) as session:
        return await call(searcher_cls(session))


def _handle_browse_categories(
    _body: dict[str, Any], _params: dict[str, str], query: dict[str, list[str]]
) -> dict[str, Any]:
    values = query.get("site") or []
    raw_site = values[0] if values else ""
    if not raw_site:
        raise _validation_error(
            [_err("site", "Field required", "missing", source="query")]
        )
    site, site_err = _site_value(raw_site, source="query")
    if site_err is not None:
        raise _validation_error([site_err])
    assert site is not None
    searcher_cls = _browse_searcher(site, "categories")
    groups = _run(
        _with_browse_searcher(searcher_cls, lambda s: s.categories()),
        timeout=_BROWSE_TIMEOUT,
        label="Browse",
    )
    return {"site": site, "groups": groups}


def _handle_browse(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    site = _site_field(body, entries)

    category_id: str | None = None
    raw_category, category_err = _typed_string(body, "category_id")
    if category_err is not None:
        entries.append(category_err)
    elif raw_category is not None:
        category_id, category_err = _clean_string(
            raw_category,
            "category_id",
            _MAX_CATEGORY_ID_LENGTH,
            allow_blank=True,
        )
        if category_err is not None:
            entries.append(category_err)
        elif not category_id:
            category_id = None

    gender = _literal_field(body, "gender", "male", _BROWSE_GENDERS, entries)
    limit = _int_field(body, "limit", 20, 1, entries, le=100)
    if entries:
        raise _validation_error(entries)
    assert site is not None and gender is not None and limit is not None

    if category_id is None:
        searcher_cls = _browse_searcher(site, "rank")
        results = _run(
            _with_browse_searcher(searcher_cls, lambda s: s.rank(limit)),
            timeout=_BROWSE_TIMEOUT,
            label="Browse",
        )
    else:
        code = "2" if gender in ("female", "2") else "1"
        searcher_cls = _browse_searcher(site, "category_books")
        results = _run(
            _with_browse_searcher(
                searcher_cls,
                lambda s: s.category_books(category_id, gender=code, limit=limit),
            ),
            timeout=_BROWSE_TIMEOUT,
            label="Browse",
        )
    return {"site": site, "total": len(results), "results": [dict(r) for r in results]}


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


def _title_field(body: dict[str, Any], entries: list[dict[str, Any]]) -> str | None:
    """Validate an optional display title like ``schemas.Title``."""
    raw, error = _typed_string(body, "title")
    if error is not None:
        entries.append(error)
        return None
    if raw is None:
        return None
    cleaned, error = _clean_string(raw, "title", _MAX_TITLE_LENGTH, allow_blank=True)
    if error is not None:
        entries.append(error)
        return None
    return cleaned or None


def _handle_tasks_post(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core.task_manager import manager

    entries: list[dict[str, Any]] = []
    title = _title_field(body, entries)
    site = _site_field(body, entries)
    book_id, book_id_err = _book_id_field(body)
    if book_id_err is not None:
        entries.append(book_id_err)
    if entries:
        raise _validation_error(entries)
    assert site is not None and book_id is not None

    resolved_title = title or f"{site} (id = {book_id})"
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


def _login_values(
    body: dict[str, Any],
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """
    Validate ``values`` like ``LoginSubmitRequest.values``.

    The values are merged into cookies/headers later, so CR/LF (header
    injection) and NUL are rejected.  The value itself is never echoed in an
    error message: it may be a password.
    """
    entries: list[dict[str, Any]] = []
    raw = body.get("values", {})
    if not isinstance(raw, dict):
        entries.append(
            _err("values", "Input should be a valid dictionary", "dict_type")
        )
        return None, entries
    if len(raw) > _MAX_LOGIN_FIELDS:
        entries.append(
            _value_error(
                "values", f"values must contain at most {_MAX_LOGIN_FIELDS} fields"
            )
        )
        return None, entries
    values: dict[str, str] = {}
    for name, value in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > _MAX_LOGIN_FIELD_NAME_LENGTH
        ):
            entries.append(
                _value_error("values", "values contains an invalid field name")
            )
            continue
        if _CONTROL_CHARS.search(name):
            entries.append(
                _value_error("values", "values contains an invalid field name")
            )
            continue
        if not isinstance(value, str):
            entries.append(
                {
                    "type": "string_type",
                    "loc": ["body", "values", name],
                    "msg": "Input should be a valid string",
                }
            )
            continue
        if _has_surrogate(value):
            entries.append(
                _value_error(
                    "values",
                    "values must not contain unpaired surrogate characters",
                )
            )
            continue
        if len(value) > _MAX_LOGIN_VALUE_LENGTH:
            entries.append(
                _value_error(
                    "values",
                    f"values must be at most {_MAX_LOGIN_VALUE_LENGTH} characters long",
                )
            )
            continue
        if any(char in value for char in "\r\n\x00"):
            entries.append(
                _value_error("values", "values must not contain newlines or NUL")
            )
            continue
        values[name] = value
    return values, entries


def _handle_login_submit(
    body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    from novel_downloader.apps.core import cred_broker

    req_id = params.get("req_id", "")
    values, entries = _login_values(body)
    if entries:
        raise _validation_error(entries)
    assert values is not None

    exists, done = _run(cred_broker.get_req_state(req_id))
    if not exists:
        raise _ApiError(404, _MSG_LOGIN_REQUEST_NOT_FOUND.format(req_id=req_id))
    if done:
        raise _ApiError(409, _MSG_LOGIN_REQUEST_ALREADY_COMPLETED.format(req_id=req_id))
    _run(cred_broker.complete_request(req_id, values))
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
    entries: list[dict[str, Any]] = []
    site = _site_field(body, entries)
    book_id, book_id_err = _book_id_field(body)
    if book_id_err is not None:
        entries.append(book_id_err)
    title = _title_field(body, entries)
    if entries:
        raise _validation_error(entries)
    assert site is not None and book_id is not None

    entry, created = _watchlist_store().add(site, book_id, title=title)
    return {"created": created, "entry": _entry_dict(entry)}


def _handle_watchlist_delete(
    _body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    # Path parameters are validated exactly like the REST route's
    # ``site: SiteKey`` / ``book_id: BookId`` (a malformed value is a 422
    # before the store lookup, not a misleading 404).
    entries: list[dict[str, Any]] = []
    site, site_err = _site_value(params.get("site", ""), source="path")
    if site_err is not None:
        entries.append(site_err)
    book_id, book_id_err = _book_id_value(params.get("book_id", ""), source="path")
    if book_id_err is not None:
        entries.append(book_id_err)
    if entries:
        raise _validation_error(entries)
    assert site is not None and book_id is not None

    if not _watchlist_store().remove(site, book_id):
        raise _ApiError(
            404, _MSG_BOOK_NOT_IN_WATCHLIST.format(site=site, book_id=book_id)
        )
    return {"ok": True}


def _sanitized_check_error(error: Any) -> str | None:
    """
    Reduce a per-book check error to its exception class name.

    The shared core formats failures as ``"<Class>: <message>"``; the REST
    handler exposes only ``type(exc).__name__`` because the message may embed
    provider URLs, tokens or internal paths.
    """
    if error is None:
        return None
    text = str(error).strip()
    if not text:
        return None
    return text.partition(":")[0].strip() or None


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
            # Only the exception class reaches the client; the full reason may
            # quote tokens or internal paths (same anti-leak rule as REST).
            "error": _sanitized_check_error(result.error),
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


#: Extensions the ``/downloads/music/<name>`` route may serve.
_MUSIC_EXTS = frozenset({"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "ape"})

#: Mirrors ``apps.api.schemas.MAX_MUSIC_TRACKS``.
_MAX_MUSIC_TRACKS = 200

#: In-process music jobs, guarded because the download callback runs in a
#: worker thread while request threads read the table.
_MUSIC_JOBS: dict[str, dict[str, Any]] = {}
_MUSIC_JOBS_LOCK = threading.Lock()
_MAX_MUSIC_JOBS = 100
#: Concurrent queued/running jobs.  Each job owns a plain thread, so this is
#: the real ceiling on worker threads (mirrors the REST API's limit).
_MAX_ACTIVE_MUSIC_JOBS = 4
_TERMINAL_MUSIC_STATUSES = frozenset({"done", "failed"})


def _music_components() -> Any:
    """
    Import the optional music engine.

    :raises _ApiError: 503 with the install hint, exactly like the REST API.
    """
    try:
        import mutagen  # noqa: F401
        import qrcode  # noqa: F401
        import websocket  # noqa: F401

        from novel_downloader.apps.music import paths
        from novel_downloader.apps.music.downloader import (
            DownloadOptions,
            download_many,
            download_mixed,
            export_m3u,
        )
        from novel_downloader.apps.music.providers import (
            available_labels,
            create,
            platform_note,
        )
        from novel_downloader.apps.music.providers.base import Track
        from novel_downloader.apps.music.providers.health import health_check
    except Exception as exc:  # noqa: BLE001 - optional dependency guard
        raise _ApiError(
            503, _MSG_MUSIC_UNAVAILABLE.format(error=type(exc).__name__)
        ) from exc
    return SimpleNamespace(
        paths=paths,
        download_options=DownloadOptions,
        download_many=download_many,
        download_mixed=download_mixed,
        export_m3u=export_m3u,
        available_labels=available_labels,
        create=create,
        platform_note=platform_note,
        track_cls=Track,
        health_check=health_check,
    )


def _music_keys(components: Any) -> list[str]:
    return [str(key) for key, _label in components.available_labels()]


def _require_music_platform(components: Any, platform: str) -> None:
    keys = _music_keys(components)
    if platform not in keys:
        raise _ApiError(
            400,
            _MSG_UNKNOWN_MUSIC_PLATFORM.format(platform=platform, keys=", ".join(keys)),
        )


def _create_music_provider(components: Any, platform: str) -> Any:
    """
    Instantiate a provider, mirroring ``routes._create_provider``.

    Unknown platforms are ``400``; a construction failure is a sanitized
    ``500`` (only the exception class reaches the client).
    """
    _require_music_platform(components, platform)
    try:
        return components.create(platform)
    except Exception as exc:  # noqa: BLE001 - provider construction
        logger.warning("Music platform %r failed to initialise: %s", platform, exc)
        raise _ApiError(
            500,
            _MSG_MUSIC_PLATFORM_INIT_FAILED.format(
                platform=platform, error=type(exc).__name__
            ),
        ) from exc


def _is_all_platforms(platform: Any) -> bool:
    """``platform`` 是否表示「全部平台」（``all`` 或空）。"""
    from novel_downloader.apps.music.aggregate import is_all_platforms

    return is_all_platforms(platform)


def _music_id_error(value: Any, *, source: str, name: str) -> dict[str, Any] | None:
    """
    Validate a ``search_id`` / ``job_id`` the way the REST models do.

    A non-string is a type error; a string must match
    :data:`_MUSIC_ID_PATTERN` (same Pydantic message/type, so both transports
    answer the same ``422``). ``search_id`` treats a blank value as absent
    *before* this check (mirroring ``MusicDownloadRequest``'s validator), so
    only a genuinely malformed id fails.
    """
    if not isinstance(value, str):
        return _err(name, "Input should be a valid string", "string_type", source)
    # ``fullmatch`` (not ``match``): ``$`` also matches before a trailing
    # newline, and Pydantic rejects ``"id\n"``; a decoded ``%0A`` must not slip
    # through into a lookup or a 404 detail.
    if _MUSIC_ID_RE.fullmatch(value) is None:
        return _err(
            name,
            f"String should match pattern '{_MUSIC_ID_PATTERN}'",
            "string_pattern_mismatch",
            source,
        )
    return None


def _platform_field(body: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """
    Validate ``platform`` the way the REST model does.

    ``MusicSearchRequest.platform`` (and the download/export bodies) is a
    ``Platform``: an absent key uses ``"all"``, while an explicit ``null``, a
    non-string, an empty string, an over-long value or one containing control
    characters is a ``422``. The value is *not* stripped -- REST does not strip
    either, so ``" netease "`` is an unknown platform on both transports
    instead of being silently accepted by only one.  A whitespace-only value
    is still accepted and means "all", exactly like ``is_all_platforms``.
    """
    if "platform" not in body:
        return "all", None
    value = body["platform"]
    if not isinstance(value, str):
        return "all", _err("platform", "Input should be a valid string", "string_type")
    if not value:
        return "all", _value_error("platform", "platform must not be empty")
    _, error = _clean_string(
        value,
        "platform",
        _MAX_PLATFORM_LENGTH,
        label="platform",
        allow_blank=True,
        strip=False,
    )
    if error is not None:
        return "all", error
    return value, None


def _optional_text_field(
    body: dict[str, Any],
    name: str,
    max_length: int,
    entries: list[dict[str, Any]],
) -> str | None:
    """
    Validate an optional path/level field like ``schemas.OptionalPath``.

    Absent/null means "use the default"; a non-string is a ``string_type``
    error (the old code ``str()``-coerced it, accepting payloads REST rejects)
    and a blank value collapses to ``None``.
    """
    raw, error = _typed_string(body, name)
    if error is not None:
        entries.append(error)
        return None
    if raw is None:
        return None
    cleaned, error = _clean_string(raw, name, max_length, allow_blank=True)
    if error is not None:
        entries.append(error)
        return None
    return cleaned or None


def _health_timeout(
    query: dict[str, list[str]], entries: list[dict[str, Any]]
) -> float:
    """
    Parse ``GET /music/health?timeout=`` like the REST ``Query(gt=0, le=60)``.

    The REST endpoint rejects an unparseable, non-positive or over-60 value
    with ``422``; the embedded engine used to clamp silently, which meant the
    same request succeeded on one transport and failed on the other.
    """
    values = query.get("timeout") or []
    if not values:
        return 5.0
    try:
        value = float(values[0])
    except (TypeError, ValueError):
        entries.append(
            _err(
                "timeout",
                "Input should be a valid number",
                "float_parsing",
                "query",
            )
        )
        return 5.0
    if not math.isfinite(value):
        # REST's ``Query(..., allow_inf_nan=False)`` reports NaN/±inf as a
        # non-finite number; don't pass those into the probe.
        entries.append(
            _err("timeout", "Input should be a finite number", "finite_number", "query")
        )
    elif value <= 0:
        entries.append(
            _err("timeout", "Input should be greater than 0", "greater_than", "query")
        )
    elif value > 60:
        entries.append(
            _err(
                "timeout",
                "Input should be less than or equal to 60",
                "less_than_equal",
                "query",
            )
        )
    return value


#: Free-form single-line text fields of ``MusicTrack`` (``TrackText``).
_MUSIC_TRACK_TEXT_FIELDS = ("provider", "id", "name", "artists", "album", "ext")


def _lax_int(value: Any) -> int | None:
    """Coerce like Pydantic's lax ``int`` mode (``"3"``/``3.0`` → 3)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _lax_bool(value: Any) -> bool | None:
    """Coerce like Pydantic's lax ``bool`` mode."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return value == 1.0
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _BOOL_TRUTHY:
            return True
        if lowered in _BOOL_FALSY:
            return False
    return None


def _media_url_error(
    value: Any, name: str, index: int, entries: list[dict[str, Any]]
) -> str | None:
    """Validate ``cover_url``/``download_url`` like ``schemas.TrackUrl``."""
    if not isinstance(value, str):
        entries.append(
            _err(
                f"tracks.{index}.{name}",
                "Input should be a valid string",
                "string_type",
            )
        )
        return None
    url = value.strip()
    if not url:
        return ""
    loc = f"tracks.{index}.{name}"
    if _has_surrogate(url):
        entries.append(
            _value_error(loc, "URL must not contain unpaired surrogate characters")
        )
        return None
    if len(url) > _MAX_MEDIA_URL_LENGTH:
        entries.append(
            _value_error(
                loc, f"URL must be at most {_MAX_MEDIA_URL_LENGTH} characters long"
            )
        )
        return None
    if _CONTROL_CHARS.search(url):
        entries.append(_value_error(loc, "URL must not contain control characters"))
        return None
    scheme = urlsplit(url).scheme.lower()
    if scheme and scheme not in {"http", "https"}:
        entries.append(_value_error(loc, "URL scheme must be http or https"))
        return None
    return url


def _music_track_fields(
    data: dict[str, Any], index: int, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Validate one submitted track like ``schemas.MusicTrack``.

    Returns the cleaned field map (only the keys the caller sent); invalid
    values append an entry to ``entries`` and are left out.  Unknown keys are
    ignored, matching Pydantic's default model config.
    """
    cleaned: dict[str, Any] = {}
    base = f"tracks.{index}"

    for name in _MUSIC_TRACK_TEXT_FIELDS:
        if name not in data:
            continue
        value = data[name]
        if not isinstance(value, str):
            entries.append(
                _err(f"{base}.{name}", "Input should be a valid string", "string_type")
            )
            continue
        text, error = _clean_string(
            value,
            f"{base}.{name}",
            _MAX_MUSIC_TRACK_TEXT_LENGTH,
            label="value",
            allow_blank=True,
        )
        if error is not None:
            entries.append(error)
        else:
            assert text is not None
            cleaned[name] = text

    for name in ("duration_s", "track_no"):
        if name not in data:
            continue
        number = _lax_int(data[name])
        if number is None:
            entries.append(
                _err(f"{base}.{name}", "Input should be a valid integer", "int_type")
            )
        else:
            cleaned[name] = number

    if "vip" in data:
        flag = _lax_bool(data["vip"])
        if flag is None:
            entries.append(
                _err(
                    f"{base}.vip",
                    "Input should be a valid boolean, unable to interpret input",
                    "bool_type",
                )
            )
        else:
            cleaned["vip"] = flag

    for name in ("cover_url", "download_url"):
        if name not in data:
            continue
        url = _media_url_error(data[name], name, index, entries)
        if url is not None:
            cleaned[name] = url

    if "path" in data:
        value = data["path"]
        if not isinstance(value, str):
            entries.append(
                _err(f"{base}.path", "Input should be a valid string", "string_type")
            )
        else:
            text, error = _clean_string(
                value,
                f"{base}.path",
                _MAX_OPTIONAL_PATH_LENGTH,
                label="path",
                allow_blank=True,
            )
            if error is not None:
                entries.append(error)
            else:
                cleaned["path"] = text

    if "lyric" in data:
        value = data["lyric"]
        if not isinstance(value, str):
            entries.append(
                _err(f"{base}.lyric", "Input should be a valid string", "string_type")
            )
        else:
            text, error = _clean_string(
                value,
                f"{base}.lyric",
                _MAX_MUSIC_LYRIC_LENGTH,
                label="lyric",
                allow_blank=True,
                allow_controls=True,
            )
            if error is not None:
                entries.append(error)
            else:
                cleaned["lyric"] = text

    if "http_headers" in data:
        headers = data["http_headers"]
        if not isinstance(headers, dict):
            entries.append(
                _err(
                    f"{base}.http_headers",
                    "Input should be a valid dictionary",
                    "dict_type",
                )
            )
        elif len(headers) > _MAX_HEADER_COUNT:
            entries.append(
                _value_error(
                    f"{base}.http_headers",
                    f"http_headers must contain at most {_MAX_HEADER_COUNT} entries",
                )
            )
        else:
            parsed_headers: dict[str, str] = {}
            for header_name, header_value in headers.items():
                if (
                    not isinstance(header_name, str)
                    or not header_name
                    or len(header_name) > _MAX_HEADER_NAME_LENGTH
                    or _HEADER_NAME_CHARS.fullmatch(header_name) is None
                ):
                    entries.append(
                        _value_error(
                            f"{base}.http_headers",
                            "http_headers contains an invalid header name",
                        )
                    )
                    continue
                if not isinstance(header_value, str):
                    entries.append(
                        _err(
                            f"{base}.http_headers",
                            "Input should be a valid string",
                            "string_type",
                        )
                    )
                    continue
                if _has_surrogate(header_value):
                    entries.append(
                        _value_error(
                            f"{base}.http_headers",
                            "http_headers values must not contain unpaired "
                            "surrogate characters",
                        )
                    )
                    continue
                if len(header_value) > _MAX_HEADER_VALUE_LENGTH:
                    entries.append(
                        _value_error(
                            f"{base}.http_headers",
                            "http_headers values must be at most "
                            f"{_MAX_HEADER_VALUE_LENGTH} characters long",
                        )
                    )
                    continue
                if "\r" in header_value or "\n" in header_value:
                    entries.append(
                        _value_error(
                            f"{base}.http_headers",
                            "http_headers values must not contain newlines",
                        )
                    )
                    continue
                parsed_headers[header_name] = header_value
            cleaned["http_headers"] = parsed_headers

    if "sources" in data:
        sources = data["sources"]
        if not isinstance(sources, list):
            entries.append(
                _err(f"{base}.sources", "Input should be a valid list", "list_type")
            )
        elif len(sources) > _MAX_TRACK_SOURCES:
            entries.append(
                _value_error(
                    f"{base}.sources",
                    f"sources must contain at most {_MAX_TRACK_SOURCES} entries",
                )
            )
        else:
            parsed_sources: list[str] = []
            for source in sources:
                if not isinstance(source, str):
                    entries.append(
                        _err(
                            f"{base}.sources",
                            "Input should be a valid string",
                            "string_type",
                        )
                    )
                    continue
                text, error = _clean_string(
                    source,
                    f"{base}.sources",
                    _MAX_MUSIC_TRACK_TEXT_LENGTH,
                    label="sources",
                    allow_blank=True,
                )
                if error is not None:
                    entries.append(error)
                else:
                    assert text is not None
                    parsed_sources.append(text)
            cleaned["sources"] = parsed_sources

    if "extra" in data:
        if not isinstance(data["extra"], dict):
            entries.append(
                _err(f"{base}.extra", "Input should be a valid dictionary", "dict_type")
            )
        else:
            cleaned["extra"] = data["extra"]

    return cleaned


def _music_track(
    components: Any,
    data: Any,
    index: int = 0,
    entries: list[dict[str, Any]] | None = None,
) -> Any:
    """Build the provider track from validated fields the dataclass knows."""
    if not isinstance(data, dict):
        if entries is not None:
            entries.append(
                _err(
                    f"tracks.{index}", "Input should be a valid dictionary", "dict_type"
                )
            )
            return None
        raise _validation_error(
            [_err("tracks", "Input should be a valid dictionary", "dict_type")]
        )
    bucket: list[dict[str, Any]] = [] if entries is None else entries
    cleaned = _music_track_fields(data, index, bucket)
    if entries is None and bucket:
        raise _validation_error(bucket)
    allowed = {field.name for field in dataclasses.fields(components.track_cls)}
    return components.track_cls(**{k: v for k, v in cleaned.items() if k in allowed})


def _music_tracks_field(
    body: dict[str, Any], components: Any, entries: list[dict[str, Any]]
) -> list[Any]:
    """Validate ``tracks`` (``1..MAX_MUSIC_TRACKS`` items) for download/export."""
    raw_tracks = body.get("tracks")
    if not isinstance(raw_tracks, list):
        entries.append(_err("tracks", "Input should be a valid list", "list_type"))
        return []
    if not raw_tracks:
        entries.append(
            _err(
                "tracks",
                "List should have at least 1 item after validation, not 0",
                "too_short",
            )
        )
        return []
    if len(raw_tracks) > _MAX_MUSIC_TRACKS:
        entries.append(
            _err(
                "tracks",
                f"List should have at most {_MAX_MUSIC_TRACKS} items",
                "too_long",
            )
        )
        return []
    tracks: list[Any] = []
    for index, item in enumerate(raw_tracks):
        if not isinstance(item, dict):
            entries.append(
                _err(
                    f"tracks.{index}",
                    "Input should be a valid dictionary or object to extract "
                    "fields from",
                    "model_attributes_type",
                )
            )
            continue
        tracks.append(_music_track(components, item, index, entries))
    return tracks


def _music_track_payload(track: Any) -> dict[str, Any]:
    """Serialise one track for the wire, dropping provider-private ``extra``.

    Shared with the REST API so both engines expose the same public fields;
    ``extra`` holds non-JSON provider objects (musicdl ``SongInfo``, NetEase
    raw payloads/cookies) and must never leave the process.
    """
    from novel_downloader.apps.music.serialize import track_dict

    return track_dict(track)


def _music_result_item(result: Any) -> dict[str, Any]:
    """Serialise one per-track download result for the wire.

    Shared rules with the REST API: the failure reason is query-stripped,
    folded onto one line and capped at ``REASON_MAX_CHARS`` (provider
    exceptions can quote signed URLs or whole HTML bodies).  ``download_url``
    is the loopback route the Android client hands to ``DownloadManager``;
    the REST API emits the same key with an empty value.
    """
    from novel_downloader.apps.music.serialize import sanitize_reason

    raw_path = str(getattr(result, "path", "") or "")
    download_url = ""
    if raw_path:
        name = Path(raw_path).name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if name and ext in _MUSIC_EXTS:
            # The Kotlin client hands this to DownloadManager, which can only
            # fetch over HTTP; the engine serves music/ through /downloads.
            download_url = f"/downloads/music/{quote(name, safe='')}"
    return {
        "status": str(getattr(result, "status", "fail")),
        "reason": sanitize_reason(getattr(result, "reason", "")),
        "path": raw_path,
        "download_url": download_url,
        "bytes": int(getattr(result, "bytes_", 0) or 0),
    }


def _handle_music_platforms(
    _body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    components = _music_components()
    return {
        "platforms": [
            {
                "key": str(key),
                "label": str(label),
                "note": str(components.platform_note(key)),
            }
            for key, label in components.available_labels()
        ]
    }


def _handle_music_search(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    components = _music_components()
    entries: list[dict[str, Any]] = []
    platform, platform_err = _platform_field(body)
    if platform_err is not None:
        entries.append(platform_err)
    keyword, keyword_err = _required_text(body, "keyword")
    if keyword_err is not None:
        entries.append(keyword_err)
    elif keyword is not None:
        keyword, keyword_err = _keyword_value(keyword)
        if keyword_err is not None:
            entries.append(keyword_err)
    limit = _int_field(body, "limit", 20, 1, entries, le=_MAX_MUSIC_SEARCH_LIMIT)
    offset = _int_field(body, "offset", 0, 0, entries, le=_MAX_MUSIC_OFFSET)
    if entries:
        raise _validation_error(entries)
    assert keyword is not None
    assert limit is not None and offset is not None

    from novel_downloader.apps.music import aggregate
    from novel_downloader.apps.music.trackcache import CACHE

    if _is_all_platforms(platform):
        result = aggregate.search_all(
            keyword,
            None,
            limit,
            per_platform_limit=aggregate.per_platform_limit_for(limit),
            timeout=aggregate.DEFAULT_TIMEOUT,
        )
        payload = [_music_track_payload(track) for track in result.tracks]
        return {
            "total": result.total,
            "tracks": payload,
            "search_id": CACHE.put(result.tracks),
            "platforms": [item.as_dict() for item in result.platforms],
            "platform": aggregate.ALL_PLATFORM_KEY,
            # Aggregate search always starts at hit 0 and does not page.
            "offset": 0,
            "supports_offset": False,
        }

    provider = _create_music_provider(components, platform)
    tracks, total = provider.search(keyword, limit=limit, offset=offset)
    payload = [_music_track_payload(track) for track in tracks]
    return {
        "total": int(total or len(payload)),
        "tracks": payload,
        "search_id": CACHE.put(tracks),
        "platform": platform,
        # Echo the skip the provider saw and whether it actually pages.
        "offset": offset,
        "supports_offset": bool(getattr(provider, "supports_offset", False)),
    }


def _handle_music_health(
    _body: dict[str, Any], _params: dict[str, str], query: dict[str, list[str]]
) -> list[dict[str, Any]]:
    components = _music_components()
    # Query parsing (``gt=0, le=60``) runs before the platform lookup: FastAPI
    # rejects a bad ``timeout`` with 422 before the handler ever sees 400 for
    # an unknown platform, so requests with both faults must agree.
    entries: list[dict[str, Any]] = []
    timeout = _health_timeout(query, entries)
    if entries:
        raise _validation_error(entries)

    values = query.get("platform") or []
    platform = values[0] if values else None
    if platform is not None:
        _require_music_platform(components, platform)
        selected = [platform]
    else:
        selected = _music_keys(components)

    results: list[dict[str, Any]] = []
    for key in selected:
        payload: dict[str, Any] = {"platform": key}
        payload.update(dict(components.health_check(key, timeout=timeout)))
        results.append(payload)
    return results


def _music_download_args(
    body: dict[str, Any], components: Any, entries: list[dict[str, Any]]
) -> tuple[str, list[Any], Any]:
    platform, platform_err = _platform_field(body)
    if platform_err is not None:
        entries.append(platform_err)
    tracks = _music_tracks_field(body, components, entries)
    # Blank means "no search id" on both transports; a malformed one is a 422
    # before the cache lookup (and before any log line quoting the value).
    search_id = body.get("search_id")
    if isinstance(search_id, str) and not search_id.strip():
        search_id = None
    if search_id is not None:
        id_error = _music_id_error(search_id, source="body", name="search_id")
        if id_error is not None:
            entries.append(id_error)
    # ``level`` / ``out_dir`` are ``OptionalLevel`` / ``OptionalPath``: a
    # non-string (or a hostile value) is a 422 here, before the filesystem
    # policy runs, exactly like the REST request model.
    level = _optional_text_field(body, "level", _MAX_LEVEL_LENGTH, entries)
    raw_out_dir = _optional_text_field(
        body, "out_dir", _MAX_OPTIONAL_PATH_LENGTH, entries
    )
    if entries:
        raise _validation_error(entries)
    if not _is_all_platforms(platform):
        _require_music_platform(components, platform)

    from novel_downloader.apps.music.trackcache import CACHE

    if search_id is not None:
        tracks = CACHE.resolve(search_id, tracks)

    out_dir = str(_resolve_music_out_dir(components, raw_out_dir))
    if raw_out_dir is None:
        # Preserve the historical behaviour: the default music directory is
        # created eagerly, while a caller-supplied directory is left to the
        # engine (which only creates it when a track is actually written).
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    options = components.download_options(out_dir=out_dir, level=level or "exhigh")
    return platform, tracks, options


def _music_export_dir() -> Path:
    """Directory the embedded engine writes music files to by default.

    No ``mkdir`` here: this runs on the validation path and must not create
    directories just to reject a request.
    """
    from novel_downloader.apps.common.history_files import _output_dir

    return Path(_output_dir()) / "music"


def _music_allowed_roots(components: Any) -> tuple[Path, list[Path]]:
    """Return ``(default_dir, allowed_roots)`` for embedded music writes."""
    default_dir = _music_export_dir()
    roots = [default_dir]
    try:
        roots.append(Path(components.paths.default_download_dir()).expanduser())
    except Exception as exc:  # noqa: BLE001 - platform dir lookup is best effort
        logger.debug("Could not resolve the default music directory: %s", exc)
    return default_dir, roots


def _resolve_music_out_dir(components: Any, out_dir: str | None) -> Path:
    """Validate a download output directory (same policy as the REST API)."""
    from novel_downloader.apps.music.export_path import (
        MSG_OUT_DIR_INVALID,
        OutDirTargetError,
        resolve_output_dir,
    )

    default_dir, roots = _music_allowed_roots(components)
    try:
        return resolve_output_dir(
            out_dir,
            base_dirs=roots,
            default_dir=default_dir,
        )
    except OutDirTargetError as exc:
        logger.warning("Rejected embedded music out_dir %r: %s", out_dir, exc)
        raise _ApiError(400, MSG_OUT_DIR_INVALID) from exc


def _resolve_music_export_target(components: Any, out_path: str | None) -> Path:
    """Validate the playlist destination (same policy as the REST API)."""
    from novel_downloader.apps.music.export_path import (
        MSG_PLAYLIST_TARGET_INVALID,
        PlaylistTargetError,
        resolve_playlist_target,
    )

    default_dir, roots = _music_allowed_roots(components)
    try:
        return resolve_playlist_target(
            out_path,
            base_dirs=roots,
            default_dir=default_dir,
        )
    except PlaylistTargetError as exc:
        logger.warning("Rejected embedded playlist out_path %r: %s", out_path, exc)
        raise _ApiError(400, MSG_PLAYLIST_TARGET_INVALID) from exc


def _handle_music_export(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    components = _music_components()
    entries: list[dict[str, Any]] = []
    platform, platform_err = _platform_field(body)
    if platform_err is not None:
        entries.append(platform_err)
    tracks = _music_tracks_field(body, components, entries)
    raw_out_path = _optional_text_field(
        body, "out_path", _MAX_OPTIONAL_PATH_LENGTH, entries
    )
    if entries:
        raise _validation_error(entries)
    if not _is_all_platforms(platform):
        _require_music_platform(components, platform)

    # Same rule as REST: a whitespace-only ``path`` is not a local file, and a
    # repeated file location is one playlist entry (not two identical rows).
    from novel_downloader.apps.music.export_path import playlist_keep_flags

    keep = playlist_keep_flags(getattr(track, "path", "") for track in tracks)
    usable = [track for track, keep_it in zip(tracks, keep, strict=True) if keep_it]
    skipped = len(tracks) - len(usable)
    target = _resolve_music_export_target(components, raw_out_path)
    try:
        written_path = components.export_m3u(usable, target)
    except OSError as exc:
        from novel_downloader.apps.music.export_path import MSG_PLAYLIST_WRITE_FAILED

        # Neither the resolved target nor the OS error text is sent to the
        # client; both may contain server-internal absolute paths.
        logger.warning("Embedded playlist write failed for %s: %s", target, exc)
        raise _ApiError(
            500, MSG_PLAYLIST_WRITE_FAILED.format(error=type(exc).__name__)
        ) from exc
    # ``export_m3u`` returns the playlist path (mirroring the REST handler's
    # ``path`` field), not a count: ``int()`` on it made every real export
    # answer 500.  Both ``written`` (the embedded spelling) and
    # ``tracks_written`` (the REST spelling) carry the number of tracks with a
    # local file, so the field set is identical on both transports and the
    # Android client's dual-key decoder keeps working.
    return {
        "path": str(written_path or target),
        "tracks_written": len(usable),
        "skipped": int(skipped),
        "written": len(usable),
    }


def _music_create_provider(components: Any, platform: str) -> Any:
    """``create_provider`` for ``download_mixed``.

    ``platform="all"`` lets every track use its own provider (aggregate
    search); an explicit key overrides it, as single-platform UIs do.  The
    explicit provider is built eagerly so an unknown/broken platform is a
    400/500 before the job starts (mirrors ``routes._music_download_callable``).
    """
    if _is_all_platforms(platform):
        return components.create
    provider = _create_music_provider(components, platform)
    return lambda _key: provider


def _handle_music_download(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    components = _music_components()
    entries: list[dict[str, Any]] = []
    platform, tracks, options = _music_download_args(body, components, entries)
    create_provider = _music_create_provider(components, platform)

    results: list[tuple[int, dict[str, Any]]] = []
    lock = threading.Lock()

    def on_result(index: int, _track: Any, result: Any) -> None:
        with lock:
            results.append((index, _music_result_item(result)))

    try:
        ok, skip, fail, _failed_pairs, _failed_tracks = components.download_mixed(
            create_provider, tracks, options, on_result=on_result
        )
    except _ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - download engine errors
        # Same anti-leak rule as REST: the client sees the exception class,
        # the operator sees the message in the log.
        logger.warning("Embedded music download failed: %s", exc)
        raise _ApiError(
            502, _MSG_MUSIC_DOWNLOAD_FAILED.format(error=type(exc).__name__)
        ) from exc
    ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
    return {"ok": int(ok), "skip": int(skip), "fail": int(fail), "results": ordered}


def _prune_music_jobs_locked() -> None:
    """
    Drop finished jobs to keep the table bounded.

    Caller must hold ``_MUSIC_JOBS_LOCK``.  Active (queued/running) records are
    never evicted here: dropping them would orphan a live worker thread and let
    clients poll a job that silently vanished.  The active count is capped
    separately by ``_MAX_ACTIVE_MUSIC_JOBS``.
    """
    while len(_MUSIC_JOBS) >= _MAX_MUSIC_JOBS:
        for job_id, job in list(_MUSIC_JOBS.items()):
            if job["status"] in _TERMINAL_MUSIC_STATUSES:
                del _MUSIC_JOBS[job_id]
                break
        else:
            return


def _active_music_jobs_locked() -> int:
    """Number of queued/running jobs (caller must hold the lock)."""
    return sum(
        1
        for job in _MUSIC_JOBS.values()
        if job["status"] not in _TERMINAL_MUSIC_STATUSES
    )


def _music_job_update(job_id: str, **fields: Any) -> None:
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _music_job_progress(job_id: str, done: int) -> None:
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is not None and job["status"] not in _TERMINAL_MUSIC_STATUSES:
            job["progress"]["done"] = done


def _music_job_worker(
    job_id: str,
    components: Any,
    create_provider: Any,
    tracks: list[Any],
    options: Any,
    timeout: float,
) -> None:
    """Run one job on a plain thread; the engine is synchronous anyway.

    A watchdog timer enforces ``timeout`` even when the engine produces no
    callbacks at all (mirroring REST's ``asyncio.wait_for``); a job that
    exceeds it is marked ``failed`` and the worker thread is left to finish on
    its own.  Terminal transitions are idempotent: the first one wins, so a
    late callback or exception cannot overwrite a recorded result.
    """
    _music_job_update(job_id, status="running")
    results: list[tuple[int, dict[str, Any]]] = []
    lock = threading.Lock()

    def _finish(status: str, reason: str | None) -> None:
        with lock:
            ordered = [item for _, item in sorted(results, key=lambda pair: pair[0])]
        with _MUSIC_JOBS_LOCK:
            job = _MUSIC_JOBS.get(job_id)
            if job is None or job["status"] in _TERMINAL_MUSIC_STATUSES:
                return
            job.update(
                status=status,
                results=ordered,
                error=reason,
                finished_at=time.time(),
            )

    def on_result(index: int, _track: Any, result: Any) -> None:
        with lock:
            results.append((index, _music_result_item(result)))
            done = len(results)
        _music_job_progress(job_id, done)

    timer = threading.Timer(
        timeout,
        _finish,
        args=("failed", _MSG_TIMED_OUT.format(label="Music download", timeout=timeout)),
    )
    timer.daemon = True
    timer.start()
    try:
        components.download_mixed(create_provider, tracks, options, on_result=on_result)
    except Exception as exc:  # noqa: BLE001 - reported in the job record
        # Only the exception class reaches the client: the message may carry
        # internal paths or credentials.  The full reason stays in the log.
        logger.warning("Embedded music job %s failed: %s", job_id, exc)
        _finish("failed", _MSG_MUSIC_DOWNLOAD_FAILED.format(error=type(exc).__name__))
        return
    finally:
        timer.cancel()
    _finish("done", None)


def _handle_music_jobs_post(
    body: dict[str, Any], _params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    components = _music_components()
    entries: list[dict[str, Any]] = []
    platform, tracks, options = _music_download_args(body, components, entries)
    create_provider = _music_create_provider(components, platform)

    job_id = uuid.uuid4().hex
    with _MUSIC_JOBS_LOCK:
        _prune_music_jobs_locked()
        if _active_music_jobs_locked() >= _MAX_ACTIVE_MUSIC_JOBS:
            raise _ApiError(
                429,
                _MSG_MUSIC_JOBS_BUSY.format(limit=_MAX_ACTIVE_MUSIC_JOBS),
                headers={"Retry-After": str(_MUSIC_JOBS_RETRY_AFTER_SECONDS)},
            )
        _MUSIC_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": {"done": 0, "total": len(tracks)},
            "results": [],
            "error": None,
            "created_at": time.time(),
            "finished_at": None,
        }
    threading.Thread(
        target=_music_job_worker,
        args=(job_id, components, create_provider, tracks, options, _MUSIC_JOB_TIMEOUT),
        name=f"noveldl-music-{job_id[:8]}",
        daemon=True,
    ).start()
    return {"job_id": job_id}


def _handle_music_job_status(
    _body: dict[str, Any], params: dict[str, str], _query: dict[str, list[str]]
) -> dict[str, Any]:
    job_id = params.get("job_id", "")
    # Same shape rule as the REST path parameter: a malformed id never reaches
    # the job table (or a log line), while an unknown well-formed one is 404.
    id_error = _music_id_error(job_id, source="path", name="job_id")
    if id_error is not None:
        raise _validation_error([id_error])
    with _MUSIC_JOBS_LOCK:
        job = _MUSIC_JOBS.get(job_id)
        if job is None:
            raise _ApiError(404, _MSG_MUSIC_JOB_NOT_FOUND.format(job_id=job_id))
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": dict(job["progress"]),
            "results": list(job["results"]),
            "error": job["error"],
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
        }


#: Handlers whose REST counterpart answers with a non-200 success status.
#: ``POST /api/v1/music/jobs`` is ``202 Accepted`` (the job runs in the
#: background); everything else is a plain ``200``.
_SUCCESS_STATUS: dict[Handler, int] = {_handle_music_jobs_post: 202}


ROUTES: dict[tuple[str, str], Handler] = {
    ("GET", "/api/v1/health"): _handle_health,
    ("GET", "/api/v1/sites"): _handle_sites,
    ("POST", "/api/v1/search"): _handle_search,
    ("GET", "/api/v1/browse/categories"): _handle_browse_categories,
    ("POST", "/api/v1/browse"): _handle_browse,
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
    ("POST", "/api/v1/music/search"): _handle_music_search,
    ("POST", "/api/v1/music/export"): _handle_music_export,
    ("POST", "/api/v1/music/download"): _handle_music_download,
    ("GET", "/api/v1/music/health"): _handle_music_health,
    ("POST", "/api/v1/music/jobs"): _handle_music_jobs_post,
    ("GET", "/api/v1/music/jobs/{job_id}"): _handle_music_job_status,
}


#: POST handlers whose REST counterpart declares **no** request-body model.
#: FastAPI only rejects a missing body when the route actually expects one, so
#: ``POST /tasks/{id}/cancel`` must keep working without a JSON payload.
_BODYLESS_POST_HANDLERS = frozenset({_handle_task_cancel})


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
            # Starlette's path converters never match an empty segment, so
            # ``/api/v1/tasks/`` is a 404, not a lookup of ``task_id=""``.
            if not got:
                return False
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


def _is_body_guarded(method: str, path: str) -> bool:
    """Whether the REST body-size/encoding guard applies to this request.

    Mirrors ``api.middleware.LimitBodySize._applies``: every state-changing
    request below ``/api/v1`` is covered, so a new POST route cannot silently
    opt out of the cap.
    """
    if method not in _BODY_LIMIT_METHODS:
        return False
    cleaned = path.rstrip("/") or "/"
    return cleaned == _BODY_LIMIT_PREFIX or cleaned.startswith(_BODY_LIMIT_PREFIX + "/")


def _allowed_methods(path: str) -> list[str]:
    """Methods the REST API would allow on ``path`` (for 405 responses)."""
    if path == "/downloads" or path.startswith("/downloads/"):
        return ["GET", "HEAD"]
    allowed: list[str] = []
    for route_method, pattern in ROUTES:
        if route_method not in allowed and _pattern_matches(pattern, path):
            allowed.append(route_method)
    # Starlette answers HEAD like GET but lists only "GET" in ``Allow`` for a
    # 405; the parity test pins that exact header, so don't add HEAD here.
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
        payload: Any,
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
        self._send_json(payload, error.status, error.headers or None)

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

    def _check_body_encoding(self) -> None:
        """Refuse compressed request bodies exactly like the REST middleware.

        The JSON endpoints parse raw UTF-8; a compressed body could only be a
        compression bomb, so it is rejected before anything is read.
        """
        encodings = [
            part.strip().lower()
            for value in self.headers.get_all("Content-Encoding") or []
            for part in value.split(",")
        ]
        if any(encoding not in ("", "identity") for encoding in encodings):
            self.close_connection = True
            raise _ApiError(415, _MSG_UNSUPPORTED_ENCODING)

    def _drain_body(self, path: str) -> None:
        """Consume an unread request body so keep-alive stays in sync."""
        guarded = _is_body_guarded(self.command, path)
        if guarded:
            self._check_body_encoding()
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
            if guarded:
                raise _ApiError(413, _MSG_BODY_TOO_LARGE)
            return
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _read_body(self, path: str) -> dict[str, Any]:
        """Read and decode the JSON request body (validation errors → 422)."""
        guarded = _is_body_guarded(self.command, path)
        if guarded:
            self._check_body_encoding()
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise _ApiError(501, "Chunked request bodies are not supported")
        length = self._content_length()
        if length is None:
            if self.headers.get("Content-Length") is not None:
                self.close_connection = True
                raise _ApiError(400, "Invalid Content-Length header")
            # Every POST route takes a required Pydantic body, so a missing
            # body is FastAPI's ``missing`` error, not an empty dictionary.
            raise _missing_body_error()
        if length == 0:
            raise _missing_body_error()
        if guarded and length > _MAX_BODY_BYTES:
            self.close_connection = True
            raise _ApiError(413, _MSG_BODY_TOO_LARGE)
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
        if loaded is None:
            # ``null`` is still "no body" to FastAPI's required-body rule.
            raise _missing_body_error()
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

        decoded = unquote(name)
        prefix, _, rest = decoded.partition("/")
        if prefix == "music":
            safe = Path(rest).name
            ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
            if not safe or ext not in _MUSIC_EXTS:
                self._send_json({"detail": "Not Found"}, 404)
                return
            path = _output_dir() / "music" / safe
        else:
            safe = Path(decoded).name
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
            if method == "POST" and handler not in _BODYLESS_POST_HANDLERS:
                body = self._read_body(path)
            else:
                self._drain_body(path)
                body = {}
            self._send_json(
                handler(body, params, query),
                _SUCCESS_STATUS.get(handler, 200),
            )
            return

        # No handler for this method on this path: mirror the REST API's
        # 404 / 405 (+ Allow).  The body, if any, is drained so the
        # keep-alive connection stays usable.
        allowed = _allowed_methods(path)
        self._drain_body(path)
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
        # Resume the queue saved by the previous engine run before the first
        # request can observe it.
        from novel_downloader.apps.core.task_manager import manager

        _run(manager.start())
        server = _QuietThreadingHTTPServer((_HOST, port), _Handler)
        _server = server
    threading.Thread(
        target=server.serve_forever,
        name="noveldl-embedded-http",
        daemon=True,
    ).start()
    bound_port = int(server.server_address[1])
    logger.info("embedded engine listening on %s:%s", _HOST, bound_port)
    # Return the socket's actual port, not the requested one: ``port=0`` asks
    # the OS for a free loopback port so parallel test runs cannot collide.
    return bound_port


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
