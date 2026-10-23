#!/usr/bin/env python3
"""
novel_downloader.apps.web
-------------------------

Web interface layer built with nicegui.

``web_main`` is imported lazily so that the REST API (and other headless
entry points) can reuse the service layer without importing NiceGUI.
"""

from typing import Any

__all__ = [
    "web_main",
]


def __getattr__(name: str) -> Any:
    if name == "web_main":
        from .main import web_main

        return web_main
    raise AttributeError(name)
