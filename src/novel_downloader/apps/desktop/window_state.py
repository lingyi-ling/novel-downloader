#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.window_state
------------------------------------------

Window geometry persistence for the desktop client.

The last main-window geometry (width, height and position) is stored as JSON
in :func:`novel_downloader.apps.music.paths.music_data_dir` and restored on
the next launch.  The validation helpers here are pure, so the file format is
testable without a display; only :func:`load_window_geometry` and
:func:`save_window_geometry` touch the disk, and both tolerate a missing or
corrupted file by falling back to ``None``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.apps.music.paths import music_data_dir

__all__ = [
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "WINDOW_STATE_FILENAME",
    "WindowGeometry",
    "clamp_geometry",
    "load_window_geometry",
    "save_window_geometry",
    "visible_origin",
    "window_geometry_from_mapping",
    "window_state_file",
]

logger = logging.getLogger(__name__)

#: Lower bound applied when restoring a stored geometry.  The window itself
#: enforces a stricter ``minsize``; this guard only keeps a bogus stored value
#: (e.g. a degenerate ``1x1``) from shrinking the restored window.
MIN_WINDOW_WIDTH = 400
MIN_WINDOW_HEIGHT = 300

#: Name of the JSON file inside :func:`music_data_dir`.
WINDOW_STATE_FILENAME = "window_state.json"


@dataclass(frozen=True)
class WindowGeometry:
    """The size and position of the main window."""

    width: int
    height: int
    #: Position offsets; ``None`` means "keep the position the window manager
    #: picks".
    x: int | None = None
    y: int | None = None


def clamp_geometry(geometry: WindowGeometry) -> WindowGeometry:
    """
    Enforce the minimum window size (:data:`MIN_WINDOW_WIDTH` x
    :data:`MIN_WINDOW_HEIGHT`); the position is passed through unchanged.

    :param geometry: The geometry to clamp.
    :return: A geometry whose width/height are never below the minimum.
    """
    return WindowGeometry(
        width=max(geometry.width, MIN_WINDOW_WIDTH),
        height=max(geometry.height, MIN_WINDOW_HEIGHT),
        x=geometry.x,
        y=geometry.y,
    )


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` when it is a plain integer, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def window_geometry_from_mapping(mapping: object) -> WindowGeometry | None:
    """
    Validate a JSON-decoded mapping as a window geometry.

    ``width`` and ``height`` must be positive integers; ``x``/``y`` are kept
    only when both are integers (any sign allowed) or both absent/``None``.
    Anything else — wrong types, missing sizes, non-mapping input — yields
    ``None`` instead of raising.

    :param mapping: Decoded JSON value.
    :return: The validated geometry, or ``None`` when unusable.
    """
    if not isinstance(mapping, Mapping):
        return None
    width = _coerce_int(mapping.get("width"))
    height = _coerce_int(mapping.get("height"))
    if width is None or height is None or width < 1 or height < 1:
        return None
    pos_x = _coerce_int(mapping.get("x"))
    pos_y = _coerce_int(mapping.get("y"))
    if pos_x is None or pos_y is None:
        pos_x = pos_y = None
    return WindowGeometry(width=width, height=height, x=pos_x, y=pos_y)


def visible_origin(
    x: int, y: int, bounds: tuple[int, int, int, int] | None
) -> tuple[int, int] | None:
    """
    Decide whether a stored window origin still lands on a monitor.

    Negative coordinates are legitimate: on multi-monitor setups a screen
    placed left of or above the primary has a negative origin, and clamping
    such a value to zero would yank the window back onto the primary screen.
    The position is only dropped when it falls outside ``bounds`` — the
    bounding rectangle of the currently connected monitors — which happens
    when the monitor the window was saved on is no longer attached.

    :param x: Stored window origin X (any sign).
    :param y: Stored window origin Y (any sign).
    :param bounds: ``(x, y, width, height)`` of the virtual desktop, or
        ``None`` when the metrics are unknown (the origin is then trusted).
    :return: ``(x, y)`` when the origin is plausibly visible, else ``None``
        meaning "let the window manager pick a position".
    """
    if bounds is None:
        return (x, y)
    bx, by, bw, bh = bounds
    if bx <= x < bx + bw and by <= y < by + bh:
        return (x, y)
    return None


def window_geometry_to_mapping(geometry: WindowGeometry) -> dict[str, int | None]:
    """
    Render a geometry as the JSON mapping :func:`window_geometry_from_mapping`
    accepts.

    :param geometry: The geometry to store.
    :return: A JSON-compatible mapping.
    """
    return {
        "width": geometry.width,
        "height": geometry.height,
        "x": geometry.x,
        "y": geometry.y,
    }


def window_state_file() -> Path:
    """Return the JSON file holding the last window geometry."""
    return music_data_dir() / WINDOW_STATE_FILENAME


def load_window_geometry(path: Path) -> WindowGeometry | None:
    """
    Read the stored geometry, clamped to the minimum window size.

    :param path: JSON file written by :func:`save_window_geometry`.
    :return: The stored geometry, or ``None`` when the file is missing,
        unreadable, corrupted or does not describe a usable geometry.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    geometry = window_geometry_from_mapping(loaded)
    if geometry is None:
        return None
    return clamp_geometry(geometry)


def save_window_geometry(path: Path, geometry: WindowGeometry) -> bool:
    """
    Persist the geometry as JSON, creating the parent directory if needed.

    :param path: Target JSON file.
    :param geometry: The geometry to store.
    :return: ``True`` when the file was written, ``False`` when the disk
        write failed (the failure is logged, never raised).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                window_geometry_to_mapping(geometry), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to save the window state to %s", path, exc_info=True)
        return False
    return True
