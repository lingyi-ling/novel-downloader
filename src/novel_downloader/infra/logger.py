#!/usr/bin/env python3
"""
novel_downloader.infra.logger
-----------------------------

Provides a configurable logging setup for Python applications.

Two hardening rules are central here:

* every record that reaches a handler is passed through :func:`redact` first,
  so credentials embedded in URLs, headers, query parameters, ``extra`` values
  and tracebacks never reach the console or the log file;
* the file handler degrades instead of aborting the application when the log
  directory is unusable (missing permissions, symlink, disk full), keeps the
  POSIX file mode at ``0600`` and serializes writes across threads and
  processes.
"""

from __future__ import annotations

__all__ = [
    "setup_logging",
    "install_root_redaction",
    "redact",
    "RedactingFilter",
    "RedactingFormatter",
    "FILE_LOG_FORMAT",
    "CONSOLE_LOG_FORMAT",
]

import importlib
import logging
import os
import re
import sys
import time
import traceback
from contextlib import suppress
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, cast

from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import PACKAGE_NAME

#: Canonical file-record format shared by every entry point that adopts this
#: module. Kept as a module constant so the CLI/API/desktop front ends can
#: converge on one format instead of re-declaring their own.
FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s: %(message)s"

#: Canonical console-record format (short timestamp, no function name).
CONSOLE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_MUTE_LOGGERS: set[str] = {
    "fontTools.ttLib.tables._p_o_s_t",
}

_REDACTED = "***"

# ``scheme://user:password@host`` -> ``scheme://***@host``.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@'\"]+@")

# ``Authorization: Basic ...`` / ``Authorization=Bearer ...``.  The
# lookbehind keeps ``?authorization=...`` (a query parameter) for the query
# pattern below, which stops at ``&`` instead of eating the next parameter.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?<![\w.\-?&])\b(authorization|proxy-authorization)\b\s*[:=]\s*"
    r"(?:(?:basic|bearer|digest|token|negotiate|ntlm)\s+)?[^\s&]+"
)

# ``X-Api-Key: ...`` and friends (an API key is not always sent as Bearer).
_API_KEY_HEADER_RE = re.compile(
    r"(?i)(?<![\w.\-?&])\b(x[_-]?api[_-]?key|api[_-]?key|x[_-]?auth[_-]?token)\b"
    r"\s*[:=]\s*[^\s&]+"
)

# ``Bearer <token>`` that is not preceded by a header name.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]+=*")

# ``Cookie: a=b; c=d`` / ``Set-Cookie: ...``.  The repeated group consumes
# further ``name=value`` pairs but stops before an unrelated header, so one
# header never swallows the next one.
_COOKIE_HEADER_RE = re.compile(
    r"(?i)(?<![\w.\-?&])\b(cookie|set-cookie)\b\s*[:=]\s*[^\r\n;&]+"
    r"(?:;\s*[^;\r\n=:]+?\s*=\s*[^\r\n;&]*)*"
)

# Token-like query parameters.  Longer names come first so ``access_token``
# is matched as a whole before the ``token`` alternative can reach it.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|auth_token|"
    r"accesstoken|refreshtoken|idtoken|authtoken|api_?token|csrftoken|"
    r"xsrf_?token|api_?key|apikey|token|key|signature|sign|sig|password|"
    r"passwd|pwd|secret|sessionid|session_id|session|cookie|authorization|"
    r"auth|ticket|credential|credentials)=)[^&\s'\"]+"
)

# Exact credential key names.  Matching whole names (rather than any word
# containing "token" or "auth") keeps a logger such as
# ``novel_downloader.infra.sessions.base`` from redacting its own message.
_SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "client_secret",
    "clientsecret",
    "api_token",
    "api_key",
    "apikey",
    "api-key",
    "csrf_token",
    "csrftoken",
    "xsrf_token",
    "xsrf",
    "signature",
    "sessionid",
    "session_id",
    "authorization",
    "credentials",
    "credential",
    "password",
    "passwd",
    "ticket",
    "secret",
    "session",
    "cookie",
    "sign",
    "token",
    "auth",
    "pass",
    "key",
    "sig",
    "pwd",
)

_KEY_ALTERNATION = "|".join(
    re.escape(key) for key in sorted(_SECRET_KEYS, key=len, reverse=True)
)

# ``password=x``, ``"password": "x"``, ``{'token': 'x'}``, ``pwd = 'x'``.
# The optional ``prefix_`` part accepts ``user_token`` / ``x-api-key`` while
# the boundaries refuse to start or end inside a dotted logger name.
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![\w.\-])"
    r"(?P<name>[\"']?(?:[A-Za-z0-9]+[_-])?(?:" + _KEY_ALTERNATION + r")[\"']?)"
    r"(?![\w.\-])"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&)\]}\"']+)"
)

_VALID_WHEN = frozenset({"S", "M", "H", "D", "MIDNIGHT"}) | {
    f"W{day}" for day in range(7)
}

# Captured at import time so the subclasses below are always built on the
# stdlib implementations, while ``setup_logging`` can still honour a
# monkeypatched module attribute (the existing tests rely on that).
_DEFAULT_TIMED_HANDLER = TimedRotatingFileHandler
_DEFAULT_ROTATING_HANDLER = RotatingFileHandler


def _normalize_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        # Level names may arrive padded (``general.debug.log_level`` values
        # are used verbatim from the config file).
        return logging._nameToLevel.get(level.strip().upper(), logging.INFO)
    return logging.INFO


def _normalize_when(when: object) -> str:
    """Return a rotation period accepted by ``TimedRotatingFileHandler``."""
    if not isinstance(when, str):
        return "MIDNIGHT"
    candidate = when.strip().upper()
    return candidate if candidate in _VALID_WHEN else "MIDNIGHT"


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return default
    return default


def _safe_log_filename(name: object) -> str:
    """
    Reduce *name* to a bare file name inside the log directory.

    Config values are user supplied; a ``../`` segment or an absolute path
    would otherwise let a log filename escape ``log_dir`` (or overwrite an
    arbitrary file).  NUL bytes are dropped because the OS rejects them.
    """
    if name is None:
        return PACKAGE_NAME
    try:
        raw = str(name).replace("\x00", "").strip()
    except Exception:  # pragma: no cover - a hostile __str__ is very unlikely
        return PACKAGE_NAME
    candidate = re.split(r"[\\/]", raw)[-1].strip()
    if candidate in {"", ".", ".."}:
        return PACKAGE_NAME
    return candidate


def redact(text: object) -> str:
    """
    Replace credentials in *text* with ``***``.

    The function is deliberately conservative and never raises: it is called
    from logging formatters, where an exception would turn a redaction bug
    into a logging failure.  It masks:

    * URL userinfo (``scheme://user:password@host``),
    * ``Authorization`` / ``Proxy-Authorization`` / ``Cookie`` /
      ``Set-Cookie`` / API-key headers,
    * token-like query parameters (``token``, ``key``, ``sign``, ``password``,
      ``cookie``, ``session``, ...),
    * credential-like ``name=value`` / ``"name": "value"`` pairs, which also
      covers mapping reprs and ``extra`` payloads.

    :param text: Arbitrary value; non-strings are converted with ``str()``.
    :return: The redacted text, or a fixed placeholder when *text* cannot be
        converted safely.
    """
    try:
        value = text if isinstance(text, str) else str(text)
    except Exception:
        return "<unprintable>"

    try:
        value = _URL_USERINFO_RE.sub(rf"\1{_REDACTED}@", value)
        # Cookies first: the Cookie pattern consumes every ``name=value`` pair.
        value = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", value)
        value = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", value)
        value = _API_KEY_HEADER_RE.sub(lambda m: f"{m.group(1)}: {_REDACTED}", value)
        value = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", value)
        value = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", value)
        return _SECRET_ASSIGN_RE.sub(_redact_assignment, value)
    except Exception:  # pragma: no cover - regexes are static and tested
        return _REDACTED


def _redact_assignment(match: re.Match[str]) -> str:
    name = match.group("name")
    sep = match.group("sep")
    value = match.group("value")
    quote = value[0] if value[:1] in {"'", '"'} else ""
    return f"{name}{sep}{quote}{_REDACTED}{quote}"


class RedactingFormatter(logging.Formatter):
    """
    A :class:`logging.Formatter` that redacts the message and traceback.

    ``formatException`` / ``formatStack`` are overridden as well: the base
    class caches their output in ``record.exc_text``, which another handler
    could reuse later.  Only the formatted text is produced here; Python's
    standard traceback formatter does not render local variables, so a secret
    held in a local (but absent from the exception message) cannot leak.
    """

    def formatException(self, ei: Any) -> str:
        return redact(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return redact(super().formatStack(stack_info))

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _sanitize_exception(record: logging.LogRecord) -> None:
    """
    Fold a traceback that contains a secret into the redacted message.

    A traceback is rendered from ``exc_info``, not from the message, and some
    renderers (Rich) draw it themselves without calling
    :meth:`RedactingFormatter.formatException` -- and include source lines,
    which a plain formatter redacts but Rich owns.  When redaction changes the
    rendered traceback, ``exc_info`` is cleared and the sanitized text is
    appended to the message instead; every renderer then prints the redacted
    version.  Tracebacks without a secret are left untouched, so the normal
    (Rich) rendering keeps its exact shape.
    """
    exc_info = record.exc_info
    if exc_info is None:
        return
    try:
        rendered = "".join(traceback.format_exception(*exc_info))
    except Exception:  # pragma: no cover - defensive, malformed exc_info
        rendered = ""
    cleaned = redact(rendered)
    if cleaned == rendered:
        return

    record.exc_info = None
    record.exc_text = None
    message = str(record.msg)
    record.msg = f"{message}\n{cleaned}" if message else cleaned
    record.args = ()


class RedactingFilter(logging.Filter):
    """
    Redact a record in place before the handler that owns this filter sees it.

    Formatters redact their own output, which is enough for the standard
    handlers.  Some handlers render the record themselves instead: Rich draws
    a traceback straight from ``record.exc_info`` and a third-party handler may
    use its own format string.  Rewriting ``msg``/``args`` (and folding a
    secret-bearing traceback into the message) means every renderer
    downstream only ever sees sanitized text.  The filter never drops a
    record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, malformed args
            message = "<unprintable record>"
        record.msg = redact(message)
        # Keep the resolved value: the original args may hold the secret and
        # a later ``getMessage`` must not reintroduce it.
        record.args = ()
        # ``formatException`` caches into exc_text; another handler may reuse
        # that cache after a different formatter has seen the record.
        record.exc_text = None
        _sanitize_exception(record)
        if record.stack_info is not None:
            record.stack_info = redact(record.stack_info)
        return True


def _safe_diagnostic(message: str) -> None:
    """Write a setup diagnostic to stderr without ever raising."""
    with suppress(OSError, ValueError):
        print(message, file=sys.stderr)


def _open_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return flags


def _harden_fd(fd: int) -> None:
    """Best-effort ``0600`` on POSIX; Windows has no equivalent mode."""
    if os.name != "posix":
        return
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:  # pragma: no cover - every POSIX build has fchmod
        return
    with suppress(OSError):
        fchmod(fd, 0o600)


def _create_log_file(path: Path) -> None:
    """
    Create (or touch) *path* with owner-only permissions.

    ``O_NOFOLLOW`` leaves a symlink alone on POSIX; the explicit ``islink``
    check covers Windows and gives a deterministic error there.  The handler
    opens the file again in append mode, so pre-creating it only validates the
    directory and fixes the mode before the first record arrives.
    """
    if os.path.islink(path):
        raise OSError(f"refusing to write through a symlink: {path}")
    fd = os.open(str(path), _open_flags(), 0o600)
    try:
        _harden_fd(fd)
    finally:
        os.close(fd)


class _SecureFileMixin:
    """Open the log (and every rotated file) append-only with mode ``0600``."""

    baseFilename: str
    mode: str
    encoding: str | None
    errors: str | None

    def _open(self) -> TextIOWrapper:
        if os.path.islink(self.baseFilename):
            raise OSError(f"refusing to write through a symlink: {self.baseFilename}")
        fd = os.open(self.baseFilename, _open_flags(), 0o600)
        try:
            _harden_fd(fd)
            stream = os.fdopen(
                fd,
                self.mode or "a",
                encoding=self.encoding,
                errors=self.errors or "backslashreplace",
            )
            return cast(TextIOWrapper, stream)
        except BaseException:
            os.close(fd)
            raise


class _SafeErrorMixin:
    """Report handler failures without replaying the record (or its secrets)."""

    def handleError(self, record: logging.LogRecord) -> None:
        if not logging.raiseExceptions:
            return
        try:
            exc = sys.exc_info()[1]
            detail = (
                redact(f"{type(exc).__name__}: {exc}")
                if exc is not None
                else "unknown logging error"
            )
            sys.stderr.write(f"--- Logging error ---\n{detail}\n")
        except Exception:
            pass


class _InterProcessLock:
    """
    Small advisory lock used to serialize writes and rotation across processes.

    A sidecar file is locked with ``fcntl.flock`` (POSIX) or
    ``msvcrt.locking`` (Windows); both are released by the OS if a process
    dies.  Locking is best effort: when it cannot be acquired within
    :data:`_LOCK_TIMEOUT` the record is still emitted, because losing a log
    line is preferable to blocking the downloader forever.
    """

    _LOCK_TIMEOUT = 10.0
    _RETRY_INTERVAL = 0.005

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if os.path.islink(self._path):
            return
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(str(self._path), flags, 0o600)
        except OSError:
            return
        try:
            _harden_fd(fd)
            if os.name == "nt":
                self._acquire_windows(fd)
            else:
                self._acquire_posix(fd)
        except (ImportError, OSError):
            os.close(fd)
            return
        self._fd = fd

    def _acquire_windows(self, fd: int) -> None:
        msvcrt = importlib.import_module("msvcrt")
        with suppress(OSError):
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
        deadline = time.monotonic() + self._LOCK_TIMEOUT
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self._RETRY_INTERVAL)

    def _acquire_posix(self, fd: int) -> None:
        fcntl = importlib.import_module("fcntl")
        deadline = time.monotonic() + self._LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self._RETRY_INTERVAL)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                with suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with suppress(OSError):
                os.close(fd)


class _LockingMixin:
    """Serialize ``emit`` (including rollover) with the other processes."""

    baseFilename: str
    _ipc_lock: _InterProcessLock | None = None

    def _interprocess_lock(self) -> _InterProcessLock:
        lock = self._ipc_lock
        if lock is None:
            name = Path(self.baseFilename).name
            lock = _InterProcessLock(Path(self.baseFilename).with_name(f".{name}.lock"))
            self._ipc_lock = lock
        return lock

    def emit(self, record: logging.LogRecord) -> None:
        lock = self._interprocess_lock()
        lock.acquire()
        try:
            super().emit(record)  # type: ignore[misc]  # mixin has no base
        finally:
            lock.release()


class _SecureTimedRotatingFileHandler(
    _SafeErrorMixin,
    _LockingMixin,
    _SecureFileMixin,
    TimedRotatingFileHandler,
):
    """Time-rotating handler with redaction, ``0600`` and a process lock."""


class _SecureRotatingFileHandler(
    _SafeErrorMixin,
    _LockingMixin,
    _SecureFileMixin,
    RotatingFileHandler,
):
    """Size-rotating handler with redaction, ``0600`` and a process lock."""


class _SecureStreamHandler(_SafeErrorMixin, logging.StreamHandler[Any]):
    """Console handler that does not echo the failing record to stderr."""


#: Marks the fallback handler owned by :func:`install_root_redaction`.
_ROOT_FALLBACK_ATTR = "_novel_downloader_root_fallback"


def install_root_redaction(
    *,
    level: int | str = "WARNING",
    fallback: bool = True,
) -> logging.Handler | None:
    """
    Make every record that propagates to the root logger redacted.

    Library loggers (``aiohttp``, ``httpx``, ``urllib3``, the vendored
    ``musicdl`` and anything else) are not children of the package logger, so
    the handlers :func:`setup_logging` installs never see them.  They reach the
    root logger instead, which is why the hardening happens there:

    * every existing root handler gets a :class:`RedactingFilter`, so even a
      handler installed by a third-party ``basicConfig`` call (its own format,
      its own file) writes sanitized text;
    * when the root logger has no handler at all and *fallback* is true, a
      message-only handler is added.  It mirrors the standard ``lastResort``
      handler (stderr, ``WARNING`` and above) but uses
      :class:`RedactingFormatter`, so the warnings that used to leak through
      the bare last-resort path stay visible and become redacted.

    The root level is never changed and no handler is ever silenced.  Calling
    the function repeatedly adds no duplicate filter or handler.

    :param level: Minimum level of the fallback handler (ignored when root
        already has a handler).
    :param fallback: Install the fallback console handler when root is empty.
    :return: The fallback handler when this call created it, else ``None``.
    """
    root = logging.getLogger()
    redacting_filter = RedactingFilter()
    for root_handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in root_handler.filters):
            root_handler.addFilter(redacting_filter)

    if root.handlers or not fallback:
        return None

    handler: logging.Handler = _SecureStreamHandler()
    handler.setLevel(_normalize_level(level))
    handler.setFormatter(RedactingFormatter(fmt="%(message)s"))
    handler.addFilter(redacting_filter)
    setattr(handler, _ROOT_FALLBACK_ATTR, True)
    root.addHandler(handler)
    return handler


def _build_file_handler(
    log_dir: str | Path,
    log_filename: str | None,
    file_level: int,
    backup_count: int,
    when: str,
    max_bytes: int,
) -> tuple[logging.Handler, Path] | None:
    """
    Create the file handler, or return ``None`` when the destination is unusable.

    ``setup_logging`` must never crash the application because logging is
    impossible (a read-only config volume, a symlinked file, a full disk);
    the caller then falls back to the console handler.
    """
    try:
        base_dir = Path(str(log_dir).strip()).expanduser()
        base_dir.mkdir(parents=True, exist_ok=True)
        log_path = base_dir / f"{_safe_log_filename(log_filename)}.log"
    except (OSError, ValueError, TypeError):
        return None

    try:
        if max_bytes > 0:
            if RotatingFileHandler is _DEFAULT_ROTATING_HANDLER:
                _create_log_file(log_path)
                handler: logging.Handler = _SecureRotatingFileHandler(
                    filename=log_path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                    errors="backslashreplace",
                    delay=True,
                )
            else:
                # Monkeypatched handler (tests supply their own factory).
                handler = RotatingFileHandler(
                    filename=log_path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                    delay=True,
                )
        elif TimedRotatingFileHandler is _DEFAULT_TIMED_HANDLER:
            _create_log_file(log_path)
            handler = _SecureTimedRotatingFileHandler(
                filename=log_path,
                when=when,
                interval=1,
                backupCount=backup_count,
                encoding="utf-8",
                errors="backslashreplace",
                utc=False,
                delay=True,
            )
        else:
            handler = TimedRotatingFileHandler(
                filename=log_path,
                when=when,
                interval=1,
                backupCount=backup_count,
                encoding="utf-8",
                utc=False,
                delay=True,
            )
    except (OSError, ValueError, TypeError):
        return None

    handler.setFormatter(
        RedactingFormatter(fmt=FILE_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    )
    handler.setLevel(file_level)
    return handler, log_path


def setup_logging(
    log_dir: str | Path = "./logs",
    log_filename: str | None = None,
    console_level: int | str = "INFO",
    file_level: int | str = "DEBUG",
    *,
    console: bool = True,
    file: bool = True,
    backup_count: int = 7,
    when: str = "midnight",
    max_bytes: int = 0,
) -> logging.Logger:
    """
    Create and configure a package logger with optional console and file handlers.

    :param log_dir: Directory where log files will be saved.
    :param log_filename: Base log file name (without date suffix).  Any
        directory component is stripped, so the file always stays inside
        *log_dir*.
    :param console_level: Minimum level for the console handler (string or int).
    :param file_level: Minimum level for the file handler (string or int).
    :param console: Add a console handler.
    :param file: Add a file handler.
    :param backup_count: How many rotated files to keep (negative values are
        treated as ``0``, i.e. no limit).
    :param when: Rotation interval for the time-based handler (e.g.
        ``"midnight"``).  Unsupported values fall back to daily rotation.
    :param max_bytes: When greater than zero, rotate by size
        (:class:`~logging.handlers.RotatingFileHandler`) instead of by time.
        Size rotation is the recommended mode when several processes write to
        the same file, because it does not depend on a per-process clock.
    :return: The configured logger.
    """
    # Tame noisy third-party loggers
    for name in _MUTE_LOGGERS:
        ml = logging.getLogger(name)
        ml.setLevel(logging.ERROR)
        ml.propagate = False

    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # otherwise may affected by PaddleOCR

    # Replace handlers from a previous call. Close them first: a file handler
    # keeps the descriptor (and a lock on Windows) until closed, so merely
    # clearing the list would leak handles on repeated initialization.
    for handler in logger.handlers[:]:
        close_handler = getattr(handler, "close", None)
        if close_handler is not None:
            close_handler()
    logger.handlers.clear()

    backup_count = max(0, _coerce_int(backup_count, 7))
    max_bytes = max(0, _coerce_int(max_bytes, 0))

    # File handler (rotates daily by default, or by size when requested)
    if file:
        file_level = _normalize_level(file_level)
        built = _build_file_handler(
            log_dir=log_dir,
            log_filename=log_filename,
            file_level=file_level,
            backup_count=backup_count,
            when=_normalize_when(when),
            max_bytes=max_bytes,
        )
        if built is None:
            # Reuse an existing translated message: no new catalog entry.
            _safe_diagnostic(
                t("Check the permissions of the log directory or change log_dir.")
            )
        else:
            file_handler, log_path = built
            logger.addHandler(file_handler)

            # Diagnostics belong on stderr so stdout stays machine readable
            # (``doctor --json`` / ``storage --json`` pipelines).
            _safe_diagnostic(t("Logging to {path}").format(path=log_path))

    # Console handler
    if console:
        console_level = _normalize_level(console_level)

        console_handler = _SecureStreamHandler()
        console_formatter = RedactingFormatter(
            fmt=CONSOLE_LOG_FORMAT,
            datefmt="%H:%M:%S",
        )
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(console_level)
        logger.addHandler(console_handler)

    return logger
