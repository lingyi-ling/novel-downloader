#!/usr/bin/env python3
"""
novel_downloader.plugins.utils.site_probe
-----------------------------------------

Lightweight, side-effect-free network health probes for rule-based sources.

``sources list --check`` is a purely offline structural check (can the
fetcher/parser/searcher be constructed?).  It must stay that way: CI and
offline users rely on it returning immediately.  This module backs the opt-in
``sources list --probe`` flag instead, which adds a reachability/latency
measurement on top of the offline check.

Design notes
------------

* **No global state.**  :func:`probe_site` and :func:`probe_sites` are pure
  coroutines; nothing is cached or mutated at module level.
* **Order preserving.**  :func:`probe_sites` fans out with a
  :class:`asyncio.Semaphore` but writes each result into a pre-allocated slot
  keyed by the input index, so the returned list always matches the input
  order (unlike a "collect results as they finish" list).
* **Project transport.**  Sessions are built through
  :func:`novel_downloader.infra.sessions.create_session` with a
  :class:`~novel_downloader.schemas.FetcherConfig`, so headers, proxy, TLS and
  timeout conventions stay the ones every fetcher already uses.
* **Safe failures.**  A failed probe never raises: it returns a
  :class:`ProbeResult` with ``ok=False``.  Error text and the reported URL are
  scrubbed -- only ``scheme://host[:port]`` is kept, so a ``?token=...`` query
  string (or an ``Authorization``/``Cookie`` header quoted in an exception)
  never leaks into the table or the JSON output.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import aiohttp

__all__ = [
    "ProbeResult",
    "probe_site",
    "probe_sites",
    "sort_by_latency",
]

#: HTTP statuses some servers return for ``HEAD`` when the method is not
#: implemented; the probe retries with ``GET`` in that case.
_HEAD_FALLBACK_STATUS: frozenset[int] = frozenset({405, 501})

#: A probe shorter than this is meaningless (and would make the session's own
#: total timeout fire before our ``wait_for``); values below it are clamped.
_MIN_TIMEOUT = 0.05

#: Upper bound for a probe timeout, so a non-finite value (``inf``) or a typo
#: cannot turn a CLI invocation into an unbounded wait.
_MAX_TIMEOUT = 60.0

#: Timeout used when the caller passes something that is not a number.
_DEFAULT_TIMEOUT = 3.0

#: Error text is truncated to this many characters before being reported.
_MAX_ERROR_LEN = 200

#: Any absolute URL embedded in a backend exception message.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

#: ``Authorization: <scheme> <credential>`` (scheme optional) quoted in an
#: exception.  The optional scheme group is what keeps ``Basic dXNlcjpwYXNz``
#: from leaving the base64 credential behind.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\b\s*[:=]\s*"
    r"(?:(?:basic|bearer|digest|token|negotiate|ntlm)\s+)?\S+"
)

#: ``Bearer <token>`` values that are not preceded by a header name.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]+=*")

#: ``Cookie: a=b; c=d`` / ``Set-Cookie: ...`` values.  The repeated group
#: consumes further ``name=value`` pairs but stops before something that is
#: not a cookie pair (e.g. ``; Authorization: Bearer ...``), so redacting a
#: cookie never swallows an unrelated header and vice versa.
_COOKIE_HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie)\b\s*[:=]\s*[^\r\n;]+"
    r"(?:;\s*[^;\r\n=:]+?\s*=\s*[^\r\n;]*)*"
)

#: Token-like query parameters (a defence in depth: URLs are already reduced).
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_?key|key|sign|signature|password|"
    r"passwd|pwd|secret)=)[^&\s'\"]+"
)

#: Standalone ``name=value`` pairs whose name looks like a credential.  This
#: catches the second and later pairs of a cookie header as well as secrets
#: logged outside any header.
_SECRET_PAIR_RE = re.compile(
    r"(?i)\b("
    r"[A-Za-z0-9_.\-]*"
    r"(?:token|secret|password|passwd|pwd|api_?key|auth|session)"
    r"[A-Za-z0-9_.\-]*"
    r")\s*=\s*[^&\s;'\"]+"
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a single site.

    :param site_key: Source key (or host when probed from a bare URL).
    :param url: Probe target, reduced to ``scheme://host[:port]``.  It is
        always the *original* target, never the URL a redirect landed on.
    :param ok: Whether an HTTP response below 400 was received.
    :param status: Final HTTP status, or ``None`` when no response arrived.
    :param latency_ms: Round-trip time in milliseconds; ``None`` on transport
        failure.  A response that arrived with a 4xx/5xx still has a latency,
        so ``ok=False`` with a non-``None`` latency is expected (the site is
        reachable but refuses the probe).
    :param error: Sanitized failure reason, or ``None`` when the probe
        produced an HTTP status.
    """

    site_key: str
    url: str
    ok: bool
    status: int | None
    latency_ms: float | None
    error: str | None


class _Response(Protocol):
    @property
    def status(self) -> int: ...


class _ResponseContext(Protocol):
    async def __aenter__(self) -> _Response: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


class _HttpClient(Protocol):
    """The subset of an aiohttp-style client the probe needs."""

    def request(self, method: str, url: str, **kwargs: Any) -> _ResponseContext: ...


class _SessionView(Protocol):
    """The project session wrapper as seen by the probe.

    ``session`` is the per-backend raw client accessor; see
    :func:`_make_session` for why the probe is pinned to aiohttp.
    """

    @property
    def session(self) -> _HttpClient: ...

    async def init(self) -> None: ...

    async def close(self) -> None: ...


def _display_url(url: str) -> str:
    """
    Reduce *url* to ``scheme://host[:port]`` for reporting.

    Credentials, path, query and fragment are dropped, so a signed URL never
    puts its token into a table cell or a JSON document.

    :param url: Raw probe target.
    :return: Safe display URL, or ``"<invalid>"`` when it is not HTTP(S).
    """
    try:
        parts = urlsplit(str(url).strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return "<invalid>"

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        return "<invalid>"

    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _site_key(url: str) -> str:
    """Return the host of *url* (used when no source key is supplied)."""
    try:
        hostname = urlsplit(str(url).strip()).hostname
    except ValueError:
        return "-"
    return hostname or "-"


def _error_text(exc: BaseException, raw_url: str) -> str:
    """
    Render *exc* as a short, sanitized error string.

    The raw URL is replaced by its display form first; any other absolute URL
    in the message is reduced the same way.  An embedded URL pointing at a
    host other than the probe target (typically the destination of a redirect)
    is replaced by ``<url>`` so an internal address never leaks.  Credentials,
    ``Bearer`` tokens, ``Authorization``/``Cookie`` headers and token-like
    query parameters are then redacted, and the result is truncated without
    cutting a ``***`` marker in half.

    :param exc: Exception raised by the transport.
    :param raw_url: The exact URL string passed to the request.
    :return: At most :data:`_MAX_ERROR_LEN` characters, never raising.
    """
    try:
        detail = f"{type(exc).__name__}: {exc}".strip()
    except Exception:  # a broken __str__ must not break the probe
        detail = type(exc).__name__

    target_display = _display_url(raw_url)

    def scrub_url(match: re.Match[str]) -> str:
        shown = _display_url(match.group(0))
        return shown if shown == target_display else "<url>"

    detail = detail.replace(str(raw_url), target_display)
    detail = _URL_RE.sub(scrub_url, detail)
    # Cookies are redacted before Authorization: once ``Authorization: Basic``
    # becomes ``Authorization=***`` it looks like a cookie pair and would be
    # swallowed by the Cookie pattern's "further name=value pairs" group.
    detail = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}=***", detail)
    detail = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}=***", detail)
    detail = _BEARER_RE.sub(lambda m: f"{m.group(1)}=***", detail)
    detail = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}***", detail)
    detail = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}=***", detail)
    detail = " ".join(detail.split())
    if len(detail) > _MAX_ERROR_LEN:
        # Never leave a partially-consumed ``***`` marker; an ellipsis makes
        # the truncation explicit instead.
        detail = detail[: _MAX_ERROR_LEN - 3].rstrip("*") + "..."
    return detail


def _normalize_timeout(timeout: float) -> float:
    """
    Coerce *timeout* to a finite value inside ``[_MIN_TIMEOUT, _MAX_TIMEOUT]``.

    :param timeout: Caller-supplied per-site timeout in seconds.
    :return: A usable timeout; non-numeric input yields the default.
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    if not math.isfinite(value):
        return _MAX_TIMEOUT if value > 0 else _MIN_TIMEOUT
    return min(max(value, _MIN_TIMEOUT), _MAX_TIMEOUT)


def _normalize_concurrency(concurrency: int) -> int:
    """
    Coerce *concurrency* to a positive integer.

    A zero or negative value would build ``asyncio.Semaphore(0)`` and
    deadlock every worker, so it falls back to 1 instead of propagating.

    :param concurrency: Requested number of simultaneous probes.
    :return: An integer ``>= 1``.
    """
    try:
        value = int(concurrency)
    except (TypeError, ValueError, OverflowError):
        return 1
    return max(1, value)


def _make_session(timeout: float) -> _SessionView:
    """
    Build a project HTTP session for probing.

    The probe needs ``HEAD`` plus a body-less ``GET``; the backend-neutral
    :class:`~novel_downloader.infra.sessions.base.BaseSession` facade only
    exposes ``get``/``post``, so the raw client is reached through the
    documented ``.session`` property every backend implements (typed return
    value, ``RuntimeError`` when uninitialised).  That accessor is public but
    not declared on ``BaseSession`` because the three backends return
    unrelated types, hence the local :class:`_SessionView` protocol.

    The backend is pinned to ``aiohttp`` deliberately:

    * aiohttp is a core dependency of this project, so the probe cannot fail
      merely because an optional backend is missing;
    * ``HEAD`` with ``allow_redirects`` is aiohttp's per-request API -- httpx
      spells the option ``follow_redirects`` and returns a different response
      object, so picking whichever backend was configured would make probe
      behaviour silently backend-dependent.

    A backend-neutral ``BaseSession.head()`` would remove this coupling; it
    requires touching all three backends and is intentionally out of scope
    here.

    :param timeout: Per-probe timeout in seconds.
    :return: A session wrapper exposing the raw client via ``.session``.
    """
    from novel_downloader.infra.sessions import create_session
    from novel_downloader.schemas import FetcherConfig

    # ``retry_times=0`` keeps one probe to one request pair -- the caller's
    # timeout is the single bound, and no retry budget hides a dead site.
    cfg = FetcherConfig(timeout=timeout, retry_times=0, request_interval=0.0)
    return cast(_SessionView, create_session("aiohttp", cfg))


async def _request_status(client: _HttpClient, method: str, url: str) -> int:
    """
    Send *method* and return the response status without reading its body.

    Entering/exiting the response context releases the connection; the body is
    never consumed, so a large page costs no download.

    :param client: Raw HTTP client (aiohttp ``ClientSession``).
    :param method: ``"HEAD"`` or ``"GET"``.
    :param url: Absolute HTTP(S) URL.
    :return: HTTP status code.
    """
    async with client.request(method, url, allow_redirects=True) as response:
        return int(response.status)


async def _fetch_status(client: _HttpClient, url: str) -> int:
    """
    Probe *url* with ``HEAD``, falling back to ``GET`` when needed.

    A ``405``/``501`` answer means the origin does not implement ``HEAD``; any
    transport error is treated the same way and retried with ``GET`` so a
    server that rejects ``HEAD`` but serves pages still reports its status.
    When ``GET`` fails too, its exception propagates and the caller reports it.

    :param client: Raw HTTP client.
    :param url: Absolute HTTP(S) URL.
    :return: Final HTTP status code.
    """
    try:
        status = await _request_status(client, "HEAD", url)
    except Exception:
        status = None

    if status is not None and status not in _HEAD_FALLBACK_STATUS:
        return status
    return await _request_status(client, "GET", url)


async def _probe_one(
    client: _HttpClient,
    url: str,
    timeout: float,
) -> ProbeResult:
    """Probe a single URL and never raise; see :func:`probe_site`."""
    target = str(url).strip()
    display = _display_url(target)
    site_key = _site_key(target)
    started = time.perf_counter()

    try:
        # ``wait_for`` bounds the *whole* probe (HEAD and a possible GET
        # fallback), not just connection establishment.
        status = await asyncio.wait_for(_fetch_status(client, target), timeout)
    except TimeoutError:
        return ProbeResult(
            site_key=site_key,
            url=display,
            ok=False,
            status=None,
            latency_ms=None,
            error="timeout",
        )
    except Exception as exc:
        return ProbeResult(
            site_key=site_key,
            url=display,
            ok=False,
            status=None,
            latency_ms=None,
            error=_error_text(exc, target),
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return ProbeResult(
        site_key=site_key,
        url=display,
        ok=status < 400,
        status=status,
        latency_ms=elapsed_ms,
        error=None,
    )


async def _close_quietly(wrapper: _SessionView | None) -> None:
    """Close *wrapper* without letting a shutdown error mask the result."""
    if wrapper is None:
        return
    with contextlib.suppress(Exception):
        await wrapper.close()


async def probe_site(
    url: str,
    *,
    timeout: float = 3.0,
    session: aiohttp.ClientSession | None = None,
) -> ProbeResult:
    """
    Probe one site and report reachability and round-trip latency.

    The probe sends ``HEAD`` first and retries with a body-less ``GET`` when
    the origin answers ``405``/``501`` or the ``HEAD`` request fails.  Failures
    are reported, never raised.

    :param url: Absolute HTTP(S) URL to probe.
    :param timeout: Overall per-site timeout in seconds, clamped to
        ``[0.05, 60]``.
    :param session: Optional existing aiohttp client to reuse; when omitted a
        short-lived project session is created and closed around this probe.
    :return: A :class:`ProbeResult` whose ``site_key`` is the URL's host.
    """
    limit = _normalize_timeout(timeout)

    if session is not None:
        return await _probe_one(cast(_HttpClient, session), url, limit)

    wrapper: _SessionView | None = None
    try:
        wrapper = _make_session(limit)
        await wrapper.init()
        client = wrapper.session
    except Exception as exc:  # backend missing/unusable: report, never raise
        # ``init`` can fail after allocating resources, so the wrapper (when
        # it exists) is still closed on this path.
        await _close_quietly(wrapper)
        return ProbeResult(
            site_key=_site_key(url),
            url=_display_url(url),
            ok=False,
            status=None,
            latency_ms=None,
            error=_error_text(exc, str(url)),
        )

    try:
        return await _probe_one(client, url, limit)
    finally:
        await _close_quietly(wrapper)


async def probe_sites(
    items: Sequence[tuple[str, str]],
    *,
    timeout: float = 3.0,
    concurrency: int = 8,
) -> list[ProbeResult]:
    """
    Probe several ``(site_key, url)`` pairs concurrently.

    At most ``concurrency`` probes are in flight at once (values below 1 are
    clamped to 1 rather than deadlocking on ``Semaphore(0)``).  Results are
    returned in **input order** regardless of completion order (each worker
    writes to the slot of its input index), which keeps CLI tables
    reproducible.  A failure to build the HTTP session marks every item as
    failed instead of raising.

    :param items: ``(site_key, url)`` pairs, in display order.
    :param timeout: Per-site timeout in seconds, clamped to ``[0.05, 60]``.
    :param concurrency: Maximum simultaneous probes (values below 1 mean 1).
    :return: One :class:`ProbeResult` per input item, same order.
    """
    entries = [(str(key), str(url)) for key, url in items]
    if not entries:
        return []

    limit = _normalize_timeout(timeout)
    wrapper: _SessionView | None = None
    try:
        wrapper = _make_session(limit)
        await wrapper.init()
        client = wrapper.session
    except Exception as exc:
        await _close_quietly(wrapper)
        return [
            ProbeResult(
                site_key=key,
                url=_display_url(url),
                ok=False,
                status=None,
                latency_ms=None,
                error=_error_text(exc, url),
            )
            for key, url in entries
        ]

    semaphore = asyncio.Semaphore(_normalize_concurrency(concurrency))
    slots: list[ProbeResult | None] = [None] * len(entries)

    async def worker(index: int, key: str, url: str) -> None:
        async with semaphore:
            result = await _probe_one(client, url, limit)
        slots[index] = replace(result, site_key=key)

    try:
        # ``return_exceptions`` keeps one unexpected worker failure from
        # cancelling the others or skipping the session close; any slot it
        # left empty is filled below so the output stays index-aligned.
        await asyncio.gather(
            *(worker(index, key, url) for index, (key, url) in enumerate(entries)),
            return_exceptions=True,
        )
    finally:
        await _close_quietly(wrapper)

    results: list[ProbeResult] = []
    for index, slot in enumerate(slots):
        if slot is None:
            key, url = entries[index]
            slot = ProbeResult(
                site_key=key,
                url=_display_url(url),
                ok=False,
                status=None,
                latency_ms=None,
                error="probe failed",
            )
        results.append(slot)
    return results


def sort_by_latency(results: Sequence[ProbeResult]) -> list[ProbeResult]:
    """
    Sort probe results by ascending latency, unreachable ones last.

    The sort is stable: entries with the same latency (including every
    unreachable one) keep their relative input order, so a tie still reads
    alphabetically when the caller sorted by ``site_key`` first.

    :param results: Results to order.
    :return: New list, nearest first and unreachable last.
    """

    def order(result: ProbeResult) -> tuple[int, float]:
        if result.ok and result.latency_ms is not None:
            return (0, result.latency_ms)
        return (1, 0.0)

    return sorted(results, key=order)
