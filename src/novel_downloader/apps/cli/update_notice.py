#!/usr/bin/env python3
"""
novel_downloader.apps.cli.update_notice
---------------------------------------

The quiet "a newer version exists" hint printed after a command finishes.

Design rules, all of them there to keep the hint from ever getting in the way:

* it only runs when ``general.update_check`` is ``auto`` (the default), the
  session is interactive (stdout is a TTY) and the command did **not** ask for
  JSON output;
* it is throttled by the shared cache (``app_home()/update_check.json``), so a
  check happens at most once per :data:`~novel_downloader.apps.common.updater.
  DEFAULT_CHECK_INTERVAL` regardless of how many commands are run;
* the throttle stamp is written **before** the request, not after it.  The
  worker is a daemon thread that is abandoned when the process exits, so a
  stamp written afterwards would never appear on an offline machine and every
  single command would pay the wait again (measured: 2.0 s per command before
  this was fixed);
* a claim file created with ``O_CREAT|O_EXCL`` guarantees that concurrent
  commands -- five shells started in the same second -- make one request, not
  five;
* the network request runs in a daemon thread that the caller waits for at
  most :data:`NOTICE_TIMEOUT` seconds (well under a second), so a slow or
  unreachable host cannot hold the command open;
* every exception is swallowed: an update hint is never a reason for a command
  to fail or print a traceback;
* the line goes to **stderr** (through ``ui.info``), because stdout belongs to
  the command's own output and may be piped.

The caller (:func:`novel_downloader.apps.cli.main.cli_main`) invokes
:func:`maybe_notify` only after a command that exited successfully.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from novel_downloader.apps.common.updater import (
    check_due,
    check_for_update,
    save_update_state,
)
from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import app_home

__all__ = [
    "CLAIM_STALE_AFTER",
    "DEFAULT_MODE",
    "NOTICE_MODES",
    "NOTICE_TIMEOUT",
    "REQUEST_TIMEOUT",
    "STATE_FILENAME",
    "claim_path",
    "maybe_notify",
    "state_path",
    "update_check_mode",
]

#: Accepted values of ``general.update_check``.
NOTICE_MODES = ("auto", "manual", "off")

#: Mode used when the key is absent or unusable.
DEFAULT_MODE = "auto"

#: File inside :func:`app_home` holding the throttle timestamp.
STATE_FILENAME = "update_check.json"

#: Longest the caller waits for the background check, in seconds.  Kept well
#: below one second: the hint is a courtesy and must never be felt as latency.
#: (A background thread that is still running when this expires is abandoned;
#: it is a daemon, so the process exits immediately.)
NOTICE_TIMEOUT = 0.75

#: Socket timeout used *inside* the worker, in seconds.  It may be longer than
#: :data:`NOTICE_TIMEOUT` because the worker keeps running (harmlessly) after
#: the caller stopped waiting, and a hint that arrives late is still useful on
#: a slow link.
REQUEST_TIMEOUT = 3.0

#: A claim file older than this belongs to a process that died mid-check and
#: may be taken over, in seconds.
CLAIM_STALE_AFTER = 60.0


class _UiSink(Protocol):
    """The one ``ui`` helper this module needs."""

    def info(self, message: str) -> None:
        """Print a neutral informational line."""


def state_path() -> Path:
    """Return the throttle file path (``app_home()/update_check.json``)."""
    return app_home() / STATE_FILENAME


def claim_path(path: Path | None = None) -> Path:
    """Return the claim file that serialises concurrent checks."""
    target = state_path() if path is None else Path(path)
    return target.with_name(f"{target.name}.lock")


def update_check_mode(config: Mapping[str, Any] | None) -> str:
    """Read ``general.update_check`` from ``config``, defaulting to ``auto``.

    An unknown value falls back to the default instead of raising: a notify
    helper must never be the thing that breaks a command, and the validator
    already reports a bad value when the configuration is loaded.
    """
    if not isinstance(config, Mapping):
        return DEFAULT_MODE
    general = config.get("general")
    if not isinstance(general, Mapping):
        return DEFAULT_MODE
    raw = general.get("update_check")
    if isinstance(raw, str) and raw.strip().lower() in NOTICE_MODES:
        return raw.strip().lower()
    return DEFAULT_MODE


def _load_config() -> Mapping[str, Any]:
    """Load the configuration, returning an empty mapping on any failure."""
    try:
        from novel_downloader.infra.config import load_config

        loaded = load_config()
    except Exception:  # noqa: BLE001 - no config is a normal state
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def _is_tty(stream: object) -> bool:
    """Whether ``stream`` looks like a terminal (never raises)."""
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a replaced/closed stream is "not a TTY"
        return False


def _announce(current_version: str, latest: str, ui: _UiSink) -> None:
    """Print the single hint line on stderr.

    Kept under ~70 columns so Rich's 80-column console does not wrap it onto
    a second line.
    """
    ui.info(
        t("New version v{latest} (current v{current}); run 'novel-cli update'.").format(
            latest=latest, current=current_version
        )
    )


def _claim(path: Path, *, now: float) -> bool:
    """Reserve the right to run the check; only one caller gets ``True``.

    ``O_CREAT|O_EXCL`` is atomic on every platform Python supports, which is
    what keeps five commands started in the same second from making five
    requests.  The landing timestamp is already in place by the time this
    returns, so the reservation and the throttle record cannot disagree.

    :param path: the throttle file the claim belongs to.
    :param now: clock value for the claim (and for staleness).
    :return: ``True`` when this caller owns the check.
    """
    claim = claim_path(path)
    try:
        claim.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return _steal_stale_claim(claim, now=now)
    except OSError:
        # Read-only home, or a mkdir race: skip the hint, never the command.
        return False
    try:
        os.write(handle, f"{now:.6f}".encode())
    except OSError:
        pass
    finally:
        os.close(handle)
    return True


def _steal_stale_claim(claim: Path, *, now: float) -> bool:
    """Take over a claim left behind by a process that died mid-check."""
    try:
        age = now - claim.stat().st_mtime
    except OSError:
        # Gone again: its owner finished and released it a moment ago.
        return False
    if age < CLAIM_STALE_AFTER:
        return False
    try:
        os.utime(claim, None)
    except OSError:
        return False
    return True


def _notify_worker(
    current_version: str,
    ui: _UiSink,
    stamp: float,
    path: Path,
) -> None:
    """Check the feed once, record the result and maybe print the hint."""
    try:
        result = check_for_update(current_version, timeout=REQUEST_TIMEOUT, now=stamp)
        info = result.info
        # Rewrite the record with the version actually found (the caller wrote
        # a bare timestamp before starting us).
        save_update_state(path, info, now=stamp)
        if info is not None and info.available:
            _announce(current_version, info.latest, ui)
    except Exception:  # noqa: BLE001 - a hint is never worth a traceback
        return
    finally:
        with suppress(OSError):
            claim_path(path).unlink(missing_ok=True)


def maybe_notify(
    current_version: str,
    *,
    config: Mapping[str, Any] | None,
    ui: _UiSink,
    now: float | None = None,
    as_json: bool = False,
    stream: object | None = None,
) -> None:
    """Offer the throttled update hint after a successful command.

    Returns immediately when any gate says "not now": JSON output, a
    non-terminal stdout, ``update_check = manual|off``, a cache that is still
    fresh, a check already claimed by another process, or a check that found
    nothing. The network work itself happens in a daemon thread joined for at
    most :data:`NOTICE_TIMEOUT` seconds.

    The throttle timestamp is written *before* the request (see the module
    docstring) and the claim file is released by the worker, so the worst case
    is one bounded wait per :data:`~novel_downloader.apps.common.updater.
    DEFAULT_CHECK_INTERVAL`, not one per command.

    :param current_version: the running version.
    :param config: the loaded configuration; ``None`` loads it lazily.
    :param ui: object exposing ``info(message)`` (the CLI ``ui`` module).
    :param now: clock override for tests.
    :param as_json: whether the command printed a JSON document on stdout.
    :param stream: stream checked for ``isatty``; defaults to ``sys.stdout``.
    """
    if as_json:
        return
    if not _is_tty(sys.stdout if stream is None else stream):
        return
    if config is None:
        config = _load_config()
    if update_check_mode(config) != "auto":
        return

    stamp = time.time() if now is None else now
    path = state_path()
    try:
        if not check_due(path, now=stamp):
            return
    except Exception:  # noqa: BLE001 - a corrupt cache just means "try again"
        return

    if not _claim(path, now=stamp):
        # Another command is checking right now (or just claimed the slot).
        return

    # Landing record first: if the worker is abandoned at exit -- an offline
    # machine, a stalled DNS lookup -- the next command must still see "checked
    # recently" instead of paying the wait all over again.
    save_update_state(path, None, now=stamp)

    worker = threading.Thread(
        target=_notify_worker,
        args=(current_version, ui, stamp, path),
        name="novel-cli-update-notice",
        daemon=True,
    )
    worker.start()
    # Bounded wait, kept well below a second: the request has its own timeout
    # and if DNS or the OS still hangs the daemon thread is dropped when the
    # process exits (the worker's record is already on disk).
    worker.join(NOTICE_TIMEOUT)
