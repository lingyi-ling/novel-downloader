#!/usr/bin/env python3
"""
novel_downloader.apps.login.main
--------------------------------

Interactive login helper: open a real browser window on the target site, let
the user sign in, poll the window for session cookies, verify the login and
persist the cookies in the same on-disk format the downloader reads.

The window is driven by ``pywebview``, which must run its event loop on the
main thread; therefore :func:`run_login` is only safe to call from a main
thread. The web/desktop UI runs it in a dedicated subprocess instead (see
:func:`login_main`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from novel_downloader.infra.config import get_config_value
from novel_downloader.infra.i18n import t

__all__ = [
    "COOKIE_FILENAME",
    "LOGIN_SITES",
    "login_main",
    "run_login",
    "save_cookies",
    "site_cache_dir",
]

logger = logging.getLogger(__name__)

#: Sites that expose an interactive login window, mapped to their entry URL.
LOGIN_SITES: dict[str, str] = {
    "fanqienovel": "https://fanqienovel.com/",
}

#: Endpoint used to confirm a session is authenticated. A JSON body whose
#: ``code`` equals ``0`` means "logged in"; anything else means anonymous.
_LOGIN_PROBES: dict[str, str] = {
    "fanqienovel": "https://fanqienovel.com/api/user/info/v2",
}

#: Cookie file name shared with the aiohttp session backend.
COOKIE_FILENAME = "aiohttp.cookies"

#: How often the login window is polled for cookies, in seconds.
POLL_INTERVAL = 1.5

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _default_notify(message: str) -> None:
    """Print a progress message to stderr, falling back to the log."""
    if sys.stderr is None:
        logger.info(message)
        return
    print(message, file=sys.stderr, flush=True)


def _configured_cache_dir() -> str:
    """Return the configured ``general.cache_dir``, tolerating a missing config."""
    try:
        return str(get_config_value(["general", "cache_dir"], "./novel_cache"))
    except Exception:  # no config file on disk yet
        return "./novel_cache"


def site_cache_dir(site: str) -> Path:
    """
    Resolve the cache directory a site's cookies are stored in.

    Mirrors the downloader's resolution order: the ``NOVEL_CACHE_DIR``
    environment variable, then the configured ``cache_dir``, then
    ``./novel_cache``.

    :param site: Site key (e.g. ``fanqienovel``).
    :return: ``<cache_dir>/<site>`` as an absolute path.
    """
    override = os.environ.get("NOVEL_CACHE_DIR", "").strip()
    base = Path(override) if override else Path(_configured_cache_dir())
    return base.expanduser().resolve() / site


def save_cookies(site: str, cookies: dict[str, str]) -> Path:
    """
    Persist cookies in the format the aiohttp session backend reads.

    :param site: Site key.
    :param cookies: Cookie name/value pairs.
    :return: The path the cookies were written to.
    """
    target = site_cache_dir(site)
    target.mkdir(parents=True, exist_ok=True)
    path = target / COOKIE_FILENAME
    data = [{"name": name, "value": value} for name, value in cookies.items()]
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _looks_logged_in(
    site: str, cookies: dict[str, str], *, timeout: float = 10.0
) -> bool:
    """
    Probe the site to confirm the cookies represent an authenticated session.

    :param site: Site key.
    :param cookies: Cookie name/value pairs collected from the login window.
    :param timeout: Per-request timeout in seconds.
    :return: True when the site reports a logged-in session.
    """
    if not cookies:
        return False

    probe = _LOGIN_PROBES.get(site)
    if probe is None:
        # No probe configured: assume any cookie means the user is signed in.
        return True

    header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    request = urllib.request.Request(
        probe,
        headers={
            "Cookie": header,
            "User-Agent": _USER_AGENT,
            "Referer": LOGIN_SITES.get(site, ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return False

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (TypeError, ValueError):
        return False

    return isinstance(payload, dict) and payload.get("code") == 0


def _collect_cookies(window: Any) -> dict[str, str]:
    """Flatten a pywebview window's ``SimpleCookie`` list into a plain dict."""
    try:
        raw = window.get_cookies()
    except Exception:
        return {}

    jar: dict[str, str] = {}
    for simple in raw or []:
        try:
            items = simple.items()
        except AttributeError:
            continue
        for name, morsel in items:
            jar[str(name)] = str(getattr(morsel, "value", morsel))
    return jar


def run_login(
    site: str,
    *,
    timeout: float = 300.0,
    notify: Callable[[str], None] | None = None,
) -> bool:
    """
    Open a login window for ``site`` and save its session cookies on success.

    Must be called from the main thread (pywebview requirement).

    :param site: Site key (see :data:`LOGIN_SITES`).
    :param timeout: Seconds to wait for the user to sign in.
    :param notify: Sink for progress messages; stderr by default.
    :return: True when a session was detected and saved, otherwise False.
    """
    emit = notify or _default_notify

    url = LOGIN_SITES.get(site)
    if url is None:
        emit(
            t("Site '{site}' does not support an interactive login.").format(site=site)
        )
        return False

    try:
        import webview
    except ImportError:
        logger.error("pywebview is not installed")
        emit(
            t(
                "pywebview is required for the login window. "
                "Install the 'desktop' extra: pip install 'novel-downloader[desktop]'"
            )
        )
        return False

    emit(t("Opening a login window for {site}...").format(site=site))
    window = webview.create_window(
        title=t("Log in to {site}").format(site=site),
        url=url,
        width=1000,
        height=760,
    )
    if window is None:
        logger.error("Failed to create the login window")
        emit(t("Could not open the login window."))
        return False

    state: dict[str, Any] = {"ok": False, "timed_out": False}

    def _watch() -> None:
        if not window.events.loaded.wait(timeout):
            state["timed_out"] = True
            return

        emit(t("Please complete the login in the opened window."))
        deadline = time.monotonic() + timeout

        while not window.events.closed.is_set():
            cookies = _collect_cookies(window)
            if _looks_logged_in(site, cookies):
                path = save_cookies(site, cookies)
                state["ok"] = True
                emit(
                    t("Login detected; session cookies saved to {path}").format(
                        path=path
                    )
                )
                break
            if time.monotonic() >= deadline:
                state["timed_out"] = True
                break
            if window.events.closed.wait(POLL_INTERVAL):
                break

        with contextlib.suppress(Exception):
            window.destroy()

    webview.start(_watch)

    if state["ok"]:
        return True
    if state["timed_out"]:
        emit(
            t("Login was not completed within {timeout} seconds.").format(
                timeout=int(timeout)
            )
        )
    else:
        emit(t("Login window closed before a session was detected."))
    return False


def login_main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point (used by the web/desktop UI as a subprocess).

    :param argv: Argument list; ``sys.argv[1:]`` when omitted.
    :return: Process exit code (0 on success).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="novel-login",
        description=t("Open a login window and save the session cookies."),
    )
    parser.add_argument(
        "site",
        nargs="?",
        default="fanqienovel",
        help=t("Site key to log in to."),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help=t("Seconds to wait for the login to complete."),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=t("List sites that support an interactive login."),
    )
    args = parser.parse_args(argv)

    if args.list:
        for key in sorted(LOGIN_SITES):
            print(key)
        return 0

    return 0 if run_login(args.site, timeout=args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(login_main())
