#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.theme
-----------------------------------

Color palette and shared color helpers for the CustomTkinter desktop client.

Every color is a ``(light, dark)`` pair: CustomTkinter widgets accept such
pairs directly and re-resolve them whenever the appearance mode changes, so
this module needs no Tk imports and stays testable without a display.  The
values mirror the web client's warm off-white / coral-orange palette.

The widget-level theme patch (writing these pairs into
``customtkinter.ThemeManager.theme``) lives in :mod:`.ui` because it needs the
CustomTkinter import; the palette itself is kept here so tests can verify the
color math without creating a window.
"""

from __future__ import annotations

__all__ = [
    "ACCENT",
    "ACCENT_HOVER",
    "ACCENT_SOFT",
    "BADGE_QUEUED",
    "BADGES",
    "BORDER",
    "CARD",
    "FIELD",
    "HOVER",
    "MUTED",
    "SIDEBAR",
    "STATUS_FAIL_TEXT",
    "STATUS_OK_TEXT",
    "TEXT",
    "TRACK",
    "Color",
    "select",
    "mix",
]

#: A color pair as CustomTkinter widgets accept it: ``(light, dark)``.
Color = tuple[str, str]

# ---------------------------------------------------------------------------
# Core palette — warm off-white surfaces with a coral-orange accent, in the
# spirit of the existing web theme.
# ---------------------------------------------------------------------------
#: Window / page background.
BG: Color = ("#F5F4EF", "#1F1E1D")
#: Fixed-width navigation sidebar.
SIDEBAR: Color = ("#ECEAE1", "#262524")
#: Elevated card surface.
CARD: Color = ("#FFFFFF", "#2B2A28")
#: Editable field surface (entries, textboxes, dropdowns).
FIELD: Color = ("#FFFFFF", "#32302E")
#: Primary text.
TEXT: Color = ("#2A2825", "#F0EEE8")
#: Secondary / de-emphasized text.
MUTED: Color = ("#8A857C", "#A29D93")
#: Hairline borders.
BORDER: Color = ("#E3E0D7", "#3B3936")
#: Accent (brand) color.
ACCENT: Color = ("#D97757", "#D97757")
#: Accent hover state.
ACCENT_HOVER: Color = ("#C4633F", "#E08A6E")
#: Soft accent tint (selected navigation items and cards).
ACCENT_SOFT: Color = ("#F9E7DF", "#4A322B")
#: Unfilled progress-bar track.
TRACK: Color = ("#E7E4DB", "#3B3936")
#: Subtle hover fill for cards and list rows.
HOVER: Color = ("#EDEBE2", "#343231")

#: Status-bar highlight text colors for task notifications.
STATUS_OK_TEXT: Color = ("#2F6B34", "#8FCE9A")
STATUS_FAIL_TEXT: Color = ("#B3392B", "#F0918A")

#: Background/foreground pairs per raw task status, used by the status badges.
BADGES: dict[str, tuple[Color, Color]] = {
    "queued": (("#ECEAE1", "#34322F"), ("#6B6862", "#A29D93")),
    "running": (("#F7E3D8", "#3D2E26"), ("#B4562F", "#ECA07E")),
    "processing": (("#F5EAD0", "#3B3323"), ("#9A6B1B", "#E4C27E")),
    "exporting": (("#F5EAD0", "#3B3323"), ("#9A6B1B", "#E4C27E")),
    "completed": (("#E2F0E4", "#26382B"), ("#2F6B34", "#8FCE9A")),
    "cancelled": (("#ECEAE1", "#34322F"), ("#8A857C", "#A29D93")),
    "failed": (("#F9E2DE", "#45282A"), ("#B3392B", "#F0918A")),
}

#: Shorthand for the most common badge look (kept for readability at call sites).
BADGE_QUEUED = BADGES["queued"]


def select(pair: Color, mode: str) -> str:
    """
    Pick one side of a ``(light, dark)`` color pair.

    :param pair: The ``(light, dark)`` pair.
    :param mode: ``"light"`` or ``"dark"`` (case-insensitive); anything else
        resolves to the light side.
    :return: The hex color for the requested appearance mode.
    """
    return pair[1] if str(mode).strip().lower() == "dark" else pair[0]


def mix(first: str, second: str, ratio: float) -> str:
    """
    Blend two ``#RRGGBB`` colors.

    :param first: The color at ``ratio`` 0.
    :param second: The color at ``ratio`` 1.
    :param ratio: Blend position, clamped to ``[0, 1]``.
    :return: The blended ``#RRGGBB`` color.
    """
    clamped = min(1.0, max(0.0, float(ratio)))
    start = _parse_hex(first)
    end = _parse_hex(second)
    blended = tuple(
        round(a + (b - a) * clamped) for a, b in zip(start, end, strict=True)
    )
    return "#{:02X}{:02X}{:02X}".format(*blended)


def _parse_hex(color: str) -> tuple[int, int, int]:
    """Decode a ``#RGB``/``#RRGGBB`` hex string into an RGB triple."""
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"Not a hex color: {color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
