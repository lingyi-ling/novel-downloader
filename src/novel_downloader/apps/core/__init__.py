#!/usr/bin/env python3
"""
novel_downloader.apps.core
--------------------------

UI-independent service layer shared by the CLI and the REST API:
download task manager, task models and engine callback adapters.
"""

__all__ = ["manager"]

from .task_manager import manager
