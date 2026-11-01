#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._httpx
--------------------------------------
"""

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
    as_connection_error,
    as_timeout_error,
    as_too_many_redirects_error,
)
from .cookie_io import (
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)
from .response import BaseResponse


class HttpxSession(BaseSession):
    """Session backend based on httpx providing async HTTP/1.1 and HTTP/2 support."""

    _session: httpx.AsyncClient | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        if self._session and not self._session.is_closed:
            return
        limits = httpx.Limits(
            max_keepalive_connections=self._max_connections,
            max_connections=self._max_connections,
        )
        proxy = self._build_proxy_config(
            self._proxy,
            self._proxy_user,
            self._proxy_pass,
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
            proxy=proxy,
            trust_env=self._trust_env,
        )

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.

        Safe to call repeatedly and concurrently: the backend reference is
        detached before awaiting so a second caller becomes a no-op.
        """
        session, self._session = self._session, None
        if session is not None and not session.is_closed:
            await session.aclose()

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
        kwargs.setdefault("follow_redirects", redirect)  # type: ignore[typeddict-item]

        try:
            r = await self.session.get(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise as_timeout_error(exc) from exc
        except httpx.TooManyRedirects as exc:
            raise as_too_many_redirects_error(exc) from exc
        except (
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
            httpx.DecodingError,
        ) as exc:
            # DecodingError covers a body whose Content-Encoding is corrupt;
            # aiohttp classifies the same failure as a payload/connection
            # error, so it is translated here too.
            raise as_connection_error(exc) from exc
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
        kwargs.setdefault("follow_redirects", redirect)  # type: ignore[typeddict-item]

        try:
            r = await self.session.post(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise as_timeout_error(exc) from exc
        except httpx.TooManyRedirects as exc:
            raise as_too_many_redirects_error(exc) from exc
        except (
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
            httpx.DecodingError,
        ) as exc:
            raise as_connection_error(exc) from exc
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
                self._session.cookies.set(
                    item["name"],
                    item["value"],
                    domain=item.get("domain", ""),
                    path=item.get("path", "/"),
                )
            except (CookieError, ValueError):
                continue
        return True

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "httpx.cookies")

        cookies: list[dict[str, str]] = []
        for cookie in self.session.cookies.jar:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value or "",
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
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

    @staticmethod
    def _build_proxy_config(
        proxy: str | None = None,
        proxy_user: str | None = None,
        proxy_pass: str | None = None,
    ) -> str | httpx.Proxy | None:
        """Builds proxy configuration."""
        if not proxy:
            return None

        if "@" in proxy:
            return proxy

        if proxy_user and proxy_pass:
            return httpx.Proxy(proxy, auth=(proxy_user, proxy_pass))

        return proxy
