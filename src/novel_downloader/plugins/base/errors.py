#!/usr/bin/env python3
"""
novel_downloader.plugins.base.errors
------------------------------------

Typed request failures shared by the fetcher and the download mixin.

``BaseSession`` normalises transport problems to the builtin ``TimeoutError`` /
``ConnectionError``. The fetcher adds typed errors so callers can tell an
*already retried* failure from a fresh one -- and a deterministic failure from a
transient one:

* :class:`TransportError` — the fetcher spent its retry budget on a transport
  problem (timeout, reset, DNS); retrying again one level up would multiply the
  request count for no benefit.
* :class:`HttpStatusError` — the server answered with a status the fetcher
  refused to retry (a deterministic 4xx) or a retryable one whose budget was
  spent. ``status`` lets the caller report ``http_404`` instead of a misleading
  "network down", and ``retry_after`` preserves a server ``Retry-After``.
* :class:`RedirectLimitError` — the redirect chain exceeded the session limit.
  Like a deterministic 4xx, the same request would follow the same chain again,
  so neither the fetcher nor a chapter-level retry loop may spend attempts on
  it.

Every message is translated and scrubbed before it is stored on a task: URL
userinfo, token-like query parameters, header credentials and absolute local
paths are replaced, so ``task.error`` never carries a signed URL or a
machine-specific path.
"""

from __future__ import annotations

import re
from typing import Any

from novel_downloader.infra.i18n import t
from novel_downloader.infra.logger import redact

__all__ = [
    "RETRYABLE_STATUS",
    "HttpStatusError",
    "RedirectLimitError",
    "TransportError",
    "is_transient_error",
]

#: Statuses worth another attempt. Everything else in the 4xx range is
#: deterministic (wrong id, forbidden, not found) and must not be retried.
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Absolute Windows paths (``C:\Users\...``) that a backend error may echo.
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/])[A-Za-z]:[\\/][^\s\"'<>|]+")

#: Absolute POSIX paths below commonly used home/scratch roots.
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/])/(?:home|Users|tmp|var|opt|root|private|data|mnt|media)/"
    r"[^\s\"'<>]+"
)


def _text(value: object) -> str:
    """Best-effort ``str()`` that never lets a hostile ``__str__`` escape."""
    try:
        return str(value)
    except Exception:
        return type(value).__name__


def _scrub(message: str) -> str:
    """
    Remove credentials and local paths from a user-visible error message.

    Backend exceptions routinely echo the request URL (including userinfo and
    token-like query parameters) and sometimes an absolute path from the
    machine that produced them.  Both are stripped before the message is
    stored on a task, shown by an app surface or parsed by a webhook.
    """
    cleaned = redact(message)
    cleaned = _WINDOWS_PATH_RE.sub("<redacted-path>", cleaned)
    return _POSIX_PATH_RE.sub("<redacted-path>", cleaned)


def _format_message(template: str, **fields: Any) -> str:
    """Format an already-translated *template* and scrub the result."""
    return _scrub(template.format(**fields))


class HttpStatusError(ConnectionError):
    """A non-2xx response that survived (or skipped) the retry budget.

    The fetcher already spent its budget on retryable statuses (5xx/429) and
    refuses deterministic ones (4xx) outright, so no higher level may retry
    this error: doing so would multiply the request count by the task-level
    budget.  ``retryable`` states that explicitly, exactly like
    :class:`TransportError`.

    ``retry_after`` carries the bounded delay from a server ``Retry-After``
    header so a caller that reports (rather than retries) can tell the user
    how long the site asked to wait.
    """

    retryable: bool = False

    def __init__(
        self,
        url: str,
        status: int,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            _format_message(
                t("Request to {url} failed with status {status}"),
                url=url,
                status=status,
            )
        )
        self.url = url
        self.status = status
        self.retry_after = retry_after


class TransportError(ConnectionError):
    """A transport failure after the fetcher's retry budget was spent."""

    #: The fetcher already retried; another level would multiply the request
    #: count for the same failure.  Also set for deterministic failures
    #: (certificate/proxy-credential rejections) that were never retried.
    retryable: bool = False

    def __init__(self, url: str, reason: BaseException) -> None:
        super().__init__(
            _format_message(
                t("Request to {url} failed: {reason}"),
                url=url,
                reason=_text(reason),
            )
        )
        self.url = url
        self.reason = reason


class RedirectLimitError(Exception):
    """
    The redirect chain exceeded the session's limit.

    Deliberately *not* a :class:`ConnectionError` (which the retry logic treats
    as transient): the chain is deterministic, so another attempt would only
    repeat it. ``retryable = False`` states that explicitly for the whole-task
    retry policy as well.
    """

    retryable: bool = False

    def __init__(self, url: str, reason: BaseException | None = None) -> None:
        if reason is None:
            message = _format_message(
                t("Too many redirects for {url}"),
                url=url,
            )
        else:
            message = _format_message(
                t("Too many redirects for {url}: {reason}"),
                url=url,
                reason=_text(reason),
            )
        super().__init__(message)
        self.url = url
        self.reason = reason


def is_transient_error(exc: BaseException) -> bool:
    """
    Whether ``exc`` is worth retrying at a higher level.

    :param exc: The exception a request raised.
    :return: True for raw timeouts and connection problems that have *not* been
        through the fetcher yet.  Typed request failures
        (:class:`TransportError`, :class:`HttpStatusError`) return False: the
        fetcher's budget is the one that counts, so a higher-level loop must
        not multiply the request count.  Their transience is still visible
        from ``exc.status in RETRYABLE_STATUS`` for callers that report rather
        than retry.  An explicit ``retryable`` flag on the exception wins for
        every other type, keeping this helper consistent with the task
        manager's policy.
    """
    if isinstance(exc, (TransportError, HttpStatusError)):
        # The fetcher already spent its budget (or refused a deterministic
        # status/TLS/proxy failure); a higher-level retry must not multiply
        # requests.  ``HttpStatusError`` covers both a deterministic 4xx and a
        # 5xx/429 whose retries are already exhausted.
        return False
    declared = getattr(exc, "retryable", None)
    if isinstance(declared, bool):
        # ``NonRetryableConnectionError`` (TLS/proxy credentials) declares
        # False even though it subclasses ``ConnectionError``.
        return declared
    return isinstance(exc, (TimeoutError, ConnectionError))
