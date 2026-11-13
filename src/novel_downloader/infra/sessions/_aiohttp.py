#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._aiohttp
----------------------------------------
"""

from email.utils import formatdate
from http.cookies import CookieError, Morsel
from pathlib import Path
from typing import Any, Unpack

import aiohttp

from .base import (
    DEFAULT_ALLOW_REDIRECTS,
    BaseSession,
    GetRequestKwargs,
    PostRequestKwargs,
    close_backend_session,
    is_certificate_verification_error,
    is_proxy_authentication_error,
)
from .cookie_io import (
    _as_expiry,
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)
from .response import BaseResponse, ResponseTooLargeError


class AiohttpSession(BaseSession):
    """Session backend implemented with aiohttp for asynchronous HTTP requests."""

    backend_name = "aiohttp"

    _session: aiohttp.ClientSession | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        async with self._init_lock:
            if self._session and not self._session.closed:
                return

            # aiohttp cannot tunnel HTTP over SOCKS or TLS-to-proxy: it would
            # try to speak HTTP to the SOCKS port until the timeout expires.
            # Failing here names the backend and the alternative instead.
            self._validate_proxy_schemes(
                {"http"},
                "use the curl_cffi backend (socks4/4a/5/5h are supported) or "
                "point the proxy setting at an http:// endpoint",
            )

            timeout = aiohttp.ClientTimeout(total=self._timeout)
            connector = aiohttp.TCPConnector(
                ssl=self._verify_ssl,
                limit_per_host=self._max_connections,
            )

            self._session = aiohttp.ClientSession(
                connector=connector,
                # httpx and curl_cffi accept and send cookies for IP-host
                # origins; aiohttp's default jar silently drops them, which
                # loses logins on sites served from bare IPs.  ``unsafe`` only
                # relaxes that IP handling — cross-domain cookies are still
                # rejected, so site A's cookies never reach site B.
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=timeout,
                headers=self._headers,
                cookies=self._cookies,
                # Proxy selection belongs to the facade: it resolves
                # HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY the same way for
                # every backend and passes the chosen proxy per request.
                # aiohttp's own env handling ignores ALL_PROXY and silently
                # drops https:// proxies, so it must stay off.
                trust_env=False,
            )

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
        if session is not None and not session.closed:
            await close_backend_session(session.close())

    async def get(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[GetRequestKwargs],
    ) -> BaseResponse:
        options: dict[str, Any] = dict(kwargs)
        max_bytes = options.pop("max_bytes", None)
        self._prepare_request(url, options, verify, allow_redirects)

        try:
            async with self.session.get(url, **options) as r:
                if max_bytes is None:
                    content = await r.read()
                else:
                    # Read at most one byte past the limit: a hostile body must
                    # never be buffered whole just to be measured.
                    content = await r.content.read(int(max_bytes) + 1)
                    if len(content) > int(max_bytes):
                        raise ResponseTooLargeError(int(max_bytes))
                return BaseResponse(
                    content=content,
                    headers=r.headers,
                    status=r.status,
                    encoding=r.charset or encoding,
                )
        except TimeoutError as exc:
            # aiohttp's ServerTimeoutError family is both a TimeoutError and
            # a ClientConnectionError; normalise to the builtin type (the
            # timeout classification wins, as on the other backends).
            raise self._timeout_error(exc) from exc
        except aiohttp.TooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except aiohttp.ClientHttpProxyError as exc:
            # A proxy that refused our credentials will refuse them again; a
            # proxy that answered 502/503 is a transient upstream failure.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
            # ClientConnectorCertificateError is a ClientConnectionError and
            # an ssl.SSLCertVerificationError; replaying the handshake cannot
            # turn an untrusted certificate into a trusted one.
            if is_certificate_verification_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except aiohttp.ClientError as exc:
            # Anything else aiohttp raises (e.g. a response-protocol error)
            # is still a transport failure, not a raw backend exception
            # leaking into the fetcher's retry loop.
            raise self._transport_error(exc) from exc

    async def post(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[PostRequestKwargs],
    ) -> BaseResponse:
        options: dict[str, Any] = dict(kwargs)
        self._prepare_request(url, options, verify, allow_redirects)

        try:
            async with self.session.post(url, **options) as r:
                content = await r.read()
                return BaseResponse(
                    content=content,
                    headers=r.headers,
                    status=r.status,
                    encoding=r.charset or encoding,
                )
        except TimeoutError as exc:
            # aiohttp's ServerTimeoutError family is both a TimeoutError and
            # a ClientConnectionError; normalise to the builtin type (the
            # timeout classification wins, as on the other backends).
            raise self._timeout_error(exc) from exc
        except aiohttp.TooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except aiohttp.ClientHttpProxyError as exc:
            # A proxy that refused our credentials will refuse them again; a
            # proxy that answered 502/503 is a transient upstream failure.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
            # ClientConnectorCertificateError is a ClientConnectionError and
            # an ssl.SSLCertVerificationError; replaying the handshake cannot
            # turn an untrusted certificate into a trusted one.
            if is_certificate_verification_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except aiohttp.ClientError as exc:
            raise self._transport_error(exc) from exc

    def _prepare_request(
        self,
        url: str,
        kwargs: dict[str, Any],
        verify: bool | None,
        allow_redirects: bool | None,
    ) -> None:
        """Apply the facade's option defaults and the resolved proxy."""
        if verify is not None:
            kwargs.setdefault("ssl", verify)
        redirect = (
            DEFAULT_ALLOW_REDIRECTS if allow_redirects is None else allow_redirects
        )
        kwargs.setdefault("allow_redirects", redirect)

        proxy = self._proxy_for_url(url)
        if proxy is None:
            return
        kwargs.setdefault("proxy", proxy.url)
        auth = proxy.auth
        if auth is not None:
            # aiohttp 3.14's recommended replacement (per-request
            # ``proxy_headers``) does not send credentials for plain-HTTP
            # targets — verified against a 407-checking proxy — so the
            # deprecated ``proxy_auth`` stays.  It is the same deprecated
            # parameter the session-level proxy used before, but per
            # request, which is what per-URL NO_PROXY needs.
            kwargs.setdefault("proxy_auth", aiohttp.BasicAuth(auth[0], auth[1]))

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "aiohttp.cookies")
        items = load_cookie_items(path)
        if items is None:
            return False

        jar = self._session.cookie_jar
        for item in items:
            try:
                morsel: Morsel[str] = Morsel()
                morsel.set(item["name"], item["value"], item["value"])
                domain = item.get("domain")
                if domain:
                    morsel["domain"] = domain
                cookie_path = item.get("path")
                if isinstance(cookie_path, str) and cookie_path.startswith("/"):
                    morsel["path"] = cookie_path
                if item.get("secure"):
                    morsel["secure"] = True
                if item.get("http_only"):
                    morsel["httponly"] = True
                same_site = item.get("same_site")
                if isinstance(same_site, str) and same_site:
                    morsel["samesite"] = same_site

                expires = item.get("expires")
                if isinstance(expires, int | float) and not isinstance(expires, bool):
                    # aiohttp stores the absolute deadline separately; feed it
                    # back as an HTTP date so a reload keeps the same deadline
                    # instead of turning the cookie into a session cookie.
                    morsel["expires"] = formatdate(float(expires), usegmt=True)
                else:
                    max_age = item.get("max_age")
                    if isinstance(max_age, int) and max_age > 0:
                        morsel["max-age"] = str(max_age)

                jar.update_cookies({item["name"]: morsel})
            except (CookieError, ValueError, OverflowError):
                continue
        return True

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None or self._session.cookie_jar is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "aiohttp.cookies")

        # aiohttp keeps absolute deadlines out of the Morsel itself.  The
        # private map is the same one its own ``CookieJar.save`` reads; fall
        # back to the Morsel's Expires attribute for jars without one.
        expirations = getattr(self._session.cookie_jar, "_expirations", None)
        if not isinstance(expirations, dict):
            expirations = {}

        cookies: list[dict[str, Any]] = []
        for cookie in self._session.cookie_jar:
            domain = cookie["domain"] or ""
            cookie_path = cookie["path"] or "/"
            expires = expirations.get((domain, cookie_path.rstrip("/"), cookie.key))
            if expires is None:
                expires = _as_expiry(cookie["expires"])
            # The domain is kept even for IP hosts: the session jar is
            # created with unsafe=True, so an IP-scoped cookie round-trips
            # scoped instead of being widened to every host.
            cookies.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": domain,
                    "path": cookie_path,
                    "secure": bool(cookie["secure"]),
                    "http_only": bool(cookie["httponly"]),
                    "same_site": cookie["samesite"] or None,
                    "expires": expires,
                    "max_age": cookie["max-age"] or None,
                    "domain_specified": bool(domain),
                    "domain_initial_dot": domain.startswith("."),
                    "path_specified": bool(cookie_path),
                    "rest": {},
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
        self._session.cookie_jar.update_cookies(cookies)

    def get_cookie(self, key: str) -> str | None:
        """
        Retrieve the value of a cookie by name from the active session.

        :param key: Cookie name.
        :return: The cookie value if found, else None.
        """
        if self._session is None:
            return None

        jar = self._session.cookie_jar
        for cookie in jar:
            if cookie.key == key:
                value: str = cookie.value
                return value
        return None

    def clear_cookie(self, name: str) -> None:
        if self._session is None:
            return

        jar = self._session.cookie_jar
        jar.clear(predicate=lambda morsel: morsel.key == name)

    def clear_cookies(self) -> None:
        """
        Remove all cookies stored in the aiohttp session.
        """
        if self._session is None:
            return

        jar = self._session.cookie_jar
        jar.clear()

    @property
    def session(self) -> aiohttp.ClientSession:
        """
        Return the active aiohttp.ClientSession.

        :raises RuntimeError: If the session is uninitialized.
        """
        if self._session is None:
            raise RuntimeError("Session is not initialized or has been shut down.")
        return self._session
