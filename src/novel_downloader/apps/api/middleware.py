#!/usr/bin/env python3
"""
novel_downloader.apps.api.middleware
------------------------------------

Request-hardening helpers shared by the REST API application.

* :class:`OriginCheckMiddleware` answers requests whose ``Origin`` header is
  not a loopback origin with ``403``. By default the API binds to
  ``127.0.0.1`` without a token: that stops *remote* clients, but not
  *browser* ones — a website the user visits can fire cross-origin requests
  at the local port (browsers always attach ``Origin`` to cross-origin
  POSTs). Non-browser clients (CLI, SDK, curl) send no ``Origin`` and pass
  unchanged; ``--cors`` removes the gate and lets CORS govern browsers.
* :class:`BodySizeLimitMiddleware` rejects oversized JSON bodies with ``413``
  *before* FastAPI parses them, so a huge payload cannot be turned into a
  memory/CPU burden by the JSON decoder.
* :class:`DownloadsAuthMiddleware` applies the same token check as
  ``/api/v1`` to the ``/downloads`` static mount, which is not covered by the
  router-level dependency.
* :class:`RateLimitMiddleware` keeps a sliding-window request counter per
  client IP (in memory; default 120 requests per 60 seconds). A client over
  the limit — e.g. an integration script stuck in an infinite retry loop — is
  answered with ``429`` + ``Retry-After`` instead of being able to hammer the
  local server indefinitely. ``/api/v1/health`` is exempt and stale per-IP
  entries are dropped periodically so the counter cannot grow without bound.
* :class:`RequestLoggingMiddleware` emits exactly one access-log line per
  request (``method path status duration``). The query string is never logged
  (it may carry tokens) and response bodies are never read for logging, so
  the file content streamed from ``/downloads`` stays out of the log.
* :class:`CacheControlMiddleware` stamps every ``/api/v1`` and ``/downloads``
  response with the right ``Cache-Control``: short client-side caching for
  slowly changing reference data, ``no-store`` for live data and for every
  error response, long-lived public caching for exported files. A header set
  by a route (the SSE streams' ``no-cache``) is never overridden.
* :func:`register_error_handlers` guarantees that every error response has the
  same ``{"detail": ...}`` shape and that unexpected exceptions never leak a
  stack trace to the client.

All of them are deliberately framework-light so they can be unit-tested
through ``httpx.ASGITransport`` without starting a server.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import time
from collections import deque
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AUTH_ERROR_DETAIL, is_authorized

logger = logging.getLogger(__name__)

#: Default request-body ceiling for JSON endpoints: 1 MiB.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

#: Path prefix whose static files require the API token when one is configured.
DOWNLOADS_PREFIX = "/downloads"

#: Endpoints whose request bodies are bounded by :class:`BodySizeLimitMiddleware`.
#: Only bodies that are fully buffered by FastAPI need a cap; the streaming
#: endpoints (search SSE, ``/downloads``) carry no request body.
BODY_LIMIT_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
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

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

#: Default requests admitted per client IP per window: 120 req/min.
DEFAULT_RATE_LIMIT = 120

#: Default sliding-window length in seconds.
DEFAULT_RATE_LIMIT_WINDOW = 60.0

#: Paths never rate limited. Uptime monitors and orchestrators must keep
#: working even while a misbehaving client is locked out.
RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset({"/api/v1/health"})

#: Clock used by :class:`RateLimitMiddleware`. A module-level hook so tests
#: can freeze or advance time deterministically instead of sleeping.
_monotonic = time.monotonic

# ---------------------------------------------------------------------------
# Cache-Control policy
# ---------------------------------------------------------------------------

#: Requests slower than this many seconds are logged at ``WARNING``.
SLOW_REQUEST_SECONDS = 1.0

#: ``Cache-Control`` for reference data the client may reuse briefly.
CACHE_CONTROL_PRIVATE = "private, max-age=60"

#: ``Cache-Control`` for exported files: written once and never rewritten.
CACHE_CONTROL_PUBLIC = "public, max-age=3600"

#: ``Cache-Control`` for live data and for every error response.
CACHE_CONTROL_NO_STORE = "no-store"

#: ``path prefix`` -> ``Cache-Control`` for successful responses. Matching is
#: boundary-aware: ``/api/v1/tasks`` also covers ``/api/v1/tasks/{task_id}``
#: and the SSE stream, but not a look-alike such as ``/api/v1/tasks-extra``.
CACHE_CONTROL_RULES: tuple[tuple[str, str], ...] = (
    ("/api/v1/sites", CACHE_CONTROL_PRIVATE),
    ("/api/v1/history", CACHE_CONTROL_PRIVATE),
    ("/api/v1/music/platforms", CACHE_CONTROL_PRIVATE),
    ("/api/v1/tasks", CACHE_CONTROL_NO_STORE),
    (DOWNLOADS_PREFIX, CACHE_CONTROL_PUBLIC),
)

#: Path prefixes the cache policy governs. Requests outside them (``/docs``,
#: ``/openapi.json``, ...) pass through without any header being added.
CACHE_POLICY_PREFIXES: tuple[str, ...] = ("/api/v1", DOWNLOADS_PREFIX)


def _content_length(scope: Scope) -> int | None:
    """Return the declared ``Content-Length`` of the request, if any."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Browser-origin gate
# ---------------------------------------------------------------------------

#: Error text for requests whose ``Origin`` is not a loopback origin.
ORIGIN_ERROR_DETAIL = (
    "Forbidden: cross-origin browser requests are not allowed on this local "
    "API. Non-browser clients (CLI, SDK, curl) are unaffected. Start the "
    "server with --cors to allow browser access from other origins."
)

#: Host name that browsers may resolve to a loopback address.
_LOCAL_HOSTNAME = "localhost"


def _is_loopback_origin(origin: str) -> bool:
    """
    Whether an ``Origin`` header value names a local (loopback) origin.

    The value must be a well-formed ``http``/``https`` origin whose host is
    ``localhost`` (case-insensitive) or a loopback IP (IPv4 ``127.0.0.0/8``
    or IPv6 ``::1``); the port is irrelevant because any local port may serve
    a helper page. Everything else — foreign hosts, look-alikes such as
    ``127.0.0.1.evil.com``, the literal ``null`` (sandboxed frames), an empty
    value and anything unparseable — is not local. Userinfo
    (``http://evil.com@127.0.0.1``) is rejected outright so the host cannot
    be smuggled past the check.
    """
    try:
        parsed = urlsplit(origin.strip())
    except ValueError:  # e.g. an IPv6 literal with invalid brackets
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host == _LOCAL_HOSTNAME:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OriginCheckMiddleware:
    """
    Answer requests with a non-loopback ``Origin`` header with ``403``.

    Threat model: by default the API binds to ``127.0.0.1`` without a token.
    That stops *remote* clients, but not *browser* ones — a website the user
    visits can still fire cross-origin requests at ``http://127.0.0.1:<port>``
    (browsers always attach ``Origin`` to cross-origin POSTs, so state-changing
    endpoints can be driven as CSRF). This gate runs closest to the router and
    closes that hole:

    * non-browser clients (CLI, SDK, curl) send no ``Origin`` and pass;
    * loopback origins (``http://127.0.0.1:any``, ``http://localhost``,
      ``http://[::1]``) pass, so locally served pages keep working;
    * every other ``Origin`` — including look-alike hosts
      (``127.0.0.1.evil.com``), userinfo tricks and the literal ``null`` —
      is answered with ``403 {"detail": ...}`` marked ``no-store``.

    Layering: it is the innermost custom layer, directly above the router.
    That keeps it *inside* the body-size limit (an oversized body keeps its
    specific ``413``), the rate limiter and the ``/downloads`` token guard,
    while still running before the router's token dependency — a forged
    origin is rejected with ``403`` even when a token is configured. Its
    ``403`` flows back out through the cache/logging layers, so it is stamped
    ``no-store`` and access-logged like every other response. ``create_app``
    does not install the gate when ``--cors`` was requested: the flag is the
    explicit opt-in for cross-origin browser access, and then CORS itself
    governs what browsers may do.

    Residual risk, by design: a same-origin DNS-rebinding page sends no
    ``Origin`` at all and is not caught by this header check; running the API
    with a token (``--token`` / ``NOVEL_API_TOKEN``) is the complete defense.

    :param app: The wrapped ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin is None or _is_loopback_origin(origin):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=HTTP_403_FORBIDDEN,
            content={"detail": ORIGIN_ERROR_DETAIL},
            # Errors must never be cached, on this layer's own authority.
            headers={"Cache-Control": CACHE_CONTROL_NO_STORE},
        )
        await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """
    Reject request bodies larger than ``max_bytes`` with ``413``.

    The check runs before the application: when ``Content-Length`` already
    exceeds the limit the body is never read, and otherwise the body is read
    chunk by chunk and the request is aborted as soon as the running total
    crosses the limit (so chunked uploads without ``Content-Length`` are
    covered too). Accepted bodies are replayed to the application unchanged.

    :param app: The wrapped ASGI application.
    :param max_bytes: Maximum accepted body size in bytes.
    :param endpoints: ``(method, path)`` pairs to guard; defaults to
        :data:`BODY_LIMIT_ENDPOINTS`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        endpoints: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.endpoints = frozenset(
            BODY_LIMIT_ENDPOINTS if endpoints is None else endpoints
        )

    def _applies(self, method: str, path: str) -> bool:
        """Whether this request is guarded by the size limit."""
        cleaned = path.rstrip("/") or "/"
        return (method.upper(), cleaned) in self.endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        if not self._applies(method, path):
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # The client went away; there is nobody to answer.
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False
        buffered = bytes(body)

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send a ``413`` error response without reading the offending body."""
        response = JSONResponse(
            status_code=413,
            content={
                "detail": (
                    "Request body too large: the limit for this endpoint is "
                    f"{self.max_bytes} bytes"
                )
            },
        )
        await response(scope, receive, send)


class DownloadsAuthMiddleware:
    """
    Require the configured API token for requests under ``/downloads``.

    Static files are served by a ``Mount`` rather than a route, so the
    router-level :func:`novel_downloader.apps.api.auth.require_token` dependency
    never runs for them. This middleware closes that gap: when
    ``app.state.api_token`` is set, ``Authorization: Bearer <token>`` or
    ``X-API-Key: <token>`` is mandatory and a missing/mismatched token yields
    ``401 {"detail": ...}``. When no token is configured the request passes
    through unchanged, preserving the localhost-only default.

    :param app: The wrapped ASGI application.
    :param prefix: Path prefix to guard; defaults to ``/downloads``.
    """

    def __init__(self, app: ASGIApp, *, prefix: str = DOWNLOADS_PREFIX) -> None:
        self.app = app
        self.prefix = "/" + prefix.strip("/")

    def _guards(self, path: str) -> bool:
        """Whether ``path`` belongs to the guarded mount (not a look-alike)."""
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._guards(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        state = getattr(scope.get("app"), "state", None)
        expected = getattr(state, "api_token", None)
        if not expected:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if is_authorized(
            expected,
            headers.get("authorization"),
            headers.get("x-api-key"),
        ):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={"detail": AUTH_ERROR_DETAIL},
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


class RateLimitMiddleware:
    """
    Limit each client IP to ``limit`` requests per sliding ``window`` seconds.

    A true sliding window: every admitted request is timestamped and the
    counter keeps one ``deque`` of timestamps per client IP. A request is
    admitted while fewer than ``limit`` timestamps fall inside
    ``(now - window, now]``; once the IP is over the limit it receives::

        429 {"detail": "Rate limit exceeded. Try again in <seconds>s."}

    with a ``Retry-After`` header naming how long it takes until the oldest
    admitted request slides out of the window. Rejected requests are *not*
    counted, so a client that stops hammering recovers as soon as its window
    drains.

    Design notes:

    * Purpose: a buggy integration client stuck in a request loop must not be
      able to hammer the local server indefinitely. Counting happens *before*
      authentication, so a token brute-force is throttled too.
    * ``limit <= 0`` disables the middleware entirely (pass-through), matching
      ``--rate-limit 0``.
    * Paths in :data:`RATE_LIMIT_EXEMPT_PATHS` (``/api/v1/health``) are never
      limited: uptime monitors must keep working even while a client is
      locked out.
    * State is a plain in-memory ``dict`` updated without an ``await`` between
      read and write, i.e. race-free under uvicorn's single event loop and
      free of locking. It is per-process by design: this is a local API, not a
      distributed one. The client IP is taken from the ASGI scope (the TCP
      peer); ``X-Forwarded-For`` is deliberately not honoured on a localhost
      service.
    * Stale per-IP entries are dropped at most once per window
      (:meth:`_maybe_cleanup`), keeping memory bounded even with many
      short-lived client IPs.

    :param app: The wrapped ASGI application.
    :param limit: Requests admitted per client IP per window; ``<= 0`` disables.
    :param window: Sliding-window length in seconds.
    :param exempt_paths: Paths never limited; defaults to
        :data:`RATE_LIMIT_EXEMPT_PATHS`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = DEFAULT_RATE_LIMIT,
        window: float = DEFAULT_RATE_LIMIT_WINDOW,
        exempt_paths: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self.limit = int(limit)
        self.window = float(window)
        self.exempt_paths = frozenset(
            RATE_LIMIT_EXEMPT_PATHS if exempt_paths is None else exempt_paths
        )
        self._hits: dict[str, deque[float]] = {}
        self._last_cleanup = 0.0

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        """Best-effort client identity for the counter (the TCP peer)."""
        client = scope.get("client")
        host = client[0] if client else None
        return str(host) if host else "unknown"

    def _is_exempt(self, path: str) -> bool:
        """Whether ``path`` is never limited (trailing slash normalised)."""
        cleaned = path.rstrip("/") or "/"
        return cleaned in self.exempt_paths

    def _register_hit(self, key: str, now: float) -> int | None:
        """
        Count one request for ``key`` at ``now``.

        :return: ``None`` when the request is admitted (its timestamp was
            recorded), otherwise the ``Retry-After`` seconds until a slot
            frees up.
        """
        timestamps = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if self.limit > 0 and len(timestamps) >= self.limit:
            return max(math.ceil(timestamps[0] + self.window - now), 1)
        timestamps.append(now)
        self._maybe_cleanup(now)
        return None

    def _maybe_cleanup(self, now: float) -> None:
        """Drop per-IP state that has fully slid out of the window."""
        if now - self._last_cleanup < self.window:
            return
        cutoff = now - self.window
        stale = {
            key
            for key, timestamps in self._hits.items()
            if not timestamps or timestamps[-1] <= cutoff
        }
        for key in stale:
            del self._hits[key]
        self._last_cleanup = now

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if self.limit <= 0 or self._is_exempt(path):
            await self.app(scope, receive, send)
            return

        retry_after = self._register_hit(self._client_ip(scope), _monotonic())
        if retry_after is not None:
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Try again in {retry_after}s."
                },
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class CacheControlMiddleware:
    """
    Stamp every API response with the ``Cache-Control`` it should carry.

    Successful responses get the value of the first boundary-aware prefix rule
    in :data:`CACHE_CONTROL_RULES`:

    * ``/api/v1/sites``, ``/api/v1/history`` and ``/api/v1/music/platforms``:
      ``private, max-age=60`` — reference data the client may reuse briefly.
    * ``/api/v1/tasks...``: ``no-store`` — live task state must be refetched.
    * ``/downloads...``: ``public, max-age=3600`` — exported files are
      write-once, so exported-file URLs never change their content.

    Every other ``/api/v1`` path and every ``4xx``/``5xx`` response (413 body
    limit, 401 auth, 404, 422, 500, ...) gets ``no-store``: errors must never
    be cached. Responses outside the governed prefixes pass through without a
    header, and a ``Cache-Control`` already set by a route — the SSE streams'
    ``no-cache`` — is never overridden. The middleware only rewrites the
    response head; bodies are streamed through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _matches(cleaned: str, prefix: str) -> bool:
        """Whether ``cleaned`` is ``prefix`` itself or below it."""
        return cleaned == prefix or cleaned.startswith(prefix + "/")

    def _governs(self, cleaned: str) -> bool:
        """Whether the policy applies to this path at all."""
        return any(self._matches(cleaned, prefix) for prefix in CACHE_POLICY_PREFIXES)

    def _rule_for(self, cleaned: str) -> str | None:
        """Return the configured ``Cache-Control`` for the path, if any."""
        for prefix, value in CACHE_CONTROL_RULES:
            if self._matches(cleaned, prefix):
                return value
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        cleaned = str(scope.get("path", "")).rstrip("/") or "/"
        if not self._governs(cleaned):
            await self.app(scope, receive, send)
            return

        rule = self._rule_for(cleaned)

        async def send_with_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    if message["status"] >= 400 or rule is None:
                        headers["cache-control"] = CACHE_CONTROL_NO_STORE
                    else:
                        headers["cache-control"] = rule
            await send(message)

        await self.app(scope, receive, send_with_cache)


class RequestLoggingMiddleware:
    """
    Emit exactly one access-log line per HTTP request.

    The line has a single fixed shape for every outcome::

        GET /api/v1/sites 200 1.8ms

    i.e. ``method path status duration_ms``. Fast requests are logged at
    ``INFO``; requests that took longer than ``slow_seconds`` (default
    :data:`SLOW_REQUEST_SECONDS`) are logged at ``WARNING`` instead, with the
    same line format.

    Privacy guarantees:

    * The query string is never logged — only the plain path — because query
      parameters may carry tokens or other credentials.
    * Response bodies are never read or logged; in particular the file
      content streamed from ``/downloads`` never reaches the log.

    The middleware wraps ``send`` only: request bodies pass through untouched
    and nothing is buffered. A request that ends without any response (the
    client disconnected, or an error propagated above the router to the outer
    ``ServerErrorMiddleware``) is still logged once, reported as ``500``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        slow_seconds: float = SLOW_REQUEST_SECONDS,
    ) -> None:
        self.app = app
        self.slow_seconds = float(slow_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "-"))
        # Deliberately the path only: the query string may carry secrets.
        path = str(scope.get("path", "-"))
        started = time.perf_counter()
        logged = False

        async def send_and_log(message: Message) -> None:
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                logged = True
                self._log(
                    method,
                    path,
                    int(message["status"]),
                    time.perf_counter() - started,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_and_log)
        finally:
            if not logged:
                # No response head ever went out (client disconnect or an
                # error raised past the router): still record the request,
                # reported as the failure it was.
                self._log(method, path, 500, time.perf_counter() - started)

    def _log(self, method: str, path: str, status: int, elapsed: float) -> None:
        """Emit the single access-log line: INFO, or WARNING when slow."""
        line = f"{method} {path} {status} {elapsed * 1000:.1f}ms"
        if elapsed > self.slow_seconds:
            logger.warning("%s", line)
        else:
            logger.info("%s", line)


def _validation_detail(exc: RequestValidationError) -> str:
    """Render a validation failure as one readable sentence."""
    parts: list[str] = []
    for error in exc.errors():
        location = [
            str(part)
            for part in error.get("loc", ())
            if part not in ("body", "query", "path", "header")
        ]
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{'.'.join(location)}: {message}" if location else message)
    if not parts:
        return "Request validation failed"
    return "Request validation failed: " + "; ".join(parts)


def _validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """
    Return machine-readable validation errors.

    Only plain-string fields are kept: Pydantic error entries may carry a
    ``ctx`` holding the original exception or non-JSON ``input`` bytes, which
    would make the response unserialisable.
    """
    return [
        {
            "type": str(error.get("type", "")),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
        }
        for error in exc.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    """
    Register the API's error handlers.

    * ``422`` keeps the ``detail`` key but renders it as a readable sentence and
      adds an ``errors`` list with the raw Pydantic entries.
    * Any unhandled exception becomes ``500 {"detail": "Internal server error"}``
      with no traceback or exception message exposed to the caller.
    """

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": _validation_detail(exc),
                "errors": _validation_errors(exc),
            },
        )

    @app.exception_handler(Exception)
    async def _on_unhandled_error(_request: Request, _exc: Exception) -> JSONResponse:
        # Never include ``str(exc)`` or a traceback: it may reveal filesystem
        # paths, tokens or provider internals to the client.
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


__all__ = [
    "AUTH_ERROR_DETAIL",
    "BODY_LIMIT_ENDPOINTS",
    "CACHE_CONTROL_NO_STORE",
    "CACHE_CONTROL_PRIVATE",
    "CACHE_CONTROL_PUBLIC",
    "CACHE_CONTROL_RULES",
    "CACHE_POLICY_PREFIXES",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_LIMIT_WINDOW",
    "DOWNLOADS_PREFIX",
    "ORIGIN_ERROR_DETAIL",
    "RATE_LIMIT_EXEMPT_PATHS",
    "SLOW_REQUEST_SECONDS",
    "BodySizeLimitMiddleware",
    "CacheControlMiddleware",
    "DownloadsAuthMiddleware",
    "OriginCheckMiddleware",
    "RateLimitMiddleware",
    "RequestLoggingMiddleware",
    "register_error_handlers",
]
