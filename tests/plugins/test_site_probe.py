#!/usr/bin/env python3
"""
Offline tests for :mod:`novel_downloader.plugins.utils.site_probe`.

No test leaves the process: the transport hook (``_request_status``) and the
session factory (``_make_session``) are monkeypatched with in-memory doubles,
so ``HEAD`` success, the ``405``/``501`` fallback, timeouts and DNS-style
connection failures are all simulated deterministically.  The integration
tests use ``aiohttp`` test servers bound to loopback (pytest-aiohttp), never an
external host; every fake domain is under the reserved ``.invalid``/``.example``
TLDs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
import aiohttp.web
import pytest

from novel_downloader.plugins.utils import site_probe as sp
from novel_downloader.plugins.utils.site_probe import (
    ProbeResult,
    probe_site,
    probe_sites,
    sort_by_latency,
)

#: A placeholder transport; ``_request_status`` is monkeypatched, so the
#: client is never actually used.  Typed ``Any`` to satisfy the strict
#: ``aiohttp.ClientSession`` annotation on the public ``session`` parameter.
_DUMMY: Any = object()


class _FakeWrapper:
    """Stand-in for the project session wrapper returned by ``_make_session``."""

    def __init__(self) -> None:
        self.session: Any = object()
        self.inited = False
        self.closed = False
        self.close_calls = 0

    async def init(self) -> None:
        self.inited = True

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class _FailingInitWrapper(_FakeWrapper):
    """A wrapper whose ``init`` raises after (conceptually) allocating state."""

    async def init(self) -> None:
        self.inited = True
        raise RuntimeError("backend refused to initialise")


def _result(
    key: str,
    *,
    ok: bool = True,
    status: int | None = 200,
    latency_ms: float | None = 1.0,
    error: str | None = None,
) -> ProbeResult:
    return ProbeResult(
        site_key=key,
        url=f"https://{key}.example",
        ok=ok,
        status=status,
        latency_ms=latency_ms,
        error=error,
    )


# --------------------------------------------------------------------------- #
# probe_site: HEAD / GET fallback
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_site_head_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_status(client: Any, method: str, url: str) -> int:
        calls.append(method)
        return 200

    monkeypatch.setattr(sp, "_request_status", fake_status)

    result = await probe_site("https://example.invalid/page?q=1", session=_DUMMY)

    assert calls == ["HEAD"]
    assert result.ok is True
    assert result.status == 200
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.error is None
    # The reported URL never carries the path or the query string.
    assert result.url == "https://example.invalid"
    assert result.site_key == "example.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("head_status", [405, 501])
async def test_probe_site_falls_back_to_get(
    monkeypatch: pytest.MonkeyPatch, head_status: int
) -> None:
    calls: list[str] = []

    async def fake_status(client: Any, method: str, url: str) -> int:
        calls.append(method)
        return head_status if method == "HEAD" else 200

    monkeypatch.setattr(sp, "_request_status", fake_status)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    assert calls == ["HEAD", "GET"]
    assert result.ok is True
    assert result.status == 200


@pytest.mark.asyncio
async def test_probe_site_falls_back_to_get_after_head_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_status(client: Any, method: str, url: str) -> int:
        calls.append(method)
        if method == "HEAD":
            raise RuntimeError("HEAD not supported")
        return 204

    monkeypatch.setattr(sp, "_request_status", fake_status)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    assert calls == ["HEAD", "GET"]
    assert result.ok is True
    assert result.status == 204


@pytest.mark.asyncio
async def test_probe_site_get_405_is_reported_not_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_status(client: Any, method: str, url: str) -> int:
        calls.append(method)
        return 405

    monkeypatch.setattr(sp, "_request_status", fake_status)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    # One HEAD, one GET, then report the refusal instead of looping.
    assert calls == ["HEAD", "GET"]
    assert result.ok is False
    assert result.status == 405
    assert result.latency_ms is not None
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_probe_site_error_status_keeps_latency(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def fake_status(client: Any, method: str, url: str) -> int:
        return status

    monkeypatch.setattr(sp, "_request_status", fake_status)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    # Matches ProbeResult's contract: a 4xx/5xx arrived, so it is reachable
    # (latency set) but not "ok".
    assert result.ok is False
    assert result.status == status
    assert result.latency_ms is not None
    assert result.error is None


# --------------------------------------------------------------------------- #
# probe_site: failures never raise
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_site_timeout_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_status(client: Any, method: str, url: str) -> int:
        await asyncio.sleep(5)
        return 200

    monkeypatch.setattr(sp, "_request_status", slow_status)

    result = await probe_site("https://slow.invalid", timeout=0.01, session=_DUMMY)

    assert result.ok is False
    assert result.status is None
    assert result.latency_ms is None
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_probe_site_dns_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dns_failure(client: Any, method: str, url: str) -> int:
        raise OSError("Name or service not known")

    monkeypatch.setattr(sp, "_request_status", dns_failure)

    result = await probe_site("https://missing.invalid/x", session=_DUMMY)

    assert result.ok is False
    assert result.status is None
    assert result.latency_ms is None
    assert result.error is not None
    assert "OSError" in result.error
    assert result.url == "https://missing.invalid"


@pytest.mark.asyncio
async def test_probe_site_non_http_url_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok(client: Any, method: str, url: str) -> int:
        return 200

    monkeypatch.setattr(sp, "_request_status", ok)

    result = await probe_site("file:///etc/passwd", session=_DUMMY)

    assert result.url == "<invalid>"
    assert "passwd" not in result.url


# --------------------------------------------------------------------------- #
# probe_site: scrubbing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_site_redacts_tokens_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://user:pass@example.invalid/private?token=SECRET&api_key=KEY&sig=XYZ"

    async def leaky_failure(client: Any, method: str, url_arg: str) -> int:
        raise RuntimeError(
            f"GET {url_arg} failed; Cookie: session=abc123; "
            "Authorization: Bearer tok-456"
        )

    monkeypatch.setattr(sp, "_request_status", leaky_failure)

    result = await probe_site(url, session=_DUMMY)

    assert result.ok is False
    assert result.url == "https://example.invalid"
    error = result.error or ""
    for secret in ("SECRET", "KEY", "XYZ", "abc123", "tok-456", "user:pass"):
        assert secret not in error, error
    assert "Cookie=***" in error
    assert "Authorization=***" in error


@pytest.mark.asyncio
async def test_probe_site_redacts_basic_auth_and_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def leaky_failure(client: Any, method: str, url: str) -> int:
        raise RuntimeError(
            "Authorization: Basic dXNlcjpwYXNz; "
            "and a bare Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        )

    monkeypatch.setattr(sp, "_request_status", leaky_failure)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    error = result.error or ""
    # Neither the base64 credential nor the JWT survives, and the scheme word
    # does not shelter a second token.
    assert "dXNlcjpwYXNz" not in error
    assert "eyJhbGciOiJIUzI1NiJ9" not in error
    assert "Authorization=***" in error
    assert "Bearer=***" in error


@pytest.mark.asyncio
async def test_probe_site_redacts_every_cookie_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def leaky_failure(client: Any, method: str, url: str) -> int:
        raise RuntimeError("Cookie: first=one; second=two; token=three")

    monkeypatch.setattr(sp, "_request_status", leaky_failure)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    error = result.error or ""
    for value in ("one", "two", "three"):
        assert value not in error, error
    assert "Cookie=***" in error


@pytest.mark.asyncio
async def test_probe_site_error_url_with_query_is_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A backend error quoting some *other* URL (e.g. a redirect destination)
    # must drop its query, and a foreign host must not be echoed verbatim.
    async def leaky_failure(client: Any, method: str, url: str) -> int:
        raise RuntimeError(
            "redirected to http://internal.corp.hidden:8080/next"
            "?token=LEAK&sig=ALSO_LEAK"
        )

    monkeypatch.setattr(sp, "_request_status", leaky_failure)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    error = result.error or ""
    assert "LEAK" not in error
    assert "internal.corp.hidden" not in error
    assert "<url>" in error


@pytest.mark.asyncio
async def test_probe_site_truncation_never_splits_a_redaction_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pad so the ``***`` produced by redaction straddles the 200-char cut
    # (the marker starts at index 196), with a tail that forces truncation.
    padding = "z" * 168

    async def leaky_failure(client: Any, method: str, url: str) -> int:
        raise RuntimeError(f"{padding}Authorization: Basic {padding} {'y' * 50}")

    monkeypatch.setattr(sp, "_request_status", leaky_failure)

    result = await probe_site("https://example.invalid", session=_DUMMY)

    error = result.error or ""
    assert len(error) <= 200
    assert error.endswith("...")
    # The marker was trimmed rather than left as ``*``/``**``.
    assert error[:-3][-1] != "*"
    assert "Basic" not in error


# --------------------------------------------------------------------------- #
# probe_sites: ordering, concurrency, session lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_sites_preserves_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def varying_delay(client: Any, method: str, url: str) -> int:
        # The slow item finishes last, yet must keep its input position.
        delay = 0.05 if "slow" in url else 0.0
        await asyncio.sleep(delay)
        return 200

    wrapper = _FakeWrapper()
    monkeypatch.setattr(sp, "_make_session", lambda timeout: wrapper)
    monkeypatch.setattr(sp, "_request_status", varying_delay)

    # Deliberately not alphabetical: order must come from the input, not
    # from completion time.
    items = [
        ("zeta", "https://zeta.invalid"),
        ("slow", "https://slow.invalid"),
        ("alpha", "https://alpha.invalid"),
        ("mid", "https://mid.invalid"),
    ]
    results = await probe_sites(items, timeout=1.0, concurrency=8)

    assert [result.site_key for result in results] == [
        "zeta",
        "slow",
        "alpha",
        "mid",
    ]
    assert wrapper.inited is True
    assert wrapper.closed is True
    assert wrapper.close_calls == 1


@pytest.mark.asyncio
async def test_probe_sites_bounds_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def tracked(client: Any, method: str, url: str) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return 200
        finally:
            active -= 1

    monkeypatch.setattr(sp, "_make_session", lambda timeout: _FakeWrapper())
    monkeypatch.setattr(sp, "_request_status", tracked)

    items = [(f"s{i}", f"https://s{i}.invalid") for i in range(6)]
    results = await probe_sites(items, timeout=1.0, concurrency=2)

    assert len(results) == 6
    assert peak == 2, "the semaphore must actually allow two probes at once"
    assert peak <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, -1, -8])
async def test_probe_sites_non_positive_concurrency_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch, concurrency: int
) -> None:
    async def instant(client: Any, method: str, url: str) -> int:
        return 200

    monkeypatch.setattr(sp, "_make_session", lambda timeout: _FakeWrapper())
    monkeypatch.setattr(sp, "_request_status", instant)

    items = [("a", "https://a.invalid"), ("b", "https://b.invalid")]
    results = await asyncio.wait_for(
        probe_sites(items, timeout=1.0, concurrency=concurrency), timeout=5.0
    )

    assert [result.site_key for result in results] == ["a", "b"]
    assert all(result.ok for result in results)


@pytest.mark.asyncio
async def test_probe_sites_reports_session_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(timeout: float) -> _FakeWrapper:
        raise RuntimeError("HTTP backend 'aiohttp' is not available")

    monkeypatch.setattr(sp, "_make_session", unavailable)

    results = await probe_sites(
        [("a", "https://a.invalid"), ("b", "https://b.invalid")], timeout=1.0
    )

    assert [result.site_key for result in results] == ["a", "b"]
    assert all(result.ok is False for result in results)
    assert all("backend" in (result.error or "") for result in results)


@pytest.mark.asyncio
async def test_probe_sites_closes_session_when_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _FailingInitWrapper()
    monkeypatch.setattr(sp, "_make_session", lambda timeout: wrapper)

    results = await probe_sites(
        [("a", "https://a.invalid"), ("b", "https://b.invalid")], timeout=1.0
    )

    assert all(result.ok is False for result in results)
    assert wrapper.closed is True
    assert wrapper.close_calls == 1


@pytest.mark.asyncio
async def test_probe_site_closes_session_when_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _FailingInitWrapper()
    monkeypatch.setattr(sp, "_make_session", lambda timeout: wrapper)

    result = await probe_site("https://a.invalid")

    assert result.ok is False
    assert wrapper.closed is True


@pytest.mark.asyncio
async def test_probe_sites_empty_input_creates_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(timeout: float) -> _FakeWrapper:
        raise AssertionError("empty input must not build a session")

    monkeypatch.setattr(sp, "_make_session", boom)

    assert await probe_sites([]) == []


# --------------------------------------------------------------------------- #
# timeout normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.0, sp._MIN_TIMEOUT),
        (-1.0, sp._MIN_TIMEOUT),
        (float("inf"), sp._MAX_TIMEOUT),
        (float("-inf"), sp._MIN_TIMEOUT),
        (float("nan"), sp._MIN_TIMEOUT),
        (1e9, sp._MAX_TIMEOUT),
        (2.5, 2.5),
    ],
)
def test_normalize_timeout_clamps_into_a_finite_window(
    raw: float, expected: float
) -> None:
    assert sp._normalize_timeout(raw) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# sort_by_latency
# --------------------------------------------------------------------------- #
def test_sort_by_latency_puts_unreachable_last() -> None:
    results = [
        _result("dead", ok=False, status=None, latency_ms=None, error="timeout"),
        _result("slow", latency_ms=300.0),
        _result("fast", latency_ms=100.0),
        _result("mid", latency_ms=200.0),
    ]

    ordered = sort_by_latency(results)

    assert [result.site_key for result in ordered] == [
        "fast",
        "mid",
        "slow",
        "dead",
    ]


def test_sort_by_latency_treats_error_status_as_unreachable() -> None:
    results = [
        _result("forbidden", ok=False, status=403, latency_ms=10.0),
        _result("healthy", latency_ms=50.0),
    ]

    ordered = sort_by_latency(results)

    assert [result.site_key for result in ordered] == ["healthy", "forbidden"]


def test_sort_by_latency_is_stable_for_ties() -> None:
    results = [
        _result("b", latency_ms=10.0),
        _result("a", latency_ms=10.0),
        _result("c", ok=False, status=None, latency_ms=None, error="timeout"),
        _result("d", ok=False, status=None, latency_ms=None, error="timeout"),
    ]

    ordered = sort_by_latency(results)

    assert [result.site_key for result in ordered] == ["b", "a", "c", "d"]


# --------------------------------------------------------------------------- #
# Real transport path (loopback only)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_site_against_loopback_server(aiohttp_server: Any) -> None:
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="ok", status=200)

    app = aiohttp.web.Application()
    app.router.add_route("*", "/", handler)
    server = await aiohttp_server(app)

    result = await probe_site(str(server.make_url("/")))

    assert result.ok is True
    assert result.status == 200
    assert result.latency_ms is not None
    assert result.error is None


@pytest.mark.asyncio
async def test_probe_site_timeout_bounds_a_server_that_never_answers(
    aiohttp_server: Any,
) -> None:
    # The connection succeeds; the origin then stalls forever.  A timeout
    # applied only at connect time would hang here.
    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        await asyncio.sleep(30)
        return aiohttp.web.Response(text="too late")

    app = aiohttp.web.Application()
    app.router.add_route("*", "/", handler)
    server = await aiohttp_server(app)

    started = time.monotonic()
    result = await probe_site(str(server.make_url("/")), timeout=0.2)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.status is None
    assert result.latency_ms is None
    assert result.error == "timeout"
    assert elapsed < 5.0, f"probe ignored its timeout (took {elapsed:.1f}s)"


@pytest.mark.asyncio
async def test_probe_site_redirect_does_not_leak_the_destination(
    aiohttp_server: Any,
) -> None:
    async def destination(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="landed", status=200)

    dest_app = aiohttp.web.Application()
    dest_app.router.add_route("*", "/secret", destination)
    dest_server = await aiohttp_server(dest_app)

    async def redirect(request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound(
            str(dest_server.make_url("/secret?token=SHOULD_NOT_LEAK"))
        )

    src_app = aiohttp.web.Application()
    src_app.router.add_route("*", "/", redirect)
    src_server = await aiohttp_server(src_app)

    result = await probe_site(str(src_server.make_url("/")))

    assert result.ok is True
    # The reported URL is the original target, not the redirect destination.
    assert result.url.startswith("http://127.0.0.1:")
    assert str(src_server.make_url("/")).startswith(result.url)
    assert "SHOULD_NOT_LEAK" not in result.url
    assert f":{dest_server.port}" not in result.url
