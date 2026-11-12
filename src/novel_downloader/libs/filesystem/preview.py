#!/usr/bin/env python3
"""
novel_downloader.libs.filesystem.preview
----------------------------------------

Read-only companion to :func:`novel_downloader.libs.filesystem.remove_path_safely`.

A dry run must answer "what would be removed, and how much would that free?"
without calling ``unlink``/``rmtree`` anywhere.  :func:`plan_removal` therefore
performs the same containment and protection checks as the removal helper and
then *inspects* the target: links are reported as links (never followed), real
directories are walked without descending into nested links, and missing paths
produce no plan.

The validation sequence deliberately mirrors ``remove_path_safely``.  Keeping
the checks in one place would require changing the deletion helper, which is
shared with the API/desktop front ends; this module instead reuses its private
path primitives.  Any change to the deletion rules must be reflected here too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .removal import _absolute, _protection_error, is_link_like

__all__ = [
    "RemovalItem",
    "plan_removal",
]

#: Kinds reported for a planned removal.  ``rows`` means the path itself stays
#: in place but the cleanup deletes records *inside* it (the chapter SQLite DB).
KIND_DIRECTORY = "directory"
KIND_FILE = "file"
KIND_LINK = "link"
KIND_OTHER = "other"
KIND_ROWS = "rows"


@dataclass(frozen=True)
class RemovalItem:
    """One path a cleanup would remove (or rewrite, for ``rows``)."""

    path: Path
    kind: str
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "files": self.files,
            "bytes": self.bytes,
        }


def _scan_tree(path: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` below ``path`` without links."""
    files = 0
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                child = Path(entry.path)
                # A nested link is removed as a link; its target is neither
                # walked nor counted, exactly like the deletion helper.
                if is_link_like(child):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        files += 1
                        total += info.st_size
                except OSError:
                    continue
    return files, total


def _describe(path: Path) -> RemovalItem:
    """Describe what deleting ``path`` would do, without deleting anything."""
    if is_link_like(path):
        return RemovalItem(path=path, kind=KIND_LINK)
    try:
        info = os.lstat(path)
    except OSError:
        return RemovalItem(path=path, kind=KIND_OTHER)
    if os.path.isdir(path):
        files, total = _scan_tree(path)
        return RemovalItem(path=path, kind=KIND_DIRECTORY, files=files, bytes=total)
    if os.path.isfile(path):
        return RemovalItem(path=path, kind=KIND_FILE, files=1, bytes=info.st_size)
    return RemovalItem(path=path, kind=KIND_OTHER)


def plan_removal(
    path: Path,
    *,
    root: Path,
    allow_root: bool = False,
) -> RemovalItem | None:
    """
    Plan the removal of ``path`` inside ``root`` without touching the disk.

    The containment, root and protected-directory checks match
    :func:`novel_downloader.libs.filesystem.remove_path_safely`; an illegal
    target raises :class:`ValueError` in dry-run just as it would when the
    deletion is attempted.  Links are reported as links and never followed.

    :param path: File/directory/link the cleanup would remove.
    :param root: Directory the target must stay inside.
    :param allow_root: Permit removing ``root`` itself.
    :return: A description of the removal, or ``None`` when nothing exists.
    :raise ValueError: When the target escapes ``root`` or is protected.
    """
    target = _absolute(path)
    allowed = _absolute(root)

    if not target.is_relative_to(allowed):
        raise ValueError(f"refusing to remove {target}: outside {allowed}")
    if not allow_root and target == allowed:
        raise ValueError(f"refusing to remove the allowed root itself: {allowed}")

    if is_link_like(target):
        if target == allowed and not allow_root:
            raise ValueError(f"refusing to remove the allowed root itself: {allowed}")
        reason = _protection_error(target)
        if reason is not None:
            raise ValueError(reason)
        return RemovalItem(path=target, kind=KIND_LINK)

    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(allowed.resolve(strict=False)):
        raise ValueError(f"refusing to remove {resolved}: outside {allowed}")
    if not allow_root and resolved == allowed.resolve(strict=False):
        raise ValueError(f"refusing to remove the allowed root itself: {allowed}")

    reason = _protection_error(resolved)
    if reason is not None:
        raise ValueError(reason)

    if not os.path.lexists(target):
        return None
    return _describe(target)
