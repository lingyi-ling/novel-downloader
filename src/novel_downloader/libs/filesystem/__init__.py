#!/usr/bin/env python3
"""
novel_downloader.libs.filesystem
--------------------------------

Filesystem utilities, including file I/O and filename sanitization.
"""

__all__ = [
    "KIND_ROWS",
    "RemovalItem",
    "format_filename",
    "font_filename",
    "image_filename",
    "is_link_like",
    "plan_removal",
    "remove_link",
    "remove_path_safely",
    "sanitize_filename",
    "write_file",
]

from .file import write_file
from .filename import font_filename, format_filename, image_filename
from .preview import KIND_ROWS, RemovalItem, plan_removal
from .removal import is_link_like, remove_link, remove_path_safely
from .sanitize import sanitize_filename
