#!/usr/bin/env python3
"""
novel_downloader.apps.api.app
-----------------------------

FastAPI application factory for the local REST API.

Security model
--------------
The API is meant to run on the same machine as the downloader. By default it
binds to ``127.0.0.1`` and performs no authentication. If a token is supplied
(``--token`` or ``NOVEL_API_TOKEN``) every ``/api/v1/*`` request and every
``/downloads/*`` static-file request must carry
``Authorization: Bearer <token>`` or ``X-API-Key: <token>``.

The optional ``--cors`` flag enables ``Access-Control-Allow-Origin: *`` so
browser extensions / local tools can call the API from a web page. This is
dangerous on a non-localhost bind: any website the user visits could then call
the API with the user's network position. Only enable it for local use and keep
a token configured when the server is exposed beyond ``127.0.0.1``.

Additional hardening
--------------------
* Request bodies of the JSON endpoints are capped (see
  :class:`novel_downloader.apps.api.middleware.BodySizeLimitMiddleware`).
* Every request is logged as a single ``method path status duration`` line
  (:class:`novel_downloader.apps.api.middleware.RequestLoggingMiddleware`,
  ``WARNING`` once it exceeds one second); query strings and response bodies
  are never logged.
* Every ``/api/v1`` and ``/downloads`` response carries the ``Cache-Control``
  its data allows (:class:`novel_downloader.apps.api.middleware.
  CacheControlMiddleware`): ``private, max-age=60`` for the reference
  endpoints (``/sites``, ``/history``, ``/music/platforms``), ``no-store``
  for live task data and for every error response, and
  ``public, max-age=3600`` for the write-once exported files.
* Every error response is shaped as ``{"detail": ...}`` and unexpected
  exceptions are turned into a stack-trace-free ``500``.

Webhook
-------
When ``NOVEL_API_WEBHOOK`` (or ``create_app(webhook_url=...)``) is set, each
download task that reaches a terminal state is announced with a background
``POST {"event": "task_finished", "task": {...}}`` carrying a 5-second timeout.
Delivery failures are logged only; they never block the task manager.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from novel_downloader import __version__
from novel_downloader.apps.core.task_manager import manager
from novel_downloader.infra.config import get_config_value

from .middleware import (
    DEFAULT_MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    CacheControlMiddleware,
    DownloadsAuthMiddleware,
    RequestLoggingMiddleware,
    register_error_handlers,
)
from .routes import OPENAPI_TAGS, router, task_to_dict
from .webhook import (
    TaskFinishedWatcher,
    WebhookNotifier,
    webhook_url_from_env,
)

logger = logging.getLogger(__name__)

#: Seconds between two webhook watcher snapshots.
WEBHOOK_POLL_INTERVAL = 1.0


def _resolve_output_dir(output_dir: str | Path | None) -> Path:
    """Return the directory served under ``/downloads``."""
    if output_dir is not None:
        return Path(output_dir).expanduser().resolve()

    try:
        configured = get_config_value(["general", "output_dir"], "./downloads")
        raw = str(configured)
    except Exception:  # noqa: BLE001 - config may be absent on import
        raw = "./downloads"
    return Path(raw).expanduser().resolve()


async def _watch_finished_tasks(
    watcher: TaskFinishedWatcher,
    *,
    interval: float = WEBHOOK_POLL_INTERVAL,
) -> None:
    """
    Poll the task manager and deliver one webhook per newly finished task.

    Runs until cancelled by the lifespan shutdown. A snapshot error is logged
    and the loop continues: webhook observation must never break the API.
    """
    while True:
        try:
            watcher.poll(manager.snapshot(), serialize=task_to_dict)
        except Exception:  # noqa: BLE001 - observability must be best-effort
            logger.warning("Task webhook watcher failed", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Start the optional webhook watcher and stop workers on shutdown."""
    watcher_task: asyncio.Task[None] | None = None
    notifier = getattr(application.state, "webhook_notifier", None)
    if notifier is not None:
        watcher = TaskFinishedWatcher(notifier)
        # Tasks already finished before startup must not trigger a webhook.
        watcher.seed(manager.snapshot())
        watcher_task = asyncio.create_task(_watch_finished_tasks(watcher))

    try:
        yield
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await watcher_task
        await manager.close()


def create_app(
    *,
    token: str | None = None,
    cors: bool = False,
    output_dir: str | Path | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeouts: Mapping[str, float] | None = None,
    webhook_url: str | None = None,
) -> FastAPI:
    """
    Build the REST API application.

    :param token: API token; when ``None`` ``NOVEL_API_TOKEN`` is used. An empty
        value disables authentication (suitable for localhost-only use). When
        set, the token protects both ``/api/v1/*`` and ``/downloads/*``.
    :param cors: Add a permissive CORS middleware (see module docstring).
    :param output_dir: Override ``general.output_dir`` for ``/downloads``;
        mainly useful in tests.
    :param max_body_bytes: Request-body ceiling for the JSON endpoints
        (``413`` above it). Defaults to 1 MiB.
    :param timeouts: Per-endpoint wall-clock budgets in seconds, keyed by
        ``search``, ``music_search`` and ``music_download``; unset keys fall
        back to the defaults in
        :data:`novel_downloader.apps.api.routes.BLOCKING_TIMEOUTS`. Mainly
        useful in tests.
    :param webhook_url: Optional URL receiving ``task_finished`` events; when
        ``None`` the :data:`NOVEL_API_WEBHOOK` environment variable is used.
        An empty string disables the webhook explicitly.
    :return: A configured :class:`fastapi.FastAPI` instance.
    """
    resolved_token = token if token is not None else os.environ.get("NOVEL_API_TOKEN")
    resolved_webhook = (
        webhook_url if webhook_url is not None else webhook_url_from_env()
    )

    application = FastAPI(
        title="Novel Downloader API",
        description=(
            "Local REST API for the Novel Downloader toolkit: aggregated book "
            "search (including a Server-Sent Events stream), download task "
            "management, watchlist checks, exported-file history and the "
            "optional music engine."
        ),
        version=__version__,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    application.state.api_token = resolved_token or None
    application.state.api_timeouts = dict(timeouts or {})
    application.state.webhook_url = resolved_webhook or None
    application.state.webhook_notifier = (
        WebhookNotifier(resolved_webhook) if resolved_webhook else None
    )

    # Registered before the app is served: turns unhandled exceptions into a
    # minimal 500 body and gives validation errors a readable ``detail``.
    register_error_handlers(application)

    # Added first so it ends up *inside* the CORS layer: the size check runs
    # before FastAPI buffers the body, while its 413 response still passes back
    # out through CORSMiddleware and therefore keeps the CORS headers.
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)

    # Static files live outside the router, so their token check needs an ASGI
    # middleware. It reads ``app.state.api_token`` at request time, which keeps
    # it in sync with the router dependency and a no-op when no token is set.
    application.add_middleware(DownloadsAuthMiddleware)

    # Cache policy sits *outside* the auth and body-limit layers so their
    # direct 401/413 responses carry the same Cache-Control as every other
    # response on the path; route-set headers (the SSE ``no-cache``) win.
    application.add_middleware(CacheControlMiddleware)

    # Outermost custom layer: logs one ``method path status duration`` line
    # per request, whether it succeeded, failed or never produced a response.
    application.add_middleware(RequestLoggingMiddleware)

    if cors:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    downloads = _resolve_output_dir(output_dir)
    # Serving will simply return 404s if the directory cannot be created; do
    # not fail application startup.
    with suppress(OSError):
        downloads.mkdir(parents=True, exist_ok=True)
    application.mount(
        "/downloads",
        # check_dir=False: a missing directory yields 404s instead of
        # preventing the API (including /api/v1/health) from starting.
        StaticFiles(directory=str(downloads), check_dir=False),
        name="downloads",
    )

    application.include_router(router)
    return application


#: ASGI application used by ``uvicorn novel_downloader.apps.api.app:fastapi_app``.
fastapi_app = create_app()
