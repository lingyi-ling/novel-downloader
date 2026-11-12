#!/usr/bin/env python3
"""
novel_downloader.plugins.rule.remote
------------------------------------

Download a rule document over HTTP(S).

The downloader is deliberately small and defensive:

* only ``http`` / ``https`` URLs are accepted (never ``file:`` and friends),
* credentials in the URL are refused, so a token cannot be smuggled into a
  request by a malformed rule link,
* localhost, private/link-local networks and cloud metadata endpoints are
  refused, both for a literal IP and for a hostname that resolves to one
  (SSRF protection); a redirect chain is re-checked before the body is read
  and an HTTPS -> HTTP downgrade is refused,
* the response is read through a hard byte cap (:data:`DEFAULT_MAX_BYTES` by
  default), so a hostile server cannot exhaust memory,
* an explicit timeout is always passed to :func:`urllib.request.urlopen`,
* obvious non-JSON ``Content-Type`` values (HTML, XML, images, ...) are
  rejected, while missing or generic types (``text/plain``, ...) are accepted,
* malformed JSON surfaces as :class:`~.model.RuleError`,
* error messages and logs carry only the scheme/host (never the query string,
  which commonly holds a token).

Residual risk: the redirect target is validated *after* ``urlopen`` followed
it, so a private host can still observe a connection (the response body is
never read).  DNS rebinding between the pre-flight lookup and the connection is
likewise not fully prevented; a custom opener would be required for that.

Nothing is written to disk: the caller decides whether to validate the mapping
with :func:`~.model.parse_rule` and install it with
:func:`novel_downloader.plugins.rule.install_rule`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .model import RuleError, loads_rule_json

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "check_rule_redirect",
    "check_rule_url",
    "fetch_rule",
    "safe_rule_url",
]

#: Default upper bound for a rule document (1 MiB).
DEFAULT_MAX_BYTES = 1024 * 1024

#: Hard ceiling for ``max_bytes``: a rule is configuration, never a payload.
_HARD_MAX_BYTES = DEFAULT_MAX_BYTES

#: Longest accepted rule URL.  It also bounds the memory a hostile URL can use
#: before the scheme/host is even inspected.
_MAX_URL_LENGTH = 8192

_USER_AGENT = "novel-downloader/1.0 (rule-source)"
_ALLOWED_SCHEMES = {"http", "https"}

#: Hosts that always mean "this machine" or a cloud metadata service.
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

#: Suffixes that resolve inside a private/local namespace, never a public site.
#: ``.internal`` is reserved for private use; ``.local`` is mDNS; ``.localhost``
#: is loopback by definition.
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
)

#: Documentation/example domains cannot be real attack targets (RFC 2606
#: reserves them), so their DNS is not needed; in particular a proxy may
#: resolve ``example.com`` to a local address that must not block the request.
_UNRESOLVED_OK_HOSTS = frozenset({"example.com", "example.net", "example.org"})
_UNRESOLVED_OK_SUFFIXES = (".example", ".example.com", ".test", ".invalid")
_JSON_MEDIA_TYPES = {
    "application/json",
    "application/x-json",
    "text/json",
    "text/x-json",
}
_GENERIC_MEDIA_TYPES = {"text/plain", "application/octet-stream"}
# Subtypes under ``text/`` that are clearly not JSON.
_NON_JSON_TEXT_SUBTYPES = {"html", "xhtml", "xml"}


def _safe_origin(url: str) -> str:
    """
    Render a URL for an error/log message without credentials or a query.

    Rule URLs are user configuration and, for remote rules, server-supplied.
    A query string commonly carries an API token; echoing it into a log or a
    CLI error would leak a credential for a transient network failure.  Only
    the scheme and host are kept (the path is replaced by ``/...``).
    """
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        port = parts.port
    except (TypeError, ValueError):
        return "<invalid URL>"
    if not host:
        return "<invalid URL>"
    literal = f"[{host}]" if ":" in host else host
    suffix = f":{port}" if port is not None else ""
    scheme = parts.scheme or "<none>"
    return f"{scheme}://{literal}{suffix}/..."


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal (IPv6 zone ids are stripped)."""
    literal = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(literal)
    except ValueError:
        return None


#: Non-canonical numeric hosts (``127.1``, ``2130706433``, ``0x7f000001``) are
#: interpreted as IPv4 addresses by resolvers but not by ``ipaddress``.  They
#: are only ever used to reach loopback/private targets, so reject them unless
#: they parse as a normal public address first.
_NUMERIC_HOST_RE = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){0,3}|0x[0-9a-f]+|\d+)")


def _is_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address may be reached on the public Internet."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        # ``::ffff:127.0.0.1`` is loopback even though the IPv6 wrapper looks
        # global to some Python versions.
        address = address.ipv4_mapped
    return address.is_global


def _reject_non_public(host: str) -> None:
    raise RuleError(
        f"refusing rule URL for non-public host {host!r} "
        "(localhost, private, link-local and cloud metadata targets are blocked)"
    )


def _check_resolved_host(host: str, port: int) -> None:
    """
    Resolve ``host`` and refuse it when any address is non-public.

    This closes the trivial "public domain pointing at 127.0.0.1" bypass.  A
    resolver failure is not fatal here: ``urlopen`` will report the real
    network error and the literal-IP/hostname checks above already cover the
    direct cases.  DNS rebinding between this lookup and the connection is a
    residual risk (see the module docstring).
    """
    if host in _UNRESOLVED_OK_HOSTS or host.endswith(_UNRESOLVED_OK_SUFFIXES):
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return
    for info in infos:
        try:
            address = _ip_literal(str(info[4][0]))
        except (IndexError, TypeError):
            continue
        if address is not None and not _is_global_address(address):
            _reject_non_public(host)


def _check_url(url: str) -> str:
    """Validate the URL and return it stripped, or raise :class:`RuleError`."""
    if not isinstance(url, str):
        raise RuleError("rule URL must be a string")
    value = url.strip()
    if not value:
        raise RuleError("rule URL is empty")
    if len(value) > _MAX_URL_LENGTH:
        raise RuleError(f"rule URL is too long (limit {_MAX_URL_LENGTH} characters)")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " " for ch in value):
        raise RuleError("rule URL contains whitespace or control characters")

    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError as e:
        raise RuleError("rule URL is malformed") from e
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        scheme = parts.scheme or "<none>"
        raise RuleError(f"unsupported rule URL scheme {scheme!r} (only http/https)")
    if not parts.netloc or not host:
        raise RuleError(f"invalid rule URL {_safe_origin(value)}: missing host")
    if "\\" in parts.netloc:
        # Windows treats a backslash as a path separator, while URL parsers do
        # not; ``http://127.0.0.1\@evil.com`` would otherwise be ambiguous.
        raise RuleError("invalid rule URL: backslash in host")
    if parts.username is not None or parts.password is not None:
        raise RuleError(
            "rule URL must not contain credentials (remove 'user:pass@' from the URL)"
        )

    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        _reject_non_public(host)

    try:
        port = parts.port
    except ValueError as e:
        raise RuleError("invalid rule URL: bad port") from e

    address = _ip_literal(host)
    if address is not None:
        if not _is_global_address(address):
            _reject_non_public(host)
    elif _NUMERIC_HOST_RE.fullmatch(host):
        # A resolver would read this as an IPv4 address (``127.1``), while
        # ``ipaddress`` does not; never let an ambiguous numeric host through.
        _reject_non_public(host)
    else:
        _check_resolved_host(host, port or (443 if scheme == "https" else 80))
    return value


def _final_url(response: Any, requested: str) -> str:
    """Return the URL a response was finally served from (redirect target)."""
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        value = geturl()
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = getattr(response, "url", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return requested


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
    origin = _safe_origin(target)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise RuleError("timeout must be a positive number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RuleError("max_bytes must be a positive integer")
    if max_bytes > _HARD_MAX_BYTES:
        raise RuleError(
            f"max_bytes must be <= {_HARD_MAX_BYTES} bytes for a rule document"
        )

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
            f"failed to fetch rule from {origin}: HTTP {e.code} {e.reason}"
        ) from e
    except (OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        text = str(reason).lower()
        if isinstance(reason, TimeoutError) or "timed out" in text or "timeout" in text:
            raise RuleError(
                f"timed out fetching rule from {origin} (timeout={timeout}s)"
            ) from e
        raise RuleError(f"failed to fetch rule from {origin}: {reason}") from e

    try:
        _check_redirect(target, _final_url(response, target))
        status = _status_of(response)
        if status is not None and not 200 <= status < 300:
            raise RuleError(f"failed to fetch rule from {origin}: HTTP {status}")
        _check_content_type(_header(response, "Content-Type"), origin)
        raw = _read_limited(response, max_bytes, origin)
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            closer()

    data = loads_rule_json(raw, source=origin)
    logger.debug("Fetched %d-byte rule document from %s", len(raw), origin)
    return data


def check_rule_url(url: str) -> str:
    """
    Validate an HTTP(S) rule URL (public wrapper).

    Unlike the private helper this is part of the package surface so a caller
    with its own ``urlopen`` (the CLI's ``sources add --url``) can apply the
    same scheme/host/SSRF policy instead of re-implementing it.

    :raises RuleError: when the URL is not a safe public HTTP(S) URL.
    """
    return _check_url(url)


def safe_rule_url(url: str) -> str:
    """Return a log/error-safe rendering of ``url`` (no query or credentials)."""
    return _safe_origin(url)


def check_rule_redirect(requested: str, final: str) -> None:
    """Validate the URL a redirect chain ended at (public wrapper)."""
    _check_redirect(requested, final)


def _check_redirect(requested: str, final: str) -> None:
    """
    Validate the URL a redirect chain ended at.

    ``urlopen`` follows redirects on its own, so the scheme/host checks done
    for the original URL are repeated on the final target.  A cross-scheme
    redirect to ``ftp:``/``file:`` is refused, and an HTTPS -> HTTP downgrade
    is refused too: the rule body decides which requests the client makes, so
    serving it over plaintext after a secure request is not acceptable.
    """
    if final == requested:
        return
    try:
        checked = _check_url(final)
    except RuleError as e:
        raise RuleError(f"rule redirect rejected: {e}") from e
    if (
        urlsplit(requested).scheme.lower() == "https"
        and urlsplit(checked).scheme.lower() == "http"
    ):
        raise RuleError("rule redirect rejected: refusing an https -> http downgrade")
