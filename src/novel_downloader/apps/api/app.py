#!/usr/bin/env python3
"""
novel_downloader.apps.api.app
-----------------------------

FastAPI application factory for the local REST API.

Security model
--------------
The API is meant to run on the same machine as the downloader. By default it
binds to ``127.0.0.1`` and performs no authentication. If a token is supplied
(``--token`` or ``NOVEL_API_TOKEN``) every ``/api/v1/*`` request must carry
``Authorization: Bearer <token>`` or ``X-API-Key: <token>``.

The optional ``--cors`` flag enables ``Access-Control-Allow-Origin: *`` so
browser extensions / local tools can call the API from a web page. This is
dangerous on a non-localhost bind: any website the user visits could then call
the API with the user's network position. Only enable it for local use and keep
a token configured when the server is exposed beyond ``127.0.0.1``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from novel_downloader import __version__
from novel_downloader.apps.web.services.task_manager import manager
from novel_downloader.infra.config import get_config_value

from .routes import router


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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Stop background workers when the API process shuts down."""
    try:
        yield
    finally:
        await manager.close()


def create_app(
    *,
    token: str | None = None,
    cors: bool = False,
    output_dir: str | Path | None = None,
) -> FastAPI:
    """
    Build the REST API application.

    :param token: API token; when ``None`` ``NOVEL_API_TOKEN`` is used. An empty
        value disables authentication (suitable for localhost-only use).
    :param cors: Add a permissive CORS middleware (see module docstring).
    :param output_dir: Override ``general.output_dir`` for ``/downloads``;
        mainly useful in tests.
    :return: A configured :class:`fastapi.FastAPI` instance.
    """
    resolved_token = token if token is not None else os.environ.get("NOVEL_API_TOKEN")

    application = FastAPI(
        title="Novel Downloader API",
        description="Local REST API for the Novel Downloader toolkit.",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.api_token = resolved_token or None

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
