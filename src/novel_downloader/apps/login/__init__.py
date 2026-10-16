#!/usr/bin/env python3
"""
novel_downloader.apps.login
---------------------------

Interactive login helpers shared by the CLI and the web/desktop UI.
"""

__all__ = [
    "COOKIE_FILENAME",
    "LOGIN_SITES",
    "login_main",
    "run_login",
    "save_cookies",
    "site_cache_dir",
]

from .main import (
    COOKIE_FILENAME,
    LOGIN_SITES,
    login_main,
    run_login,
    save_cookies,
    site_cache_dir,
)
