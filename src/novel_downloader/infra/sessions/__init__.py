#!/usr/bin/env python3
"""
novel_downloader.infra.sessions
-------------------------------
"""

from __future__ import annotations

import importlib.util
from typing import Any

from novel_downloader.schemas import FetcherConfig

from .base import BaseSession

__all__ = ["available_backends", "create_session"]

#: Backend name -> top-level package that must be importable for it.  Used
#: both for availability probing and for the "what can I use instead" hint
#: in the error raised when a backend is requested but not installed.
_BACKEND_PACKAGES: dict[str, str] = {
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "curl_cffi": "curl_cffi",
}


def available_backends() -> list[str]:
    """
    Return the session backends whose packages are installed.

    Desktop and Android builds ship a subset of the optional HTTP backends,
    so callers (settings validation, the doctor command) can probe what is
    actually usable before constructing a session.  Probing uses
    :func:`importlib.util.find_spec`, which locates the package without
    executing it.

    :return: Sorted subset of ``["aiohttp", "httpx", "curl_cffi"]``.
    """
    return [
        name
        for name, package in _BACKEND_PACKAGES.items()
        if importlib.util.find_spec(package) is not None
    ]


def _resolve_backend(backend: str) -> type[BaseSession]:
    """Import and return the session class for *backend*."""
    match backend:
        case "aiohttp":
            from ._aiohttp import AiohttpSession

            return AiohttpSession
        case "httpx":
            from ._httpx import HttpxSession

            return HttpxSession
        case "curl_cffi":
            from ._curl_cffi import CurlCffiSession

            return CurlCffiSession
        case _:
            raise ValueError(f"Unsupported backend: {backend!r}")


def create_session(
    backend: str,
    cfg: FetcherConfig,
    cookies: dict[str, str] | None = None,
    **kwargs: Any,
) -> BaseSession:
    """
    Factory method to create a session backend instance.

    Available backends:
      * aiohttp
      * httpx
      * curl_cffi

    The backend's library is imported lazily, so importing this package
    never requires all three to be installed.  When the requested backend's
    package is missing the factory raises a :class:`RuntimeError` naming the
    backends that are available instead — there is no silent fallback,
    because substituting e.g. httpx for curl_cffi would silently drop the
    browser impersonation the caller asked for.

    :param backend: Backend name (see :func:`available_backends`).
    :param cfg: Session configuration.
    :param cookies: Optional initial cookies to set on the session.
    :raises ValueError: If *backend* is not a known backend name.
    :raises RuntimeError: If *backend*'s package is not installed.
    """
    try:
        session_cls = _resolve_backend(backend)
    except ImportError as exc:
        installed = ", ".join(available_backends()) or "none"
        raise RuntimeError(
            f"HTTP backend {backend!r} is not available in this environment "
            f"({exc}). Available backends: {installed}."
        ) from exc
    return session_cls(cfg, cookies, **kwargs)
