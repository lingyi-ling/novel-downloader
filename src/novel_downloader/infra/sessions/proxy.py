#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.proxy
-------------------------------------

Backend-neutral proxy parsing, environment resolution and ``NO_PROXY``
matching.

The three backends disagree about almost everything proxy-related:

* aiohttp ignores ``ALL_PROXY`` and silently drops ``https://`` proxies;
* httpx and aiohttp read the Windows registry through
  ``urllib.request.getproxies()`` while libcurl only reads the environment;
* libcurl echoes a proxy URL in some error messages, plaintext password
  included;
* credentials embedded in the proxy URL and the separate
  ``proxy_user``/``proxy_pass`` settings have different precedence per
  library.

Resolving the proxy once, in the facade, removes all of that: every backend
receives the same credential-free URL plus an explicit ``(user, password)``
pair, and the environment is read with one documented rule set instead of
three.

Rules
-----
* An explicit ``proxy`` setting always wins; ``proxy_user``/``proxy_pass``
  override credentials embedded in the URL.  ``trust_env`` is then ignored,
  including ``NO_PROXY`` (a proxy the user configured explicitly is not
  something an environment variable should silently bypass).
* Otherwise, with ``trust_env=True``: ``http_proxy``/``HTTP_PROXY`` for
  ``http`` targets, ``https_proxy``/``HTTPS_PROXY`` for ``https`` targets,
  and ``all_proxy``/``ALL_PROXY`` as the fallback for both.  Lower case wins
  over upper case, matching curl.  ``no_proxy``/``NO_PROXY`` is a
  comma-separated list of hosts (see :func:`should_bypass_proxy`).
* The Windows registry / macOS system configuration is never consulted: the
  setting is called ``trust_env`` and the backends must agree.
* A proxy URL without a scheme gets ``http://``.  Blank values are ignored.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = [
    "NoProxyEntry",
    "ProxyConfig",
    "parse_no_proxy",
    "parse_proxy",
    "proxy_from_env",
    "redact_secrets",
    "should_bypass_proxy",
    "split_proxy_settings",
]

#: Proxy schemes each backend may be asked to speak.
HTTP_PROXY_SCHEMES: frozenset[str] = frozenset({"http", "https"})
SOCKS_PROXY_SCHEMES: frozenset[str] = frozenset(
    {"socks4", "socks4a", "socks5", "socks5h"}
)
ALL_PROXY_SCHEMES: frozenset[str] = HTTP_PROXY_SCHEMES | SOCKS_PROXY_SCHEMES


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme)


@dataclass(frozen=True)
class ProxyConfig:
    """
    A parsed proxy endpoint.

    :attr:`url` never contains credentials: they live in :attr:`username` and
    :attr:`password` so a library cannot copy them into an error message, and
    so the same endpoint can be handed to all three backends in the form each
    one expects.
    """

    url: str
    scheme: str
    host: str
    port: int | None = None
    username: str | None = None
    password: str | None = None

    @property
    def auth(self) -> tuple[str, str] | None:
        """``(username, password)`` or ``None`` when the proxy is open."""
        if self.username is None:
            return None
        return (self.username, self.password or "")

    @property
    def is_socks(self) -> bool:
        """Whether this is a SOCKS endpoint (unsupported by aiohttp)."""
        return self.scheme in SOCKS_PROXY_SCHEMES

    def secret_values(self) -> tuple[str, ...]:
        """Strings that must never appear in a log line or exception."""
        if not self.password:
            return ()
        values = [self.password]
        if self.username:
            values.append(f"{self.username}:{self.password}")
        return tuple(values)

    def __repr__(self) -> str:
        # Dataclass reprs include every field, password included.  Sessions
        # and config dumps get logged, so the password column is replaced
        # with a boolean marker.
        return (
            f"ProxyConfig(url={self.url!r}, scheme={self.scheme!r}, "
            f"host={self.host!r}, port={self.port!r}, "
            f"authenticated={self.username is not None!r})"
        )


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """
    Remove proxy credentials from *text*.

    Two passes: the ``scheme://user:password@`` prefix is masked first (that
    is what libcurl echoes), then any known password value is replaced
    wholesale, in case a library reformatted the URL.

    :param text: Arbitrary text, usually an exception message.
    :param secrets: Password fragments collected from :class:`ProxyConfig`.
    :return: The text with credentials replaced by ``***``.
    """
    start = text.find("://")
    while start != -1:
        authority = start + 3
        at = text.find("@", authority)
        slash = text.find("/", authority)
        if at != -1 and (slash == -1 or at < slash):
            text = f"{text[:authority]}***@{text[at + 1 :]}"
        start = text.find("://", start + 3)

    # Longest first so ``user:password`` is masked before ``password``
    # leaves a stray ``user:`` behind.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def parse_proxy(
    raw: str | None,
    username: str | None = None,
    password: str | None = None,
) -> ProxyConfig | None:
    """
    Parse a proxy setting into a credential-free :class:`ProxyConfig`.

    The URL is normalised to ``scheme://host:port``: a missing scheme becomes
    ``http``, a missing port becomes the scheme default and any path or
    fragment is dropped (proxies are addressed by host and port; a trailing
    ``/`` from a config field must not change the value each backend sees).

    :param raw: Proxy URL or ``host:port``; blank means "no proxy".
    :param username: Explicit user name, overriding any embedded one.
    :param password: Explicit password; only used together with *username*.
    :return: The parsed proxy, or ``None`` for a blank setting.
    :raises ValueError: If the URL has no host or an unusable port.
    """
    value = (raw or "").strip()
    if not value:
        return None

    if "://" not in value:
        # ``host:port`` is what people paste from a browser or a shell
        # export; every backend would otherwise reject it for lacking a
        # scheme, each with its own error.
        value = f"http://{value}"

    try:
        parts = urlsplit(value)
    except ValueError as exc:  # pragma: no cover - urlsplit rarely raises
        raise ValueError(f"Invalid proxy URL: {redact_secrets(value)}") from exc

    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        raise ValueError(f"Invalid proxy URL: {redact_secrets(value)}")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid proxy port in {redact_secrets(value)}") from exc
    if port is None:
        port = _default_port(scheme)

    embedded_user = unquote(parts.username) if parts.username else None
    embedded_pass = unquote(parts.password) if parts.password else None

    wanted_user = (username or "").strip() or None
    if wanted_user is not None:
        # Explicit settings win.  A user name without a password is a valid
        # proxy configuration (empty password), and was previously dropped
        # entirely because both halves had to be truthy.
        final_user: str | None = wanted_user
        final_pass: str | None = password if password is not None else ""
    else:
        final_user = embedded_user
        final_pass = embedded_pass
        if final_user is not None and final_pass is None:
            final_pass = ""

    host_text = f"[{host}]" if ":" in host else host
    url = f"{scheme}://{host_text}"
    if port is not None:
        url = f"{url}:{port}"

    return ProxyConfig(
        url=url,
        scheme=scheme,
        host=host.lower(),
        port=port,
        username=final_user,
        password=final_pass,
    )


@dataclass(frozen=True)
class NoProxyEntry:
    """One ``NO_PROXY`` item.

    :param host: Host name or IP literal; ``None`` only for ``*``.
    :param port: Port the entry is restricted to, if it named one.
    :param subdomains_only: The entry started with a dot (``.example.com``),
        which excludes the apex host.
    :param wildcard: The entry was ``*``: bypass the proxy for everything.
    :param is_ip: The entry is an IP literal, so only an exact match counts.
    """

    host: str | None
    port: int | None = None
    subdomains_only: bool = False
    wildcard: bool = False
    is_ip: bool = False


def _strip_entry_scheme(value: str) -> str:
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0]


def parse_no_proxy(raw: str | None) -> tuple[NoProxyEntry, ...]:
    """
    Parse a ``NO_PROXY`` value.

    :param raw: Comma-separated hosts, e.g. ``localhost,.corp.example,10.0.0.1``.
    :return: Parsed entries; malformed items are skipped.
    """
    entries: list[NoProxyEntry] = []
    for item in (raw or "").split(","):
        value = _strip_entry_scheme(item.strip())
        if not value:
            continue
        if value == "*":
            entries.append(NoProxyEntry(host=None, wildcard=True))
            continue

        subdomains_only = value.startswith(".")
        if subdomains_only:
            value = value.lstrip(".")

        port: int | None = None
        host: str
        if value.startswith("["):
            closing = value.find("]")
            if closing == -1:
                continue
            host = value[1:closing]
            rest = value[closing + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
        elif value.count(":") == 1:
            host, _, port_text = value.partition(":")
            if port_text.isdigit():
                port = int(port_text)
            elif port_text:
                continue
        else:
            host = value

        host = host.strip().lower()
        if not host:
            continue

        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False

        entries.append(
            NoProxyEntry(
                host=host,
                port=port,
                subdomains_only=subdomains_only,
                is_ip=is_ip,
            )
        )
    return tuple(entries)


def should_bypass_proxy(
    entries: Iterable[NoProxyEntry],
    host: str,
    port: int | None = None,
) -> bool:
    """
    Whether *host* is excluded from proxying by ``NO_PROXY``.

    Matching: ``*`` bypasses everything; an IP literal matches exactly; a
    bare domain matches itself and its subdomains; a dot-prefixed domain
    matches subdomains only; an entry with a port matches only that port.

    :param entries: Parsed ``NO_PROXY`` entries.
    :param host: Request host (no brackets, no port).
    :param port: Request port, when known.
    :return: True when the request must go direct.
    """
    target = (host or "").strip().lower().strip("[]")
    for entry in entries:
        if entry.wildcard:
            return True
        if entry.host is None:
            continue
        if entry.port is not None and port is not None and entry.port != port:
            continue
        if entry.is_ip:
            if target == entry.host:
                return True
            continue
        if entry.subdomains_only:
            if target.endswith(f".{entry.host}"):
                return True
        elif target == entry.host or target.endswith(f".{entry.host}"):
            return True
    return False


def no_proxy_patterns(entries: Iterable[NoProxyEntry]) -> tuple[str, ...]:
    """
    Express ``NO_PROXY`` entries as httpx mount patterns.

    httpx matches mounts with :class:`httpx._utils.URLPattern`.  ``*.host``
    matches subdomains only, a bare ``host`` matches exactly, so a bare
    ``NO_PROXY`` domain needs both patterns to reproduce
    :func:`should_bypass_proxy`.  Callers prefix the strings with ``all://``.

    :param entries: Parsed ``NO_PROXY`` entries.
    :return: Patterns without the scheme prefix (``"*"`` for a wildcard).
    """
    patterns: list[str] = []
    for entry in entries:
        if entry.wildcard:
            return ("*",)
        if entry.host is None:
            continue
        host = f"[{entry.host}]" if ":" in entry.host else entry.host
        with_port = f"{host}:{entry.port}" if entry.port is not None else host
        if entry.subdomains_only:
            patterns.append(f"*.{with_port}")
        elif entry.is_ip:
            patterns.append(with_port)
        else:
            patterns.extend((with_port, f"*.{with_port}"))
    return tuple(dict.fromkeys(patterns))


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """Read an environment variable case-insensitively, lower case first."""
    for key in (name.lower(), name.upper()):
        value = env.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def proxy_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[ProxyConfig | None, ProxyConfig | None, tuple[NoProxyEntry, ...]]:
    """
    Resolve proxy settings from the environment.

    :param env: Environment mapping; defaults to :data:`os.environ`.
    :return: ``(http_proxy, https_proxy, no_proxy_entries)``.
    :raises ValueError: If a configured proxy URL is unusable.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    all_proxy = _env_value(source, "all_proxy")
    http_proxy = _env_value(source, "http_proxy") or all_proxy
    https_proxy = _env_value(source, "https_proxy") or all_proxy
    entries = parse_no_proxy(_env_value(source, "no_proxy"))
    return (
        parse_proxy(http_proxy),
        parse_proxy(https_proxy),
        entries,
    )


def split_proxy_settings(
    proxy: str | None,
    proxy_user: str | None,
    proxy_pass: str | None,
    trust_env: bool,
) -> tuple[ProxyConfig | None, ProxyConfig | None, tuple[NoProxyEntry, ...]]:
    """
    Resolve the facade's proxy settings for both request schemes.

    :param proxy: Explicit proxy URL from the config.
    :param proxy_user: Explicit proxy user.
    :param proxy_pass: Explicit proxy password.
    :param trust_env: Whether to fall back to the environment.
    :return: ``(http_proxy, https_proxy, no_proxy_entries)``.
    :raises ValueError: If a proxy URL is unusable.
    """
    explicit = parse_proxy(proxy, proxy_user, proxy_pass)
    if explicit is not None:
        return explicit, explicit, ()
    if trust_env:
        return proxy_from_env()
    return None, None, ()
