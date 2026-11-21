#!/usr/bin/env python3
"""
tests.apps.test_sse_backpressure
--------------------------------

Backpressure, cancellation and slow-client behaviour of the SSE endpoints.

Everything is driven at the ASGI level: a scripted ``receive`` hands out
``http.disconnect`` on demand and a scripted ``send`` can be blocked mid-frame
to model a client that stopped reading. No socket is opened, and every
interval is shortened to milliseconds, so nothing waits on a real 15 s
keepalive.

What is pinned here:

* a blocked (slow) consumer does not make the server buffer ahead -- at most
  one source pull is ever outstanding;
* ``http.disconnect`` closes the source generator and cancels its in-flight
  pull, including when the disconnect arrives while ``send`` is blocked (the
  case Starlette's own ``StreamingResponse`` leaks);
* a silent source keeps receiving keepalive frames without growing memory or
  leaking tasks;
* concurrent streams do not block each other -- one slow stream cannot stall
  the event loop for another;
* an exception from the source closes the connection and still runs the
  source's cleanup;
* the stream is bounded by construction: there is no queue, only the single
  pull-ahead, and the tests assert that bound rather than a ``maxsize``.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

import pytest

from novel_downloader.apps.api import create_app
from novel_downloader.apps.api import routes as api_routes

API = "/api/v1"

#: A frame big enough that the assertions about buffering are meaningful.
BIG_FRAME = "event: results\ndata: " + "x" * 4096 + "\n\n"


# ---------------------------------------------------------------------------
# ASGI harness
# ---------------------------------------------------------------------------


def _http_scope(path: str) -> dict[str, Any]:
    """A minimal HTTP scope, as an ASGI server would hand it to the app."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }


class _Receiver:
    """``receive`` that blocks until the test pushes a message."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queue.put_nowait(
            {"type": "http.request", "body": b"", "more_body": False}
        )

    async def receive(self) -> dict[str, Any]:
        return await self._queue.get()

    def disconnect(self) -> None:
        self._queue.put_nowait({"type": "http.disconnect"})


class _Recorder:
    """
    ``send`` that records messages and can be held on a body frame.

    ``gate`` models a client that stopped reading: the server blocks inside
    ``send`` until the test releases it, exactly as a full socket buffer does.
    ``hold_on`` restricts the hold to frames whose text contains that marker
    (``"keepalive"`` or ``"snapshot"``), so a test can stall one kind of frame.
    """

    def __init__(self, *, gate: asyncio.Event | None = None, hold_on: str = "") -> None:
        self.messages: list[dict[str, Any]] = []
        self._gate = gate
        self._hold_on = hold_on
        self.held = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        body = message.get("body", b"")
        if (
            self._gate is not None
            and message["type"] == "http.response.body"
            and body
            and (not self._hold_on or self._hold_on in body.decode("utf-8", "replace"))
        ):
            self.held.set()
            await self._gate.wait()
        self.messages.append(message)

    @property
    def bodies(self) -> list[str]:
        return [
            message.get("body", b"").decode("utf-8")
            for message in self.messages
            if message["type"] == "http.response.body" and message.get("body")
        ]


def _start(
    app: Callable[..., Awaitable[None]],
    path: str,
    *,
    receiver: _Receiver | None = None,
    recorder: _Recorder | None = None,
) -> asyncio.Task[None]:
    """Start the app as a task (the caller drives and awaits it)."""
    receiver = receiver or _Receiver()
    recorder = recorder or _Recorder()
    return asyncio.create_task(app(_http_scope(path), receiver.receive, recorder.send))


def _live_tasks() -> set[asyncio.Task[Any]]:
    """Tasks still running besides the current one (leak detector)."""
    current = asyncio.current_task()
    return {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }


async def _settle() -> None:
    """Let cancelled tasks finish unwinding."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Instrumented sources
# ---------------------------------------------------------------------------


class _Stats:
    """Observations about one source generator."""

    def __init__(self) -> None:
        self.produced = 0
        self.resumed = 0
        self.cancelled = False
        self.closed = False
        #: Pulls (``__anext__`` calls) in flight at the same time, and the max.
        self.inflight = 0
        self.max_inflight = 0


class _Counting:
    """
    Wrap a source iterator and measure concurrent ``__anext__`` calls.

    ``_stream_with_keepalive`` should never have more than one pull outstanding:
    it creates the next one only after handing a frame to the consumer.
    """

    def __init__(self, inner: AsyncIterator[str], stats: _Stats) -> None:
        self._inner = inner
        self._stats = stats

    def __aiter__(self) -> _Counting:
        return self

    async def __anext__(self) -> str:
        stats = self._stats
        stats.inflight += 1
        stats.max_inflight = max(stats.max_inflight, stats.inflight)
        try:
            return await self._inner.__anext__()
        finally:
            stats.inflight -= 1

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()


def _source(
    frames: list[str] | None,
    stats: _Stats,
    *,
    endless: bool = False,
    delay: float = 0.0,
    fail_after: int | None = None,
    error: BaseException | None = None,
) -> AsyncIterator[str]:
    """
    Build an instrumented async generator.

    ``fail_after`` raises ``error`` after that many frames have been yielded;
    ``endless`` keeps yielding ``BIG_FRAME`` forever; ``delay`` sleeps between
    frames. The ``finally`` always records that cleanup ran.
    """

    async def gen() -> AsyncIterator[str]:
        try:
            index = 0
            while endless or (frames is not None and index < len(frames)):
                frame = BIG_FRAME if endless else frames[index]  # type: ignore[index]
                stats.produced += 1
                if fail_after is not None and stats.produced > fail_after:
                    assert error is not None
                    raise error
                yield frame
                stats.resumed += 1
                index += 1
                if delay:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            stats.cancelled = True
            raise
        finally:
            stats.closed = True

    return gen()


def _silent_source(stats: _Stats) -> tuple[AsyncIterator[str], asyncio.Event]:
    """A source that never produces; returns it and the event that ends it."""
    release = asyncio.Event()

    async def gen() -> AsyncIterator[str]:
        try:
            await release.wait()
            yield BIG_FRAME
        except asyncio.CancelledError:
            stats.cancelled = True
            raise
        finally:
            stats.closed = True

    return gen(), release


@pytest.fixture(autouse=True)
def _millisecond_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never wait a real keepalive interval."""
    monkeypatch.setattr(api_routes, "SSE_KEEPALIVE_INTERVAL", 0.01)
    monkeypatch.setattr(api_routes, "TASK_STREAM_INTERVAL", 0.01)
    monkeypatch.setattr(api_routes, "LOGIN_STREAM_INTERVAL", 0.01)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("NOVEL_API_TOKEN", raising=False)


def _build_app(tmp_path: Any) -> Any:
    return create_app(output_dir=tmp_path / "downloads", rate_limit=None)


def _use_task_source(monkeypatch: pytest.MonkeyPatch, source: Any) -> None:
    """Point ``/tasks/stream`` at ``source`` regardless of the interval."""
    monkeypatch.setattr(api_routes, "_task_event_stream", lambda mgr, interval: source)


# ---------------------------------------------------------------------------
# Slow consumer: no unbounded buffering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_consumer_does_not_buffer_ahead(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A client that stops reading must stop the server too.

    While ``send`` is blocked on the first frame the source is not pulled
    again: the wrapper hands the frame to the consumer and creates the next
    pull only when the consumer asks for another frame. There is no queue
    between them, so the buffered amount is a constant, not a function of
    elapsed time.
    """
    stats = _Stats()
    _use_task_source(monkeypatch, _Counting(_source(None, stats, endless=True), stats))

    app = _build_app(tmp_path)
    gate = asyncio.Event()
    receiver = _Receiver()
    recorder = _Recorder(gate=gate, hold_on="results")
    task = _start(app, f"{API}/tasks/stream", receiver=receiver, recorder=recorder)

    await asyncio.wait_for(recorder.held.wait(), 2.0)
    produced_at_block = stats.produced
    await asyncio.sleep(0.1)  # ten keepalive intervals' worth of wall time

    assert stats.produced == produced_at_block, "the source ran on while blocked"
    assert stats.max_inflight <= 1, "more than one pull was outstanding"

    # Walk away rather than unblocking: the request ends without ever
    # accepting the buffered frame.
    receiver.disconnect()
    await asyncio.wait_for(task, 2.0)
    assert stats.closed


@pytest.mark.asyncio
async def test_slow_consumer_does_not_leak_tasks(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer slower than the source leaves nothing behind when it stops."""
    stats = _Stats()
    _use_task_source(
        monkeypatch, _Counting(_source(None, stats, endless=True, delay=0.001), stats)
    )

    app = _build_app(tmp_path)
    baseline = _live_tasks()
    receiver = _Receiver()
    recorder = _Recorder()
    task = _start(app, f"{API}/tasks/stream", receiver=receiver, recorder=recorder)

    await asyncio.sleep(0.05)  # consume a few frames, slowly
    receiver.disconnect()
    await asyncio.wait_for(task, 2.0)
    await _settle()

    assert stats.closed
    assert _live_tasks() <= baseline
    # Everything written is bounded by what the client actually took.  Keepalive
    # comments are not source output, and a loaded machine can emit one between
    # two pulls, so they are excluded from the bound.
    data_frames = [
        body for body in recorder.bodies if body != api_routes.SSE_KEEPALIVE_FRAME
    ]
    assert len(data_frames) <= stats.produced + 1


# ---------------------------------------------------------------------------
# Client disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_closes_the_source_and_cancels_the_pull(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``http.disconnect`` runs the source's cleanup and cancels its pull."""
    stats = _Stats()
    _use_task_source(
        monkeypatch, _Counting(_source(None, stats, endless=True, delay=0.001), stats)
    )

    app = _build_app(tmp_path)
    baseline = _live_tasks()
    receiver = _Receiver()
    recorder = _Recorder()
    task = _start(app, f"{API}/tasks/stream", receiver=receiver, recorder=recorder)
    await asyncio.sleep(0.05)

    receiver.disconnect()
    await asyncio.wait_for(task, 2.0)
    await _settle()

    assert stats.closed
    assert _live_tasks() <= baseline


@pytest.mark.asyncio
async def test_disconnect_while_send_is_blocked_still_closes_the_source(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The regression this file exists for.

    When the client vanishes while the server is blocked in ``send``, Starlette
    cancels the body task at its ``send`` await. That cancellation never
    reaches a generator parked at its ``yield``, so a plain
    ``StreamingResponse`` abandons it -- ``finally`` never runs and the pull
    task survives until the garbage collector notices. The SSE response class
    closes the body iterator itself, so cleanup is prompt and complete.
    """
    stats = _Stats()
    _use_task_source(monkeypatch, _Counting(_source(None, stats, endless=True), stats))

    app = _build_app(tmp_path)
    baseline = _live_tasks()
    gate = asyncio.Event()  # never released: the client is gone
    receiver = _Receiver()
    recorder = _Recorder(gate=gate, hold_on="results")
    task = _start(app, f"{API}/tasks/stream", receiver=receiver, recorder=recorder)

    await asyncio.wait_for(recorder.held.wait(), 2.0)
    receiver.disconnect()
    await asyncio.wait_for(task, 2.0)

    # No garbage collection is allowed to help: the close must have happened
    # as part of the request.
    gc.disable()
    try:
        assert stats.closed, "source generator was abandoned, not closed"
        await _settle()
        assert _live_tasks() <= baseline, "a pull task outlived the request"
    finally:
        gc.enable()


@pytest.mark.asyncio
async def test_disconnect_while_idle_keepalive_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A silent source with a keepalive frame in ``send`` also cleans up.

    This is the harder variant: the wrapper is parked at ``yield
    SSE_KEEPALIVE_FRAME`` while a background pull is outstanding, so the
    abandoned state includes a live pull task.
    """
    stats = _Stats()
    source, release = _silent_source(stats)
    baseline = _live_tasks()

    response = api_routes._SSEStreamingResponse(
        api_routes._stream_with_keepalive(source, 0.01),
        media_type="text/event-stream",
    )
    gate = asyncio.Event()
    receiver = _Receiver()
    recorder = _Recorder(gate=gate, hold_on="keepalive")
    task = asyncio.create_task(
        response(_http_scope("/stream"), receiver.receive, recorder.send)
    )

    await asyncio.wait_for(recorder.held.wait(), 2.0)
    receiver.disconnect()
    await asyncio.wait_for(task, 2.0)

    gc.disable()
    try:
        assert stats.closed
        await _settle()
        assert _live_tasks() <= baseline
    finally:
        gc.enable()
        release.set()


@pytest.mark.asyncio
async def test_wrapper_aclose_closes_a_source_parked_at_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the wrapper alone is enough -- the response class is a belt."""
    stats = _Stats()
    stream = api_routes._stream_with_keepalive(
        _source(["event: results\ndata: {}\n\n"], stats), 0.01
    )

    assert await asyncio.wait_for(anext(stream), 2.0) == "event: results\ndata: {}\n\n"
    await asyncio.wait_for(stream.aclose(), 2.0)

    assert stats.closed


# ---------------------------------------------------------------------------
# Idle keepalives: sustained, bounded, no leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_stream_keeps_sending_keepalives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent source still produces comment frames, on a fixed cadence."""
    stats = _Stats()
    source, release = _silent_source(stats)
    stream = api_routes._stream_with_keepalive(source, 0.005)

    frames: list[str] = []
    try:
        for _ in range(20):
            frames.append(await asyncio.wait_for(anext(stream), 1.0))
    finally:
        release.set()
        await stream.aclose()

    assert frames == [api_routes.SSE_KEEPALIVE_FRAME] * 20
    # One shared constant, not a fresh buffer per frame.
    assert len({id(frame) for frame in frames}) == 1


@pytest.mark.asyncio
async def test_keepalives_do_not_grow_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Hundreds of idle frames cost no memory.

    A buffering implementation would accumulate the emitted frames; this one
    hands the same constant string to the consumer and keeps nothing.
    """
    stats = _Stats()
    source, release = _silent_source(stats)
    stream = api_routes._stream_with_keepalive(source, 0.0)

    try:
        for _ in range(10):  # warm up the allocator
            await anext(stream)

        gc.collect()
        tracemalloc.start()
        try:
            for _ in range(500):
                assert await anext(stream) == api_routes.SSE_KEEPALIVE_FRAME
            gc.collect()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        release.set()
        await stream.aclose()

    # 500 retained 4 KiB frames would be ~2 MiB.
    assert peak < 256 * 1024


@pytest.mark.asyncio
async def test_idle_stream_does_not_leak_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keepalive mode keeps exactly one pull alive, and drops it on close."""
    stats = _Stats()
    source, release = _silent_source(stats)
    counting = _Counting(source, stats)

    baseline = _live_tasks()
    stream = api_routes._stream_with_keepalive(counting, 0.005)
    for _ in range(30):
        await anext(stream)

    live = _live_tasks() - baseline
    assert len(live) == 1, "exactly one pull should be outstanding"
    assert stats.max_inflight == 1

    release.set()
    await asyncio.wait_for(stream.aclose(), 2.0)
    await _settle()
    assert stats.closed
    assert _live_tasks() <= baseline


# ---------------------------------------------------------------------------
# Concurrent streams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_streams_do_not_block_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One stalled stream must not freeze another.

    The slow stream holds an unresolved pull for the whole test. A second
    stream on the same event loop must still deliver its frames; a blocking
    implementation would serialize them behind the first.
    """
    slow_stats = _Stats()
    slow_source, slow_release = _silent_source(slow_stats)
    fast_stats = _Stats()

    # A long interval keeps the slow stream parked inside its very first pull
    # for the whole test; the fast stream is unaffected by that setting.
    slow = api_routes._stream_with_keepalive(slow_source, 60.0)
    fast = api_routes._stream_with_keepalive(_source(["a", "b", "c"], fast_stats), 0.01)

    try:
        slow_pull = asyncio.create_task(anext(slow))
        await asyncio.sleep(0.02)
        assert not slow_pull.done()

        started = asyncio.get_running_loop().time()
        frames = [frame async for frame in fast]
        elapsed = asyncio.get_running_loop().time() - started

        assert frames == ["a", "b", "c"]
        assert elapsed < 1.0, "the fast stream waited on the slow one"
        assert not slow_pull.done()
    finally:
        # Cancelling the stalled pull unwinds the wrapper, which cancels its
        # own in-flight pull and closes the source; only then is the generator
        # free to be closed again.
        slow_pull.cancel()
        with suppress(asyncio.CancelledError):
            await slow_pull
        slow_release.set()
        await slow.aclose()
        await fast.aclose()


@pytest.mark.asyncio
async def test_many_concurrent_streams_stay_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten streams in flight at once each get their own keepalive cadence."""
    releases: list[asyncio.Event] = []
    streams = []
    for _ in range(10):
        stats = _Stats()
        source, release = _silent_source(stats)
        releases.append(release)
        streams.append(api_routes._stream_with_keepalive(source, 0.005))

    async def pull_five(stream: AsyncIterator[str]) -> list[str]:
        # Pulls on one stream are sequential; the ten streams run concurrently.
        return [await anext(stream) for _ in range(5)]

    baseline = _live_tasks()
    try:
        batches = await asyncio.gather(*(pull_five(stream) for stream in streams))
        for frames in batches:
            assert frames == [api_routes.SSE_KEEPALIVE_FRAME] * 5
    finally:
        for release in releases:
            release.set()
        for stream in streams:
            await stream.aclose()
    await _settle()
    assert _live_tasks() <= baseline


# ---------------------------------------------------------------------------
# Source errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_exception_closes_the_connection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A raising source propagates out of the response -- the ASGI server turns
    that into a closed connection -- and its cleanup still runs.
    """
    stats = _Stats()
    _use_task_source(
        monkeypatch,
        _source(None, stats, endless=True, fail_after=1, error=RuntimeError("boom")),
    )

    app = _build_app(tmp_path)
    baseline = _live_tasks()
    recorder = _Recorder()
    task = _start(app, f"{API}/tasks/stream", recorder=recorder)

    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(task, 2.0)
    await _settle()

    assert stats.closed
    assert _live_tasks() <= baseline
    # The head and the one good frame reached the wire; no final empty frame
    # (``more_body: False``) was sent, so the connection ends abnormally.
    assert recorder.messages[0]["type"] == "http.response.start"
    assert recorder.messages[-1]["type"] == "http.response.body"
    assert recorder.messages[-1]["more_body"] is True
    assert not any(
        message["type"] == "http.response.body" and not message.get("more_body")
        for message in recorder.messages
    )


@pytest.mark.asyncio
async def test_source_exception_during_idle_pull_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error raised by the pending pull surfaces at the next frame request."""
    release = asyncio.Event()
    closed = asyncio.Event()

    async def failing() -> AsyncIterator[str]:
        try:
            await release.wait()
            raise ValueError("source failed while idle")
            yield BIG_FRAME  # pragma: no cover - unreachable
        finally:
            closed.set()

    stream = api_routes._stream_with_keepalive(failing(), 0.005)
    frames: list[str] = []
    with pytest.raises(ValueError, match="source failed while idle"):
        async for frame in stream:
            frames.append(frame)
            if len(frames) == 3:
                release.set()

    assert frames == [api_routes.SSE_KEEPALIVE_FRAME] * 3
    assert closed.is_set()


# ---------------------------------------------------------------------------
# Boundedness by construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_most_one_pull_is_outstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The wrapper buffers exactly one frame, never a growing queue.

    ``_stream_with_keepalive`` deliberately uses no ``asyncio.Queue``: the only
    look-ahead is the single ``anext`` task that a keepalive may overtake.
    """
    stats = _Stats()
    stream = api_routes._stream_with_keepalive(
        _Counting(_source(None, stats, endless=True), stats), 0.005
    )
    try:
        # Pull one frame, then stop consuming: nothing else may be pulled.
        assert await anext(stream) == BIG_FRAME
        await asyncio.sleep(0.05)
        assert stats.produced == 1
        assert stats.max_inflight == 1
    finally:
        await stream.aclose()

    assert stats.max_inflight == 1


@pytest.mark.asyncio
async def test_all_sse_routes_return_the_self_closing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    All three SSE handlers build the response class that owns iterator cleanup.

    A plain ``StreamingResponse`` would silently reintroduce the abandoned
    generator on a disconnect that lands during ``send``.
    """
    assert issubclass(api_routes._SSEStreamingResponse, api_routes.StreamingResponse)

    stats = _Stats()
    _use_task_source(monkeypatch, _source(None, stats, endless=True))

    class _FakeManager:
        def snapshot(self) -> dict[str, list[Any]]:
            return {"pending": [], "running": [], "completed": []}

    responses = [
        await api_routes.stream_search("斗破", sites=""),
        await api_routes.stream_tasks(_FakeManager()),
        await api_routes.stream_login_requests(),
    ]

    for response in responses:
        assert isinstance(response, api_routes._SSEStreamingResponse)
        assert response.media_type == "text/event-stream"
        aclose = getattr(response.body_iterator, "aclose", None)
        if aclose is not None:
            await aclose()
