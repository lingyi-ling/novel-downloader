#!/usr/bin/env python3
"""
novel_downloader.apps.api.constants
-----------------------------------

Contract strings shared by the REST API server and its Python client.

:mod:`novel_downloader.apps.api.routes` emits these as HTTP ``detail``
payloads; :mod:`novel_downloader.apps.api.client` re-raises some of them
locally when it reproduces server behaviour (e.g. the ``task()`` fallback for
servers without ``GET /tasks/{task_id}``). Building both sides from the same
constants keeps a wording change from desynchronising the wire contract from
the SDK.

Placeholders are ``str.format`` fields, filled in at the call site with
``MSG_X.format(...)``.
"""

from __future__ import annotations

__all__ = [
    "LABEL_MUSIC_DOWNLOAD",
    "MSG_BOOK_NOT_IN_WATCHLIST",
    "MSG_LOGIN_REQUEST_ALREADY_COMPLETED",
    "MSG_LOGIN_REQUEST_NOT_FOUND",
    "MSG_MUSIC_DOWNLOAD_FAILED",
    "MSG_MUSIC_JOB_NOT_FOUND",
    "MSG_MUSIC_PLATFORM_INIT_FAILED",
    "MSG_MUSIC_UNAVAILABLE",
    "MSG_PLAYLIST_WRITE_FAILED",
    "MSG_TASK_NOT_FOUND",
    "MSG_TIMED_OUT",
    "MSG_UNKNOWN_MUSIC_PLATFORM",
]

# ---------------------------------------------------------------------------
# Download tasks
# ---------------------------------------------------------------------------

#: ``detail`` of the 404 responses for unknown tasks (and the client's own
#: error when its ``task()`` fallback cannot find the task either).
MSG_TASK_NOT_FOUND = "Task not found: {task_id}"

# ---------------------------------------------------------------------------
# Login credential requests
# ---------------------------------------------------------------------------

#: ``detail`` of the 404 response for an unknown credential request id.
MSG_LOGIN_REQUEST_NOT_FOUND = "Login request not found: {req_id}"

#: ``detail`` of the 409 response for a request that is done but not yet reaped.
MSG_LOGIN_REQUEST_ALREADY_COMPLETED = "Login request already completed: {req_id}"

# ---------------------------------------------------------------------------
# Watchlist (追更)
# ---------------------------------------------------------------------------

#: ``detail`` of the 404 response when removing a book that is not followed.
MSG_BOOK_NOT_IN_WATCHLIST = "Book not in watchlist: {site}/{book_id}"

# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

#: ``detail`` of the 503 response when the ``music`` extras are missing.
#: ``error`` carries only the exception class name — internal error details
#: must not leak to clients (same anti-leak rule as the generic 500 handler).
MSG_MUSIC_UNAVAILABLE = (
    "Music support is not available: {error}. "
    'Install it with: pip install "novel-downloader[music]"'
)

#: ``detail`` of the 400 responses for a platform that is not registered.
#: ``keys`` is filled in at the call site with the comma-joined registered
#: platform keys, mirroring the CLI's message for the same condition.
MSG_UNKNOWN_MUSIC_PLATFORM = "Unknown music platform: {platform}. Available: {keys}"

#: ``detail`` of the 500 response when a known platform cannot be constructed.
#: ``error`` carries only the exception class name — the message may embed
#: internal paths or credentials and is kept in the server log instead.
MSG_MUSIC_PLATFORM_INIT_FAILED = (
    "Could not initialise music platform {platform!r}: {error}"
)

#: ``detail`` of the 500 response when writing an M3U8 playlist fails.
#: ``target`` is the caller's own input and safe to echo; ``error`` carries
#: only the exception class name (same anti-leak rule as above).
MSG_PLAYLIST_WRITE_FAILED = "Could not write playlist {target}: {error}"

#: ``detail`` of the 502 response when the download engine raises.
#: ``error`` carries only the exception class name (same anti-leak rule).
MSG_MUSIC_DOWNLOAD_FAILED = "Music download failed: {error}"

#: ``detail`` of the 404 response for an unknown async music job id.
MSG_MUSIC_JOB_NOT_FOUND = "Music job not found: {job_id}"

# ---------------------------------------------------------------------------
# Blocking endpoints
# ---------------------------------------------------------------------------

#: ``detail`` of the 504 responses when an endpoint exceeds its budget, and the
#: error recorded on a music job that was killed by its timeout.
MSG_TIMED_OUT = "{label} timed out after {timeout:g}s"

#: Label for the music download budget; shared by the 504 ``detail`` and the
#: asynchronous job's ``error`` field so both spell it the same way.
LABEL_MUSIC_DOWNLOAD = "Music download"
