#!/usr/bin/env python3
"""
novel_downloader.apps.login
---------------------------

Interactive login helpers shared by the CLI and the web/desktop UI.
"""

__all__ = [
    "COOKIE_FILENAME",
    "DEFAULT_TIMEOUT",
    "EXIT_LOGIN_FAILED",
    "EXIT_LOGIN_SUCCESS",
    "EXIT_LOGIN_UNAVAILABLE",
    "EXIT_LOGIN_USAGE",
    "LOGIN_SITES",
    "POLL_INTERVAL",
    "PROBE_TIMEOUT",
    "LoginOutcome",
    "LoginStatus",
    "SiteLoginConfig",
    "evaluate_probe_payload",
    "exit_code_for",
    "get_site_config",
    "login_main",
    "run_login",
    "run_login_result",
    "save_cookies",
    "site_cache_dir",
    "supported_sites",
]

from .main import (
    COOKIE_FILENAME,
    DEFAULT_TIMEOUT,
    EXIT_LOGIN_FAILED,
    EXIT_LOGIN_SUCCESS,
    EXIT_LOGIN_UNAVAILABLE,
    EXIT_LOGIN_USAGE,
    LOGIN_SITES,
    POLL_INTERVAL,
    PROBE_TIMEOUT,
    LoginOutcome,
    LoginStatus,
    exit_code_for,
    login_main,
    run_login,
    run_login_result,
    save_cookies,
    site_cache_dir,
)
from .sites import (
    SiteLoginConfig,
    evaluate_probe_payload,
    get_site_config,
    supported_sites,
)
