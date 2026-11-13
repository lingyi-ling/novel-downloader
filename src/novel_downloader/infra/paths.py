#!/usr/bin/env python3
"""
novel_downloader.infra.paths
----------------------------

"""

import os
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from platformdirs import user_data_path

PACKAGE_NAME = "novel_downloader"  # Python package name

#: Windows device names that cannot be used as ordinary file/directory names.
#: Opening them addresses a device instead of a file in the current directory.
_WIN_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def is_safe_path_component(value: object) -> bool:
    """
    Return whether ``value`` can be joined onto a directory as one component.

    Site keys, book IDs, pipeline stages and similar identifiers are used as
    directory/file name segments under a configured root (``raw_data/<site>/
    <book_id>/...``).  A value containing a separator, a drive/stream colon,
    a ``.``/``..`` segment, control characters or a Windows device name would
    make that join address a different path, so it must be rejected *before*
    any read or write.  The check is intentionally the same on every platform:
    data written on POSIX may later be read on Windows (and vice versa), so a
    name that is only safe on one of them is not accepted.

    Trailing spaces/dots are rejected as well because Windows strips them,
    which turns ``".. "`` into a parent reference.

    :param value: Candidate identifier (converted with ``str()``).
    :return: ``True`` when the value is a single, non-escaping name.
    """
    text = str(value)
    path = Path(text)
    if (
        not text.strip(" .")
        or text != text.rstrip(" .")
        or "/" in text
        or "\\" in text
        or ":" in text
        or path.is_absolute()
        or path.name != text
    ):
        return False
    if any(ord(char) < 32 or ord(char) == 0x7F for char in text):
        return False
    # Lone surrogates cannot be encoded into a path on any platform.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        return False
    return not (
        os.name == "nt"
        and text.split(".", 1)[0].strip(" .").upper() in _WIN_RESERVED_NAMES
    )


def app_home() -> Path:
    """
    Return the application's home directory.

    Resolution order:
      1. ``NOVEL_HOME`` environment variable (used by the desktop app / tests)
      2. Platform data directory, e.g. ``%LOCALAPPDATA%\\novel_downloader``
         on Windows, ``~/.local/share/novel_downloader`` on Linux.

    :return: Absolute path to the application home directory.
    """
    # ``strip`` keeps a blank/whitespace-only override (common in shell
    # scripts) from turning into a directory literally named " " or " ~ "
    # instead of falling back to the platform data directory.
    override = os.environ.get("NOVEL_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return user_data_path(PACKAGE_NAME, appauthor=False)


# Base directory holding state/config data (e.g. ~/AppData/Local/novel_downloader/)
STATE_PATH = app_home() / "state.json"

RES = files("novel_downloader.resources")


def _res(*parts: str) -> Traversable:
    """Join resource segments one at a time.

    With editable installs on Python 3.11/3.13 ``files()`` can hand back
    a ``MultiplexedPath`` whose ``joinpath`` takes exactly one argument,
    so multi-segment calls must be chained.
    """
    target: Traversable = RES
    for part in parts:
        target = target.joinpath(part)
    return target


# Config
DEFAULT_CONFIG_FILE = _res("config", "settings.sample.toml")

# Default config filename (used when copying embedded template)
DEFAULT_CONFIG_FILENAME = "settings.toml"

# Brand assets used by the desktop window / favicon
APP_ICON_PATH = _res("icons", "app.ico")
APP_FAVICON_PATH = _res("icons", "app.png")

# CSS Styles
EPUB_CSS_STYLE_PATH = _res("styles", "epub_style.css")
HTML_CSS_INDEX_PATH = _res("styles", "html_index.css")
HTML_CSS_CHAPTER_PATH = _res("styles", "html_chapter.css")

# JSON
FANQIENOVEL_MAP_PATH = _res("json", "fanqienovel.json")
HONGXIUZHAO_MAP_PATH = _res("json", "hongxiuzhao.json")
LINOVELIB_MAP_PATH = _res("json", "linovelib.json")
N69YUE_MAP_PATH = _res("json", "n69yue.json")
XIGUASHUWU_MAP_PATH = _res("json", "xiguashuwu.json")
YODU_MAP_PATH = _res("json", "yodu.json")

# JavaScript
EXPR_TO_JSON_SCRIPT_PATH = _res("scripts", "expr_to_json.js")
QD_DECRYPT_SCRIPT_PATH = _res("scripts", "qidian_decrypt_node.js")
QQ_DECRYPT_SCRIPT_PATH = _res("scripts", "qq_decrypt_node.js")
HTML_JS_MAIN_PATH = _res("scripts", "html_main.js")
