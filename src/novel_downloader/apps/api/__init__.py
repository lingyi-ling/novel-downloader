#!/usr/bin/env python3
"""
novel_downloader.apps.api
-------------------------

Local REST API for other software to drive the novel downloader.

Typical use::

    from novel_downloader.apps.api import create_app

    app = create_app(token="secret")  # ASGI app for uvicorn / TestClient

The pre-built :data:`fastapi_app` is handy for ``uvicorn`` when configuration
comes entirely from environment variables.
"""

__all__ = ["create_app", "fastapi_app"]

from .app import create_app, fastapi_app
