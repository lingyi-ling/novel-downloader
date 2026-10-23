#!/usr/bin/env python3
"""
novel_downloader.apps.api.auth
------------------------------

Bearer-token / API-key authentication for the local REST API.

The token is configured on ``app.state.api_token`` by :func:`create_app`. When
it is empty (the default), no authentication is performed; this is acceptable
only because the server binds to localhost by default.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


def _provided_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract a token from either supported header."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def _token_matches(provided: str, expected: str) -> bool:
    """
    Compare two tokens in constant time.

    ``secrets.compare_digest`` only accepts ASCII-only ``str`` values and raises
    :class:`TypeError` otherwise, so both sides are encoded to UTF-8 bytes
    first. That keeps the comparison constant-time and also supports tokens
    containing non-ASCII characters.
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """
    Enforce ``Authorization: Bearer <token>`` or ``X-API-Key: <token>``.

    :raises HTTPException: 401 when a token is configured and the request does
        not carry a matching one.
    """
    expected = getattr(request.app.state, "api_token", None)
    if not expected:
        return

    provided = _provided_token(authorization, x_api_key)
    if provided is None or not _token_matches(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
