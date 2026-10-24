#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.remote
------------------------------------

Download a rule document over HTTP(S).

The downloader is deliberately small and defensive:

* only ``http`` / ``https`` URLs are accepted (never ``file:`` and friends),
* the response is read through a hard byte cap (:data:`DEFAULT_MAX_BYTES` by
  default), so a hostile server cannot exhaust memory,
* an explicit timeout is always passed to :func:`urllib.request.urlopen`,
* obvious non-JSON ``Content-Type`` values (HTML, XML, images, ...) are
  rejected, while missing or generic types (``text/plain``, ...) are accepted,
* malformed JSON surfaces as :class:`~.model.RuleError`.

Nothing is written to disk: the caller decides whether to validate the mapping
with :func:`~.model.parse_rule` and install it with
:func:`novel_downloader.plugins.rule.install_rule`.
"""

from __future__ import annotations

import json
import logging
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .model import RuleError

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_MAX_BYTES", "fetch_rule"]

#: Default upper bound for a rule document (1 MiB).
DEFAULT_MAX_BYTES = 1024 * 1024

_USER_AGENT = "novel-downloader/1.0 (rule-source)"
_ALLOWED_SCHEMES = {"http", "https"}
_JSON_MEDIA_TYPES = {
    "application/json",
    "application/x-json",
    "text/json",
    "text/x-json",
}
_GENERIC_MEDIA_TYPES = {"text/plain", "application/octet-stream"}
# Subtypes under ``text/`` that are clearly not JSON.
_NON_JSON_TEXT_SUBTYPES = {"html", "xhtml", "xml"}


def _check_url(url: str) -> str:
    """Validate the URL and return it stripped, or raise :class:`RuleError`."""
    if not isinstance(url, str):
        raise RuleError("rule URL must be a string")
    value = url.strip()
    if not value:
        raise RuleError("rule URL is empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " " for ch in value):
        raise RuleError(f"invalid rule URL {url!r}")

    parts = urlsplit(value)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        scheme = parts.scheme or "<none>"
        raise RuleError(f"unsupported rule URL scheme {scheme!r} (only http/https)")
    if not parts.netloc:
        raise RuleError(f"invalid rule URL {value!r}: missing host")
    return value


def _header(response: Any, name: str) -> str:
    """Read a response header from an ``HTTPResponse`` or a test double."""
    headers = getattr(response, "headers", None)
    if headers is not None:
        get = getattr(headers, "get", None)
        if callable(get):
            value = get(name)
            if value is None:
                value = get(name.title())
            if value is not None:
                return str(value)

    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        value = getheader(name)
        if value is not None:
            return str(value)
    return ""


def _status_of(response: Any) -> int | None:
    """Best-effort HTTP status extraction (``None`` when unavailable)."""
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _check_content_type(content_type: str, url: str) -> None:
    """
    Reject responses that are obviously not JSON, leniently.

    Missing/blank types and generic types such as ``text/plain`` (the type used
    by raw-file hosts) are accepted; ``text/html``, XML, images and binaries are
    refused before the body is parsed.
    """
    if not content_type:
        return

    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type or "/" not in media_type:
        return

    if media_type in _JSON_MEDIA_TYPES or media_type.endswith("+json"):
        return
    if media_type in _GENERIC_MEDIA_TYPES or media_type == "*/*":
        return

    main, _, sub = media_type.partition("/")
    if main == "text" and sub not in _NON_JSON_TEXT_SUBTYPES:
        return
    if main == "application" and "json" in sub:
        return

    raise RuleError(
        f"unexpected Content-Type {media_type!r} for rule from {url}; expected JSON"
    )


def _read_limited(response: Any, max_bytes: int, url: str) -> bytes:
    """Read the body, refusing anything larger than ``max_bytes``."""
    declared = _header(response, "Content-Length").strip()
    if declared:
        try:
            length = int(declared)
        except ValueError:
            length = -1
        if length > max_bytes:
            raise RuleError(
                f"rule response from {url} is too large "
                f"({length} bytes, limit {max_bytes})"
            )

    try:
        raw = response.read(max_bytes + 1)
    except (OSError, HTTPException) as e:
        raise RuleError(f"failed to read rule response from {url}: {e}") from e

    if raw is None:
        raw = b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise RuleError(f"unexpected rule response body from {url}")
    body = bytes(raw)
    if len(body) > max_bytes:
        raise RuleError(
            f"rule response from {url} is too large (limit {max_bytes} bytes)"
        )
    return body


def fetch_rule(
    url: str,
    timeout: float = 15,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """
    Download a JSON rule document and return it as a ``dict``.

    :param url: Absolute ``http`` / ``https`` URL of the rule document.
    :param timeout: Socket timeout in seconds (must be greater than zero).
    :param max_bytes: Maximum accepted body size in bytes.
    :raises RuleError: when the URL is unsupported, the request fails or times
        out, the body is too large / not JSON, or the JSON root is not an object.
    """
    target = _check_url(url)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise RuleError("timeout must be a positive number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RuleError("max_bytes must be a positive integer")

    request = Request(
        target,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.1",
            # Ask for the raw body so the size cap applies to what we parse.
            "Accept-Encoding": "identity",
        },
    )

    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as e:
        raise RuleError(
            f"failed to fetch rule from {target}: HTTP {e.code} {e.reason}"
        ) from e
    except (OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        text = str(reason).lower()
        if isinstance(reason, TimeoutError) or "timed out" in text or "timeout" in text:
            raise RuleError(
                f"timed out fetching rule from {target} (timeout={timeout}s)"
            ) from e
        raise RuleError(f"failed to fetch rule from {target}: {reason}") from e

    try:
        status = _status_of(response)
        if status is not None and not 200 <= status < 300:
            raise RuleError(f"failed to fetch rule from {target}: HTTP {status}")
        _check_content_type(_header(response, "Content-Type"), target)
        raw = _read_limited(response, max_bytes, target)
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            closer()

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise RuleError(f"invalid JSON in rule response from {target}: {e}") from e

    if not isinstance(data, dict):
        raise RuleError(f"rule response from {target} must be a JSON object")
    logger.debug("Fetched %d-byte rule document from %s", len(raw), target)
    return data
