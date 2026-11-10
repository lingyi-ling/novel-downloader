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
  "network down".
* :class:`RedirectLimitError` — the redirect chain exceeded the session limit.
  Like a deterministic 4xx, the same request would follow the same chain again,
  so neither the fetcher nor a chapter-level retry loop may spend attempts on
  it.
"""

from __future__ import annotations

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


class HttpStatusError(ConnectionError):
    """A non-2xx response that survived (or skipped) the retry budget."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"Request to {url} failed with status {status}")
        self.url = url
        self.status = status


class TransportError(ConnectionError):
    """A transport failure after the fetcher's retry budget was spent."""

    def __init__(self, url: str, reason: BaseException) -> None:
        super().__init__(f"Request to {url} failed: {reason}")
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
        message = f"Too many redirects for {url}"
        if reason is not None:
            message = f"{message}: {reason}"
        super().__init__(message)
        self.url = url
        self.reason = reason


def is_transient_error(exc: BaseException) -> bool:
    """
    Whether ``exc`` is worth retrying at a higher level.

    :param exc: The exception a request raised.
    :return: True for timeouts, connection problems and retryable statuses.
    """
    if isinstance(exc, HttpStatusError):
        return exc.status in RETRYABLE_STATUS
    return isinstance(exc, (TimeoutError, ConnectionError))
