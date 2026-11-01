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
N69YUE_MAP_PATH = RES.joinpath("json", "n69yue.json")
XIGUASHUWU_MAP_PATH = RES.joinpath("json", "xiguashuwu.json")
YODU_MAP_PATH = RES.joinpath("json", "yodu.json")

# JavaScript
EXPR_TO_JSON_SCRIPT_PATH = RES.joinpath("scripts", "expr_to_json.js")
QD_DECRYPT_SCRIPT_PATH = RES.joinpath("scripts", "qidian_decrypt_node.js")
QQ_DECRYPT_SCRIPT_PATH = RES.joinpath("scripts", "qq_decrypt_node.js")
HTML_JS_MAIN_PATH = RES.joinpath("scripts", "html_main.js")
