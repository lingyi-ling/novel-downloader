#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.base
------------------------------------
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
import types
from abc import ABC, abstractmethod
from collections.abc import Coroutine, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Self, TypedDict, Unpack
from urllib.parse import urlsplit

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS
from novel_downloader.schemas import FetcherConfig

from .proxy import (
    ProxyConfig,
    redact_secrets,
    should_bypass_proxy,
    split_proxy_settings,
)
from .response import BaseResponse

logger = logging.getLogger(__name__)

#: Backend shutdowns that outlived the caller that started them.  Kept as a
#: module-level strong reference: an un-awaited task is only weakly held by the
#: event loop, so without this a shielded close could be garbage-collected
#: mid-shutdown and leak the very connections it exists to release.
_PENDING_CLOSES: set[asyncio.Task[None]] = set()


def _on_close_done(task: asyncio.Task[None]) -> None:
    """Forget a finished shielded close and surface its failure."""
    _PENDING_CLOSES.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Error while closing HTTP session backend: %s", exc)


async def close_backend_session(
    close: Coroutine[Any, Any, None],
) -> None:
    """
    Run a backend's ``close``/``aclose`` without losing it to cancellation.

    The facade's ``close`` detaches the backend reference before awaiting so a
    concurrent second caller is a no-op.  Without shielding, cancelling that
    caller (a cancellation propagating through ``async with session`` during
    shutdown is enough) would abort the backend shutdown after the reference
    was already dropped: the connection pool's sockets then stay open until
    garbage collection, with no handle left to retry the close.

    The close itself runs in a task and is shielded from the caller, so the
    caller still observes cancellation immediately while the pool is released
    in the background.  A failing close still propagates to a normal caller;
    its error is logged when the caller was already cancelled.

    :param close: Backend shutdown coroutine, e.g. ``session.close()``.
    """
    task = asyncio.ensure_future(close)
    _PENDING_CLOSES.add(task)
    task.add_done_callback(_on_close_done)
    await asyncio.shield(task)


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


def _error_message(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """Best-effort text for *exc*; never raises.

    Backend exceptions can have a ``__str__`` that dereferences optional
    constructor arguments (e.g. aiohttp's ``ClientResponseError`` reads
    ``request_info.real_url``), so the translation helpers must not let a
    malformed instance crash the mapping itself.

    Proxy credentials are masked here, at the single place every backend
    funnels its messages through: libcurl and httpx copy the proxy URL into
    some error strings, and those strings end up in the fetcher's retry
    logs.
    """
    try:
        message = str(exc)
    except Exception:
        return type(exc).__name__
    return redact_secrets(message, secrets) or type(exc).__name__


def as_timeout_error(
    exc: BaseException, *, secrets: Iterable[str] = ()
) -> TimeoutError:
    """
    Translate a backend-specific timeout into the unified builtin type.

    :param exc: Original backend exception.
    :param secrets: Proxy credentials to mask in the message.
    :return: A builtin :class:`TimeoutError` carrying the original message.
    """
    return TimeoutError(_error_message(exc, secrets))


def as_too_many_redirects_error(
    exc: BaseException, *, secrets: Iterable[str] = ()
) -> TooManyRedirectsError:
    """
    Translate a backend-specific redirect excess into the unified type.

    :param exc: Original backend exception.
    :param secrets: Proxy credentials to mask in the message.
    :return: A :class:`TooManyRedirectsError` carrying the original message.
    """
    return TooManyRedirectsError(_error_message(exc, secrets))


def as_connection_error(
    exc: BaseException, *, secrets: Iterable[str] = ()
) -> ConnectionError:
    """
    Translate a backend-specific transport failure into the unified type.

    :param exc: Original backend exception.
    :param secrets: Proxy credentials to mask in the message.
    :return: A builtin :class:`ConnectionError` carrying the original message.
    """
    return ConnectionError(_error_message(exc, secrets))


class NonRetryableConnectionError(ConnectionError):
    """
    A connection-level failure that another attempt cannot fix.

    TLS certificate verification failures (expired/mismatched/untrusted
    certificate) and proxy authentication refusals (HTTP 407) are
    deterministic: the retry would use the same configuration against the same
    endpoint and fail identically.  Retrying them burns the request budget and
    can look like a hammering client to the proxy.

    It subclasses :class:`ConnectionError` so existing callers that catch
    transport failures keep working, while the fetcher can check the concrete
    type and skip its retry loop.
    """

    #: Duck-typed hint for retry policies that inspect the attribute instead
    #: of the class (see ``apps.core.task_manager.is_retryable_error``).
    retryable: bool = False


def as_non_retryable_connection_error(
    exc: BaseException, *, secrets: Iterable[str] = ()
) -> NonRetryableConnectionError:
    """
    Translate a deterministic transport failure into the unified type.

    :param exc: Original backend exception.
    :param secrets: Proxy credentials to mask in the message.
    :return: A :class:`NonRetryableConnectionError` carrying the message.
    """
    return NonRetryableConnectionError(_error_message(exc, secrets))


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """
    Walk an exception's ``__cause__``/``__context__`` chain.

    Backends wrap the real failure several levels deep (httpx re-raises
    ``httpcore.ConnectError``, which in turn wraps ``ssl.SSLCertVerificationError``),
    so classifying an error often means inspecting what it was raised from.
    Cycles are tolerated: an exception re-raised from itself (or a context
    loop) yields each link once.

    :param exc: Outermost exception.
    :return: Iterator starting with *exc* and following the chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_certificate_verification_error(exc: BaseException) -> bool:
    """
    Whether *exc* (or one of its causes) is a TLS certificate rejection.

    :param exc: Backend exception.
    :return: True for certificate verification failures.
    """
    return any(
        isinstance(link, ssl.SSLCertVerificationError)
        for link in iter_exception_chain(exc)
    )


#: Text a proxy library uses when it refused the supplied credentials.  The
#: status is only exposed as an attribute by aiohttp; httpx and curl_cffi
#: surface it in the message ("407 Proxy Authentication Required",
#: "CONNECT tunnel failed, response 407", "Invalid username/password" for
#: SOCKS proxies).  ``\b407\b`` deliberately does not match a port such as
#: ``:4070`` that merely starts with the same digits.
_PROXY_AUTH_STATUS_RE = re.compile(r"\b407\b")
_PROXY_AUTH_MARKERS = (
    "proxy authentication required",
    "invalid username/password",
)


def is_proxy_authentication_error(exc: BaseException) -> bool:
    """
    Whether *exc* is a proxy refusing our credentials rather than a proxy that
    is temporarily unreachable.

    A proxy connection failure (resolver/connect error, 502/503 from the
    proxy) is transient and stays retryable; only credential refusals are
    deterministic.

    :param exc: Proxy-related backend exception.
    :return: True when the proxy answered with an authentication failure.
    """
    if getattr(exc, "status", None) == 407:
        return True
    text = _error_message(exc).lower()
    return bool(_PROXY_AUTH_STATUS_RE.search(text)) or any(
        marker in text for marker in _PROXY_AUTH_MARKERS
    )


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
    #: Name used in diagnostics (proxy-scheme errors, doctor output).
    backend_name: str = "unknown"

    def __init__(
        self,
        config: FetcherConfig,
        cookies: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the async session with configuration.

        ``timeout`` is a *total* per-request deadline on every backend.  A
        positive value is enforced from connect to the last body byte;
        ``None`` explicitly disables the deadline; zero and negative values
        are rejected instead of meaning "no deadline" on aiohttp/curl_cffi
        and "fail immediately" on httpx.

        Proxy settings are resolved once, here: the explicit config wins,
        otherwise ``trust_env`` reads the standard variables (see
        :mod:`novel_downloader.infra.sessions.proxy`).  Backends receive a
        credential-free URL plus an explicit ``(user, password)`` pair, so
        no library can echo the password from a URL it was handed.

        :param config: Configuration object for session behavior
        :param cookies: Optional initial cookies to set on the session.
        :raises ValueError: If ``timeout`` is zero/negative, or a proxy URL
            is unusable.
        """
        self._backoff_factor = config.backoff_factor
        self._request_interval = config.request_interval
        self._retry_times = config.retry_times
        self._timeout = self._validated_timeout(config.timeout)
        self._max_connections = config.max_connections
        self._verify_ssl = config.verify_ssl
        self._impersonate = config.impersonate
        self._http2 = config.http2
        self._proxy_http, self._proxy_https, self._no_proxy = split_proxy_settings(
            config.proxy,
            config.proxy_user,
            config.proxy_pass,
            config.trust_env,
        )
        # Backends are created once, but callers can reach ``init()`` from
        # several tasks (``async with`` in two workers).  Without the lock
        # both would build a client and one pool of sockets would leak.
        self._init_lock = asyncio.Lock()
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

    @staticmethod
    def _validated_timeout(timeout: float | None) -> float | None:
        """Return *timeout* unchanged, or raise for a value backends read
        differently.

        aiohttp and curl_cffi treat ``0`` as "no deadline" while httpx turns
        it into an immediate timeout (and rejects negatives only when the
        request is made).  Config validation already requires ``> 0``; this
        catches programmatic configs before a single request is sent.
        """
        if timeout is None:
            return None
        if timeout <= 0:
            raise ValueError(
                "timeout must be a positive number of seconds or None to "
                f"disable the deadline, got {timeout!r}"
            )
        return timeout

    def _proxy_for_url(self, url: str) -> ProxyConfig | None:
        """
        Return the proxy to use for *url*, or ``None`` to connect directly.

        Applies the per-scheme proxy and ``NO_PROXY``.  Credentials are
        intentionally split out of :attr:`ProxyConfig.url`.
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        proxy = self._proxy_https if scheme == "https" else self._proxy_http
        if proxy is None:
            return None
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is None:
            # A NO_PROXY entry may name a port (``example.com:8080``); compare
            # against the effective port so a URL without an explicit one is
            # still matched correctly.
            port = {"http": 80, "https": 443}.get(scheme)
        if should_bypass_proxy(self._no_proxy, parts.hostname or "", port):
            return None
        return proxy

    def _proxy_secrets(self) -> tuple[str, ...]:
        """Credential strings to mask in backend error messages."""
        values: list[str] = []
        for proxy in (self._proxy_http, self._proxy_https):
            if proxy is not None:
                values.extend(proxy.secret_values())
        return tuple(dict.fromkeys(values))

    def _validate_proxy_schemes(self, allowed: Iterable[str], hint: str) -> None:
        """
        Reject proxy schemes the backend cannot speak.

        Every backend otherwise fails differently and late: aiohttp tries to
        talk HTTP to a SOCKS port until the timeout expires, httpx raises a
        bare ``ImportError`` for a missing optional dependency, and libcurl
        reports its own curl code.  The message is scheme-only — never the
        host or credentials.

        :param allowed: Schemes the backend supports.
        :param hint: What the user can do instead.
        """
        permitted = frozenset(allowed)
        for proxy in (self._proxy_http, self._proxy_https):
            if proxy is not None and proxy.scheme not in permitted:
                raise ValueError(
                    f"The {self.backend_name} backend does not support "
                    f"{proxy.scheme}:// proxies: {hint}"
                )

    def _timeout_error(self, exc: BaseException) -> TimeoutError:
        return as_timeout_error(exc, secrets=self._proxy_secrets())

    def _redirect_error(self, exc: BaseException) -> TooManyRedirectsError:
        return as_too_many_redirects_error(exc, secrets=self._proxy_secrets())

    def _transport_error(self, exc: BaseException) -> ConnectionError:
        return as_connection_error(exc, secrets=self._proxy_secrets())

    def _non_retryable_transport_error(
        self, exc: BaseException
    ) -> NonRetryableConnectionError:
        return as_non_retryable_connection_error(exc, secrets=self._proxy_secrets())

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
