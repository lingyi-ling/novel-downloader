#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._aiohttp
----------------------------------------
"""

from http.cookies import CookieError, Morsel
from pathlib import Path
from typing import Any, Unpack

import aiohttp

from .base import (
    BaseSession,
    GetRequestKwargs,
    PostRequestKwargs,
)
from .cookie_io import (
    load_cookie_items,
    resolve_cookie_path,
    write_cookie_items,
)
from .response import BaseResponse


def _is_ip_address(host: str) -> bool:
    """Call aiohttp's IP check, going through the module so patches apply."""
    checker: Any = getattr(aiohttp.cookiejar, "is_ip_address", None)
    return bool(checker and checker(host))


class AiohttpSession(BaseSession):
    """Session backend implemented with aiohttp for asynchronous HTTP requests."""

    _session: aiohttp.ClientSession | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        if self._session and not self._session.closed:
            return

        proxy_auth: aiohttp.BasicAuth | None = None
        if self._proxy_user and self._proxy_pass:
            proxy_auth = aiohttp.BasicAuth(self._proxy_user, self._proxy_pass)

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        connector = aiohttp.TCPConnector(
            ssl=self._verify_ssl,
            limit_per_host=self._max_connections,
        )

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self._headers,
            cookies=self._cookies,
            trust_env=self._trust_env,
            proxy=self._proxy,
            proxy_auth=proxy_auth,
        )

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.

        Safe to call repeatedly and concurrently: the backend reference is
        detached before awaiting so a second caller becomes a no-op.
        """
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def get(
        self,
        url: str,
        *,
        allow_redirects: bool | None = None,
        verify: bool | None = None,
        encoding: str = "utf-8",
        **kwargs: Unpack[GetRequestKwargs],
    ) -> BaseResponse:
        if verify is not None:
            kwargs.setdefault("ssl", verify)  # type: ignore[typeddict-item]
        if allow_redirects is not None:
            kwargs.setdefault("allow_redirects", allow_redirects)  # type: ignore[typeddict-item]

        async with self.session.get(url, **kwargs) as r:
            content = await r.read()
            return BaseResponse(
                content=content,
                headers=r.headers,
                status=r.status,
                encoding=r.charset or encoding,
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
        if verify is not None:
            kwargs.setdefault("ssl", verify)  # type: ignore[typeddict-item]
        if allow_redirects is not None:
            kwargs.setdefault("allow_redirects", allow_redirects)  # type: ignore[typeddict-item]

        async with self.session.post(url, **kwargs) as r:
            content = await r.read()
            return BaseResponse(
                content=content,
                headers=r.headers,
                status=r.status,
                encoding=r.charset or encoding,
            )

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
                domain = item.get("domain")
                if domain:
                    morsel: Morsel[str] = Morsel()
                    morsel.set(item["name"], item["value"], item["value"])
                    morsel["domain"] = domain
                    morsel["path"] = item.get("path", "/")
                    jar.update_cookies({item["name"]: morsel})
                else:
                    jar.update_cookies({item["name"]: item["value"]})
            except (CookieError, ValueError):
                continue
        return True

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None or self._session.cookie_jar is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "aiohttp.cookies")

        cookies: list[dict[str, str]] = []
        for cookie in self._session.cookie_jar:
            domain = cookie["domain"]
            # aiohttp only sends domain-scoped cookies to IP hosts when the
            # jar is "unsafe"; keep legacy shared behaviour for IP cookies.
            if domain and _is_ip_address(domain):
                domain = ""
            item = {"name": cookie.key, "value": cookie.value}
            if domain:
                item["domain"] = domain
            if cookie["path"]:
                item["path"] = cookie["path"]
            cookies.append(item)

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
