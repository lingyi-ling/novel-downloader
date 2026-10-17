#!/usr/bin/env python3
"""
novel_downloader.apps.browser.main
----------------------------------

Browser-assisted search.

Some sites sign their search requests inside their own JavaScript (fanqienovel
being one), so an HTTP client cannot query them even with a valid session. This
module drives a real browser window instead — reusing the profile created by
:mod:`novel_downloader.apps.login` — lets the site's own script issue the signed
request, and captures the JSON response through the Chrome DevTools Protocol.

pywebview has to own the main thread, so :func:`search_via_browser` runs the
search in a dedicated subprocess and reads back the captured JSON from its
stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from novel_downloader.infra.i18n import t

__all__ = [
    "BROWSER_SEARCH_TIMEOUT",
    "BrowserSearchSpec",
    "browser_search_main",
    "capture_search_json",
    "resolve_spec",
    "run_browser_search",
    "search_via_browser",
]

logger = logging.getLogger(__name__)

#: Default seconds to wait for the site to produce the search response.
BROWSER_SEARCH_TIMEOUT = 60.0

#: Attribute a site plugin module has to expose as its search description.
SPEC_ATTR = "BROWSER_SEARCH"

#: Subdirectory of a site's cache dir holding the persistent browser profile.
PROFILE_DIRNAME = "browser"

_POLL_INTERVAL = 0.25
_CDP_READY_TIMEOUT = 15.0
_PAGE_LOAD_TIMEOUT = 30.0
_SUBPROCESS_GRACE = 60.0


@dataclass(frozen=True)
class BrowserSearchSpec:
    """
    Description of how a site's search page is reached through a browser.

    :param site: Site key, also used to pick the persistent browser profile.
    :param home_url: Page loaded first, so the site can set up its own session.
    :param search_url: Search page URL template containing ``{keyword}``.
    :param response_marker: Substring identifying the search response URL.
    """

    site: str
    home_url: str
    search_url: str
    response_marker: str


def _default_notify(message: str) -> None:
    """Print a progress message to stderr, falling back to the log."""
    if sys.stderr is None:
        logger.info(message)
        return
    print(message, file=sys.stderr, flush=True)


def resolve_spec(site: str) -> BrowserSearchSpec | None:
    """
    Look up the browser search description of a site plugin.

    :param site: Site key (e.g. ``fanqienovel``).
    :return: The site's :class:`BrowserSearchSpec`, or None when the site does
        not support browser-assisted search.
    """
    module_name = (
        f"novel_downloader.plugins.sites.{site.strip().lower()}.browser_search"
    )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None
    spec = getattr(module, SPEC_ATTR, None)
    return spec if isinstance(spec, BrowserSearchSpec) else None


def _profile_dir(site: str) -> Path:
    """Return the persistent browser profile dir of ``site``."""
    from novel_downloader.apps.login import site_cache_dir

    return site_cache_dir(site) / PROFILE_DIRNAME


def _free_port() -> int:
    """Ask the OS for a free local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_devtools_targets(port: int) -> list[dict[str, Any]]:
    """Read the DevTools target list of a remote debugging port."""
    url = f"http://127.0.0.1:{port}/json"
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read())
    return payload if isinstance(payload, list) else []


async def _find_target(port: int, deadline: float) -> str | None:
    """Wait for a debuggable page target and return its WebSocket URL."""
    while time.monotonic() < deadline:
        try:
            targets = await asyncio.to_thread(_read_devtools_targets, port)
        except Exception:
            targets = []
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return str(target["webSocketDebuggerUrl"])
        await asyncio.sleep(_POLL_INTERVAL)
    return None


async def _response_body(
    ws: aiohttp.ClientWebSocketResponse, request_id: str
) -> str | None:
    """Ask for a captured response body, ignoring unrelated events."""
    await ws.send_json(
        {
            "id": 2,
            "method": "Network.getResponseBody",
            "params": {"requestId": request_id},
        }
    )
    while True:
        try:
            message = await ws.receive(timeout=10)
        except TimeoutError:
            return None
        if message.type != aiohttp.WSMsgType.TEXT:
            return None
        payload = json.loads(message.data)
        if payload.get("id") == 2:
            body = (payload.get("result") or {}).get("body")
            return body if isinstance(body, str) and body else None


async def _capture_search_response(
    port: int,
    marker: str,
    deadline: float,
    result: dict[str, Any],
    ready: threading.Event,
) -> None:
    """
    Capture the search response body through the DevTools Protocol.

    :param port: Remote debugging port of the browser window.
    :param marker: Substring identifying the search response URL.
    :param deadline: ``time.monotonic()`` value after which to give up.
    :param result: Shared dict receiving ``body``/``done``/``error``.
    :param ready: Set once the network domain is enabled, so the caller knows
        it is safe to navigate to the search page.
    """
    ws_url = await _find_target(
        port, min(deadline, time.monotonic() + _CDP_READY_TIMEOUT)
    )
    if ws_url is None:
        result.setdefault("error", "devtools endpoint not available")
        return

    pending: dict[str, str] = {}
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(ws_url, max_msg_size=0) as ws,
    ):
        await ws.send_json({"id": 1, "method": "Network.enable"})
        ready.set()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.setdefault("error", "search response not captured in time")
                return
            try:
                message = await ws.receive(timeout=remaining)
            except TimeoutError:
                result.setdefault("error", "search response not captured in time")
                return
            if message.type != aiohttp.WSMsgType.TEXT:
                result.setdefault("error", "devtools connection closed")
                return

            event = json.loads(message.data)
            method = event.get("method")
            params = event.get("params")
            if not isinstance(params, dict):
                continue

            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                if marker in url:
                    pending[str(params.get("requestId"))] = url
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId"))
                if request_id in pending:
                    # The body is only available once the response is complete.
                    body = await _response_body(ws, request_id)
                    if body is None:
                        result.setdefault("error", "empty search response body")
                        return
                    result["body"] = body
                    result["done"] = True
                    return


def capture_search_json(
    spec: BrowserSearchSpec,
    keyword: str,
    *,
    timeout: float = BROWSER_SEARCH_TIMEOUT,
    notify: Callable[[str], None] | None = None,
) -> str | None:
    """
    Search through a browser window and return the captured JSON body.

    Must be called from the main thread (pywebview requirement). The window
    stays hidden: the site's own script performs the search, so nothing needs
    to be shown or clicked.

    :param spec: Site search description (see :func:`resolve_spec`).
    :param keyword: Search keyword.
    :param timeout: Seconds to wait for the search response.
    :param notify: Sink for progress messages; stderr by default.
    :return: The raw JSON body of the search response, or None on failure.
    """
    emit = notify or _default_notify

    try:
        import webview
    except ImportError:
        logger.error("pywebview is not installed")
        emit(
            t(
                "pywebview is required for browser-assisted search. "
                "Install the 'desktop' extra: pip install 'novel-downloader[desktop]'"
            )
        )
        return None

    port = _free_port()
    # Already present in pywebview's settings, so it can be adjusted in place.
    webview.settings["REMOTE_DEBUGGING_PORT"] = port
    search_url = spec.search_url.format(keyword=quote(keyword, safe=""))
    deadline = time.monotonic() + timeout
    result: dict[str, Any] = {}
    ready = threading.Event()

    window = webview.create_window(
        title=t("Searching {site} in a browser window...").format(site=spec.site),
        url=spec.home_url,
        hidden=True,
        width=1200,
        height=800,
    )
    if window is None:
        logger.error("Failed to create the browser search window")
        emit(t("Could not open the browser search window."))
        return None

    def _watch() -> None:
        if not window.events.loaded.wait(_PAGE_LOAD_TIMEOUT):
            result["error"] = "home page load timeout"
        else:
            threading.Thread(
                target=lambda: asyncio.run(
                    _capture_search_response(
                        port, spec.response_marker, deadline, result, ready
                    )
                ),
                daemon=True,
            ).start()
            if ready.wait(_CDP_READY_TIMEOUT):
                # A full page load is required; the site's router does not fire
                # the search request on an in-page navigation.
                window.evaluate_js(f"location.href = {json.dumps(search_url)}")
            else:
                result["error"] = "devtools did not become ready"

            while not result.get("done") and time.monotonic() < deadline:
                time.sleep(_POLL_INTERVAL)

        with contextlib.suppress(Exception):
            window.destroy()

    webview.start(_watch, private_mode=False, storage_path=str(_profile_dir(spec.site)))

    body = result.get("body")
    if isinstance(body, str) and body:
        return body
    logger.warning(
        "browser-assisted search for %s failed: %s", spec.site, result.get("error")
    )
    return None


def run_browser_search(
    site: str,
    keyword: str,
    *,
    timeout: float = BROWSER_SEARCH_TIMEOUT,
    notify: Callable[[str], None] | None = None,
) -> str | None:
    """
    Search ``site`` for ``keyword`` through a browser window.

    Must be called from the main thread (pywebview requirement); the web/desktop
    UI runs it in a subprocess instead.

    :param site: Site key (see :func:`resolve_spec`).
    :param keyword: Search keyword.
    :param timeout: Seconds to wait for the search response.
    :param notify: Sink for progress messages; stderr by default.
    :return: The raw JSON body of the search response, or None on failure.
    """
    emit = notify or _default_notify

    spec = resolve_spec(site)
    if spec is None:
        emit(
            t("Site '{site}' does not support browser-assisted search.").format(
                site=site
            )
        )
        return None

    emit(t("Searching {site} in a browser window...").format(site=spec.site))
    body = capture_search_json(spec, keyword, timeout=timeout, notify=emit)
    if body is None:
        emit(
            t(
                "Browser-assisted search for {site} returned no data; "
                "run 'novel-cli login {site}' if no session is available."
            ).format(site=site)
        )
    return body


def _parse_subprocess_output(text: str | None) -> str | None:
    """Pick the JSON result line out of a subprocess stdout stream."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or not payload.get("ok"):
            continue
        body = payload.get("body")
        if isinstance(body, str) and body:
            return body
    return None


def search_via_browser(
    site: str,
    keyword: str,
    *,
    timeout: float = BROWSER_SEARCH_TIMEOUT,
) -> str | None:
    """
    Blocking browser search, run in a subprocess so pywebview keeps the main
    thread to itself. Safe to call from any thread (use ``asyncio.to_thread``).

    :param site: Site key.
    :param keyword: Search keyword.
    :param timeout: Seconds the browser search may take.
    :return: The raw JSON body of the search response, or None on failure.
    """
    cmd = [
        sys.executable,
        "-m",
        "novel_downloader.apps.browser",
        site,
        keyword,
        "--timeout",
        str(timeout),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + _SUBPROCESS_GRACE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("browser-assisted search subprocess failed: %s", e)
        return None

    body = _parse_subprocess_output(proc.stdout)
    if body is None:
        logger.warning(
            "browser-assisted search for %s produced no usable output (exit=%s)",
            site,
            proc.returncode,
        )
    return body


def browser_search_main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point (used as a subprocess by :func:`search_via_browser`).

    :param argv: Argument list; ``sys.argv[1:]`` when omitted.
    :return: Process exit code (0 when a response was captured).
    """
    parser = argparse.ArgumentParser(
        prog="novel-browser-search",
        description=t(
            "Search a site through a browser window and print the raw JSON response."
        ),
    )
    parser.add_argument("site", help=t("Site key to search."))
    parser.add_argument("keyword", help=t("Search keyword."))
    parser.add_argument(
        "--timeout",
        type=float,
        default=BROWSER_SEARCH_TIMEOUT,
        help=t("Seconds to wait for the search response."),
    )
    args = parser.parse_args(argv)

    body = run_browser_search(args.site, args.keyword, timeout=args.timeout)
    payload = {"ok": bool(body), "body": body or ""}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if body else 1


if __name__ == "__main__":
    raise SystemExit(browser_search_main())
