#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._curl_cffi
------------------------------------------
"""

# mypy: disable-error-code=unused-ignore

from http.cookies import CookieError
from pathlib import Path
from typing import Any, Unpack

from curl_cffi.requests import AsyncSession

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


class CurlCffiSession(BaseSession):
    """Session backend using curl_cffi for browser-like HTTP requests."""

    _session: AsyncSession[Any] | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        if self._session:
            return

        proxy_auth = None
        if self._proxy_user and self._proxy_pass:
            proxy_auth = (self._proxy_user, self._proxy_pass)

        self._session = AsyncSession(
            headers=self._headers,
            cookies=self._cookies,
            timeout=self._timeout,
            impersonate=self._impersonate,  # type: ignore[arg-type]
            verify=self._verify_ssl,
            proxy=self._proxy,
            proxy_auth=proxy_auth,
            trust_env=self._trust_env,
        )

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.

        Safe to call repeatedly and concurrently: the backend reference is
        detached before awaiting so a second caller becomes a no-op.  This
        matters for curl_cffi, whose ``AsyncSession.close`` cannot run twice.
        """
        session, self._session = self._session, None
        if session is not None:
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
            kwargs.setdefault("verify", verify)  # type: ignore[typeddict-item]
        if allow_redirects is not None:
            kwargs.setdefault("allow_redirects", allow_redirects)  # type: ignore[typeddict-item]

        r = await self.session.get(url, **kwargs)
        return BaseResponse(
            content=r.content,
            headers=r.headers,
            status=r.status_code,
            encoding=r.encoding or encoding,
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
            kwargs.setdefault("verify", verify)  # type: ignore[typeddict-item]
        if allow_redirects is not None:
            kwargs.setdefault("allow_redirects", allow_redirects)  # type: ignore[typeddict-item]

        r = await self.session.post(url, **kwargs)
        return BaseResponse(
            content=r.content,
            headers=r.headers,
            status=r.status_code,
            encoding=r.encoding or encoding,
        )

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "curl_cffi.cookies")
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

        path = resolve_cookie_path(cookies_dir, filename or "curl_cffi.cookies")

        cookies: list[dict[str, str]] = []
        for cookie in self._session.cookies.jar:
            item = {"name": cookie.name, "value": cookie.value or ""}
            if cookie.domain:
                item["domain"] = cookie.domain
            if cookie.path:
                item["path"] = cookie.path
            cookies.append(item)

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
        if self._session is None:
            return None

        value: str | None = self._session.cookies.get(key)
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="ignore")
            except Exception:
                return None
        return value

    def clear_cookie(self, name: str) -> None:
        if self._session is None:
            return

        self._session.cookies.pop(name, None)

    def clear_cookies(self) -> None:
        """
        Remove all cookies stored in the curl_cffi session.
        """
        if self._session is None:
            return

        self._session.cookies.clear()

    @property
    def session(self) -> AsyncSession[Any]:
        """
        Return the active AsyncSession.

        :raises RuntimeError: If the session is uninitialized.
        """
        if self._session is None:
            raise RuntimeError("Session is not initialized or has been shut down.")
        return self._session
