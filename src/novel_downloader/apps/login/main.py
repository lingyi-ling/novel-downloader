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

Robustness notes
----------------
* A missing GUI backend or a failure to create/start the window is reported
  explicitly through :class:`LoginOutcome` (and a distinct exit code from
  :func:`login_main`), instead of being indistinguishable from a cancelled
  login.
* The wait loop is interruptible: closing the window cancels immediately and
  the deadline is sliced into short waits, so a hung page load cannot make
  the command unresponsive.
* Cookies are written atomically through
  :func:`novel_downloader.infra.sessions.cookie_io.write_cookie_items`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http.cookies import BaseCookie
from pathlib import Path
from typing import Any, Literal

from novel_downloader.infra.config import get_config_value
from novel_downloader.infra.cookies import parse_cookies
from novel_downloader.infra.i18n import t
from novel_downloader.infra.sessions.cookie_io import (
    resolve_cookie_path,
    write_cookie_items,
)
from novel_downloader.schemas.config import default_cache_dir

from .sites import (
    LOGIN_SITE_CONFIGS,
    SiteLoginConfig,
    evaluate_probe_payload,
    get_site_config,
    login_sites,
    normalize_site,
)

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
    "exit_code_for",
    "login_main",
    "run_login",
    "run_login_result",
    "save_cookies",
    "site_cache_dir",
]

logger = logging.getLogger(__name__)

#: Sites that expose an interactive login window, mapped to their entry URL.
LOGIN_SITES: dict[str, str] = login_sites()

#: Cookie file name shared with the aiohttp session backend.
COOKIE_FILENAME = "aiohttp.cookies"

#: Default seconds to wait for the user to complete the login.
DEFAULT_TIMEOUT = 300.0

#: How often the login window is polled for cookies, in seconds.
POLL_INTERVAL = 1.5

#: Per-request timeout for the login probe, in seconds.
PROBE_TIMEOUT = 10.0

#: Longest single slice spent waiting on a GUI event, so a close or deadline
#: is noticed promptly even when the backend blocks inside ``wait()``.
_WAIT_SLICE = 0.25

#: Never spin the poll loop faster than this.
_MIN_POLL_INTERVAL = 0.05

#: Site keys become directory names, so they must not contain path separators
#: (or any other character that could escape the cache directory).
_SITE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Process exit codes used by :func:`login_main`.
EXIT_LOGIN_SUCCESS = 0
EXIT_LOGIN_FAILED = 1
EXIT_LOGIN_USAGE = 2
EXIT_LOGIN_UNAVAILABLE = 3

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LoginStatus(StrEnum):
    """Outcome of an interactive login attempt."""

    SUCCESS = "success"
    UNSUPPORTED_SITE = "unsupported_site"
    WEBVIEW_UNAVAILABLE = "webview_unavailable"
    WINDOW_ERROR = "window_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SAVE_ERROR = "save_error"


@dataclass(frozen=True)
class LoginOutcome:
    """
    Detailed result of :func:`run_login_result`.

    :param status: What happened.
    :param site: Normalized site key.
    :param path: Cookie file path, set only on success.
    :param error: Human-readable failure detail, when available.
    """

    status: LoginStatus
    site: str
    path: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return True when the login succeeded and cookies were saved."""
        return self.status is LoginStatus.SUCCESS


_EXIT_CODES: dict[LoginStatus, int] = {
    LoginStatus.SUCCESS: EXIT_LOGIN_SUCCESS,
    LoginStatus.UNSUPPORTED_SITE: EXIT_LOGIN_USAGE,
    LoginStatus.WEBVIEW_UNAVAILABLE: EXIT_LOGIN_UNAVAILABLE,
    LoginStatus.WINDOW_ERROR: EXIT_LOGIN_UNAVAILABLE,
    LoginStatus.TIMEOUT: EXIT_LOGIN_FAILED,
    LoginStatus.CANCELLED: EXIT_LOGIN_FAILED,
    LoginStatus.SAVE_ERROR: EXIT_LOGIN_FAILED,
}


def exit_code_for(status: LoginStatus) -> int:
    """
    Map a login outcome to a process exit code.

    ``0`` means success, ``1`` means the login itself failed or was cancelled,
    ``2`` means the site is unsupported (usage error) and ``3`` means the
    window could not be provided (missing pywebview, no GUI, backend error).

    :param status: Login status.
    :return: Process exit code.
    """
    return _EXIT_CODES.get(status, EXIT_LOGIN_FAILED)


def _default_notify(message: str) -> None:
    """Print a progress message to stderr, falling back to the log."""
    if sys.stderr is None:
        logger.info(message)
        return
    print(message, file=sys.stderr, flush=True)


def _emit(notify: Callable[[str], None], message: str) -> None:
    """Deliver a progress message, never letting a bad sink break the flow."""
    try:
        notify(message)
    except Exception:
        logger.debug("login notification callback failed", exc_info=True)


def _with_detail(message: str, detail: object) -> str:
    """
    Append a diagnostic detail to a translated message.

    The base message is a catalogued msgid; the detail is raw diagnostic text,
    so the translation catalog does not have to grow for every backend error.
    """
    text = str(detail).strip()
    return f"{message} ({text})" if text else message


def _configured_cache_dir() -> str:
    """Return the configured ``general.cache_dir``, tolerating a missing config."""
    try:
        return str(get_config_value(["general", "cache_dir"], str(default_cache_dir())))
    except Exception:  # no config file on disk yet
        return str(default_cache_dir())


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
    key = normalize_site(site)
    if not _SITE_KEY_RE.match(key) or ".." in key:
        raise ValueError(f"Invalid site key for a cache directory: {site!r}")
    return base.expanduser().resolve() / key


def save_cookies(site: str, cookies: Mapping[str, str]) -> Path:
    """
    Persist cookies in the format the aiohttp session backend reads.

    The write is atomic (temporary file + ``os.replace``) and malformed names
    or control characters are dropped before serialization, so a cookie file
    is never observed half-written and can always be parsed back.

    :param site: Site key.
    :param cookies: Cookie name/value pairs.
    :return: The path the cookies were written to.
    """
    target = site_cache_dir(site)
    path = resolve_cookie_path(target, COOKIE_FILENAME)
    normalized = parse_cookies(cookies)
    write_cookie_items(
        path,
        ({"name": name, "value": value} for name, value in normalized.items()),
    )
    return path


def _site_config(site: str) -> SiteLoginConfig | None:
    """
    Return the effective login configuration for *site*.

    The declarative table lives in :mod:`novel_downloader.apps.login.sites`.
    ``LOGIN_SITES``/``_LOGIN_PROBES`` are still honoured as overrides so that
    callers (and tests) can add a site or disable a network probe without
    rebuilding the table.
    """
    key = normalize_site(site)
    config = get_site_config(key)
    if config is None:
        url = LOGIN_SITES.get(key)
        if not url:
            return None
        probe = _LOGIN_PROBES.get(key)
        return SiteLoginConfig(
            site=key,
            start_url=url,
            probe_url=probe,
            probe_kind="json_code" if probe else "cookies",
        )

    probe = _LOGIN_PROBES.get(key)
    start_url = LOGIN_SITES.get(key, config.start_url)
    if probe == config.probe_url and start_url == config.start_url:
        return config
    return dataclasses.replace(config, start_url=start_url, probe_url=probe)


#: Probe overrides, kept as a mutable mapping for backwards compatibility.
_LOGIN_PROBES: dict[str, str] = {
    site: config.probe_url
    for site, config in LOGIN_SITE_CONFIGS.items()
    if config.probe_url is not None
}


def _decode_probe(config: SiteLoginConfig, payload: str | bytes | None) -> bool:
    """Thin wrapper around the pure probe evaluation (kept for testability)."""
    return evaluate_probe_payload(config, payload)


def is_logged_in(
    config: SiteLoginConfig,
    cookies: Mapping[str, str],
    *,
    timeout: float = PROBE_TIMEOUT,
) -> bool:
    """
    Decide whether *cookies* represent an authenticated session.

    Sites without a probe endpoint accept any non-empty cookie jar that also
    satisfies their required-cookie names.  Sites with a probe endpoint send
    the cookies to it and interpret the response according to the site's
    ``probe_kind``.  Network failures are treated as "not logged in yet" so
    the caller can keep polling.

    :param config: Site login configuration.
    :param cookies: Cookie name/value pairs collected from the window.
    :param timeout: Per-request timeout in seconds.
    :return: True when the site reports a logged-in session.
    """
    if not cookies:
        return False
    if not config.has_required_cookies(cookies):
        return False
    if not config.needs_probe:
        return True

    probe_url = config.probe_url
    if probe_url is None:  # pragma: no cover - guarded by needs_probe
        return True

    header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    request = urllib.request.Request(
        probe_url,
        headers={
            "Cookie": header,
            "User-Agent": _USER_AGENT,
            "Referer": config.start_url,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            payload = response.read()
    except Exception:
        # Connection errors, TLS errors, HTTP errors and timeouts all mean
        # "not verified yet"; the poll loop retries until its deadline.
        logger.debug("login probe failed for %s", config.site, exc_info=True)
        return False

    return _decode_probe(config, payload)


def _cookie_pairs(item: Any) -> dict[str, str]:
    """
    Extract ``name -> value`` pairs from one pywebview cookie item.

    pywebview backends disagree on the representation: the Windows
    EdgeChromium backend returns ``dict`` items with ``name``/``value`` keys,
    while other backends return ``http.cookies.SimpleCookie`` instances.
    """
    if isinstance(item, Mapping) and not isinstance(item, BaseCookie):
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and name:
            return {name: "" if value is None else str(value)}
        return {}

    try:
        entries = item.items()
    except (AttributeError, TypeError):
        return {}

    result: dict[str, str] = {}
    for name, morsel in entries:
        if not isinstance(name, str) or not name:
            continue
        value = getattr(morsel, "value", morsel)
        result[name] = "" if value is None else str(value)
    return result


def _collect_cookies(window: Any) -> dict[str, str]:
    """
    Flatten a pywebview window's cookies into a plain, validated dictionary.

    Both backend representations are accepted (list of dicts or list of
    ``SimpleCookie`` objects).  Invalid names and control characters are
    removed by :func:`novel_downloader.infra.cookies.parse_cookies`.
    """
    try:
        raw = window.get_cookies()
    except Exception:
        logger.debug("could not read cookies from the login window", exc_info=True)
        return {}

    if raw is None:
        return {}
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]

    jar: dict[str, str] = {}
    for item in items:
        jar.update(_cookie_pairs(item))
    return parse_cookies(jar)


def _current_url(window: Any) -> str:
    """Return the window's current URL, or ``""`` when unavailable."""
    getter = getattr(window, "get_current_url", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        logger.debug("could not read the login window URL", exc_info=True)
        return ""


def _window_event(window: Any, name: str) -> Any:
    """Return ``window.events.<name>`` or ``None`` when unavailable."""
    events = getattr(window, "events", None)
    return getattr(events, name, None)


def _remaining(deadline: float, *, now: float | None = None) -> float:
    """Return the non-negative seconds left until *deadline*."""
    current = time.monotonic() if now is None else now
    return max(0.0, deadline - current)


def _poll_delay(
    deadline: float, interval: float, *, now: float | None = None
) -> float | None:
    """
    Return how long the next poll may sleep.

    The result never overshoots the deadline, and ``None`` means the deadline
    has passed (the caller should report a timeout).

    :param deadline: ``time.monotonic()`` value of the login deadline.
    :param interval: Desired poll interval in seconds.
    :param now: Current monotonic time; defaults to ``time.monotonic()``.
    :return: Sleep duration in seconds, or ``None`` when expired.
    """
    remaining = _remaining(deadline, now=now)
    if remaining <= 0:
        return None
    return min(max(interval, 0.0), remaining)


WaitReason = Literal["event", "closed", "timeout"]


def _wait_for_event(
    event: Any,
    closed: Any,
    timeout: float,
    *,
    slice_interval: float = _WAIT_SLICE,
) -> WaitReason:
    """
    Wait for *event*, the window closing, or the timeout.

    The wait is sliced so closing the window is noticed even when the GUI
    backend blocks inside ``Event.wait`` for the full duration.

    :param event: Event to wait for (may be ``None``).
    :param closed: Window-closed event (may be ``None``).
    :param timeout: Maximum seconds to wait.
    :param slice_interval: Maximum length of a single blocking wait.
    :return: ``"event"``, ``"closed"`` or ``"timeout"``.
    """
    if event is None:
        return "timeout"

    deadline = time.monotonic() + max(0.0, timeout)
    is_set = getattr(event, "is_set", None)
    wait = getattr(event, "wait", None)

    while True:
        if callable(is_set) and is_set():
            return "event"
        if closed is not None:
            closed_set = getattr(closed, "is_set", None)
            if callable(closed_set) and closed_set():
                return "closed"

        remaining = _remaining(deadline)
        if remaining <= 0:
            return "timeout"

        pause = min(max(slice_interval, 0.0), remaining)
        if not callable(wait):
            time.sleep(pause)
            continue
        try:
            if wait(pause):
                return "event"
        except TypeError:
            # Some event objects do not accept a timeout argument.
            time.sleep(pause)


def _session_ready(
    config: SiteLoginConfig,
    cookies: Mapping[str, str],
    window: Any,
    *,
    timeout: float,
) -> bool:
    """
    Decide whether the current cookies mean the user has finished logging in.

    Probe-backed sites always ask the endpoint, because a cookie may be set
    before authentication actually completes.  Cookie-only sites additionally
    use the URL keywords from their configuration.
    """
    if not cookies:
        return False

    if config.needs_probe:
        return is_logged_in(config, cookies, timeout=timeout)

    if not config.has_required_cookies(cookies):
        return False

    url = _current_url(window)
    if url and config.is_login_url(url):
        return False
    if config.success_url_keywords:
        return bool(url) and config.is_success_url(url)
    return True


def _watch_window(
    config: SiteLoginConfig,
    window: Any,
    *,
    timeout: float,
    poll_interval: float,
    emit: Callable[[str], None],
    state: dict[str, Any],
) -> None:
    """
    Drive the login window: wait for the page, then poll cookies until done.

    This runs inside ``webview.start`` (i.e. on the GUI thread).  It never
    raises: every failure is recorded in *state* so the caller can map it to
    an exit code.
    """
    closed = _window_event(window, "closed")
    loaded = _window_event(window, "loaded")
    deadline = time.monotonic() + max(0.0, timeout)

    reason = _wait_for_event(loaded, closed, max(0.0, timeout))
    if reason == "closed":
        state["status"] = LoginStatus.CANCELLED
        return
    if reason == "timeout":
        state["status"] = LoginStatus.TIMEOUT
        return

    _emit(emit, t("Please complete the login in the opened window."))

    interval = max(_MIN_POLL_INTERVAL, float(poll_interval))
    while True:
        if closed is not None:
            closed_set = getattr(closed, "is_set", None)
            if callable(closed_set) and closed_set():
                state["status"] = LoginStatus.CANCELLED
                return

        remaining = _remaining(deadline)
        if remaining <= 0:
            state["status"] = LoginStatus.TIMEOUT
            return

        cookies = config.filter_cookies(_collect_cookies(window))
        if _session_ready(
            config,
            cookies,
            window,
            timeout=min(PROBE_TIMEOUT, remaining),
        ):
            try:
                path = save_cookies(config.site, cookies)
            except Exception as exc:
                logger.exception("failed to save login cookies")
                state["status"] = LoginStatus.SAVE_ERROR
                state["error"] = str(exc)
                _emit(
                    emit,
                    _with_detail(t("Login failed."), exc),
                )
                return
            state["status"] = LoginStatus.SUCCESS
            state["path"] = path
            _emit(
                emit,
                t("Login detected; session cookies saved to {path}").format(path=path),
            )
            return

        delay = _poll_delay(deadline, interval)
        if delay is None:
            state["status"] = LoginStatus.TIMEOUT
            return
        if closed is not None:
            closed_wait = getattr(closed, "wait", None)
            if callable(closed_wait):
                try:
                    if closed_wait(delay):
                        state["status"] = LoginStatus.CANCELLED
                        return
                    continue
                except TypeError:
                    pass
        # No usable close event: sleep in deadline-bounded slices.
        time.sleep(delay)


def run_login_result(
    site: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    notify: Callable[[str], None] | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> LoginOutcome:
    """
    Open a login window for ``site`` and save its session cookies on success.

    Must be called from the main thread (pywebview requirement).

    :param site: Site key (see :data:`LOGIN_SITES`).
    :param timeout: Seconds to wait for the user to sign in.
    :param notify: Sink for progress messages; stderr by default.
    :param poll_interval: Seconds between cookie polls.
    :return: A :class:`LoginOutcome` describing the result.
    """
    emit = notify or _default_notify
    config = _site_config(site)
    if config is None:
        _emit(
            emit,
            t("Site '{site}' does not support an interactive login.").format(site=site),
        )
        return LoginOutcome(LoginStatus.UNSUPPORTED_SITE, normalize_site(site))

    try:
        import webview
    except ImportError:
        logger.error("pywebview is not installed")
        _emit(
            emit,
            t(
                "pywebview is required for the login window. "
                "Reinstall the package: pip install -U novel-downloader"
            ),
        )
        return LoginOutcome(
            LoginStatus.WEBVIEW_UNAVAILABLE,
            config.site,
            error="pywebview is not installed",
        )

    _emit(emit, t("Opening a login window for {site}...").format(site=config.site))
    try:
        window = webview.create_window(
            title=t("Log in to {site}").format(site=config.site),
            url=config.start_url,
            width=1000,
            height=760,
        )
    except Exception as exc:
        logger.exception("failed to create the login window")
        _emit(
            emit,
            _with_detail(t("Could not open the login window."), exc),
        )
        return LoginOutcome(LoginStatus.WINDOW_ERROR, config.site, error=str(exc))
    if window is None:
        logger.error("Failed to create the login window")
        _emit(emit, t("Could not open the login window."))
        return LoginOutcome(
            LoginStatus.WINDOW_ERROR,
            config.site,
            error="create_window returned None",
        )

    state: dict[str, Any] = {"status": None, "path": None, "error": None}

    def _watch() -> None:
        try:
            _watch_window(
                config,
                window,
                timeout=max(0.0, timeout),
                poll_interval=poll_interval,
                emit=emit,
                state=state,
            )
        except Exception as exc:  # never let an unexpected error kill the GUI loop
            logger.exception("login watcher failed")
            state["status"] = LoginStatus.WINDOW_ERROR
            state["error"] = str(exc)
            _emit(
                emit,
                _with_detail(t("Login failed."), exc),
            )
        finally:
            with contextlib.suppress(Exception):
                window.destroy()

    start_error: str | None = None
    try:
        webview.start(_watch)
    except Exception as exc:
        logger.exception("failed to start the login window")
        start_error = str(exc)

    status = state.get("status")
    if status is LoginStatus.SUCCESS:
        return LoginOutcome(
            LoginStatus.SUCCESS,
            config.site,
            path=state.get("path"),
        )
    if status is None and start_error is not None:
        _emit(
            emit,
            _with_detail(t("Could not open the login window."), start_error),
        )
        return LoginOutcome(LoginStatus.WINDOW_ERROR, config.site, error=start_error)

    if isinstance(status, LoginStatus):
        if status is LoginStatus.TIMEOUT:
            _emit(
                emit,
                t("Login was not completed within {timeout} seconds.").format(
                    timeout=int(timeout)
                ),
            )
        elif status is LoginStatus.CANCELLED:
            _emit(emit, t("Login window closed before a session was detected."))
        return LoginOutcome(
            status,
            config.site,
            error=state.get("error"),
        )

    # ``webview.start`` returned without the watcher recording a result.
    _emit(emit, t("Login was not completed; no cookies were saved."))
    return LoginOutcome(
        LoginStatus.WINDOW_ERROR,
        config.site,
        error=start_error or "login window produced no result",
    )


def run_login(
    site: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    notify: Callable[[str], None] | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> bool:
    """
    Open a login window for ``site`` and save its session cookies on success.

    Backwards-compatible boolean wrapper around :func:`run_login_result`.

    :param site: Site key (see :data:`LOGIN_SITES`).
    :param timeout: Seconds to wait for the user to sign in.
    :param notify: Sink for progress messages; stderr by default.
    :param poll_interval: Seconds between cookie polls.
    :return: True when a session was detected and saved, otherwise False.
    """
    return run_login_result(
        site,
        timeout=timeout,
        notify=notify,
        poll_interval=poll_interval,
    ).ok


def login_main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point (used by the web/desktop UI as a subprocess).

    :param argv: Argument list; ``sys.argv[1:]`` when omitted.
    :return: Process exit code (see :func:`exit_code_for`).
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
        default=DEFAULT_TIMEOUT,
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
        return EXIT_LOGIN_SUCCESS

    if args.timeout <= 0:
        _default_notify(
            _with_detail(t("Invalid input."), "--timeout must be greater than zero")
        )
        return EXIT_LOGIN_USAGE

    outcome = run_login_result(args.site, timeout=float(args.timeout))
    return exit_code_for(outcome.status)


if __name__ == "__main__":
    raise SystemExit(login_main())
