#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._httpx
--------------------------------------
"""

import asyncio
import importlib.util
from http.cookiejar import Cookie
from http.cookies import CookieError
from pathlib import Path
from typing import Any, Unpack

import httpx

from .base import (
    DEFAULT_ALLOW_REDIRECTS,
    DEFAULT_MAX_REDIRECTS,
    BaseSession,
    GetRequestKwargs,
    PostRequestKwargs,
    close_backend_session,
    is_certificate_verification_error,
    is_proxy_authentication_error,
)
from .cookie_io import (
    _cookie_rest,
    _rest_has,
    _rest_value,
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)
from .proxy import ALL_PROXY_SCHEMES, no_proxy_patterns
from .response import BaseResponse


class HttpxSession(BaseSession):
    """Session backend based on httpx providing async HTTP/1.1 and HTTP/2 support."""

    backend_name = "httpx"

    _session: httpx.AsyncClient | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        async with self._init_lock:
            if self._session and not self._session.is_closed:
                return
            self._validate_proxy_schemes(
                ALL_PROXY_SCHEMES,
                "use an http://, https:// or socks5:// proxy URL",
            )
            if self._uses_socks_proxy() and importlib.util.find_spec("socksio") is None:
                # httpx raises a bare ImportError from inside httpcore here;
                # naming the extra tells the user what to install (or that
                # curl_cffi supports SOCKS out of the box).
                raise RuntimeError(
                    "The httpx backend needs the optional 'socksio' package "
                    "for SOCKS proxies (install it with "
                    "`pip install httpx[socks]`) or use the curl_cffi "
                    "backend."
                )
            limits = httpx.Limits(
                max_keepalive_connections=self._max_connections,
                max_connections=self._max_connections,
            )

            self._session = httpx.AsyncClient(
                http2=self._http2,
                timeout=self._timeout,
                verify=self._verify_ssl,
                headers=self._headers,
                cookies=self._cookies,
                limits=limits,
                # httpx defaults to 20 redirects; pin it to the unified ceiling
                # so a long chain behaves like on the other backends.
                max_redirects=DEFAULT_MAX_REDIRECTS,
                # Per-scheme proxy plus NO_PROXY entries.  ``mounts`` is the
                # only way to give httpx per-host proxy rules; the facade
                # resolves the environment itself so all three backends agree.
                mounts=self._proxy_mounts(limits),
                trust_env=False,
            )

    def _uses_socks_proxy(self) -> bool:
        return any(
            proxy is not None and proxy.is_socks
            for proxy in (self._proxy_http, self._proxy_https)
        )

    def _proxy_mounts(
        self, limits: httpx.Limits
    ) -> dict[str, httpx.AsyncBaseTransport | None] | None:
        """Build httpx's per-scheme transport map from the facade's settings.

        httpx 0.28 accepts transports (not ``Proxy`` objects) as mount
        values, so each proxied scheme gets its own ``AsyncHTTPTransport``
        with the same verification and pool limits as the main transport.
        """
        if (
            self._proxy_http is None
            and self._proxy_https is None
            and not self._no_proxy
        ):
            return None

        mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
        for scheme, proxy in (
            ("http", self._proxy_http),
            ("https", self._proxy_https),
        ):
            if proxy is not None:
                mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(
                    verify=self._verify_ssl,
                    trust_env=False,
                    http2=self._http2,
                    limits=limits,
                    proxy=httpx.Proxy(proxy.url, auth=proxy.auth),
                )

        for pattern in no_proxy_patterns(self._no_proxy):
            if pattern == "*":
                # NO_PROXY=* means "never proxy", matching the other
                # backends' matcher.
                return {}
            mounts[f"all://{pattern}"] = None
        return mounts

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.

        Safe to call repeatedly and concurrently: the backend reference is
        detached before awaiting so a second caller becomes a no-op.  The
        detach happens under the init lock, so a concurrent ``init()`` cannot
        install a new client that an in-flight ``close()`` then never sees.
        The backend close is shielded, so cancelling this caller cannot abort
        the shutdown after the reference is gone.
        """
        async with self._init_lock:
            session, self._session = self._session, None
        if session is not None and not session.is_closed:
            await close_backend_session(session.aclose())

    async def get(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[GetRequestKwargs],
    ) -> BaseResponse:
        self._check_verify_override(verify)
        redirect = (
            DEFAULT_ALLOW_REDIRECTS if allow_redirects is None else allow_redirects
        )
        options: dict[str, Any] = dict(kwargs)
        options.setdefault("follow_redirects", redirect)

        try:
            # ``httpx.Timeout`` applies per operation, so a server that
            # trickles bytes every timeout-1 seconds could hold the request
            # open forever.  The outer deadline restores the per-request
            # guarantee the other backends already provide.  ``None``
            # disables it, exactly like the other two backends.
            r = await asyncio.wait_for(self.session.get(url, **options), self._timeout)
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise self._timeout_error(exc) from exc
        except httpx.TooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except httpx.ProxyError as exc:
            # CONNECT refusals surface here (httpcore raises
            # "407 Proxy Authentication Required"); a proxy that is merely
            # unreachable stays a retryable connection failure.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except httpx.ConnectError as exc:
            # Must precede NetworkError: a certificate rejection arrives as a
            # ConnectError wrapping ssl.SSLCertVerificationError, and retrying
            # cannot repair the certificate.
            if is_certificate_verification_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.DecodingError,
        ) as exc:
            # DecodingError covers a body whose Content-Encoding is corrupt;
            # aiohttp classifies the same failure as a payload/connection
            # error, so it is translated here too.
            raise self._transport_error(exc) from exc
        except httpx.RequestError as exc:
            # Remaining request failures (unsupported protocol, invalid
            # header, ...) are transport problems too; leaking a raw httpx
            # class would make the fetcher's classification backend-dependent.
            raise self._transport_error(exc) from exc
        return BaseResponse(
            content=r.content,
            headers=r.headers,
            status=r.status_code,
            encoding=r.charset_encoding or encoding,
        )

    async def post(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[PostRequestKwargs],
    ) -> BaseResponse:
        self._check_verify_override(verify)
        redirect = (
            DEFAULT_ALLOW_REDIRECTS if allow_redirects is None else allow_redirects
        )
        options: dict[str, Any] = dict(kwargs)
        options.setdefault("follow_redirects", redirect)

        try:
            r = await asyncio.wait_for(self.session.post(url, **options), self._timeout)
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise self._timeout_error(exc) from exc
        except httpx.TooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except httpx.ProxyError as exc:
            # CONNECT refusals surface here (httpcore raises
            # "407 Proxy Authentication Required"); a proxy that is merely
            # unreachable stays a retryable connection failure.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except httpx.ConnectError as exc:
            # Must precede NetworkError: a certificate rejection arrives as a
            # ConnectError wrapping ssl.SSLCertVerificationError, and retrying
            # cannot repair the certificate.
            if is_certificate_verification_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.DecodingError,
        ) as exc:
            raise self._transport_error(exc) from exc
        except httpx.RequestError as exc:
            raise self._transport_error(exc) from exc
        return BaseResponse(
            content=r.content,
            headers=r.headers,
            status=r.status_code,
            encoding=r.charset_encoding or encoding,
        )

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "httpx.cookies")
        items = load_cookie_items(path)
        if items is None:
            return False

        for item in items:
            try:
                domain = item.get("domain") or ""
                cookie_path = item.get("path") or "/"
                expires = item.get("expires")
                rest: dict[str, Any] = dict(item.get("rest") or {})
                if item.get("http_only") and not _rest_has(rest, "httponly"):
                    rest["HttpOnly"] = None
                same_site = item.get("same_site")
                if isinstance(same_site, str) and not _rest_has(rest, "samesite"):
                    rest["SameSite"] = same_site
                max_age = item.get("max_age")
                if isinstance(max_age, int) and not _rest_has(rest, "max-age"):
                    rest["Max-Age"] = str(max_age)

                cookie = Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(item.get("domain_specified", bool(domain))),
                    domain_initial_dot=bool(
                        item.get("domain_initial_dot", domain.startswith("."))
                    ),
                    path=cookie_path,
                    path_specified=bool(item.get("path_specified", bool(cookie_path))),
                    secure=bool(item.get("secure")),
                    expires=int(expires) if expires is not None else None,
                    # No expires means a session cookie.  ``discard`` is what
                    # http.cookiejar uses to decide whether to persist it.
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest=_cookie_rest(rest),
                    rfc2109=False,
                )
                self._session.cookies.jar.set_cookie(cookie)
            except (CookieError, ValueError, TypeError, OverflowError):
                continue
        return True

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "httpx.cookies")

        cookies: list[dict[str, Any]] = []
        for cookie in self.session.cookies.jar:
            rest = dict(getattr(cookie, "_rest", None) or {})
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value or "",
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                    "http_only": _rest_has(rest, "httponly"),
                    "same_site": _rest_value(rest, "samesite"),
                    "expires": cookie.expires,
                    "max_age": _rest_value(rest, "max-age"),
                    "domain_specified": bool(cookie.domain_specified),
                    "domain_initial_dot": bool(cookie.domain_initial_dot),
                    "path_specified": bool(cookie.path_specified),
                    "rest": rest,
                }
            )
        try:
            write_cookie_items(path, cookies)
        except OSError:
            return False
        return True

    def update_cookies(self, cookies: dict[str, str]) -> None:
        if self._session is None:
            return
        self._session.cookies.update(cookies)

    def get_cookie(self, key: str) -> str | None:
        """
        Retrieve the value of a cookie by name from the active session.

        :param key: Cookie name.
        :return: The cookie value if found, else None.
        """
        if self._session is None:
            return None

        # The same name can exist for several domains.  Return the first jar
        # entry instead of letting ``Cookies.get`` raise ``CookieConflict``,
        # which keeps this getter consistent with the other backends.
        for cookie in self._session.cookies.jar:
            if cookie.name != key:
                continue
            value = cookie.value
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            return str(value) if value is not None else None
        return None

    def clear_cookie(self, name: str) -> None:
        if self._session is None:
            return

        jar = self._session.cookies.jar
        matches = [
            (cookie.domain, cookie.path, cookie.name)
            for cookie in jar
            if cookie.name == name
        ]
        for domain, path, cookie_name in matches:
            jar.clear(domain, path, cookie_name)

    def clear_cookies(self) -> None:
        """
        Remove all cookies stored in the httpx session.
        """
        if self._session is None:
            return
        self._session.cookies.clear()

    @property
    def session(self) -> httpx.AsyncClient:
        """
        Return the active httpx.AsyncClient.

        :raises RuntimeError: If the session is uninitialized.
        """
        if self._session is None:
            raise RuntimeError("Session is not initialized or has been shut down.")
        return self._session

    def _check_verify_override(self, verify: bool | None) -> None:
        """
        Reject a per-request TLS override that cannot be honoured.

        httpx owns verification at the client level and has no per-request
        switch, while the aiohttp and curl_cffi backends do honour one.  A
        conflicting value therefore fails loudly here instead of being
        silently dropped (which would let a caller who explicitly asked for
        verification keep downloading over an unverified connection).
        """
        if verify is not None and verify != self._verify_ssl:
            raise RuntimeError(
                "The httpx backend cannot override SSL verification per "
                f"request: got verify={verify} while the session was created "
                f"with verify_ssl={self._verify_ssl}. Configure verify_ssl "
                "on the session instead, or use the aiohttp/curl_cffi "
                "backend."
            )
