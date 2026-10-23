#!/usr/bin/env python3
"""
novel_downloader.apps.web.services
----------------------------------

Convenience re-exports for web UI services.

``setup_dialog`` is imported lazily because it depends on NiceGUI, while
``manager`` is UI-independent and safe to import from headless entry points
(such as the REST API).
"""

from typing import Any

__all__ = [
    "setup_dialog",
    "manager",
]

from .task_manager import manager


def __getattr__(name: str) -> Any:
    if name == "setup_dialog":
        from .client_dialog import setup_dialog

        return setup_dialog
    raise AttributeError(name)
