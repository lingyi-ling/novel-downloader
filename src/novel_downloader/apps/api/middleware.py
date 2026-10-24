#!/usr/bin/env python3
"""
novel_downloader.apps.api.middleware
------------------------------------

Request-hardening helpers shared by the REST API application.

* :class:`BodySizeLimitMiddleware` rejects oversized JSON bodies with ``413``
  *before* FastAPI parses them, so a huge payload cannot be turned into a
  memory/CPU burden by the JSON decoder.
* :class:`DownloadsAuthMiddleware` applies the same token check as
  ``/api/v1`` to the ``/downloads`` static mount, which is not covered by the
  router-level dependency.
* :func:`register_error_handlers` guarantees that every error response has the
  same ``{"detail": ...}`` shape and that unexpected exceptions never leak a
  stack trace to the client.

Both are deliberately framework-light so they can be unit-tested through
``httpx.ASGITransport`` without starting a server.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.status import HTTP_401_UNAUTHORIZED
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AUTH_ERROR_DETAIL, is_authorized

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


def _content_length(scope: Scope) -> int | None:
    """Return the declared ``Content-Length`` of the request, if any."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


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
    "DEFAULT_MAX_BODY_BYTES",
    "DOWNLOADS_PREFIX",
    "BodySizeLimitMiddleware",
    "DownloadsAuthMiddleware",
    "register_error_handlers",
]
