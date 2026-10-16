#!/usr/bin/env python3
"""
novel_downloader.apps.desktop.main
----------------------------------

Desktop application: the NiceGUI UI in a native window (pywebview).

The working directory is pinned to the application home directory *before* any
project module is imported, so the existing relative-path logic
(``settings.toml``, ``./downloads``, ``./rules``, ``./logs``) keeps working no
matter where the app is launched from.

Nothing at module level may call ``ui.run``: NiceGUI spawns a child process for
the native window, and that child re-imports this module.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from importlib.util import find_spec
from pathlib import Path

#: WebView2 runtime client id published by Microsoft.
WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _window_size(value: str) -> tuple[int, int]:
    """
    Parse a ``WxH`` window size, e.g. ``1100x800``.

    :param value: Raw command line value.
    :return: Width and height in pixels.
    """
    from novel_downloader.infra.i18n import t

    try:
        width, height = value.lower().split("x")
        return int(width), int(height)
    except ValueError as err:
        raise argparse.ArgumentTypeError(
            t("Expected a window size like 1100x800.")
        ) from err


def _webview2_present() -> bool:
    """
    Best-effort detection of the Microsoft Edge WebView2 runtime.

    Windows 11 ships it; Windows 10 does not necessarily.

    :return: ``True`` when a runtime is believed to be installed.
    """
    if sys.platform != "win32":
        return False

    try:
        import winreg
    except ImportError:  # pragma: no cover - only on odd Windows builds
        return False

    sub_keys = (
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
    )
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub_key in sub_keys:
            try:
                with winreg.OpenKey(root, sub_key) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if version and version != "0.0.0.0":
                return True

    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
        if not base:
            continue
        pattern = os.path.join(
            base, "Microsoft", "EdgeWebView", "Application", "*", "msedgewebview2.exe"
        )
        if glob.glob(pattern):
            return True

    return False


def _native_blocker() -> str:
    """
    Explain why the native window mode cannot be used.

    :return: A short diagnostic, or ``""`` when native mode is available.
    """
    if os.environ.get("NOVEL_NO_NATIVE") == "1":
        return "NOVEL_NO_NATIVE is set"
    if find_spec("webview") is None:
        return "pywebview is not installed"
    if sys.platform == "win32" and not _webview2_present():
        return "the Microsoft Edge WebView2 runtime is missing"
    return ""


def _preparse(argv: list[str] | None) -> tuple[str | None, str | None]:
    """
    Read ``--home`` and ``--lang`` before any project module is imported.

    ``infra.paths`` and ``infra.persistence.state`` resolve their locations at
    import time, so ``NOVEL_HOME`` has to be set first.

    :param argv: Argument list to scan; defaults to ``sys.argv[1:]``.
    :return: The raw ``--home`` and ``--lang`` values.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--home")
    pre.add_argument("--lang")
    pre_args, _ = pre.parse_known_args(argv)
    return pre_args.home, pre_args.lang


def _build_parser() -> argparse.ArgumentParser:
    """Build the desktop command line parser (help text is localized)."""
    from novel_downloader.infra.i18n import t

    p = argparse.ArgumentParser(
        prog="novel-desktop",
        description=t("Novel Downloader desktop UI."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--home",
        metavar="DIR",
        help=t("Application home directory. Defaults to the platform data directory."),
    )
    p.add_argument(
        "--lang",
        metavar="CODE",
        help=t("Override the interface language (e.g. en, zh)."),
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help=t("Open the UI in the default browser instead of a native window."),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=t("Print an environment report and exit."),
    )
    p.add_argument(
        "--window-size",
        metavar="WxH",
        type=_window_size,
        default=None,
        help=t("Initial native window size."),
    )
    return p


def _print_report(home: Path, config_path: Path, blocker: str) -> None:
    """
    Print the environment report requested by ``--check``.

    :param home: Resolved application home directory.
    :param config_path: Path of the settings file inside ``home``.
    :param blocker: Reason native mode is unavailable, or ``""``.
    """
    from novel_downloader.infra.i18n import get_language, t

    yes, no = t("Yes"), t("No")
    rows = (
        (t("Home directory: {value}"), str(home)),
        (t("Config file: {value}"), str(config_path)),
        (t("Interface language: {value}"), get_language()),
        (
            t("pywebview installed: {value}"),
            no if find_spec("webview") is None else yes,
        ),
        (
            t("WebView2 runtime present (Windows): {value}"),
            yes if _webview2_present() else no,
        ),
        (t("Native window mode: {value}"), blocker or t("available")),
    )
    for label, value in rows:
        print(label.format(value=value))


def desktop_main(argv: list[str] | None = None) -> int:
    """
    Launch the desktop application.

    :param argv: Argument list; defaults to ``sys.argv[1:]``.
    :return: Process exit code.
    """
    home_arg, lang_arg = _preparse(argv)

    # Must happen before the first project import (see ``_preparse``).
    if home_arg:
        os.environ["NOVEL_HOME"] = str(Path(home_arg).expanduser().resolve())

    from novel_downloader.apps.web.main import setup_app
    from novel_downloader.infra.config import copy_default_config
    from novel_downloader.infra.i18n import (
        LANG_ENV_VAR,
        available_languages,
        normalize_language,
        t,
    )
    from novel_downloader.infra.paths import DEFAULT_CONFIG_FILENAME, app_home

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)

    if lang_arg:
        code = normalize_language(lang_arg)
        if code:
            os.environ[LANG_ENV_VAR] = code
        else:
            langs = ", ".join(sorted(available_languages()))
            print(
                t("Unsupported language: {lang}. Available: {langs}").format(
                    lang=lang_arg, langs=langs
                )
            )

    args = _build_parser().parse_args(argv)

    config_path = home / DEFAULT_CONFIG_FILENAME
    if not config_path.is_file():
        copy_default_config(config_path)

    blocker = _native_blocker()

    if args.check:
        _print_report(home, config_path, blocker)
        return 0

    native = not args.browser and not blocker
    if not args.browser and blocker:
        print(
            t(
                "Native window is unavailable ({reason}); falling back to the browser."
            ).format(reason=blocker)
        )

    setup_app(
        host="127.0.0.1",
        port=0,
        native=native,
        window_size=args.window_size,
    )
    return 0
