#!/usr/bin/env python3
"""
novel_downloader.apps.web.main
------------------------------

Novel Downloader web UI (NiceGUI).

This entry point starts the local server and registers the app's pages. The
launch logic is shared with the desktop app through :func:`setup_app`.
"""

from __future__ import annotations

import argparse
import asyncio
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from nicegui import app, ui

from novel_downloader.apps.web.services import manager
from novel_downloader.infra.config import get_config_value
from novel_downloader.infra.i18n import t
from novel_downloader.infra.logger import setup_logging
from novel_downloader.infra.paths import APP_FAVICON_PATH, APP_ICON_PATH

#: Initial size of the native window when none is requested.
DEFAULT_WINDOW_SIZE = (1100, 800)


def mount_exports() -> None:
    """Serve the exported books from the configured output directory."""
    output_dir = get_config_value(["general", "output_dir"], "./downloads")
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    # serves /downloads/<filename> from the export dir
    app.add_static_files("/downloads", local_directory=out)


async def shutdown() -> None:
    """Stop the background workers when the server goes down."""
    print(t("Shutting down workers..."))
    await manager.close()


def _static_path(resource: Traversable) -> str | None:
    """
    Return a filesystem path for a bundled resource.

    :param resource: Resource looked up through ``importlib.resources``.
    :return: The path as a string, or ``None`` when the resource is missing.
    """
    try:
        return str(resource) if resource.is_file() else None
    except OSError:
        return None


def setup_app(
    *,
    host: str,
    port: int,
    native: bool = False,
    reload: bool = False,
    window_size: tuple[int, int] | None = None,
) -> None:
    """
    Configure and launch the NiceGUI application.

    Shared by :func:`web_main` and the desktop entry point. The current working
    directory must already be pinned, because page modules resolve relative
    config paths when they are imported.

    :param host: Address the local server binds to.
    :param port: TCP port to serve on; ``0`` lets the OS pick a free one.
    :param native: Open a native window (pywebview) instead of a browser tab.
    :param reload: Enable NiceGUI autoreload (development only).
    :param window_size: Initial size of the native window, in pixels.
    """
    log_level = get_config_value(["general", "debug", "log_level"], "INFO")
    log_dir = get_config_value(["general", "debug", "log_dir"], "./logs")
    setup_logging(log_dir=log_dir, console_level=log_level)

    # Imported inside the function (never at module level) so that relative
    # config paths resolve against the pinned working directory.
    import novel_downloader.apps.web.pages  # noqa: F401

    app.on_startup(mount_exports)
    app.on_shutdown(shutdown)

    run_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "reload": reload,
        "title": t("Novel Downloader"),
    }

    favicon = _static_path(APP_FAVICON_PATH)
    if favicon is not None:
        run_kwargs["favicon"] = favicon

    if native:
        # NiceGUI reuses ``favicon`` as the native window icon on Windows, and
        # ``LoadImageW(IMAGE_ICON)`` requires a real ``.ico``, not a PNG.
        icon = _static_path(APP_ICON_PATH)
        if icon is not None:
            run_kwargs["favicon"] = icon
        run_kwargs["native"] = True
        run_kwargs["window_size"] = window_size or DEFAULT_WINDOW_SIZE

    try:
        ui.run(**run_kwargs)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(t("Server has stopped"))


def web_main() -> None:
    p = argparse.ArgumentParser(
        description=t("Novel Downloader web UI."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--listen",
        choices=["local", "public"],
        default="local",
        help=(
            "Bind address mode: 'local' binds to 127.0.0.1; 'public' binds to 0.0.0.0."
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port to serve the app on.",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="Enable autoreload on code changes (development).",
    )
    p.add_argument(
        "--native",
        action="store_true",
        help="Open a native window (pywebview) instead of a browser tab.",
    )
    args = p.parse_args()

    host = "127.0.0.1" if args.listen == "local" else "0.0.0.0"
    setup_app(host=host, port=args.port, native=args.native, reload=args.reload)


if __name__ in {"__main__", "__mp_main__"}:
    web_main()
