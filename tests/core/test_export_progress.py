#!/usr/bin/env python3
"""
tests.core.test_export_progress
-------------------------------

Offline tests for export-stage progress reporting.

Export progress is reported at *format* granularity: the export client fires
``on_start`` once per requested format (the format name already travels in
the existing ``ExportUI.on_start`` signature, so the client needs no change)
and ``on_success`` once per written file. The ``WebExportUI`` adapter turns
those callbacks into consumer-visible state:

* ``task.export_stage`` -- the format currently being written, so a consumer
  can render ``Exporting (txt) ...`` instead of a bare ``Exporting``;
* ``WebExportUI.exported_count`` -- files written so far in this run.

The stage deliberately never flows through ``task.error``: the final-status
contract (``tests.core.test_export_outcome``) keeps failure details in that
field, and a later format's ``on_start`` must not erase them.

Layers exercised without touching the network:

* the real ``WebExportUI`` adapter (stage tracking, per-file counting);
* the real ``CommonClient.export_book`` callback order for multi- and
  single-format runs, failures and unsupported formats;
* the real ``TaskManager`` export worker, proving the stage survives on the
  task a consumer receives from ``snapshot()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.core.adapters import WebExportUI
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.task_manager import TaskManager
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.schemas import BookConfig, ExporterConfig

BOOK = BookConfig(book_id="42")
OUT_DIR = Path("downloads") / "demo"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any lazy config/state lookup outside the real user profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))


def _task() -> DownloadTask:
    return DownloadTask(title="Demo Book", site="demo", book_id="42")


# ---------------------------------------------------------------------------
# WebExportUI: stage tracking and per-file counting
# ---------------------------------------------------------------------------


def test_export_ui_init_resets_stage_and_counter() -> None:
    task = _task()
    task.export_stage = "txt"  # stale from a prior run
    ui = WebExportUI(task)

    assert ui.export_stage is None
    assert task.export_stage is None
    assert ui.exported_count == 0


def test_export_ui_on_start_tracks_current_format() -> None:
    task = _task()
    ui = WebExportUI(task)

    ui.on_start(BOOK, "txt")
    assert ui.export_stage == "txt"
    assert task.export_stage == "txt"
    assert task.status is Status.EXPORTING

    # The next format's on_start switches the stage right away.
    ui.on_start(BOOK, "epub")
    assert ui.export_stage == "epub"
    assert task.export_stage == "epub"
    assert task.status is Status.EXPORTING


def test_export_ui_on_start_without_format_clears_stage() -> None:
    """``fmt`` is optional in the protocol; ``None`` means unknown, not stale."""
    task = _task()
    ui = WebExportUI(task)

    ui.on_start(BOOK, "txt")
    ui.on_start(BOOK)

    assert ui.export_stage is None
    assert task.export_stage is None


def test_export_ui_success_counts_files_not_formats() -> None:
    """One format may write several files (e.g. one per volume)."""
    task = _task()
    ui = WebExportUI(task)
    ui.on_start(BOOK, "txt")

    ui.on_success(BOOK, "txt", OUT_DIR / "vol1.txt")
    assert ui.exported_count == 1
    assert ui.export_stage == "txt"  # stage stays until the next on_start

    ui.on_success(BOOK, "txt", OUT_DIR / "vol2.txt")
    assert ui.exported_count == 2
    assert ui.exported_any is True
    assert task.exported_paths == {"txt": OUT_DIR / "vol2.txt"}


def test_export_ui_error_keeps_stage_and_count() -> None:
    task = _task()
    ui = WebExportUI(task)
    ui.on_start(BOOK, "txt")

    ui.on_error(BOOK, "txt", RuntimeError("disk full"))

    assert ui.exported_count == 0
    assert ui.export_stage == "txt"
    assert task.export_stage == "txt"
    assert task.error == "disk full"
    assert task.status is Status.FAILED


def test_export_stage_never_flows_through_error_field() -> None:
    """Starting a format must not overwrite task.error's failure semantics."""
    task = _task()
    ui = WebExportUI(task)

    ui.on_start(BOOK, "txt")
    assert task.error is None

    ui.on_error(BOOK, "txt", RuntimeError("disk full"))
    ui.on_start(BOOK, "epub")  # a later format must not erase the failure
    assert task.error == "disk full"


# ---------------------------------------------------------------------------
# CommonClient.export_book: callback order through the real client
# ---------------------------------------------------------------------------


class _RecordingExportUI(WebExportUI):
    """The real adapter plus callback recording for order assertions."""

    def __init__(self, task: DownloadTask) -> None:
        super().__init__(task)
        self.started: list[str | None] = []
        self.succeeded: list[tuple[str, Path]] = []
        self.errors: list[tuple[str | None, str]] = []
        self.unsupported: list[str] = []

    def on_start(self, book: BookConfig, fmt: str | None = None) -> None:
        self.started.append(fmt)
        super().on_start(book, fmt)

    def on_success(self, book: BookConfig, fmt: str, path: Path) -> None:
        self.succeeded.append((fmt, path))
        super().on_success(book, fmt, path)

    def on_error(self, book: BookConfig, fmt: str | None, error: Exception) -> None:
        self.errors.append((fmt, str(error)))
        super().on_error(book, fmt, error)

    def on_unsupported(self, book: BookConfig, fmt: str) -> None:
        self.unsupported.append(fmt)
        super().on_unsupported(book, fmt)


def _bare_client(
    **format_funcs: Callable[[Any, Any, str | None], list[Path]],
) -> CommonClient:
    """
    Build a ``CommonClient`` without running ``__init__`` and shadow the
    named ``_export_book_<fmt>`` mixin methods with the given fakes, so the
    export loop never touches real data.
    """
    client = object.__new__(CommonClient)
    for name, func in format_funcs.items():
        setattr(client, f"_export_book_{name}", func)
    return client


def test_real_client_multi_format_reports_in_order() -> None:
    """on_start/on_success alternate per format, in request order."""
    txt_out = OUT_DIR / "book.txt"
    epub_out = OUT_DIR / "book.epub"

    def good_fmt(out: Path) -> Callable[[Any, Any, str | None], list[Path]]:
        def func(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
            return [out]

        return func

    client = _bare_client(txt=good_fmt(txt_out), epub=good_fmt(epub_out))
    task = _task()
    ui = _RecordingExportUI(task)

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["txt", "epub"], ui=ui
    )

    assert results == {"txt": [txt_out], "epub": [epub_out]}
    assert ui.started == ["txt", "epub"]
    assert ui.succeeded == [("txt", txt_out), ("epub", epub_out)]
    assert ui.errors == []
    assert ui.unsupported == []
    assert task.exported_paths == {"txt": txt_out, "epub": epub_out}
    assert ui.exported_count == 2
    # The stage ends on the last *started* format; the export worker owns the
    # final status, so the adapter leaves it in place.
    assert task.export_stage == "epub"
    assert task.status is Status.EXPORTING


def test_real_client_single_format_reports_stage() -> None:
    out = OUT_DIR / "book.txt"

    def good_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        return [out]

    client = _bare_client(txt=good_txt)
    task = _task()
    ui = _RecordingExportUI(task)

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["txt"], ui=ui
    )

    assert results == {"txt": [out]}
    assert ui.started == ["txt"]
    assert ui.succeeded == [("txt", out)]
    assert ui.exported_count == 1
    assert task.export_stage == "txt"


def test_real_client_failure_keeps_earlier_successes() -> None:
    """A later format failing must not lose the earlier format's file."""
    txt_out = OUT_DIR / "book.txt"

    def good_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        return [txt_out]

    def broken_epub(
        book: Any, cfg: Any, stage: str | None, **kwargs: Any
    ) -> list[Path]:
        raise RuntimeError("no cover")

    client = _bare_client(txt=good_txt, epub=broken_epub)
    task = _task()
    ui = _RecordingExportUI(task)

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["txt", "epub"], ui=ui
    )

    assert results == {"txt": [txt_out], "epub": []}
    assert ui.errors == [("epub", "no cover")]
    # The successful format's path survives the later failure.
    assert task.exported_paths == {"txt": txt_out}
    assert ui.exported_count == 1
    assert ui.exported_any is True
    assert task.export_stage == "epub"  # the failing format did start
    assert task.error == "no cover"
    assert task.status is Status.FAILED


def test_real_client_unsupported_format_never_starts() -> None:
    """An unsupported format is skipped without touching stage or count."""
    out = OUT_DIR / "book.txt"

    def good_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        return [out]

    client = _bare_client(txt=good_txt)
    task = _task()
    ui = _RecordingExportUI(task)

    results = client.export_book(
        BOOK, ExporterConfig(split_mode="book"), formats=["pdf", "txt"], ui=ui
    )

    assert results == {"pdf": [], "txt": [out]}
    assert ui.unsupported == ["pdf"]
    assert ui.started == ["txt"]
    assert ui.exported_count == 1
    assert task.export_stage == "txt"
    assert task.status is Status.EXPORTING  # unsupported alone never fails


# ---------------------------------------------------------------------------
# Whole chain: real CommonClient -> real WebExportUI -> real export worker
# ---------------------------------------------------------------------------


class _DelegatingClient:
    """Site client double delegating export to a real ``CommonClient``."""

    def __init__(self, bare: CommonClient, formats: list[str]) -> None:
        self._bare = bare
        self._formats = formats

    async def __aenter__(self) -> _DelegatingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def login(self, *, ui: Any, login_cfg: Any = None, **kwargs: Any) -> bool:
        return True

    async def download_book(self, book: Any, *, ui: Any = None, **kwargs: Any) -> None:
        return None

    def export_book(
        self, book: Any, cfg: Any = None, *, ui: Any = None, **kwargs: Any
    ) -> dict[str, list[Path]]:
        assert ui is not None
        return CommonClient.export_book(
            self._bare, book, cfg, formats=self._formats, ui=ui
        )


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


def _manager(client: Any) -> TaskManager:
    """Build a manager wired to fakes (never touches registrar or config)."""
    manager = TaskManager()
    manager._adapter = _NoStagesAdapter()  # type: ignore[assignment]
    manager._clients["demo"] = client  # type: ignore[assignment]
    return manager


@pytest.mark.asyncio
async def test_export_worker_leaves_final_stage_on_task() -> None:
    """A consumer reading the task snapshot sees the last exported format."""
    out = OUT_DIR / "42.txt"

    def good_txt(book: Any, cfg: Any, stage: str | None, **kwargs: Any) -> list[Path]:
        return [out]

    bare = _bare_client(txt=good_txt)
    manager = _manager(_DelegatingClient(bare, formats=["pdf", "txt"]))
    try:
        task = await manager.add_task(title="Demo Book", site="demo", book_id="42")

        async def _poll() -> None:
            while task.finished_at is None:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_poll(), timeout=2.0)

        assert task.status is Status.COMPLETED
        assert task.export_stage == "txt"  # last started format stays visible
        assert task.exported_paths == {"txt": out}
        assert task.error == "Export format 'pdf' is not supported."
    finally:
        await manager.close()
