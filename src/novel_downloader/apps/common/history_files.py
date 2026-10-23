#!/usr/bin/env python3
"""
novel_downloader.apps.common.history_files
------------------------------------------

Pure helpers for browsing exported book files.

The functions here are shared by the NiceGUI history page and the REST API,
so they deliberately contain no UI or framework imports. Behaviour matches the
original implementations that used to live in
:mod:`novel_downloader.apps.web.pages.history`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from novel_downloader.infra.config import get_config_value

_EXTS = {"txt", "epub"}

SortKey = Literal["name", "mtime", "size"]
SortOrder = Literal["asc", "desc"]
TypeFilter = Literal["All", "txt", "epub"]

# Direction applied when the user switches to a sort key: names read best
# ascending, while time and size are most useful newest/largest first.
_DEFAULT_ORDER: dict[SortKey, SortOrder] = {
    "name": "asc",
    "mtime": "desc",
    "size": "desc",
}


def _output_dir() -> Path:
    """Resolve the export directory at runtime (config may not exist on import)."""
    return Path(get_config_value(["general", "output_dir"], "./downloads"))


@dataclass(frozen=True)
class FileItem:
    path: Path
    name: str
    ext: str  # 'txt' or 'epub'
    size: int  # bytes
    mtime: float


def _scan_files() -> list[FileItem]:
    """Scan the output directory for txt/epub files."""
    try:
        it = os.scandir(_output_dir())
    except FileNotFoundError:
        return []
    items: list[FileItem] = []
    for entry in it:
        try:
            if not entry.is_file():
                continue
            ext = Path(entry.name).suffix.lower().lstrip(".")
            if ext not in _EXTS:
                continue
            st = entry.stat()
            items.append(
                FileItem(
                    path=Path(entry.path),
                    name=entry.name,
                    ext=ext,
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
        except FileNotFoundError:
            continue
    return items


def _apply_filter(
    items: list[FileItem], type_filter: TypeFilter, name_query: str = ""
) -> list[FileItem]:
    """Filter by extension and case-insensitive name substring."""
    result = items
    if type_filter != "All":
        result = [it for it in result if it.ext == type_filter]
    query = name_query.strip().lower()
    if query:
        result = [it for it in result if query in it.name.lower()]
    return result


def _apply_sort(
    items: list[FileItem], sort_by: SortKey, order: SortOrder
) -> list[FileItem]:
    """Sort items by name/mtime/size in the given order.

    An empty list (no files, or everything filtered out) is safe: ``sorted``
    simply yields an empty list for every key.
    """
    reverse = order == "desc"
    if sort_by == "name":
        return sorted(items, key=lambda it: it.name.lower(), reverse=reverse)
    if sort_by == "size":
        return sorted(items, key=lambda it: it.size, reverse=reverse)
    return sorted(items, key=lambda it: it.mtime, reverse=reverse)


def _format_size(size: int) -> str:
    """Render a byte count as a compact human-readable string."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit in ("B", "KB"):
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(value)} GB"
