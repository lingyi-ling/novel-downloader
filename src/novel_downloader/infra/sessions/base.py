#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.base
------------------------------------
"""

from __future__ import annotations

import types
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self, TypedDict, Unpack

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.schemas import FetcherConfig

from .response import BaseResponse

#: Unified redirect policy for every backend.  aiohttp and curl_cffi follow
#: redirects by default while httpx does not; the session facade exposes one
#: contract instead of leaking that difference to callers.
DEFAULT_ALLOW_REDIRECTS = True

#: Unified redirect ceiling for every backend.  The three libraries default
#: to wildly different limits (aiohttp raises after 10 hops because the
#: limit is hardcoded, httpx allows 20 and curl_cffi 30), so the facade pins
#: httpx and curl_cffi to aiohttp's ceiling.  aiohttp itself allows one hop
#: fewer than the configured limit of the other two; chains long enough to
#: notice are pathological (usually loops) and raise on every backend.
DEFAULT_MAX_REDIRECTS = 10

#: Timeout and connection failures are re-raised as the builtin
#: :class:`TimeoutError` / :class:`ConnectionError`, so callers (and the retry
#: policy) see the same type regardless of the selected backend.


def _error_message(exc: BaseException) -> str:
    """Best-effort text for *exc*; never raises.

    Backend exceptions can have a ``__str__`` that dereferences optional
    constructor arguments (e.g. aiohttp's ``ClientResponseError`` reads
    ``request_info.real_url``), so the translation helpers must not let a
    malformed instance crash the mapping itself.
    """
    try:
        message = str(exc)
    except Exception:
        return type(exc).__name__
    return message or type(exc).__name__


def as_timeout_error(exc: BaseException) -> TimeoutError:
    """
    Translate a backend-specific timeout into the unified builtin type.

    :param exc: Original backend exception.
    :return: A builtin :class:`TimeoutError` carrying the original message.
    """
    return TimeoutError(_error_message(exc))


def as_too_many_redirects_error(exc: BaseException) -> TooManyRedirectsError:
    """
    Translate a backend-specific redirect excess into the unified type.

    :param exc: Original backend exception.
    :return: A :class:`TooManyRedirectsError` carrying the original message.
    """
    return TooManyRedirectsError(_error_message(exc))


def as_connection_error(exc: BaseException) -> ConnectionError:
    """
    Translate a backend-specific transport failure into the unified type.

    :param exc: Original backend exception.
    :return: A builtin :class:`ConnectionError` carrying the original message.
    """
    return ConnectionError(_error_message(exc))


class TooManyRedirectsError(Exception):
    """
    Raised when a request exceeds :data:`DEFAULT_MAX_REDIRECTS`.

    Every backend raises its own exception class once a redirect chain is
    too long; the facade re-raises this unified type instead so callers
    never see a backend-specific class.  It deliberately subclasses neither
    :class:`ConnectionError` nor :class:`TimeoutError`: a redirect loop is
    deterministic, so retrying it would only burn the retry budget.
    """


class BaseRequestKwargs(TypedDict, total=False):
    headers: Mapping[str, str] | Sequence[tuple[str, str]]
    cookies: dict[str, str] | list[tuple[str, str]]


class GetRequestKwargs(BaseRequestKwargs, total=False):
    params: dict[str, Any] | list[tuple[str, Any]] | None


class PostRequestKwargs(BaseRequestKwargs, total=False):
    params: dict[str, Any] | list[tuple[str, Any]] | None
    data: Any
    json: Any


class BaseSession(ABC):
    def __init__(
        self,
        config: FetcherConfig,
        cookies: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the async session with configuration.

        :param config: Configuration object for session behavior
        :param cookies: Optional initial cookies to set on the session.
        """
        self._backoff_factor = config.backoff_factor
        self._request_interval = config.request_interval
        self._retry_times = config.retry_times
        self._timeout = config.timeout
        self._max_connections = config.max_connections
        self._verify_ssl = config.verify_ssl
        self._impersonate = config.impersonate
        self._http2 = config.http2
        self._proxy = config.proxy
        self._proxy_user = config.proxy_user
        self._proxy_pass = config.proxy_pass
        self._trust_env = config.trust_env
        # Copy the mapping: callers commonly reuse a shared cookie dict for
        # several sessions and later mutate it for another account.
        self._cookies = dict(cookies) if cookies else {}
        self._session: Any = None
        # Depth of active ``async with`` blocks.  Helper coroutines that are
        # themselves wrapped in ``async with session:`` are frequently called
        # from code that already holds the context, so only the outermost
        # block may shut the backend down.
        self._context_depth: int = 0

        self._headers = (
            config.headers.copy()
            if config.headers is not None
            else DEFAULT_USER_HEADERS.copy()
        )
        if config.user_agent:
            self._headers["User-Agent"] = config.user_agent

    @abstractmethod
    async def init(
        self,
        **kwargs: Any,
    ) -> None: ...

    @abstractmethod
    async def close(self) -> None:
        """
        Shutdown and clean up any resources.
        """
        ...

    @abstractmethod
    async def get(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[GetRequestKwargs],
    ) -> BaseResponse:
        """
        Create an HTTP GET request context manager.

        :param url: The target URL.
        :param allow_redirects: Whether redirects are followed.  ``None``
            (the default) follows redirects on every backend.
        :param verify: Per-request TLS verification override.  aiohttp and
            curl_cffi honour it; the httpx backend cannot (its client owns
            verification) and raises :class:`RuntimeError` when the value
            conflicts with the session's ``verify_ssl`` setting instead of
            silently ignoring it.
        :param kwargs: Additional args passed to session.get().
        :raises RuntimeError: If the session is not initialized.
        :raises TimeoutError: If the request times out.
        :raises ConnectionError: If the connection cannot be established or
            is interrupted, or the body cannot be decoded.
        :raises TooManyRedirectsError: If the redirect chain exceeds
            :data:`DEFAULT_MAX_REDIRECTS`.
        """
        ...

    @abstractmethod
    async def post(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[PostRequestKwargs],
    ) -> BaseResponse:
        """
        Create an HTTP POST request context manager.

        :param url: The target URL.
        :param allow_redirects: Whether redirects are followed.  ``None``
            (the default) follows redirects on every backend.
        :param verify: Per-request TLS verification override.  aiohttp and
            curl_cffi honour it; the httpx backend cannot (its client owns
            verification) and raises :class:`RuntimeError` when the value
            conflicts with the session's ``verify_ssl`` setting instead of
            silently ignoring it.
        :param kwargs: Additional args passed to session.post().
        :raises RuntimeError: If the session is not initialized.
        :raises TimeoutError: If the request times out.
        :raises ConnectionError: If the connection cannot be established or
            is interrupted, or the body cannot be decoded.
        :raises TooManyRedirectsError: If the redirect chain exceeds
            :data:`DEFAULT_MAX_REDIRECTS`.
        """
        ...

    @abstractmethod
    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        """
        Load cookies from JSON file.

        :return: True if the session state was loaded, False otherwise.
        """
        ...

    @abstractmethod
    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        """
        Save cookies (list of dicts) to JSON file.

        :return: True if the session state was saved, False otherwise.
        """
        ...

    @abstractmethod
    def update_cookies(self, cookies: dict[str, str]) -> None:
        """
        Update or add multiple cookies in the session.

        :param cookies: A dictionary of cookie key-value pairs.
        """
        ...

    @abstractmethod
    def get_cookie(self, key: str) -> str | None:
        """
        Retrieve the value of a cookie by name from the active session.

        :param key: Cookie name.
        :return: The cookie value if found, else None.
        """
        ...

    @abstractmethod
    def clear_cookie(self, name: str) -> None:
        """Remove a single cookie."""
        ...

    @abstractmethod
    def clear_cookies(self) -> None:
        """
        Remove all cookies stored in the session.
        """
        ...

    @property
    def headers(self) -> dict[str, str]:
        """
        Get a copy of the current session headers for temporary use.

        :return: A dict mapping header names to their values.
        """
        return self._headers.copy()

    async def __aenter__(self) -> Self:
        await self.init()
        self._context_depth += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        if self._context_depth:
            self._context_depth -= 1
        if self._context_depth == 0:
            await self.close()
