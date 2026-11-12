#!/usr/bin/env python3
"""Offline tests: site fetchers share 429/503 cooldowns with their peers.

``QidianFetcher`` and ``N17kFetcher`` override ``fetch()`` with their own
retry loops, so they bypass the shared-cooldown handling in
:meth:`BaseFetcher.fetch`.  These tests pin down that a rate-limit response
observed on either override still pauses every worker using the same limiter,
without changing the site's own retry/backoff/header behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.base.fetcher as base_fetcher_mod
import novel_downloader.plugins.sites.n17k.fetcher as n17k_mod
import novel_downloader.plugins.sites.qidian.fetcher as qidian_mod
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.plugins.sites.n17k.fetcher import N17kFetcher
from novel_downloader.plugins.sites.qidian.fetcher import QidianFetcher
from novel_downloader.schemas import FetcherConfig

Handler = Callable[[str, dict[str, Any]], BaseResponse]

#: 40 hex characters: ``N17kFetcher._reorder`` indexes positions up to 39.
_HEX40 = "0123456789ABCDEF0123456789ABCDEF01234567"
_ARG1_BODY = f"var arg1 = '{_HEX40}';".encode()


class FakeSession:
    """Minimal stand-in for ``BaseSession``; no network access."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cookies: dict[str, str] = {}
        self._headers = {"User-Agent": "test-agent"}

    async def get(self, url: str, **kwargs: Any) -> BaseResponse:
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)

    def update_cookies(self, cookies: dict[str, str]) -> None:
        self.cookies.update(cookies)

    def get_cookie(self, key: str) -> str | None:
        return self.cookies.get(key)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)


class RecordingLimiter:
    """Limiter that records ``penalize`` calls and never actually sleeps."""

    def __init__(self) -> None:
        self.waits = 0
        self.penalties: list[float] = []

    async def wait(self) -> None:
        self.waits += 1

    def penalize(self, seconds: float) -> float:
        self.penalties.append(seconds)
        return seconds


class WaitOnlyLimiter:
    """Backward-compatible limiter without the optional ``penalize``."""

    def __init__(self) -> None:
        self.waits = 0

    async def wait(self) -> None:
        self.waits += 1


def _sequence(*responses: BaseResponse) -> Handler:
    """Return a handler serving ``responses`` in order."""
    queue = list(responses)

    def handler(url: str, kwargs: dict[str, Any]) -> BaseResponse:
        assert queue, f"unexpected extra request: {url}"
        return queue.pop(0)

    return handler


SITES = [
    pytest.param(qidian_mod, QidianFetcher, id="qidian"),
    pytest.param(n17k_mod, N17kFetcher, id="n17k"),
]


def _make(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    cls: type[Any],
    handler: Handler,
    *,
    retry_times: int = 1,
    limiter: Any | None = None,
) -> tuple[Any, FakeSession, Any]:
    session = FakeSession(handler)
    monkeypatch.setattr(base_fetcher_mod, "create_session", lambda **kwargs: session)

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module, "async_jitter_sleep", _no_sleep)

    fetcher = cls(
        FetcherConfig(
            cache_dir=str(tmp_path),
            max_rps=0,
            retry_times=retry_times,
            backoff_factor=0,
            request_interval=0,
        )
    )
    active = limiter if limiter is not None else RecordingLimiter()
    fetcher._rate_limiter = active
    return fetcher, session, active


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cls", SITES)
@pytest.mark.parametrize("status", [429, 503])
async def test_rate_limit_response_registers_shared_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    cls: type[Any],
    status: int,
) -> None:
    """A 429/503 with ``Retry-After`` pauses the whole worker pool."""
    handler = _sequence(
        BaseResponse(content=b"", status=status, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, session, limiter = _make(monkeypatch, tmp_path, module, cls, handler)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == [7.0]
    assert len(session.calls) == 2  # the site still retried as before


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cls", SITES)
@pytest.mark.parametrize("status", [401, 404])
async def test_non_rate_limit_status_does_not_register(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    cls: type[Any],
    status: int,
) -> None:
    """Deterministic 4xx failures are not evidence of site-wide throttling."""
    handler = _sequence(
        BaseResponse(content=b"", status=status, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _, limiter = _make(monkeypatch, tmp_path, module, cls, handler)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cls", SITES)
async def test_rate_limit_without_retry_after_does_not_register(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    cls: type[Any],
) -> None:
    """Without an explicit header there is no shared deadline to register."""
    handler = _sequence(
        BaseResponse(content=b"", status=429),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _, limiter = _make(monkeypatch, tmp_path, module, cls, handler)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module,cls", SITES)
async def test_limiter_without_penalize_keeps_working(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    cls: type[Any],
) -> None:
    """A foreign limiter only has to provide ``wait()``; behaviour is unchanged."""
    handler = _sequence(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    limiter = WaitOnlyLimiter()
    fetcher, session, active = _make(
        monkeypatch, tmp_path, module, cls, handler, limiter=limiter
    )

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert active is limiter
    assert limiter.waits == 1  # the override's single pre-loop wait
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_n17k_registers_cooldown_on_cookie_refresh_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The second request after the anti-bot cookie update is covered too."""
    handler = _sequence(
        BaseResponse(content=_ARG1_BODY, status=200),
        BaseResponse(content=b"", status=429, headers={"Retry-After": "5"}),
        BaseResponse(content=_ARG1_BODY, status=200),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, session, limiter = _make(
        monkeypatch, tmp_path, n17k_mod, N17kFetcher, handler
    )

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == [5.0]
    assert len(session.calls) == 4
