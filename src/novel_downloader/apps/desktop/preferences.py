#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.preferences
-----------------------------------------

Pure preference helpers for the desktop client.

The functions here keep the Tk widgets thin and make the interesting parts
(settings defaults, quality-level mapping, remembered search input and the
status-bar environment summary) testable without a display.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.infra.i18n import t

__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_MUSIC_LEVEL",
    "MUSIC_LEVELS",
    "MusicSettings",
    "SearchState",
    "environment_summary",
    "level_code",
    "level_display",
    "music_settings_from_mapping",
    "music_settings_to_mapping",
    "normalize_level",
    "remember_search",
    "resolve_output_dir",
]

#: Quality levels accepted by the music engine, highest first.
MUSIC_LEVELS: tuple[str, ...] = (
    "hires",
    "lossless",
    "exhigh",
    "higher",
    "standard",
)

#: Level used when the stored value is missing or unknown.
DEFAULT_MUSIC_LEVEL = "exhigh"

#: Display labels for the level combobox.  Lambdas keep lazily-translated
#: literals discoverable by the ``t()`` extraction pass.
_LEVEL_LABELS: dict[str, Callable[[], str]] = {
    "hires": lambda: t("Hi-Res"),
    "lossless": lambda: t("Lossless"),
    "exhigh": lambda: t("ExHigh"),
    "higher": lambda: t("Higher"),
    "standard": lambda: t("Standard"),
}

#: Address ``novel-api`` listens on by default.  The desktop client only
#: displays it; it does not start or probe the server.
DEFAULT_API_BASE = "http://127.0.0.1:8081"


@dataclass(frozen=True)
class MusicSettings:
    """The music options exposed on the desktop music tab."""

    out_dir: str
    level: str
    embed_tags: bool
    embed_cover: bool
    save_lyric: bool
    split_folder: bool
    skip_existing: bool


@dataclass(frozen=True)
class SearchState:
    """The last submitted search keyword and site filter."""

    keyword: str
    sites: str

    @property
    def has_previous(self) -> bool:
        """Whether this state holds a usable keyword."""
        return bool(self.keyword.strip())


def _coerce_bool(value: Any, default: bool) -> bool:
    """Interpret stored JSON-ish values as a boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        return text not in {"0", "false", "no", "off"}
    return bool(value)


def resolve_output_dir(raw: Any, default_dir: str | Path) -> str:
    """
    Normalize a user-entered output directory.

    Blank input falls back to ``default_dir``; ``~`` and environment variables
    are expanded so paths pasted from a shell work.

    :param raw: Value read from the entry widget (or the settings file).
    :param default_dir: Directory used when ``raw`` is empty.
    :return: The normalized directory string.
    """
    text = str(raw or "").strip()
    if not text:
        text = str(default_dir)
    return os.path.expandvars(os.path.expanduser(text))


def normalize_level(raw: str) -> str:
    """
    Return a supported quality level, falling back to the default.

    :param raw: Level code from the settings file or the combobox.
    :return: One of :data:`MUSIC_LEVELS`.
    """
    text = str(raw or "").strip().lower()
    return text if text in MUSIC_LEVELS else DEFAULT_MUSIC_LEVEL


def level_display(level: str) -> str:
    """
    Return the combobox label for a quality level.

    :param level: Level code such as ``"exhigh"``.
    :return: The localized display label.
    """
    return _LEVEL_LABELS[normalize_level(level)]()


def level_code(display: str) -> str:
    """
    Map a combobox label (or a raw level code) back to its code.

    :param display: Text selected in the combobox.
    :return: A supported level code.
    """
    text = str(display or "").strip()
    for code in MUSIC_LEVELS:
        if text == code or text == level_display(code):
            return code
    return DEFAULT_MUSIC_LEVEL


def music_settings_from_mapping(
    values: Mapping[str, Any] | None, default_dir: str | Path
) -> MusicSettings:
    """
    Merge stored music settings over the built-in defaults.

    The mapping comes from :func:`novel_downloader.apps.music.settings.load_settings`
    but may also be a plain dict in tests; unknown levels and empty paths are
    normalized here so the widgets always receive usable values.

    :param values: Settings loaded from disk, or ``None``.
    :param default_dir: Fallback output directory.
    :return: A fully populated settings object.
    """
    data: Mapping[str, Any] = values or {}
    return MusicSettings(
        out_dir=resolve_output_dir(data.get("out_dir"), default_dir),
        level=normalize_level(str(data.get("level") or "")),
        embed_tags=_coerce_bool(data.get("embed_tags"), True),
        embed_cover=_coerce_bool(data.get("embed_cover"), True),
        save_lyric=_coerce_bool(data.get("save_lyric"), True),
        split_folder=_coerce_bool(data.get("split_folder"), True),
        skip_existing=_coerce_bool(data.get("skip_existing"), True),
    )


def music_settings_to_mapping(settings: MusicSettings) -> dict[str, Any]:
    """
    Render settings as the keys understood by the music settings file.

    :param settings: Settings collected from the widgets.
    :return: A JSON-compatible mapping.
    """
    return {
        "out_dir": settings.out_dir,
        "level": settings.level,
        "embed_tags": settings.embed_tags,
        "embed_cover": settings.embed_cover,
        "save_lyric": settings.save_lyric,
        "split_folder": settings.split_folder,
        "skip_existing": settings.skip_existing,
    }


def remember_search(keyword: str, sites: str) -> SearchState:
    """
    Build the in-memory record of a submitted search.

    :param keyword: Keyword entered by the user.
    :param sites: Raw site filter text.
    :return: A trimmed :class:`SearchState`.
    """
    return SearchState(keyword=str(keyword).strip(), sites=str(sites).strip())


def environment_summary(api_base: str, data_dir: str | Path) -> str:
    """
    Render the status-bar text describing the API and data locations.

    :param api_base: Base URL the local API normally listens on.
    :param data_dir: Application data directory.
    :return: A single-line summary.
    """
    return t("API: {api} | Data: {data}").format(api=api_base, data=data_dir)
