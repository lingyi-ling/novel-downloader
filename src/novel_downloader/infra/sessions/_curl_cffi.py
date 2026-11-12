#!/usr/bin/env python3
"""
novel_downloader.infra.sessions._curl_cffi
------------------------------------------
"""

# mypy: disable-error-code=unused-ignore

from http.cookiejar import Cookie
from http.cookies import CookieError
from pathlib import Path
from typing import Any, Unpack

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import (
    CertificateVerifyError as CurlCertificateVerifyError,
)
from curl_cffi.requests.exceptions import (
    ChunkedEncodingError as CurlChunkedEncodingError,
)
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
)
from curl_cffi.requests.exceptions import (
    ContentDecodingError as CurlContentDecodingError,
)
from curl_cffi.requests.exceptions import (
    HTTPError as CurlHTTPError,
)
from curl_cffi.requests.exceptions import (
    IncompleteRead as CurlIncompleteRead,
)
from curl_cffi.requests.exceptions import (
    ProxyError as CurlProxyError,
)
from curl_cffi.requests.exceptions import (
    RequestException as CurlRequestException,
)
from curl_cffi.requests.exceptions import (
    Timeout as CurlTimeout,
)
from curl_cffi.requests.exceptions import (
    TooManyRedirects as CurlTooManyRedirects,
)

from .base import (
    DEFAULT_ALLOW_REDIRECTS,
    DEFAULT_MAX_REDIRECTS,
    BaseSession,
    GetRequestKwargs,
    PostRequestKwargs,
    close_backend_session,
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
from .proxy import ALL_PROXY_SCHEMES
from .response import BaseResponse


class CurlCffiSession(BaseSession):
    """Session backend using curl_cffi for browser-like HTTP requests."""

    backend_name = "curl_cffi"

    _session: AsyncSession[Any] | None

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        async with self._init_lock:
            if self._session is not None:
                return
            self._validate_proxy_schemes(
                ALL_PROXY_SCHEMES,
                "use an http://, https:// or socks5:// proxy URL",
            )

            self._session = AsyncSession(
                headers=self._headers,
                cookies=self._cookies,
                # ``None`` disables the deadline; the stub only advertises
                # floats, but libcurl treats a missing value as "no timeout".
                timeout=self._timeout,  # type: ignore[arg-type]
                impersonate=self._impersonate,  # type: ignore[arg-type]
                verify=self._verify_ssl,
                # curl_cffi defaults to 30 redirects; pin it to the unified
                # ceiling so a long chain behaves like on the other backends.
                max_redirects=DEFAULT_MAX_REDIRECTS,
                # Without this curl_cffi opens one connection per request, so a
                # 16-worker download ignores ``max_connections`` entirely.
                max_clients=self._max_connections,
                # ``trust_env`` is stored by curl_cffi 0.16 but never read, and
                # libcurl consults the environment unless a proxy option is
                # set: HTTP_PROXY was silently used even with trust_env=False.
                # An empty per-scheme entry makes every request that the
                # facade leaves proxy-less explicitly direct, and a per-request
                # ``proxy=`` still takes precedence over this base map.
                proxies={"http": "", "https": ""},
                # libcurl applies NO_PROXY from the environment even to an
                # explicit proxy, which would bypass the facade's own decision
                # on the curl backend only.  An empty bypass list hands that
                # decision back to us.
                curl_options={CurlOpt.NOPROXY: ""},
                trust_env=False,
            )

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.

        Safe to call repeatedly and concurrently: the backend reference is
        detached before awaiting so a second caller becomes a no-op.  This
        matters for curl_cffi, whose ``AsyncSession.close`` cannot run twice;
        the detach happens under the init lock so a concurrent ``init()``
        cannot install a client that an in-flight ``close()`` never sees, and
        the shielded close keeps a cancelled caller from aborting the
        shutdown after the reference is gone.
        """
        async with self._init_lock:
            session, self._session = self._session, None
        if session is not None:
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
        self._prepare_request(url, options, verify, allow_redirects)

        try:
            r = await self.session.get(url, **options)
        except CurlTimeout as exc:
            # Must precede the connection clause: ConnectTimeout inherits
            # from both ConnectionError and Timeout, and the timeout
            # classification wins.
            raise self._timeout_error(exc) from exc
        except CurlTooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except CurlCertificateVerifyError as exc:
            # Must precede ConnectionError: CertificateVerifyError is one, and
            # the peer's certificate cannot become valid between attempts.
            raise self._non_retryable_transport_error(exc) from exc
        except CurlProxyError as exc:
            # CONNECT tunnel failures carry the proxy status in the message
            # ("... response 407"); only credential refusals are permanent.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (
            CurlConnectionError,
            CurlIncompleteRead,
            CurlHTTPError,
            CurlContentDecodingError,
            CurlChunkedEncodingError,
        ) as exc:
            # CurlHTTPError is how curl reports a corrupt Content-Encoding
            # (CURLE_BAD_CONTENT_ENCODING), which aiohttp and httpx classify
            # as a payload/connection failure.
            raise self._transport_error(exc) from exc
        except CurlRequestException as exc:
            # Everything else the library raises (setopt failures, session
            # state, ...) must not escape as a raw curl_cffi class; invalid
            # URLs are programming errors and stay ValueError, like on the
            # other backends.
            if isinstance(exc, ValueError):
                raise
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
        options: dict[str, Any] = dict(kwargs)
        self._prepare_request(url, options, verify, allow_redirects)

        try:
            r = await self.session.post(url, **options)
        except CurlTimeout as exc:
            # Must precede the connection clause: ConnectTimeout inherits
            # from both ConnectionError and Timeout, and the timeout
            # classification wins.
            raise self._timeout_error(exc) from exc
        except CurlTooManyRedirects as exc:
            raise self._redirect_error(exc) from exc
        except CurlCertificateVerifyError as exc:
            # Must precede ConnectionError: CertificateVerifyError is one, and
            # the peer's certificate cannot become valid between attempts.
            raise self._non_retryable_transport_error(exc) from exc
        except CurlProxyError as exc:
            # CONNECT tunnel failures carry the proxy status in the message
            # ("... response 407"); only credential refusals are permanent.
            if is_proxy_authentication_error(exc):
                raise self._non_retryable_transport_error(exc) from exc
            raise self._transport_error(exc) from exc
        except (
            CurlConnectionError,
            CurlIncompleteRead,
            CurlHTTPError,
            CurlContentDecodingError,
            CurlChunkedEncodingError,
        ) as exc:
            raise self._transport_error(exc) from exc
        except CurlRequestException as exc:
            if isinstance(exc, ValueError):
                raise
            raise self._transport_error(exc) from exc
        return BaseResponse(
            content=r.content,
            headers=r.headers,
            status=r.status_code,
            encoding=r.charset_encoding or encoding,
        )

    def _prepare_request(
        self,
        url: str,
        options: dict[str, Any],
        verify: bool | None,
        allow_redirects: bool | None,
    ) -> None:
        """Apply the facade's option defaults and the resolved proxy."""
        if verify is not None:
            options.setdefault("verify", verify)
        redirect = (
            DEFAULT_ALLOW_REDIRECTS if allow_redirects is None else allow_redirects
        )
        options.setdefault("allow_redirects", redirect)

        proxy = self._proxy_for_url(url)
        if proxy is None:
            return
        options.setdefault("proxy", proxy.url)
        auth = proxy.auth
        if auth is not None:
            options.setdefault("proxy_auth", auth)

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        if self._session is None:
            return False

        path = resolve_cookie_path(cookies_dir, filename or "curl_cffi.cookies")
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

        path = resolve_cookie_path(cookies_dir, filename or "curl_cffi.cookies")

        cookies: list[dict[str, Any]] = []
        for cookie in self._session.cookies.jar:
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
        if self._session is None:
            return None

        # The same name can exist for several domains.  Return the first jar
        # entry so duplicate names resolve deterministically (and like the
        # other backends) instead of relying on ``Cookies.get``.
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
