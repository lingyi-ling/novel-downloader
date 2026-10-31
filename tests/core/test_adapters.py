#!/usr/bin/env python3
"""
tests.core.test_adapters
------------------------

Offline unit tests for the web UI adapters (``apps.core.adapters``) and the
credential request broker (``apps.core.cred_broker``) that
:class:`~novel_downloader.apps.core.adapters.WebLoginUI` drives.

The adapters are exercised exactly the way ``TaskManager`` consumes them: a
:class:`~novel_downloader.apps.core.models.DownloadTask` is mutated through the
adapter callbacks, and ``WebLoginUI.prompt`` runs as a task while another
coroutine claims and completes the credential request. Everything runs inside
pytest-asyncio; there is no network, no GUI and no real site client.

The two final tests wire the adapters into a real ``TaskManager`` whose site
client and config adapter are fakes, mirroring the convention of
``tests.core.test_task_manager_robustness``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.core import adapters, cred_broker
from novel_downloader.apps.core.adapters import (
    WebDownloadUI,
    WebExportUI,
    WebLoginUI,
    WebProcessUI,
)
from novel_downloader.apps.core.cred_broker import (
    CLAIM_TTL,
    claim_next_request,
    cleanup_request,
    complete_request,
    create_cred_request,
    get_req_state,
    list_requests,
    refresh_claim,
)
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import TaskManager
from novel_downloader.schemas import BookConfig, LoginField

BOOK = BookConfig(book_id="42")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any lazy config/state lookup outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Give every test a pristine credential broker.

    The broker keeps a module-global request store and an ``asyncio.Lock``
    created at import time. A contended lock binds itself to the first event
    loop that ever queues on it, which would poison later tests running on a
    fresh loop, and leftover requests would be handed out first by
    ``claim_next_request``. Both are reset before each test.
    """
    monkeypatch.setattr(cred_broker, "_CRED_LOCK", asyncio.Lock())
    monkeypatch.setattr(cred_broker, "_CRED_REQS", {})


def _task(**kwargs: Any) -> DownloadTask:
    kwargs.setdefault("title", "Demo Book")
    kwargs.setdefault("site", "demo")
    kwargs.setdefault("book_id", "42")
    return DownloadTask(**kwargs)


def _field(name: str, type_: str = "text") -> LoginField:
    return LoginField(name=name, label=name.title(), type=type_, required=False)  # type: ignore[arg-type]


async def _claim_any(client_id: str, *, timeout: float = 2.0) -> Any:
    """Claim the next request, polling until the producer has created one."""
    deadline = time.monotonic() + timeout
    while True:
        req = await claim_next_request(client_id)
        if req is not None:
            return req
        if time.monotonic() > deadline:
            raise AssertionError("no pending credential request appeared")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# WebDownloadUI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_ui_lifecycle_updates_status_and_progress() -> None:
    task = _task()
    ui = WebDownloadUI(task)
    assert task.status is Status.QUEUED

    await ui.on_start(BOOK)
    assert task.status is Status.RUNNING

    # Long enough for time.monotonic() (coarse on Windows) to advance, so a
    # chapter time is actually recorded.
    await asyncio.sleep(0.05)
    await ui.on_progress(3, 10)
    assert task.chapters_total == 10
    assert task.chapters_done == 3
    assert task.progress() == 0.3
    # Chapter timings feed the ETA estimate.
    eta = task.eta()
    assert eta is not None and 0 < eta < 120

    await ui.on_complete(BOOK)
    assert task.status is Status.EXPORTING


@pytest.mark.asyncio
async def test_download_ui_progress_error_propagates_but_counters_stick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter never swallows callback errors: they reach the client."""
    task = _task()
    ui = WebDownloadUI(task)
    await ui.on_start(BOOK)

    def boom() -> None:
        raise RuntimeError("speed tracker broken")

    monkeypatch.setattr(task, "record_chapter_time", boom)

    with pytest.raises(RuntimeError, match="speed tracker broken"):
        await ui.on_progress(5, 10)

    # Counters are updated before the bookkeeping hook runs.
    assert task.chapters_done == 5
    assert task.chapters_total == 10
    assert task.status is Status.RUNNING


# ---------------------------------------------------------------------------
# WebExportUI
# ---------------------------------------------------------------------------


def test_export_ui_clears_stale_paths_on_init() -> None:
    task = _task()
    task.exported_paths = {"txt": Path("old.txt")}
    WebExportUI(task)
    assert task.exported_paths == {}


def test_export_ui_success_records_paths_without_completing() -> None:
    task = _task()
    ui = WebExportUI(task)

    ui.on_start(BOOK, "txt")
    assert task.status is Status.EXPORTING

    ui.on_success(BOOK, "txt", Path("out/book.txt"))
    ui.on_success(BOOK, "epub", Path("out/book.epub"))

    assert task.exported_paths == {
        "txt": Path("out/book.txt"),
        "epub": Path("out/book.epub"),
    }
    # Marking the task COMPLETED is the export worker's job, not the adapter's.
    assert task.status is Status.EXPORTING
    assert task.error is None


def test_export_ui_error_marks_task_failed() -> None:
    task = _task()
    ui = WebExportUI(task)
    ui.on_start(BOOK)

    ui.on_error(BOOK, "epub", RuntimeError("disk full"))

    assert task.status is Status.FAILED
    assert task.error == "disk full"


def test_export_ui_unsupported_format_sets_error_only() -> None:
    task = _task()
    ui = WebExportUI(task)
    ui.on_start(BOOK)

    ui.on_unsupported(BOOK, "pdf")

    assert task.error == "Export format 'pdf' is not supported."
    # The adapter records the problem but leaves the status decision alone.
    assert task.status is Status.EXPORTING
    assert task.exported_paths == {}


def test_export_ui_empty_export_records_nothing() -> None:
    """An export that returns no files looks like a normal in-flight export."""
    task = _task()
    ui = WebExportUI(task)
    ui.on_start(BOOK)

    # No on_success / on_error ever fired.
    assert task.exported_paths == {}
    assert task.error is None
    assert task.status is Status.EXPORTING


# ---------------------------------------------------------------------------
# WebProcessUI
# ---------------------------------------------------------------------------


def test_process_ui_stage_start_resets_progress() -> None:
    task = _task()
    task.chapters_done = 7
    ui = WebProcessUI(task)

    ui.on_stage_start(BOOK, "merge")

    assert task.status is Status.PROCESSING
    assert task.chapters_done == 0


def test_process_ui_stage_progress_updates_counters() -> None:
    task = _task()
    ui = WebProcessUI(task)
    ui.on_stage_start(BOOK, "merge")

    # Long enough for time.monotonic() (coarse on Windows) to advance, so a
    # chapter time is actually recorded.
    time.sleep(0.05)
    ui.on_stage_progress(BOOK, "merge", 2, 9)

    assert task.chapters_total == 9
    assert task.chapters_done == 2
    eta = task.eta()
    assert eta is not None and 0 < eta < 120


def test_process_ui_stage_complete_is_quiet() -> None:
    task = _task()
    ui = WebProcessUI(task)
    ui.on_stage_start(BOOK, "merge")

    ui.on_stage_complete(BOOK, "merge")

    assert task.status is Status.PROCESSING
    assert task.error is None


def test_process_ui_missing_data_fails_task() -> None:
    task = _task()
    ui = WebProcessUI(task)
    missing = Path("raw_data") / "42"

    ui.on_missing(BOOK, "raw chapters", missing)

    assert task.status is Status.FAILED
    assert task.error == f"Missing required data (raw chapters): {missing}"


# ---------------------------------------------------------------------------
# Cancellation is terminal: thread-side callbacks never resurrect a task
# ---------------------------------------------------------------------------


def test_export_ui_callbacks_never_overwrite_cancelled() -> None:
    """
    Export callbacks run inside the export worker *thread* while
    ``TaskManager.cancel_task`` runs on the event loop, so a callback fired
    after a cancellation must not flip the task back to EXPORTING/FAILED.
    """
    task = _task()
    task.status = Status.CANCELLED
    task.error = "Cancelled by user"
    ui = WebExportUI(task)

    ui.on_start(BOOK, "txt")
    assert task.status is Status.CANCELLED

    ui.on_error(BOOK, "txt", RuntimeError("disk full"))
    assert task.status is Status.CANCELLED
    assert task.error == "Cancelled by user"

    ui.on_unsupported(BOOK, "pdf")
    assert task.error == "Cancelled by user"

    # Factual bookkeeping (a file that was really written) is still recorded.
    ui.on_success(BOOK, "txt", Path("out/42.txt"))
    assert task.exported_paths == {"txt": Path("out/42.txt")}
    assert task.status is Status.CANCELLED


def test_process_ui_callbacks_never_overwrite_cancelled() -> None:
    """Same guarantee for the process worker's thread-side callbacks."""
    task = _task()
    task.status = Status.CANCELLED
    task.error = "Cancelled by user"
    ui = WebProcessUI(task)

    ui.on_stage_start(BOOK, "merge")
    assert task.status is Status.CANCELLED

    ui.on_missing(BOOK, "raw chapters", Path("raw_data/42"))
    assert task.status is Status.CANCELLED
    assert task.error == "Cancelled by user"


# ---------------------------------------------------------------------------
# Callback order across the whole pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapters_walk_task_through_the_pipeline() -> None:
    """QUEUED -> RUNNING -> PROCESSING -> EXPORTING, as TaskManager drives it."""
    task = _task()
    download_ui = WebDownloadUI(task)
    process_ui = WebProcessUI(task)
    export_ui = WebExportUI(task)

    timeline = [task.status]
    await download_ui.on_start(BOOK)
    timeline.append(task.status)
    await download_ui.on_progress(4, 4)
    await download_ui.on_complete(BOOK)
    timeline.append(task.status)

    process_ui.on_stage_start(BOOK, "merge")
    timeline.append(task.status)
    process_ui.on_stage_progress(BOOK, "merge", 4, 4)
    process_ui.on_stage_complete(BOOK, "merge")

    export_ui.on_start(BOOK, "txt")
    timeline.append(task.status)
    export_ui.on_success(BOOK, "txt", Path("book.txt"))

    assert [status.value for status in timeline] == [
        "queued",
        "running",
        "exporting",
        "processing",
        "exporting",
    ]
    assert task.exported_paths == {"txt": Path("book.txt")}


@pytest.mark.asyncio
async def test_adapters_surface_failures_from_any_stage() -> None:
    task = _task()
    WebDownloadUI(task)
    WebProcessUI(task)
    WebExportUI(task)

    # Login failure.
    WebLoginUI(task).on_login_failed()
    assert task.status is Status.FAILED
    assert task.error == "Login failed."

    # Download stage recovered first: on_login_success resumes it.
    WebLoginUI(task).on_login_success()
    assert task.status is Status.RUNNING

    # Processing failure.
    WebProcessUI(task).on_missing(BOOK, "cache", Path("x"))
    assert task.status is Status.FAILED

    # Export failure wins last.
    WebExportUI(task).on_error(BOOK, "epub", ValueError("bad metadata"))
    assert task.status is Status.FAILED
    assert task.error == "bad metadata"


# ---------------------------------------------------------------------------
# WebLoginUI.prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_returns_credentials_and_cleans_up() -> None:
    task = _task()
    ui = WebLoginUI(task)
    fields = [_field("username"), _field("cookie", "cookie")]
    prefill = {"username": "  pre  ", "cookie": "a=1"}

    prompt_task = asyncio.create_task(ui.prompt(fields, prefill=prefill))
    req = await _claim_any("frontend")

    # The request carries the task identity and a snapshot of the fields.
    assert req.task_id == task.task_id
    assert req.title == "Demo Book"
    assert req.fields == fields
    # The caller's prefill dict is copied, never mutated.
    assert prefill == {"username": "  pre  ", "cookie": "a=1"}

    # The broker stores the raw submitted strings...
    await complete_request(
        req.req_id,
        {"username": " ui_user ", "cookie": "k=v; x=y", "junk": 123},
    )
    assert req.result == {"username": " ui_user ", "cookie": "k=v; x=y", "junk": 123}
    assert req.done

    result = await asyncio.wait_for(prompt_task, timeout=2.0)

    # ...while the adapter strips them, drops non-string values and parses
    # cookie fields into a dict.
    assert result == {"username": "ui_user", "cookie": {"k": "v", "x": "y"}}
    # prompt() itself does not touch the task status.
    assert task.status is Status.QUEUED
    # The request was fully consumed: nothing is left to claim.
    assert await claim_next_request("frontend") is None


@pytest.mark.asyncio
async def test_prompt_without_prefill_returns_submitted_values() -> None:
    task = _task()
    ui = WebLoginUI(task)

    prompt_task = asyncio.create_task(ui.prompt([_field("username")]))
    req = await _claim_any("frontend")
    await complete_request(req.req_id, {"username": "ui"})
    result = await asyncio.wait_for(prompt_task, timeout=2.0)

    assert result == {"username": "ui"}


@pytest.mark.asyncio
async def test_prompt_times_out_and_returns_prefill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapters, "REQUEST_TIMEOUT", 0.05)
    task = _task()
    ui = WebLoginUI(task)
    prefill = {"username": " pre "}

    result = await asyncio.wait_for(
        ui.prompt([_field("username")], prefill=prefill),
        timeout=2.0,
    )

    # The task falls back to the prefill instead of failing hard. Note that
    # the timeout path returns the values verbatim: unlike the success path
    # they are not stripped (kept as-is here and reported separately).
    assert result == {"username": " pre "}
    assert task.status is Status.QUEUED
    # The timed-out request was completed with None and removed from the store.
    assert await claim_next_request("frontend") is None


@pytest.mark.asyncio
async def test_prompt_returns_prefill_when_task_cancelled() -> None:
    task = _task()
    ui = WebLoginUI(task)

    prompt_task = asyncio.create_task(
        ui.prompt([_field("username")], prefill={"username": "pre"})
    )
    req = await _claim_any("frontend")

    task.status = Status.CANCELLED
    await complete_request(req.req_id, {"username": "too-late"})
    result = await asyncio.wait_for(prompt_task, timeout=2.0)

    # Submitted credentials are discarded once the task is cancelled.
    assert result == {"username": "pre"}
    assert await claim_next_request("frontend") is None


@pytest.mark.asyncio
async def test_prompt_cancellation_releases_the_request() -> None:
    """
    Cancelling the waiter must not leak the credential request.

    The download task is cancelled while ``prompt`` waits for the frontend
    (``TaskManager.cancel_task`` cancels the download asyncio task). The
    abandoned request used to stay in the broker forever: claimable and
    listed, a zombie login form for a dead task.
    """
    task = _task()
    ui = WebLoginUI(task)

    prompt_task = asyncio.create_task(ui.prompt([_field("username")]))
    req = await _claim_any("frontend")

    prompt_task.cancel()
    await asyncio.gather(prompt_task, return_exceptions=True)

    assert await get_req_state(req.req_id) == (False, False)
    assert await list_requests() == []
    assert await claim_next_request("other-client") is None


def test_login_callbacks_move_status() -> None:
    task = _task()
    ui = WebLoginUI(task)

    ui.on_login_success()
    assert task.status is Status.RUNNING
    assert task.error is None

    ui.on_login_failed()
    assert task.status is Status.FAILED
    assert task.error == "Login failed."


# ---------------------------------------------------------------------------
# Credential broker lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cred_request_full_lifecycle() -> None:
    fields = [_field("username")]
    req = await create_cred_request(task_id="t1", title="Book", fields=fields)

    assert await get_req_state(req.req_id) == (True, False)
    assert req.event.is_set() is False
    assert req.claimed_by is None
    assert req.result is None

    first = await claim_next_request("client-a")
    assert first is req
    assert first.claimed_by == "client-a"
    assert first.claimed_at is not None

    # A claimed, unexpired request is not handed out twice.
    assert await claim_next_request("client-b") is None

    await complete_request(req.req_id, {"username": "u"})
    assert await get_req_state(req.req_id) == (True, True)
    assert req.result == {"username": "u"}
    assert req.event.is_set()

    # Completing twice keeps the first result.
    await complete_request(req.req_id, {"username": "other"})
    assert req.result == {"username": "u"}

    cleanup_request(req.req_id)
    assert await get_req_state(req.req_id) == (False, False)
    # And the freed id can be claimed by nobody.
    assert await claim_next_request("client-c") is None


@pytest.mark.asyncio
async def test_create_snapshots_fields_and_generates_unique_ids() -> None:
    fields = [_field("username")]
    req = await create_cred_request(task_id="t1", title="Book", fields=fields)
    fields.append(_field("password"))
    # The field list is copied at creation time.
    assert [f.name for f in req.fields] == ["username"]
    assert req.prefill == {}

    other = await create_cred_request(
        task_id="t2",
        title="Book 2",
        fields=fields,
        prefill={"password": "s3cret"},
    )
    assert other.prefill == {"password": "s3cret"}
    assert other.req_id != req.req_id


class _FakeClock:
    """Stand-in for the ``time`` module used inside :mod:`cred_broker`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_stale_claim_is_released_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(cred_broker, "time", clock)

    req = await create_cred_request(task_id="t1", title="Book", fields=[])
    await claim_next_request("client-a")
    assert req.claimed_by == "client-a"

    clock.now += CLAIM_TTL - 1
    assert await claim_next_request("client-b") is None

    clock.now += 2
    again = await claim_next_request("client-b")
    assert again is req
    assert again.claimed_by == "client-b"


@pytest.mark.asyncio
async def test_refresh_claim_extends_lease_only_for_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(cred_broker, "time", clock)

    req = await create_cred_request(task_id="t1", title="Book", fields=[])
    await claim_next_request("client-a")
    assert req.claimed_at is not None

    clock.now += 10
    await refresh_claim(req.req_id, "client-a")
    assert req.claimed_at == clock.now

    # Inside the refreshed lease nobody else can claim.
    clock.now += CLAIM_TTL - 1
    assert await claim_next_request("client-b") is None

    # Refreshing under a client id that does not own the claim is a no-op.
    owned_at = req.claimed_at
    clock.now += 5
    await refresh_claim(req.req_id, "client-b")
    assert req.claimed_at == owned_at

    # Past the refreshed lease the claim is stale and reassigned.
    clock.now += CLAIM_TTL
    assert await claim_next_request("client-b") is req

    # The original owner can no longer refresh the claim it lost.
    reassigned_at = req.claimed_at
    clock.now += 5
    await refresh_claim(req.req_id, "client-a")
    assert req.claimed_at == reassigned_at


@pytest.mark.asyncio
async def test_refresh_claim_ignores_done_and_unknown_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(cred_broker, "time", clock)

    req = await create_cred_request(task_id="t1", title="Book", fields=[])
    await claim_next_request("client-a")
    await complete_request(req.req_id, {"username": "u"})
    claimed_at = req.claimed_at

    clock.now += 5
    await refresh_claim(req.req_id, "client-a")
    await refresh_claim("no-such-id", "client-a")
    assert req.claimed_at == claimed_at


@pytest.mark.asyncio
async def test_broker_ignores_unknown_request_ids() -> None:
    await complete_request("missing", {"a": "b"})  # must not raise
    assert await get_req_state("missing") == (False, False)
    cleanup_request("missing")  # must not raise
    assert await claim_next_request("client-a") is None  # empty store


# ---------------------------------------------------------------------------
# Adapters as consumed by the real task manager
# ---------------------------------------------------------------------------


class _NoStagesAdapter:
    """Config double: no login, no processors, default exporter."""

    def get_login_required(self, site: str) -> bool:
        return False

    def get_login_config(self, site: str) -> dict[str, Any]:
        return {}

    def get_processor_configs(self, site: str) -> list[Any]:
        return []

    def get_exporter_config(self, site: str) -> None:
        return None

    def get_client_config(self, site: str) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {}


class _LoginRequiredAdapter(_NoStagesAdapter):
    def get_login_required(self, site: str) -> bool:
        return True


class _PipelineClient:
    """Site client double that drives the real adapter callbacks."""

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir

    async def __aenter__(self) -> _PipelineClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        return True

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        await ui.on_start(book)
        await ui.on_progress(1, 2)
        await ui.on_progress(2, 2)
        await ui.on_complete(book)

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> dict[str, list[Path]]:
        ui.on_start(book, "txt")
        out = self._out_dir / f"{book.book_id}.txt"
        out.write_text("exported", encoding="utf-8")
        ui.on_success(book, "txt", out)
        return {"txt": [out]}


class _LoginClient:
    """Site client double whose login round-trips through the cred broker."""

    async def __aenter__(self) -> _LoginClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        values = await ui.prompt(
            [LoginField(name="username", label="Username", type="text", required=True)]
        )
        return bool(values.get("username"))

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        await ui.on_start(book)

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> None:
        return None


async def _wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, yielding to the event loop."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


def _seeded_manager(client: Any, adapter: Any) -> TaskManager:
    """Build a manager wired to fakes (never touches registrar or config)."""
    manager = TaskManager()
    manager._adapter = adapter  # type: ignore[assignment]
    manager._clients["demo"] = client  # type: ignore[assignment]
    return manager


@pytest.mark.asyncio
async def test_manager_drives_adapters_to_completed(tmp_path: Path) -> None:
    manager = _seeded_manager(_PipelineClient(tmp_path), _NoStagesAdapter())
    try:
        task = await manager.add_task(title="Demo Book", site="demo", book_id="42")
        assert task.status is Status.QUEUED

        # The site worker already lists the task as completed while the
        # separate export worker is still running, so wait for the status.
        await _wait_until(lambda: task.status is Status.COMPLETED)

        assert list(manager.completed) == [task]
        assert task.status is Status.COMPLETED
        assert task.error is None
        assert task.finished_at is not None
        # The WebDownloadUI progress callbacks were consumed.
        assert (task.chapters_done, task.chapters_total) == (2, 2)
        # The WebExportUI recorded the produced file.
        assert task.exported_paths == {"txt": tmp_path / "42.txt"}
        assert (tmp_path / "42.txt").read_text(encoding="utf-8") == "exported"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_manager_login_flow_completes_via_cred_broker() -> None:
    """A login-required site parks in prompt() until the broker resolves it."""
    manager = _seeded_manager(_LoginClient(), _LoginRequiredAdapter())
    try:
        task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

        req = await _claim_any("frontend", timeout=5.0)
        assert req.task_id == task.task_id
        assert task.status is Status.RUNNING

        await complete_request(req.req_id, {"username": "alice"})

        await _wait_until(lambda: task.status is Status.COMPLETED)
        assert list(manager.completed) == [task]
        assert task.error is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_manager_cancel_of_login_task_releases_cred_request() -> None:
    """
    Cancelling a task parked in the login prompt resolves its request.

    The end-to-end path for the leak: ``cancel_task`` cancels the download
    asyncio task, the CancelledError unwinds ``prompt`` -- which must complete
    and remove the request instead of leaving a zombie behind.
    """
    manager = _seeded_manager(_LoginClient(), _LoginRequiredAdapter())
    try:
        task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

        req = await _claim_any("frontend", timeout=5.0)

        assert await manager.cancel_task(task.task_id) is True

        await _wait_until(lambda: not manager._worker_tasks)
        assert task.status is Status.CANCELLED
        assert task.error == "Cancelled by user"
        assert await get_req_state(req.req_id) == (False, False)
        assert await list_requests() == []
    finally:
        await manager.close()
