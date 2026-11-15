#!/usr/bin/env python3
"""
tests.apps.test_sse_keepalive_unit
----------------------------------

Deterministic unit tests for the SSE keepalive wrapper
(:func:`novel_downloader.apps.api.routes._stream_with_keepalive`).

The tests never wait a real keepalive interval: the module constant is lowered
to milliseconds, and the cases that would otherwise be timing-dependent are
driven by events instead of sleeps, so the ordering assertions are exact.

They also pin the wire format of the comment frame (a genuine SSE comment,
ignored by compliant parsers) and prove that all three SSE routes build their
stream through the wrapper.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.responses import StreamingResponse

from novel_downloader.apps.api import routes as api_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DATA_1 = 'event: results\ndata: {"n":1}\n\n'
DATA_2 = 'event: results\ndata: {"n":2}\n\n'


def _parse_sse_events(raw: str) -> list[tuple[str, Any]]:
    """
    Parse events the way a compliant SSE client does.

    Comment lines (those starting with ``:``) are skipped entirely, exactly as
    the WHATWG event-stream interpretation requires.
    """
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    """Drain a stream to completion."""
    return [frame async for frame in stream]


def _live_tasks() -> set[asyncio.Task[Any]]:
    """Tasks still running besides the current one (leak detector)."""
    current = asyncio.current_task()
    running = {task for task in asyncio.all_tasks() if not task.done()}
    running.discard(current)
    return running


async def _join_cancelled(task: asyncio.Task[Any]) -> None:
    """Await a task that must have been cancelled, failing loudly otherwise."""
    with pytest.raises(asyncio.CancelledError):
        await task


class _FakeManager:
    """Minimal manager stand-in; only used to build the task stream."""

    def snapshot(self) -> dict[str, list[Any]]:
        return {"pending": [], "running": [], "completed": []}


# ---------------------------------------------------------------------------
# Frame format
# ---------------------------------------------------------------------------


def test_keepalive_frame_is_a_valid_sse_comment() -> None:
    frame = api_routes.SSE_KEEPALIVE_FRAME

    # Exact bytes on the wire: tests that compare byte buffers must match.
    assert frame.encode("utf-8") == b": keepalive\n\n"
    assert isinstance(frame, str)

    # A comment is a line whose first character is a colon. It carries no
    # event and no data field, so it can never be mistaken for an event.
    assert frame.startswith(":")
    assert frame.endswith("\n\n")
    assert "data:" not in frame
    assert "event:" not in frame
    for line in frame.split("\n"):
        if line:
            assert line.startswith(":"), line

    # A compliant parser sees nothing at all in it.
    assert _parse_sse_events(frame) == []


def test_keepalive_frames_are_invisible_to_the_event_contract() -> None:
    raw = DATA_1 + api_routes.SSE_KEEPALIVE_FRAME + DATA_2

    events = _parse_sse_events(raw)

    assert [event for event, _ in events] == ["results", "results"]
    assert [payload["n"] for _, payload in events] == [1, 2]


# ---------------------------------------------------------------------------
# Interleaving and ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_is_interleaved_between_data_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data frame -> keepalive -> data frame, using the patched interval."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    gate = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        yield DATA_1
        # Held open until the test has observed the keepalive.
        await gate.wait()
        yield DATA_2

    # No interval argument: the wrapper must resolve the patched module
    # constant at call time.
    stream = api_routes._stream_with_keepalive(source())
    try:
        assert await asyncio.wait_for(anext(stream), 1.0) == DATA_1

        keepalive = await asyncio.wait_for(anext(stream), 1.0)
        assert keepalive == api_routes.SSE_KEEPALIVE_FRAME
        assert keepalive.encode("utf-8") == b": keepalive\n\n"

        # Releasing the source now means the next pull completes immediately,
        # so exactly one keepalive is emitted and the ordering is fixed.
        gate.set()
        assert await asyncio.wait_for(anext(stream), 1.0) == DATA_2

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1.0)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_no_keepalive_before_a_fast_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timer starts at stream start, but nothing is pre-emitted at time zero."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.2)

    async def source() -> AsyncIterator[str]:
        await asyncio.sleep(0.01)
        yield DATA_1

    frames = await asyncio.wait_for(
        _collect(api_routes._stream_with_keepalive(source())), 1.0
    )

    assert frames == [DATA_1]


@pytest.mark.asyncio
async def test_no_keepalive_after_the_source_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def source() -> AsyncIterator[str]:
        yield DATA_1
        yield DATA_2

    frames = await asyncio.wait_for(
        _collect(api_routes._stream_with_keepalive(source())), 1.0
    )

    assert frames == [DATA_1, DATA_2]
    assert api_routes.SSE_KEEPALIVE_FRAME not in frames


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_first", [False, True])
async def test_source_completion_cuts_the_keepalive_wait_short(
    monkeypatch: pytest.MonkeyPatch,
    emit_first: bool,
) -> None:
    """
    A source that finishes during the keepalive wait must not burn an interval.

    ``asyncio.wait`` returns as soon as the pull completes; a buggy
    ``wait_for``-style implementation would only notice at the timeout. The
    interval is 0.5 s, so a full wait is far outside the budget asserted here.
    """
    interval = 0.5
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", interval)

    async def source() -> AsyncIterator[str]:
        if emit_first:
            yield DATA_1
        await asyncio.sleep(0.01)
        return

    started = time.monotonic()
    frames = await asyncio.wait_for(
        _collect(api_routes._stream_with_keepalive(source())),
        timeout=interval / 2,
    )
    elapsed = time.monotonic() - started

    assert frames == ([DATA_1] if emit_first else [])
    # Well below one interval, not merely below the outer timeout.
    assert elapsed < interval / 4


# ---------------------------------------------------------------------------
# Transparency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_frames_pass_through_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must not re-encode, normalise or copy source frames."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.05)
    # Deliberately unusual: non-ASCII, double spaces, a colon inside data and
    # no trailing separator normalisation to hide behind.
    passthrough = 'event: results\ndata:  {"title":"斗破 苍穹"}  \n\n'
    comment_lookalike = "data: : keepalive\n\n"

    async def source() -> AsyncIterator[str]:
        yield passthrough
        yield comment_lookalike

    stream = api_routes._stream_with_keepalive(source())
    try:
        first = await asyncio.wait_for(anext(stream), 1.0)
        second = await asyncio.wait_for(anext(stream), 1.0)
    finally:
        await stream.aclose()

    # Identity, not just equality: the very object handed out is the one the
    # source yielded.
    assert first is passthrough
    assert second is comment_lookalike
    assert first == passthrough
    assert second == comment_lookalike


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_cancels_the_pending_pull_and_closes_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client disconnect closes the wrapper, which must cancel the pull at once."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()
    baseline = _live_tasks()

    async def source() -> AsyncIterator[str]:
        try:
            started.set()
            await asyncio.sleep(3600)
            yield "never"
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            closed.set()

    stream = api_routes._stream_with_keepalive(source())
    assert await asyncio.wait_for(anext(stream), 1.0) == api_routes.SSE_KEEPALIVE_FRAME
    # Make sure the pull being cancelled is a started coroutine, so its
    # except/finally are guaranteed to run.
    await asyncio.wait_for(started.wait(), 1.0)

    # Far less than the source's 3600 s sleep: the in-flight pull is cancelled,
    # not abandoned to finish on its own.
    await asyncio.wait_for(stream.aclose(), 1.0)

    assert cancelled.is_set()
    assert closed.is_set()
    await asyncio.sleep(0)
    assert _live_tasks() <= baseline


@pytest.mark.asyncio
async def test_consumer_cancellation_cancels_the_pending_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the consuming task (what Starlette does) leaves nothing running."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()
    first_frame = asyncio.Event()
    baseline = _live_tasks()

    async def source() -> AsyncIterator[str]:
        try:
            started.set()
            await asyncio.sleep(3600)
            yield "never"
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            closed.set()

    stream = api_routes._stream_with_keepalive(source())

    async def consume() -> None:
        async for _frame in stream:
            first_frame.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1.0)
    await asyncio.wait_for(first_frame.wait(), 1.0)
    task.cancel()
    await asyncio.wait_for(_join_cancelled(task), 1.0)

    assert task.cancelled()
    assert cancelled.is_set()
    assert closed.is_set()
    await asyncio.sleep(0)
    assert _live_tasks() <= baseline


@pytest.mark.asyncio
async def test_cancel_before_the_first_frame_cancels_the_first_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect at t=0 interrupts the pending pull, not only later ones."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()
    baseline = _live_tasks()

    async def source() -> AsyncIterator[str]:
        try:
            started.set()
            await asyncio.sleep(3600)
            yield "never"
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            closed.set()

    stream = api_routes._stream_with_keepalive(source())

    async def consume() -> None:
        async for _frame in stream:
            pass

    task = asyncio.create_task(consume())
    # Wait until the wrapper is inside its very first pull (not merely
    # scheduled), so the assertion below is about a pull that really exists.
    await asyncio.wait_for(started.wait(), 1.0)
    task.cancel()
    await asyncio.wait_for(_join_cancelled(task), 1.0)

    assert cancelled.is_set()
    assert closed.is_set()
    await asyncio.sleep(0)
    assert _live_tasks() <= baseline


@pytest.mark.asyncio
async def test_source_cancelled_error_propagates_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pull cancelled by the source itself surfaces instead of hanging."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    closed = asyncio.Event()
    baseline = _live_tasks()

    async def source() -> AsyncIterator[str]:
        try:
            yield DATA_1
            raise asyncio.CancelledError
        finally:
            closed.set()

    frames: list[str] = []
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        async for frame in api_routes._stream_with_keepalive(source()):
            frames.append(frame)
    elapsed = time.monotonic() - started

    assert frames == [DATA_1]
    assert closed.is_set()
    # No extra keepalive interval was awaited on the way out.
    assert elapsed < 0.5
    await asyncio.sleep(0)
    assert _live_tasks() <= baseline


# ---------------------------------------------------------------------------
# Route wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_sse_routes_stream_through_the_keepalive_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Each SSE handler must hand the wrapper's generator (not its raw source) to
    Starlette, so a keepalive reaches the wire on all three endpoints.
    """
    original = api_routes._stream_with_keepalive
    created: list[AsyncIterator[str]] = []

    def spy(
        source: AsyncIterator[str], interval: float | None = None
    ) -> AsyncIterator[str]:
        stream = original(source, interval)
        created.append(stream)
        return stream

    monkeypatch.setattr(api_routes, "_stream_with_keepalive", spy)

    responses: list[tuple[str, StreamingResponse]] = [
        ("search", await api_routes.stream_search("斗破", sites="")),
        ("tasks", await api_routes.stream_tasks(_FakeManager())),
        ("login", await api_routes.stream_login_requests()),
    ]

    assert len(created) == len(responses)
    for index, (name, response) in enumerate(responses):
        assert response.media_type == "text/event-stream", name
        # Stronger than "the wrapper was called": the exact generator the
        # wrapper returned is the response body.
        assert response.body_iterator is created[index], name

    for _name, response in responses:
        aclose = getattr(response.body_iterator, "aclose", None)
        if aclose is not None:
            await aclose()
