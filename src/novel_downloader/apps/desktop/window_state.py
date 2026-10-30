#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.window_state
------------------------------------------

Window geometry persistence for the Tkinter desktop client.

The last main-window geometry (width, height and position) is stored as JSON
in :func:`novel_downloader.apps.music.paths.music_data_dir` and restored on
the next launch.  The parsing, validation and serialization helpers here are
pure (no Tk imports), so the file format is testable without a display; only
:func:`load_window_geometry` and :func:`save_window_geometry` touch the disk,
and both tolerate a missing or corrupted file by falling back to ``None``.
"""

from __future__ import annotations

import json
import logging
import re
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
    "parse_geometry",
    "save_window_geometry",
    "serialize_geometry",
    "window_geometry_from_mapping",
    "window_geometry_to_mapping",
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

#: Tk geometry string: ``WIDTHxHEIGHT`` optionally followed by signed X/Y
#: offsets (a negative offset is anchored to the right/bottom screen edge).
_GEOMETRY_RE = re.compile(r"^=?(\d+)x(\d+)(?:([+-])(\d+)([+-])(\d+))?$")


@dataclass(frozen=True)
class WindowGeometry:
    """The size and position of the main window."""

    width: int
    height: int
    #: Position offsets; ``None`` (or a half-pair) means "keep the position
    #: the window manager picks".  Negative values keep their Tk meaning:
    #: distance from the right/bottom screen edge.
    x: int | None = None
    y: int | None = None


def parse_geometry(raw: str) -> WindowGeometry | None:
    """
    Parse a Tk geometry string such as ``"1100x720+50+30"``.

    An optional leading ``=`` and surrounding whitespace are accepted; the
    position part is optional.  Anything unparsable yields ``None``.

    :param raw: Geometry string as returned by ``Tk.geometry()``.
    :return: The parsed geometry, or ``None`` when ``raw`` is not one.
    """
    match = _GEOMETRY_RE.match(str(raw or "").strip())
    if match is None:
        return None
    width, height, x_sign, x_text, y_sign, y_text = match.groups()
    pos_x: int | None = None
    pos_y: int | None = None
    if x_sign is not None and y_sign is not None:
        pos_x = int(x_text) if x_sign == "+" else -int(x_text)
        pos_y = int(y_text) if y_sign == "+" else -int(y_text)
    return WindowGeometry(width=int(width), height=int(height), x=pos_x, y=pos_y)


def serialize_geometry(geometry: WindowGeometry) -> str:
    """
    Render a geometry as the string accepted by ``Tk.geometry()``.

    :param geometry: The geometry to render.
    :return: ``"WIDTHxHEIGHT"`` plus ``"+X+Y"`` when the position is known.
    """
    text = f"{geometry.width}x{geometry.height}"
    if geometry.x is None or geometry.y is None:
        return text
    return f"{text}{geometry.x:+d}{geometry.y:+d}"


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
