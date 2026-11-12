#!/usr/bin/env python3
"""Offline robustness tests for :mod:`novel_downloader.plugins.base.fetcher`.

Every HTTP interaction is served by an in-memory fake session, so these tests
never touch the network.  They pin down the current error-handling contract:
what is retried, what is swallowed, and what is left on disk after a failure.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

import novel_downloader.plugins.base.fetcher as fetcher_mod
import novel_downloader.plugins.utils.rate_limiter as rate_limiter_module
from novel_downloader.infra.http_defaults import IMAGE_HEADERS
from novel_downloader.infra.sessions.base import NonRetryableConnectionError
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.libs.filesystem import font_filename, image_filename
from novel_downloader.plugins.base.errors import (
    HttpStatusError,
    TransportError,
    is_transient_error,
)
from novel_downloader.plugins.base.fetcher import (
    BaseFetcher,
    GenericFetcher,
    _retry_after_seconds,
)
from novel_downloader.schemas import FetcherConfig

HandlerResult = BaseResponse | BaseException | Awaitable[BaseResponse | BaseException]
Handler = Callable[[str, dict[str, Any]], HandlerResult]


class FakeSession:
    """Minimal stand-in for ``BaseSession`` used by :class:`BaseFetcher`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.init_calls = 0
        self.close_calls = 0
        self.load_calls = 0
        self.save_calls = 0
        self.updated_cookies: dict[str, str] = {}
        self.load_result = False
        self.save_result = True
        self._headers = {"User-Agent": "test-agent"}

    async def init(self, **kwargs: Any) -> None:
        self.init_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def get(self, url: str, **kwargs: Any) -> BaseResponse:
        self.calls.append((url, kwargs))
        result = self.handler(url, kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result

    def load_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        self.load_calls += 1
        return self.load_result

    def save_cookies(self, cookies_dir: Path, filename: str | None = None) -> bool:
        self.save_calls += 1
        return self.save_result

    def update_cookies(self, cookies: dict[str, str]) -> None:
        self.updated_cookies.update(cookies)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)


class _Fetcher(BaseFetcher):
    site_name = "fake"
    login_status = True

    async def _check_login_status(self) -> bool:
        return type(self).login_status

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        return []

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        return []


def _make(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler | None = None,
    *,
    fetcher_cls: type[BaseFetcher] = _Fetcher,
    **cfg_kwargs: Any,
) -> tuple[BaseFetcher, FakeSession]:
    session = FakeSession(handler or (lambda url, kw: BaseResponse(content=b"ok")))
    monkeypatch.setattr(fetcher_mod, "create_session", lambda **kwargs: session)

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fetcher_mod, "async_jitter_sleep", _no_sleep)

    cfg_kwargs.setdefault("max_rps", 0)  # no rate-limiter sleeps
    cfg_kwargs.setdefault("request_interval", 0)
    cfg_kwargs.setdefault("backoff_factor", 0)
    fetcher = fetcher_cls(FetcherConfig(**cfg_kwargs))
    return fetcher, session


def _sequence_handler(
    *responses: BaseResponse | BaseException,
) -> Handler:
    """Return one response per call, repeating the last one forever."""
    queue = list(responses)

    def _handler(url: str, kwargs: dict[str, Any]) -> BaseResponse | BaseException:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item

    return _handler


# --------------------------------------------------------------------------- #
# fetch(): retry boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_returns_decoded_text(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "正文内容".encode()
    fetcher, session = _make(monkeypatch, lambda url, kw: BaseResponse(content=body))

    assert await fetcher.fetch("https://demo/1", encoding="utf-8") == "正文内容"
    assert session.calls[0][1]["encoding"] == "utf-8"


@pytest.mark.asyncio
async def test_fetch_retries_http_errors_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _sequence_handler(
        BaseResponse(content=b"", status=500),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_fetch_raises_connection_error_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(content=b"", status=503)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    with pytest.raises(ConnectionError, match="status 503"):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 3  # retry_times + 1


@pytest.mark.asyncio
async def test_fetch_zero_retries_makes_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(content=b"", status=404)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(ConnectionError):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_retries_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset/timeout is worth another attempt: the budget exists for it."""
    handler = _sequence_handler(
        ConnectionError("connection reset"),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_fetch_raises_transport_error_after_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted transport retries surface as a typed, non-retryable error."""
    handler = lambda url, kw: ConnectionError("connection reset")  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    with pytest.raises(TransportError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 3  # retry_times + 1
    assert isinstance(excinfo.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_fetch_does_not_retry_a_deterministic_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 cannot change between attempts, so it must not be retried."""
    handler = lambda url, kw: BaseResponse(content=b"", status=404)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert excinfo.value.status == 404
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429/503 with ``Retry-After`` sleeps exactly what the server asked."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=2)
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert delays == [7.0]


def test_retry_after_accepts_both_header_forms() -> None:
    assert _retry_after_seconds({"Retry-After": "12"}) == 12.0
    # An HTTP-date in the past means "retry now", never a negative sleep.
    assert _retry_after_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0
    assert _retry_after_seconds({"Retry-After": "not-a-date"}) is None
    assert _retry_after_seconds({}) is None


def test_retry_after_is_capped() -> None:
    assert _retry_after_seconds({"Retry-After": "3600"}) == 30.0


def test_retry_after_accepts_a_future_http_date() -> None:
    """The date form is honoured, then bounded by the same ceiling."""
    when = datetime.now(UTC) + timedelta(seconds=60)
    header = format_datetime(when, usegmt=True)

    assert _retry_after_seconds({"Retry-After": header}) == 30.0


def test_retry_after_ignores_a_malformed_value() -> None:
    assert _retry_after_seconds({"Retry-After": ""}) is None
    assert _retry_after_seconds({"Retry-After": "nan"}) is None


@pytest.mark.asyncio
async def test_fetch_retries_429_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 is transient even without a Retry-After header."""
    handler = lambda url, kw: BaseResponse(content=b"", status=429)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=2)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert excinfo.value.status == 429
    # The fetcher spent the whole budget above, so no higher level may retry.
    assert excinfo.value.retryable is False
    assert is_transient_error(excinfo.value) is False
    assert len(session.calls) == 3  # retry_times + 1


def test_typed_http_status_error_is_not_transient() -> None:
    """5xx budget is spent inside the fetcher; 4xx is deterministic.

    ``is_transient_error`` gates higher-level retries, so it must agree with
    ``HttpStatusError.retryable`` even though ``RETRYABLE_STATUS`` still marks
    5xx/429 as transient for reporting callers.
    """
    for status in (503, 429, 500, 404, 403):
        error = HttpStatusError("https://demo/1", status)
        assert error.retryable is False
        assert is_transient_error(error) is False


@pytest.mark.asyncio
async def test_http_status_error_carries_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused 429 keeps the server's delay available to reporting callers."""
    handler = lambda url, kw: BaseResponse(  # noqa: E731
        content=b"", status=429, headers={"Retry-After": "7"}
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch("https://demo/1")

    error = excinfo.value
    assert error.status == 429
    assert error.retry_after == 7.0
    assert error.retryable is False
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_http_status_error_has_no_retry_after_without_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(content=b"", status=404)  # noqa: E731
    fetcher, _ = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert excinfo.value.status == 404
    assert excinfo.value.retry_after is None


def test_is_transient_error_honours_an_explicit_retryable_flag() -> None:
    """The flag must win in both directions, exactly like the task manager."""
    assert (
        is_transient_error(NonRetryableConnectionError("certificate expired")) is False
    )

    class _RetryableConnectionError(ConnectionError):
        retryable = True

    assert is_transient_error(_RetryableConnectionError("flaky")) is True


@pytest.mark.asyncio
async def test_http_status_error_redacts_url_credentials_and_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user sees the failure, never the signed URL it came from."""
    url = "https://alice:s3cret@demo/1?token=abc123&page=2"
    handler = lambda u, kw: BaseResponse(content=b"", status=403)  # noqa: E731
    fetcher, _ = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch(url)

    message = str(excinfo.value)
    assert "alice" not in message
    assert "s3cret" not in message
    assert "abc123" not in message
    assert "demo/1" in message
    # The structured field stays the exact URL the caller passed in.
    assert excinfo.value.url == url


@pytest.mark.asyncio
async def test_transport_error_redacts_credentials_and_local_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend messages can echo URLs and local paths; neither may leak."""
    raw = (
        "connect failed for https://bob:hunter2@demo/1?token=xyz "
        r"while reading C:\Users\bob\.cache\chapter.tmp"
    )
    handler = lambda url, kw: ConnectionError(raw)  # noqa: E731
    fetcher, _ = _make(monkeypatch, handler, retry_times=0)

    with pytest.raises(TransportError) as excinfo:
        await fetcher.fetch("https://demo/1")

    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "bob" not in message
    assert "xyz" not in message
    assert "C:\\Users" not in message
    assert "<redacted-path>" in message


@pytest.mark.asyncio
async def test_fetch_does_not_retry_proxy_authentication_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct 407 is deterministic: one request, no retry budget spent."""
    handler = lambda url, kw: BaseResponse(content=b"", status=407)  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    with pytest.raises(HttpStatusError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert excinfo.value.status == 407
    assert excinfo.value.retryable is False
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_does_not_retry_a_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body that cannot be parsed is deterministic, not transient."""
    handler = lambda url, kw: ValueError("Failed to parse response body as JSON")  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    with pytest.raises(ValueError):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_data_never_swallows_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The media path only catches transport errors, never BaseException."""
    fetcher, session = _make(monkeypatch, lambda url, kw: KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        await fetcher.fetch_data("https://demo/cover.jpg")

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_fetch_data_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Media retries use the same 429/Retry-After contract as page fetches."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "4"}),
        BaseResponse(content=b"image"),
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=1)
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch_data("https://demo/cover.jpg") == b"image"
    assert delays == [4.0]
    assert len(session.calls) == 2


class _RecordingLimiter:
    """Stand-in for a limiter that may or may not expose ``penalize``."""

    def __init__(self, covered: float | None = None, *, with_penalize: bool = True):
        self.waits = 0
        self.penalties: list[float] = []
        self._covered = covered
        self._with_penalize = with_penalize

    async def wait(self) -> None:
        self.waits += 1

    def penalize(self, seconds: float) -> float:
        self.penalties.append(seconds)
        if self._covered is None:
            return seconds
        return min(self._covered, seconds)


def _attach_limiter(fetcher: BaseFetcher, limiter: Any) -> Any:
    fetcher._rate_limiter = limiter  # type: ignore[assignment]
    return limiter


@pytest.mark.asyncio
async def test_fetch_429_retry_after_pauses_all_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared cooldown replaces the local sleep: one pause, not two."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=2)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == [7.0]
    # The retry must wait through the limiter's cooldown, not sleep on top.
    assert delays == []
    assert limiter.waits == 2


@pytest.mark.asyncio
async def test_fetch_503_retry_after_pauses_all_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _sequence_handler(
        BaseResponse(content=b"", status=503, headers={"Retry-After": "3"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == [3.0]


@pytest.mark.asyncio
async def test_fetch_500_retry_after_does_not_pause_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 with Retry-After may be one broken route, not site throttling."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=500, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == []
    assert delays == [7.0]


@pytest.mark.asyncio
async def test_fetch_429_without_retry_after_does_not_pause_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit header the server gave no shared deadline."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == []


@pytest.mark.asyncio
async def test_fetch_falls_back_when_limiter_has_no_penalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compatibility: a foreign limiter only has to provide wait()."""

    class _WaitOnly:
        def __init__(self) -> None:
            self.waits = 0

        async def wait(self) -> None:
            self.waits += 1

    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _WaitOnly())
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.waits == 2
    assert delays == [7.0]  # the old local sleep still happens


@pytest.mark.asyncio
async def test_fetch_sleeps_only_the_part_the_cooldown_could_not_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a cooldown is capped shorter than Retry-After, sleep the remainder."""
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "7"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter(covered=2.0))
    delays: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        delays.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"
    assert limiter.penalties == [7.0]
    assert delays == [5.0]


@pytest.mark.asyncio
async def test_fetch_data_429_retry_after_pauses_all_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "4"}),
        BaseResponse(content=b"image"),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())

    assert await fetcher.fetch_data("https://demo/cover.jpg") == b"image"
    assert limiter.penalties == [4.0]


@pytest.mark.asyncio
async def test_fetch_penalises_even_after_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last 429 still tells the pool to slow down, though we give up."""
    handler = lambda url, kw: BaseResponse(  # noqa: E731
        content=b"", status=429, headers={"Retry-After": "7"}
    )
    fetcher, session = _make(monkeypatch, handler, retry_times=1)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())

    with pytest.raises(HttpStatusError):
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 2
    assert limiter.penalties == [7.0, 7.0]


@pytest.mark.asyncio
async def test_fetch_data_penalises_even_after_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: BaseResponse(  # noqa: E731
        content=b"", status=503, headers={"Retry-After": "2"}
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=0)
    limiter = _attach_limiter(fetcher, _RecordingLimiter())

    assert await fetcher.fetch_data("https://demo/cover.jpg") is None
    assert limiter.penalties == [2.0]


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_retry_after_and_the_bucket_do_not_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: one 429 costs one Retry-After pause, not two.

    With a real ``TokenBucketRateLimiter`` the fetcher registers a shared
    cooldown and then ``wait()`` sleeps it out.  If the fetcher also slept the
    header locally, the retry would be delayed by ~2 x Retry-After.
    """
    clock = _Clock()
    monkeypatch.setattr(rate_limiter_module, "time", clock)

    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    handler = _sequence_handler(
        BaseResponse(content=b"", status=429, headers={"Retry-After": "5"}),
        BaseResponse(content=b"ok", status=200),
    )
    fetcher, _ = _make(monkeypatch, handler, retry_times=1, max_rps=1000.0)
    limiter = fetcher._rate_limiter
    assert limiter is not None
    penalties: list[float] = []
    original_penalize = limiter.penalize

    def _spy(seconds: float, **kwargs: Any) -> float:
        penalties.append(seconds)
        return original_penalize(seconds, **kwargs)

    monkeypatch.setattr(limiter, "penalize", _spy)
    backoffs: list[float | None] = []

    async def _record(attempt: int, delay: float | None = None) -> None:
        backoffs.append(delay)

    fetcher._retry_backoff = _record  # type: ignore[method-assign]

    assert await fetcher.fetch("https://demo/1") == "ok"

    assert penalties == [5.0]
    assert backoffs == []  # no local sleep on top of the cooldown
    # Exactly one pause, and it is the cooldown's (5 s + <=0.5 s jitter).
    assert len(slept) == 1
    assert 5.0 <= slept[0] <= 5.6


@pytest.mark.asyncio
async def test_fetch_does_not_retry_a_non_retryable_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected certificate/proxy credential fails on the first attempt."""
    handler = lambda url, kw: NonRetryableConnectionError("certificate expired")  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    with pytest.raises(TransportError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1
    assert isinstance(excinfo.value.__cause__, NonRetryableConnectionError)
    assert is_transient_error(excinfo.value) is False


@pytest.mark.asyncio
async def test_fetch_data_does_not_retry_a_non_retryable_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = lambda url, kw: NonRetryableConnectionError("bad proxy credentials")  # noqa: E731
    fetcher, session = _make(monkeypatch, handler, retry_times=3)

    assert await fetcher.fetch_data("https://demo/cover.jpg") is None
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_backoff_doubles_with_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, backoff_factor=2.0)
    bases: list[float] = []

    async def _record(base: float, **kwargs: Any) -> None:
        bases.append(base)

    monkeypatch.setattr(fetcher_mod, "async_jitter_sleep", _record)

    for attempt in range(3):
        await fetcher._retry_backoff(attempt)

    assert bases == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher, _ = _make(monkeypatch, backoff_factor=2.0)
    bases: list[float] = []

    async def _record(base: float, **kwargs: Any) -> None:
        bases.append(base)

    monkeypatch.setattr(fetcher_mod, "async_jitter_sleep", _record)

    await fetcher._retry_backoff(20)

    assert bases == [60.0]


@pytest.mark.asyncio
async def test_retry_after_jitter_is_upward_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workers must not retry in lockstep, but never before the server's time."""
    fetcher, _ = _make(monkeypatch)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(fetcher_mod.asyncio, "sleep", _fake_sleep)

    await fetcher._retry_backoff(0, 10.0)
    await fetcher._retry_backoff(0, 0.0)

    assert len(slept) == 2
    assert 10.0 <= slept[0] <= 11.0
    assert slept[1] == 0.0


@pytest.mark.asyncio
async def test_pagination_stops_when_a_page_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site that echoes page 1 forever must not loop forever."""

    class _AlwaysMore(_Generic):
        def should_continue_pagination(self, **kwargs: Any) -> bool:
            return True

    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=b"same page"),
        fetcher_cls=_AlwaysMore,
    )

    pages = await fetcher._paginate(
        make_suffix=lambda i: f"/p{i}",
        page_type="chapter",
        book_id="7",
        chapter_id="1",
    )

    assert pages == ["same page"]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_pagination_stops_at_max_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with unique pages, the loop is bounded by ``MAX_PAGES``."""

    class _Endless(_Generic):
        MAX_PAGES = 3

        def should_continue_pagination(self, **kwargs: Any) -> bool:
            return True

    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=url.encode()),
        fetcher_cls=_Endless,
    )

    pages = await fetcher._paginate(
        make_suffix=lambda i: f"/p{i}",
        page_type="chapter",
        book_id="7",
        chapter_id="1",
    )

    assert len(pages) == 3
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_fetch_empty_body_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b""))

    assert await fetcher.fetch("https://demo/1") == ""


@pytest.mark.asyncio
async def test_fetch_falls_back_for_misdeclared_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Server claims utf-8 but actually sends GBK; BaseResponse.text falls back.
    body = "中文".encode("gbk")
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=body, encoding="utf-8")
    )

    assert await fetcher.fetch("https://demo/1") == "中文"


# --------------------------------------------------------------------------- #
# fetch_data(): binary downloads never raise
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_data_returns_bytes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b"raw"))

    assert await fetcher.fetch_data("https://demo/a") == b"raw"


@pytest.mark.asyncio
async def test_fetch_data_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"x", status=404)
    )

    assert await fetcher.fetch_data("https://demo/a") is None


@pytest.mark.asyncio
async def test_fetch_data_returns_none_on_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b""))

    assert await fetcher.fetch_data("https://demo/a") is None


@pytest.mark.asyncio
async def test_fetch_data_swallows_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, _ = _make(monkeypatch, lambda url, kw: ConnectionError("boom"))

    assert await fetcher.fetch_data("https://demo/a") is None


# --------------------------------------------------------------------------- #
# media writes: failures leave no partial artefacts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_image_uses_image_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"\x89PNG")
    )

    path = await fetcher.fetch_image("https://img/a.png", tmp_path)

    assert path is not None and path.is_file()
    assert session.calls[0][1]["headers"] == IMAGE_HEADERS


@pytest.mark.asyncio
async def test_fetch_image_skips_existing_file_without_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://img/a.png"
    existing = tmp_path / image_filename(url)
    existing.write_bytes(b"old")

    fetcher, session = _make(monkeypatch)

    assert await fetcher.fetch_image(url, tmp_path) == existing
    assert session.calls == []
    assert existing.read_bytes() == b"old"


@pytest.mark.asyncio
async def test_fetch_image_failure_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"", status=500)
    )
    img_dir = tmp_path / "media"

    assert await fetcher.fetch_image("https://img/a.png", img_dir) is None
    assert not img_dir.exists()


@pytest.mark.asyncio
async def test_fetch_images_continues_after_partial_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    urls = ["https://img/a.png", "https://img/b.png", "https://img/c.png"]
    bodies = {
        urls[0]: BaseResponse(content=b"A"),
        urls[1]: BaseResponse(content=b"", status=404),
        urls[2]: BaseResponse(content=b"C"),
    }
    fetcher, _ = _make(monkeypatch, lambda url, kw: bodies[url])

    await fetcher.fetch_images(tmp_path, urls, concurrent=2)

    saved = {p.name for p in tmp_path.iterdir()}
    assert saved == {image_filename(urls[0]), image_filename(urls[2])}


@pytest.mark.asyncio
async def test_fetch_images_empty_list_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(monkeypatch)
    img_dir = tmp_path / "media"

    await fetcher.fetch_images(img_dir, [])

    assert not img_dir.exists()
    assert session.calls == []


@pytest.mark.asyncio
async def test_fetch_images_all_failed_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, _ = _make(
        monkeypatch, lambda url, kw: BaseResponse(content=b"", status=500)
    )
    img_dir = tmp_path / "media"

    await fetcher.fetch_images(img_dir, ["https://img/a.png"])

    assert not img_dir.exists()


@pytest.mark.asyncio
async def test_fetch_fonts_saves_successful_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://fonts/a.woff2"
    fetcher, _ = _make(monkeypatch, lambda u, kw: BaseResponse(content=b"font"))

    await fetcher.fetch_fonts(tmp_path, [url])

    assert (tmp_path / font_filename(url)).read_bytes() == b"font"


@pytest.mark.asyncio
async def test_fetch_media_dispatches_by_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = "https://img/a.png"
    font = "https://fonts/a.woff2"
    fetcher, _ = _make(monkeypatch, lambda url, kw: BaseResponse(content=b"x"))

    await fetcher.fetch_media(
        tmp_path,
        [
            {"url": image, "type": "image"},
            {"url": font, "type": "font"},
            {"url": "https://other/a.mp3", "type": "audio"},
            {"type": "image"},  # no url
        ],
    )

    assert {p.name for p in tmp_path.iterdir()} == {
        image_filename(image),
        font_filename(font),
    }


@pytest.mark.asyncio
async def test_fetch_media_without_downloadable_entries_creates_no_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetcher, session = _make(monkeypatch)
    media_dir = tmp_path / "media"

    await fetcher.fetch_media(media_dir, [{"type": "audio", "url": "https://x/a"}])

    assert not media_dir.exists()
    assert session.calls == []


# --------------------------------------------------------------------------- #
# login / session state
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_without_cookies_fails_without_touching_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    assert await fetcher.login() is False
    assert session.updated_cookies == {}
    assert fetcher.is_logged_in is False


@pytest.mark.asyncio
async def test_login_with_cookies_updates_session_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    assert await fetcher.login(cookies={"sid": "abc"}) is True
    assert session.updated_cookies == {"sid": "abc"}
    assert fetcher.is_logged_in is True


@pytest.mark.asyncio
async def test_login_reports_false_when_status_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RejectingFetcher(_Fetcher):
        login_status = False

    fetcher, _ = _make(monkeypatch, fetcher_cls=_RejectingFetcher)

    assert await fetcher.login(cookies={"sid": "bad"}) is False
    assert fetcher.is_logged_in is False


@pytest.mark.asyncio
async def test_load_state_delegates_to_session_and_reports_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)
    session.load_result = True

    assert await fetcher.load_state() is True
    assert session.load_calls == 1
    assert fetcher.is_logged_in is True


@pytest.mark.asyncio
async def test_save_state_delegates_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)
    session.save_result = False

    assert await fetcher.save_state() is False
    assert session.save_calls == 1


@pytest.mark.asyncio
async def test_context_manager_initialises_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch)

    async with fetcher:
        assert session.init_calls == 1

    assert session.close_calls == 1


# --------------------------------------------------------------------------- #
# GenericFetcher URL/error paths
# --------------------------------------------------------------------------- #
class _Generic(GenericFetcher):
    site_name = "generic"
    BASE_URL = "https://demo.test"
    DEFAULT_BASE_URL = "https://demo.test"
    BOOK_INFO_URL = "{base_url}/book/{book_id}"
    CHAPTER_URL = "{base_url}/chapter/{book_id}/{chapter_id}"


@pytest.mark.asyncio
async def test_generic_fetcher_returns_single_info_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=b"<html>info</html>"),
        fetcher_cls=_Generic,
    )

    assert await fetcher.fetch_book_info("7") == ["<html>info</html>"]
    assert session.calls[0][0] == "https://demo.test/book/7"


@pytest.mark.asyncio
async def test_generic_fetcher_missing_base_url_for_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Paginated(_Generic):
        BASE_URL = ""
        USE_PAGINATED_CHAPTER = True

        @classmethod
        def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
            return f"/{book_id}/{chapter_id}/{idx}"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_Paginated)

    with pytest.raises(RuntimeError, match="BASE_URL is required"):
        await fetcher.fetch_chapter_content("7", "1")


@pytest.mark.asyncio
async def test_generic_fetcher_requires_url_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoTemplates(GenericFetcher):
        site_name = "notemplates"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_NoTemplates)

    with pytest.raises(NotImplementedError, match="BOOK_INFO_URL not set"):
        await fetcher.fetch_book_info("7")
    with pytest.raises(NotImplementedError, match="CHAPTER_URL not set"):
        await fetcher.fetch_chapter_content("7", "1")


@pytest.mark.asyncio
async def test_generic_fetcher_transforms_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Prefixed(_Generic):
        BOOK_ID_REPLACEMENTS = [("-", "/")]
        CHAP_ID_REPLACEMENTS = [("_", "/")]

    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=b"ok"),
        fetcher_cls=_Prefixed,
    )

    await fetcher.fetch_chapter_content("a-b", "c_d")

    assert session.calls[0][0] == "https://demo.test/chapter/a/b/c/d"


@pytest.mark.asyncio
async def test_generic_fetcher_appends_separate_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WithCatalog(_Generic):
        HAS_SEPARATE_CATALOG = True
        BOOK_CATALOG_URL = "{base_url}/catalog/{book_id}"

    fetcher, _ = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=url.encode()),
        fetcher_cls=_WithCatalog,
    )

    pages = await fetcher.fetch_book_info("7")

    assert pages == [
        "https://demo.test/book/7",
        "https://demo.test/catalog/7",
    ]


@pytest.mark.asyncio
async def test_generic_fetcher_pagination_stops_without_next_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Paginated(_Generic):
        USE_PAGINATED_CHAPTER = True

        @classmethod
        def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
            return f"/chapter/{book_id}/{chapter_id}_{idx}"

    pages = {
        "https://demo.test/chapter/7/1_1": 'second at "/chapter/7/1_2"',
        "https://demo.test/chapter/7/1_2": "last page",
    }
    fetcher, session = _make(
        monkeypatch,
        lambda url, kw: BaseResponse(content=pages[url].encode()),
        fetcher_cls=_Paginated,
    )

    result = await fetcher.fetch_chapter_content("7", "1")

    assert result == [
        'second at "/chapter/7/1_2"',
        "last page",
    ]
    assert len(session.calls) == 2


def test_resolve_base_url_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Variants(GenericFetcher):
        site_name = "variants"
        BASE_URL_MAP = {"traditional": "https://t.example"}
        DEFAULT_BASE_URL = "https://s.example"

    fetcher, _ = _make(monkeypatch, fetcher_cls=_Variants)

    assert fetcher._base_url == "https://s.example"
