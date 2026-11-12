#!/usr/bin/env python3
"""
novel_downloader.libs.filesystem.removal
----------------------------------------

Deletion helpers that never follow links out of an allowed directory.

``shutil.rmtree`` is convenient but has two properties a cleanup command must
not rely on: on older Windows/Python combinations a junction is not seen as a
link, and the caller gets no containment check at all.  The helpers here:

* reject a target whose lexical path is outside ``root``;
* resolve the target and reject it when the resolved path escapes ``root``;
* remove a symlink/junction itself instead of walking into its target;
* walk a real directory without following nested links, so a link planted
  inside a cache/book directory cannot delete data elsewhere;
* refuse the filesystem root, the user's home directory, the current working
  directory, and any ancestor of those.

They deliberately raise on failure; silent ``ignore_errors=True`` hides a
partially deleted tree from the caller that is supposed to report it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = [
    "is_link_like",
    "remove_link",
    "remove_path_safely",
]

#: Windows reparse-point attributes.  ``getattr`` keeps the module importable
#: on platforms where the constants do not exist.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400
)
_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def _lstat(path: Path) -> os.stat_result | None:
    """Return ``lstat`` without following the final component, or ``None``."""
    try:
        return os.lstat(path)
    except OSError:
        return None


def is_link_like(path: Path) -> bool:
    """
    Return whether ``path`` is a symlink or a Windows junction.

    Junctions are reparse points with the ``MOUNT_POINT`` tag.  ``Path.is_symlink``
    reports ``False`` for them on Windows, so using it alone would let a
    ``rmtree`` walk into the junction target.

    :param path: Path to inspect; it is not followed.
    :return: ``True`` for symlinks and directory junctions.
    """
    info = _lstat(path)
    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    tag = getattr(info, "st_reparse_tag", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and (
        tag == _IO_REPARSE_TAG_MOUNT_POINT
    )


def remove_link(path: Path) -> None:
    """
    Remove a symlink or junction without touching what it points at.

    On Windows a directory symlink/junction has to be removed with ``rmdir``;
    ``unlink`` only works for file symlinks.  Trying ``rmdir`` first is safe
    because it can never delete a non-empty real directory: the caller only
    reaches this function for a path :func:`is_link_like` accepted.
    """
    if os.name == "nt":
        try:
            os.rmdir(path)
            return
        except OSError:
            # A file symlink (or an old/broken directory link) needs unlink.
            path.unlink()
            return
    path.unlink()


def _remove_tree_no_follow(path: Path) -> None:
    """Recursively delete ``path``, removing nested links instead of following."""
    if is_link_like(path):
        remove_link(path)
        return
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            if is_link_like(child):
                remove_link(child)
            elif entry.is_dir(follow_symlinks=False):
                _remove_tree_no_follow(child)
            else:
                child.unlink()
    path.rmdir()


def _absolute(path: Path) -> Path:
    """Return an absolute, lexically normalised path."""
    return Path(os.path.abspath(os.fspath(path)))


def _protection_error(path: Path) -> str | None:
    """Return a reason when ``path`` must never be removed, else ``None``."""
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        return "refusing to remove a filesystem root"
    for protected in (Path.home(), Path.cwd()):
        try:
            protected_path = protected.resolve()
        except OSError:
            continue
        # Refuse the protected directory itself *and* any ancestor of it: a
        # configured output_dir of ``~`` or a project parent must not take the
        # user's home or the running directory with it.
        if protected_path.is_relative_to(resolved):
            return f"refusing to remove protected directory {resolved}"
    return None


def remove_path_safely(
    path: Path,
    *,
    root: Path,
    allow_root: bool = False,
) -> None:
    """
    Remove ``path`` only when it is inside ``root`` and is safe to delete.

    A link (symlink or junction) is removed as a link; its target is never
    walked into.  A real directory is walked without following any nested
    link.  Errors are propagated so a partial deletion is visible to the
    caller instead of being swallowed.

    :param path: File/directory/link to remove.
    :param root: Directory the target must stay inside.
    :param allow_root: Permit removing ``root`` itself (``clean cache`` with no
        site is an explicit "remove the whole cache root" request).
    :raise ValueError: When the target escapes ``root`` or is protected.
    :raise OSError: When the filesystem operation fails.
    """
    target = _absolute(path)
    allowed = _absolute(root)

    if not target.is_relative_to(allowed):
        raise ValueError(f"refusing to remove {target}: outside {allowed}")
    if not allow_root and target == allowed:
        raise ValueError(f"refusing to remove the allowed root itself: {allowed}")

    # A link may legitimately point outside; it is still safe to remove the
    # link itself, so containment for links is checked on the parent only.
    if is_link_like(target):
        if target == allowed and not allow_root:
            raise ValueError(f"refusing to remove the allowed root itself: {allowed}")
        reason = _protection_error(target)
        if reason is not None:
            raise ValueError(reason)
        remove_link(target)
        return

    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(allowed.resolve(strict=False)):
        raise ValueError(f"refusing to remove {resolved}: outside {allowed}")
    if not allow_root and resolved == allowed.resolve(strict=False):
        raise ValueError(f"refusing to remove the allowed root itself: {allowed}")

    reason = _protection_error(resolved)
    if reason is not None:
        raise ValueError(reason)

    if not os.path.lexists(target):
        return
    _remove_tree_no_follow(target)
