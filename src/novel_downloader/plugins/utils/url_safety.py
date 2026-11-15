#!/usr/bin/env python3
"""
novel_downloader.plugins.utils.url_safety
-----------------------------------------

Shared URL allow-list used before a URL reaches an HTTP backend.

The rule engine receives URLs from three untrusted places: a rule JSON file, a
rendered template (``book_id`` / ``chapter_id`` may come from page data) and a
scraped ``href``/``src``.  Every backend treats a URL differently -- curl will
happily read ``file:///`` and follow an ``ftp:`` redirect, aiohttp/httpx refuse
those but accept ``user:pass@host`` and send Basic auth -- so the decision has
to be made once, here, instead of relying on whichever backend is configured.

Policy:

* only ``http`` / ``https`` (a scheme-less value is accepted only when the
  caller explicitly allows relative targets, e.g. a chapter link resolved
  later against ``base_url``);
* an absolute URL needs a host and a valid port;
* userinfo (``user:pass@``) is refused: it is never needed by a book source and
  would silently become an ``Authorization`` header;
* control characters (C0 and DEL) and backslashes in the authority are
  refused: parsers disagree about them, and curl passes some through verbatim;
* the length is bounded so a hostile link cannot bloat every log line.

Private / loopback addresses are deliberately **not** refused here.  A rule is
local configuration and self-hosted or LAN book sources are a legitimate use
case; the caller that consumes untrusted *remote* rules applies a stricter
public-only policy (see :mod:`novel_downloader.plugins.rule.remote`).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = [
    "MAX_FETCH_URL_LENGTH",
    "has_url_control_chars",
    "is_http_fetch_url",
]

#: Hard ceiling on a URL handed to the HTTP client (or echoed into a log).
MAX_FETCH_URL_LENGTH = 8192

#: C0 controls and DEL.  Never valid URL syntax; curl forwards some of them.
_URL_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def has_url_control_chars(url: str) -> bool:
    """Whether *url* contains a C0 control character or DEL."""
    return _URL_CTRL_RE.search(url) is not None


def is_http_fetch_url(url: str, *, allow_relative: bool = False) -> bool:
    """
    Whether *url* is a safe target for the HTTP client.

    :param url: Candidate URL (already rendered/resolved by the caller).
    :param allow_relative: Accept a scheme-less value.  A relative path is
        meaningful only when a base URL fills it in later, so this is opt-in.
    :return: ``True`` for an allowed target, ``False`` for anything else.
        The function never raises, so one hostile link cannot abort a parse.
    """
    if not isinstance(url, str):
        return False
    value = url.strip()
    if not value or len(value) > MAX_FETCH_URL_LENGTH:
        return False
    if has_url_control_chars(value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        # A malformed IPv6 literal (``http://[::1/x``) raises here.
        return False

    scheme = parts.scheme.lower()
    if not scheme:
        if not allow_relative:
            return False
        # Protocol-relative (``//host/path``) carries an authority, so the
        # userinfo/backslash ambiguity checks apply to it as well.
        return "\\" not in parts.netloc and not (
            parts.username is not None or parts.password is not None
        )
    if scheme not in ("http", "https"):
        return False
    if not parts.netloc or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if "\\" in parts.netloc:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return port is None or 1 <= port <= 65535
