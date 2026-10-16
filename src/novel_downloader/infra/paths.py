#!/usr/bin/env python3
"""
novel_downloader.infra.paths
----------------------------

"""

import os
from importlib.resources import files
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
    override = os.environ.get("NOVEL_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return user_data_path(PACKAGE_NAME, appauthor=False)


# Base directory holding state/config data (e.g. ~/AppData/Local/novel_downloader/)
STATE_PATH = app_home() / "state.json"

RES = files("novel_downloader.resources")

# Config
DEFAULT_CONFIG_FILE = RES.joinpath("config", "settings.sample.toml")

# Default config filename (used when copying embedded template)
DEFAULT_CONFIG_FILENAME = "settings.toml"

# Brand assets used by the desktop window / favicon
APP_ICON_PATH = RES.joinpath("icons", "app.ico")
APP_FAVICON_PATH = RES.joinpath("icons", "app.png")

# CSS Styles
EPUB_CSS_STYLE_PATH = RES.joinpath("styles", "epub_style.css")
HTML_CSS_INDEX_PATH = RES.joinpath("styles", "html_index.css")
HTML_CSS_CHAPTER_PATH = RES.joinpath("styles", "html_chapter.css")

# JSON
FANQIENOVEL_MAP_PATH = RES.joinpath("json", "fanqienovel.json")
HONGXIUZHAO_MAP_PATH = RES.joinpath("json", "hongxiuzhao.json")
LINOVELIB_MAP_PATH = RES.joinpath("json", "linovelib.json")
N69YUE_MAP_PATH = RES.joinpath("json", "n69yue.json")
XIGUASHUWU_MAP_PATH = RES.joinpath("json", "xiguashuwu.json")
YODU_MAP_PATH = RES.joinpath("json", "yodu.json")

# JavaScript
EXPR_TO_JSON_SCRIPT_PATH = RES.joinpath("scripts", "expr_to_json.js")
QD_DECRYPT_SCRIPT_PATH = RES.joinpath("scripts", "qidian_decrypt_node.js")
QQ_DECRYPT_SCRIPT_PATH = RES.joinpath("scripts", "qq_decrypt_node.js")
HTML_JS_MAIN_PATH = RES.joinpath("scripts", "html_main.js")
