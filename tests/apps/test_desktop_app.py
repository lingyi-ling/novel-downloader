#!/usr/bin/env python3
"""
tests.apps.test_desktop_app
---------------------------

Offline tests for the pywebview desktop client.

The suite covers :mod:`novel_downloader.apps.desktop.formatting`,
:mod:`novel_downloader.apps.desktop.notifications` and
:mod:`novel_downloader.apps.desktop.window_state`, which are deliberately free
of GUI imports, plus the threading helpers in
:mod:`novel_downloader.apps.desktop.engine`, the home-directory bootstrap in
:mod:`novel_downloader.apps.desktop.main`, the single-file front end in
:mod:`novel_downloader.apps.desktop.web_assets` and the JavaScript bridge in
:mod:`novel_downloader.apps.desktop.webview_app`.

No network access and no windowed interaction happen here.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.watchlist import WatchEntry
from novel_downloader.apps.desktop import formatting as fmt
from novel_downloader.apps.desktop import main as desktop_main
from novel_downloader.apps.desktop import notifications as notif
from novel_downloader.apps.desktop import window_state as wstate
from novel_downloader.apps.desktop.engine import AsyncEngine
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.apps.desktop.webview_app import (
    ApiBridge,
    _format_health_ms,
    _i18n_catalog,
    _music_track_dict,
    _result_dict,
    _task_dict,
)
from novel_downloader.infra.i18n import t
from novel_downloader.schemas import SearchResult


def _search_result(**overrides: Any) -> SearchResult:
    """Return a fully populated :class:`SearchResult` with optional overrides."""
    base: dict[str, Any] = {
        "site": "qidian",
        "book_id": "42",
        "book_url": "https://example.invalid/book/42",
        "cover_url": "https://example.invalid/cover.jpg",
        "title": "Sample",
        "author": "Author",
        "latest_chapter": "Chapter 1",
        "update_date": "2024-01-02",
        "word_count": "12345",
        "priority": 0,
    }
    base.update(overrides)
    return SearchResult(**base)  # type: ignore[typeddict-item]


def _track(**overrides: Any) -> SimpleNamespace:
    """Duck-typed stand-in for ``apps.music.providers.Track``."""
    values: dict[str, Any] = {
        "name": "Song",
        "artists": "Singer",
        "album": "Album",
        "duration_s": 215,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# Status labels
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.QUEUED, "Queued"),
        (Status.RUNNING, "Running"),
        (Status.PROCESSING, "Processing"),
        (Status.EXPORTING, "Exporting"),
        (Status.COMPLETED, "Completed"),
        (Status.CANCELLED, "Cancelled"),
        (Status.FAILED, "Failed"),
        # Raw string values are accepted as well as enum members.
        ("queued", "Queued"),
        ("failed", "Failed"),
    ],
)
def test_status_label_maps_known_statuses(status: object, expected: str) -> None:
    assert fmt.status_label(status) == expected


def test_status_label_covers_every_status_member() -> None:
    for member in Status:
        label = fmt.status_label(member)
        assert label
        # Every member must map to a real translation, not the raw value.
        assert label == t(member.value.capitalize())


def test_status_label_uses_value_attribute_of_duck_typed_status() -> None:
    assert fmt.status_label(SimpleNamespace(value="completed")) == "Completed"


@pytest.mark.parametrize("unknown", ["bogus", "", "None", "PENDING"])
def test_status_label_falls_back_to_raw_value(unknown: str) -> None:
    assert fmt.status_label(unknown) == unknown


def test_status_label_handles_none() -> None:
    assert fmt.status_label(None) == "None"


# ---------------------------------------------------------------------------
# Task progress / rows
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Size / time / duration formatting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Music platform health check: pure formatting / summary / ordering
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Site parsing
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------------
def test_format_error_includes_type_and_message() -> None:
    assert fmt.format_error(ValueError("bad input")) == "ValueError: bad input"


def test_format_error_handles_typed_subclasses_and_empty_messages() -> None:
    class Custom(RuntimeError):
        pass

    assert fmt.format_error(Custom("nope")) == "Custom: nope"
    assert fmt.format_error(RuntimeError()) == "RuntimeError: "


def test_format_error_is_used_by_the_desktop_ui() -> None:
    """The UI must not re-implement the wrapping inline."""
    bridge_path = Path(fmt.__file__).with_name("webview_app.py")
    source = bridge_path.read_text(encoding="utf-8")
    assert "type(exc).__name__" not in source
    assert "format_error(" in source


# ---------------------------------------------------------------------------
# AsyncEngine
# ---------------------------------------------------------------------------
def test_engine_is_not_running_before_first_use() -> None:
    engine = AsyncEngine()
    assert engine.running is False


def test_engine_submits_coroutines_on_a_background_loop() -> None:
    engine = AsyncEngine()
    try:

        async def compute(value: int) -> int:
            return value * 2

        future = engine.submit(compute(21))
        assert future.result(timeout=10) == 42
        assert engine.running is True
    finally:
        engine.shutdown()


def test_engine_reuses_one_loop_across_submissions() -> None:
    engine = AsyncEngine()

    async def ident(value: object) -> object:
        return value

    try:
        first = engine.submit(ident("a")).result(timeout=10)
        second = engine.submit(ident("b")).result(timeout=10)
        assert (first, second) == ("a", "b")
    finally:
        engine.shutdown()
    assert engine.running is False


def test_engine_propagates_coroutine_exceptions() -> None:
    engine = AsyncEngine()

    async def boom() -> None:
        raise RuntimeError("kaboom")

    try:
        with pytest.raises(RuntimeError, match="kaboom"):
            engine.submit(boom()).result(timeout=10)
    finally:
        engine.shutdown()


def test_engine_can_be_restarted_after_shutdown() -> None:
    engine = AsyncEngine()

    async def value() -> str:
        return "ok"

    try:
        assert engine.submit(value()).result(timeout=10) == "ok"
    finally:
        engine.shutdown()
    assert engine.running is False

    try:
        assert engine.submit(value()).result(timeout=10) == "ok"
    finally:
        engine.shutdown()


def test_shutdown_without_start_closes_the_task_manager() -> None:
    """A window closed before any task ran still tears the manager down."""
    engine = AsyncEngine()
    engine.shutdown(timeout=10.0)  # must not raise
    assert engine.running is False


# ---------------------------------------------------------------------------
# Home-directory bootstrap
# ---------------------------------------------------------------------------
def test_prepare_home_seeds_config_and_switches_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.infra.paths import app_home

    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    original = Path.cwd()
    try:
        desktop_main._prepare_home()
        home = app_home()
        assert Path.cwd() == home
        config = home / "settings.toml"
        assert config.is_file()
        assert config.stat().st_size > 0

        # The template is only copied when missing: an existing file is kept.
        config.write_text("sentinel = true\n", encoding="utf-8")
        desktop_main._prepare_home()
        assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    finally:
        os.chdir(original)


# ---------------------------------------------------------------------------
# Music settings / preference helpers
# ---------------------------------------------------------------------------
def _task(
    status: Status,
    exported: dict[str, Path] | None = None,
    *,
    error: str | None = None,
) -> DownloadTask:
    """Build a finished task snapshot with optional exported paths."""
    task = DownloadTask(title="Book", site="qidian", book_id="1")
    task.status = status
    task.error = error
    if exported:
        task.exported_paths = dict(exported)
    return task


# ---------------------------------------------------------------------------
# Task output actions / detail text
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Retry parameters
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [Status.FAILED, Status.CANCELLED])
def test_retry_params_for_failed_and_cancelled_tasks(status: Status) -> None:
    task = DownloadTask(title="  Book  ", site=" qidian ", book_id=" 42 ")
    task.status = status
    assert fmt.retry_params(task) == fmt.RetryParams(
        title="Book", site="qidian", book_id="42"
    )


@pytest.mark.parametrize(
    "status",
    [
        Status.QUEUED,
        Status.RUNNING,
        Status.PROCESSING,
        Status.EXPORTING,
        Status.COMPLETED,
    ],
)
def test_retry_params_reject_tasks_that_are_not_retryable(status: Status) -> None:
    task = _task(status)
    assert fmt.retry_params(task) is None


def test_retry_params_require_site_and_book_id() -> None:
    task = DownloadTask(title="Book", site="", book_id="1")
    task.status = Status.FAILED
    assert fmt.retry_params(task) is None

    task = DownloadTask(title="Book", site="qidian", book_id="   ")
    task.status = Status.CANCELLED
    assert fmt.retry_params(task) is None


def test_retry_params_for_no_selection() -> None:
    assert fmt.retry_params(None) is None


# ---------------------------------------------------------------------------
# Exported-file choices
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Context-menu availability
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Status / keyword filtering
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Task notifications: pure transition rules (the Android TaskEvents pattern)
# ---------------------------------------------------------------------------
def _status_task(
    status: Status,
    task_id: str = "task-1",
    title: str = "Alpha",
    error: str | None = None,
) -> DownloadTask:
    """Build a task snapshot with a fixed id and status."""
    task = DownloadTask(title=title, site="qidian", book_id="1", task_id=task_id)
    task.status = status
    task.error = error
    return task


def _task_snapshot(
    running: Iterable[DownloadTask] = (),
    pending: Iterable[DownloadTask] = (),
    completed: Iterable[DownloadTask] = (),
) -> dict[str, list[DownloadTask]]:
    """Build a manager-shaped snapshot."""
    return {
        "running": list(running),
        "pending": list(pending),
        "completed": list(completed),
    }


def test_play_notification_sound_uses_the_system_beep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    fake = SimpleNamespace(MessageBeep=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    notif.play_notification_sound()
    assert calls == [()]


def test_play_notification_sound_swallows_audio_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("no audio device")

    monkeypatch.setitem(sys.modules, "winsound", SimpleNamespace(MessageBeep=boom))
    notif.play_notification_sound()  # must not raise


# ---------------------------------------------------------------------------
# Follow list ("追更"): title normalization
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Follow list: matching a search result to a followed book
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Follow list: row formatting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        # Naive timestamps are shown as written (no zone conversion).
        ("2024-01-02T03:04:05", "2024-01-02 03:04:05"),
        ("not-a-date", "not-a-date"),
    ],
)
def test_format_follow_updated(raw: str, expected: str) -> None:
    assert fmt.format_follow_updated(raw) == expected


def test_format_follow_updated_converts_utc_to_local_time() -> None:
    rendered = fmt.format_follow_updated("2024-01-02T03:04:05+00:00")
    expected = (
        datetime.fromisoformat("2024-01-02T03:04:05+00:00")
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    assert rendered == expected
    assert len(rendered) == 19


# ---------------------------------------------------------------------------
# Follow list: following a search result (availability + feedback)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Search results context menu (pure availability logic)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Window state: geometry parsing / serialization (pure)
# ---------------------------------------------------------------------------
def test_clamp_geometry_enforces_the_minimum_size() -> None:
    clamped = wstate.clamp_geometry(wstate.WindowGeometry(200, 100, 7, 8))
    assert clamped == wstate.WindowGeometry(
        wstate.MIN_WINDOW_WIDTH, wstate.MIN_WINDOW_HEIGHT, 7, 8
    )


def test_clamp_geometry_keeps_usable_sizes_and_position() -> None:
    geometry = wstate.WindowGeometry(1100, 720, -40, 30)
    assert wstate.clamp_geometry(geometry) == geometry


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        (
            {"width": 1100, "height": 720, "x": 50, "y": 30},
            wstate.WindowGeometry(1100, 720, 50, 30),
        ),
        # Negative positions are valid (right/bottom screen edge anchoring).
        (
            {"width": 900, "height": 600, "x": -40, "y": -20},
            wstate.WindowGeometry(900, 600, -40, -20),
        ),
        # Missing or null positions mean "let the window manager decide".
        ({"width": 800, "height": 500}, wstate.WindowGeometry(800, 500)),
        (
            {"width": 800, "height": 500, "x": None, "y": None},
            wstate.WindowGeometry(800, 500),
        ),
        # A half-pair is dropped: the position is kept only when both match.
        ({"width": 800, "height": 500, "x": 10}, wstate.WindowGeometry(800, 500)),
    ],
)
def test_window_geometry_from_mapping_accepts_valid_mappings(
    mapping: dict[str, Any], expected: wstate.WindowGeometry
) -> None:
    assert wstate.window_geometry_from_mapping(mapping) == expected


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        "geometry",
        42,
        [],
        {},
        {"height": 720},
        {"width": 1100},
        {"width": "1100", "height": 720},  # string size
        {"width": 1100.0, "height": 720},  # float size
        {"width": True, "height": 720},  # bool is not an int here
        {"width": 0, "height": 720},
        {"width": -5, "height": 720},
        {"width": 1100, "height": 0},
    ],
)
def test_window_geometry_from_mapping_rejects_invalid_mappings(
    mapping: object,
) -> None:
    assert wstate.window_geometry_from_mapping(mapping) is None


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        # A wrongly-typed position is dropped, not fatal: the usable size is
        # still recovered from a half-corrupted file.
        (
            {"width": 1100, "height": 720, "x": "50", "y": 30},
            wstate.WindowGeometry(1100, 720),
        ),
        (
            {"width": 1100, "height": 720, "x": 50.0, "y": 30},
            wstate.WindowGeometry(1100, 720),
        ),
        (
            {"width": 1100, "height": 720, "x": 50, "y": None},
            wstate.WindowGeometry(1100, 720),
        ),
    ],
)
def test_window_geometry_from_mapping_drops_invalid_positions(
    mapping: dict[str, Any], expected: wstate.WindowGeometry
) -> None:
    assert wstate.window_geometry_from_mapping(mapping) == expected


# ---------------------------------------------------------------------------
# Window state: file loading / saving (tmp_path, no Tk)
# ---------------------------------------------------------------------------
def test_load_window_geometry_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert wstate.load_window_geometry(tmp_path / "absent.json") is None


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "",
        "[]",  # valid JSON, wrong shape
        '"a string"',
        '{"width": "900", "height": 600}',  # wrong value types
        '{"width": 900}',  # missing height
        "{'width': 900, 'height': 600}",  # not JSON (single quotes)
    ],
)
def test_load_window_geometry_tolerates_corrupted_files(
    tmp_path: Path, content: str
) -> None:
    state_file = tmp_path / "window_state.json"
    state_file.write_text(content, encoding="utf-8")
    assert wstate.load_window_geometry(state_file) is None


def test_load_window_geometry_clamps_small_sizes(tmp_path: Path) -> None:
    state_file = tmp_path / "window_state.json"
    assert wstate.save_window_geometry(
        state_file, wstate.WindowGeometry(200, 100, 5, 6)
    )
    assert wstate.load_window_geometry(state_file) == wstate.WindowGeometry(
        wstate.MIN_WINDOW_WIDTH, wstate.MIN_WINDOW_HEIGHT, 5, 6
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "window_state.json"
    geometry = wstate.WindowGeometry(980, 640, 15, 25)
    assert wstate.save_window_geometry(state_file, geometry) is True
    # The parent directory was created on demand.
    assert state_file.is_file()
    assert wstate.load_window_geometry(state_file) == geometry


def test_save_window_geometry_reports_write_failures(tmp_path: Path) -> None:
    # Writing over a directory raises OSError, which is reported as False
    # instead of propagating into the window teardown path.
    assert wstate.save_window_geometry(tmp_path, wstate.WindowGeometry(1, 1)) is False


def test_window_state_file_lives_in_the_music_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path / "music"))
    assert wstate.window_state_file() == tmp_path / "music" / "window_state.json"


# ---------------------------------------------------------------------------
# Web front end (web_assets): single-file HTML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needle",),
    [
        ('id="page-search"',),
        ('id="page-download"',),
        ('id="page-history"',),
        ('id="page-music"',),
        ('id="page-follow"',),
        ("window.pywebview",),
        ("function boot(",),
        (':root[data-theme="dark"]',),
        ("--accent:",),
    ],
)
def test_app_html_contains_core_structures(needle: str) -> None:
    """The single-file front end keeps its pages, bridge calls and theming."""
    assert needle in APP_HTML


def test_app_html_is_self_contained() -> None:
    """No external stylesheets or scripts: everything is inlined."""
    assert not re.search(r"<script[^>]+src=", APP_HTML)
    assert not re.search(r"<link[^>]+stylesheet", APP_HTML)


def test_app_html_declares_five_navigation_items() -> None:
    keys = re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"', APP_HTML)
    assert keys == ["search", "download", "history", "music", "follow"]


def test_i18n_catalog_covers_every_html_key() -> None:
    """Every data-i18n / data-i18n-ph / data-i18n-title key resolves via t()."""
    catalog = _i18n_catalog()
    keys = set(re.findall(r'data-i18n(?:-(?:ph|title))?="([^"]+)"', APP_HTML))
    assert keys, "the front end must declare translatable strings"
    missing = sorted(key for key in keys if key not in catalog)
    assert not missing, f"untranslated front-end keys: {missing}"


def test_i18n_catalog_covers_script_only_keys() -> None:
    """JS-side t() literals are represented in the Python catalogue."""
    catalog = _i18n_catalog()
    for key in (
        "Searching…",
        "{n} results in {s}s",
        "Search failed",
        "No results",
        "Best match",
        "Open in browser",
        "Queued: {title}",
        "Added to follow list",
        "Already followed",
        "Nothing in the queue",
        "No exported books yet",
        "Search a song to begin",
        "Downloading {done}/{total}",
        "Checked {n} books, {u} updated, {f} failed",
        "Site and book ID are required",
        "A download is already running",
    ):
        assert key in catalog, key


# ---------------------------------------------------------------------------
# Webview bridge (webview_app): serialisation helpers
# ---------------------------------------------------------------------------


def _fake_task(**overrides: Any) -> SimpleNamespace:
    """A duck-typed DownloadTask covering every serialised field."""
    task = SimpleNamespace(
        task_id="abc123",
        title="Sample",
        site="qidian",
        book_id="42",
        status=Status.RUNNING,
        chapters_done=3,
        chapters_total=10,
        error=None,
        retry_count=1,
        export_stage=None,
        exported_paths={},
        created_at=1_700_000_000.0,
        finished_at=None,
        progress=lambda: 0.3,
        eta=lambda: 12.4,
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def test_task_dict_serializes_core_fields() -> None:
    data = _task_dict(_fake_task())
    assert data["task_id"] == "abc123"
    assert data["title"] == "Sample"
    assert data["site"] == "qidian"
    assert data["status"] == "running"
    assert data["progress"] == 0.3
    assert data["chapters_done"] == 3
    assert data["chapters_total"] == 10
    assert data["retry_count"] == 1
    assert data["error"] == ""
    assert data["exports"] == []
    assert data["retryable"] is False
    assert data["eta"] == 12


def test_task_dict_flags_retryable_terminal_tasks() -> None:
    failed = _fake_task(status=Status.FAILED, error="boom")
    assert _task_dict(failed)["retryable"] is True
    assert _task_dict(failed)["error"] == "boom"
    done = _fake_task(status=Status.COMPLETED)
    assert _task_dict(done)["retryable"] is False


def test_task_dict_lists_export_paths() -> None:
    task = _fake_task(
        status=Status.COMPLETED,
        exported_paths={"txt": Path("downloads/a.txt")},
    )
    assert _task_dict(task)["exports"] == [str(Path("downloads/a.txt"))]


def test_task_dict_survives_mutating_export_dict() -> None:
    class Racy:
        task_id = "x"
        title = "t"
        site = "s"
        book_id = "b"
        status = Status.RUNNING
        chapters_done = 0
        chapters_total = 0
        error = None
        retry_count = 0
        export_stage = None
        created_at = None
        finished_at = None

        def __init__(self) -> None:
            self.exported_paths = {"txt": Path("a.txt")}

        def progress(self) -> float:
            self.exported_paths.clear()
            return 0.0

        def eta(self) -> None:
            return None

    # The export dict is copied before anything can mutate it concurrently.
    assert _task_dict(Racy())["exports"] == [str(Path("a.txt"))]


def test_result_dict_flags_only_the_first_hit_as_best() -> None:
    results = [{"site": "a"}, {"site": "b"}]
    flagged = [_result_dict(r, i) for i, r in enumerate(results)]
    assert [r["best"] for r in flagged] == [True, False]


def test_music_track_dict_uses_dataclass_fields() -> None:
    base = pytest.importorskip("novel_downloader.apps.music.providers.base")
    track = base.Track(
        provider="netease", id="1", name="Song", artists="Artist", album="Album"
    )
    data = _music_track_dict(track)
    assert data["name"] == "Song"
    assert data["vip"] is False


def test_music_track_dict_falls_back_to_attributes() -> None:
    data = _music_track_dict(SimpleNamespace(name="X", vip=True, duration_s=10))
    assert data["name"] == "X"
    assert data["vip"] is True
    assert data["duration_s"] == 10


def test_format_health_ms_units() -> None:
    assert _format_health_ms(850) == "850ms"
    assert _format_health_ms(1500) == "1.5s"


# ---------------------------------------------------------------------------
# Webview bridge (webview_app): ApiBridge behaviour
# ---------------------------------------------------------------------------


def test_bridge_add_task_requires_site_and_book_id() -> None:
    bridge = ApiBridge()
    assert bridge.add_task("", "42")["ok"] is False
    assert bridge.add_task("qidian", "")["ok"] is False


def test_bridge_add_task_queues_via_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_add(*, title: str, site: str, book_id: str) -> Any:
        return SimpleNamespace(task_id="tid-1")

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.add_task", fake_add
    )
    bridge = ApiBridge()
    try:
        result = bridge.add_task("qidian", "42", "Sample")
    finally:
        bridge.shutdown()
    assert result == {"ok": True, "task_id": "tid-1"}


def test_bridge_get_tasks_buckets_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = _fake_task()
    pending = _fake_task(status=Status.QUEUED)
    done = _fake_task(status=Status.COMPLETED)
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {"running": [running], "pending": [pending], "completed": [done]},
    )
    snap = ApiBridge().get_tasks()
    assert [item["task_id"] for item in snap["running"]] == ["abc123"]
    assert [item["status"] for item in snap["queued"]] == ["queued"]
    assert [item["task_id"] for item in snap["completed"]] == ["abc123"]


def test_bridge_cancel_task_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_cancel(_task_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.cancel_task", fake_cancel
    )
    bridge = ApiBridge()
    try:
        assert bridge.cancel_task("tid")["ok"] is True
    finally:
        bridge.shutdown()


def test_bridge_retry_task_requeues_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_add(*, title: str, site: str, book_id: str) -> Any:
        captured.update(title=title, site=site, book_id=book_id)
        return SimpleNamespace(task_id="new")

    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.add_task", fake_add
    )
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {
            "running": [],
            "pending": [],
            "completed": [_fake_task(status=Status.FAILED, error="x")],
        },
    )
    bridge = ApiBridge()
    try:
        assert bridge.retry_task("abc123")["ok"] is True
    finally:
        bridge.shutdown()
    assert captured["site"] == "qidian"


def test_bridge_retry_task_ignores_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "novel_downloader.apps.core.task_manager.manager.snapshot",
        lambda: {"running": [], "pending": [], "completed": [_fake_task()]},
    )
    assert ApiBridge().retry_task("abc123") == {"ok": False}


def test_bridge_watchlist_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.apps.core.watchlist.default_watchlist_path",
        lambda: tmp_path / "watch.json",
    )
    bridge = ApiBridge()
    assert bridge.get_watchlist() == []
    added = bridge.add_watch("qidian", "42", "Sample")
    assert added["ok"] is True and added["created"] is True
    again = bridge.add_watch("qidian", "42", "Sample")
    assert again["created"] is False
    entries = bridge.get_watchlist()
    assert len(entries) == 1
    assert entries[0]["site"] == "qidian"
    assert entries[0]["title"] == "Sample"
    assert bridge.remove_watch("qidian", "42")["ok"] is True
    assert bridge.get_watchlist() == []


def test_bridge_check_updates_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from novel_downloader.apps.core.watchlist import CheckResult

    async def fake_check_all(_store: Any, **_kwargs: Any) -> list[Any]:
        return [
            CheckResult(entry=WatchEntry(site="a", book_id="1"), changed=True),
            CheckResult(entry=WatchEntry(site="b", book_id="2"), changed=False),
            CheckResult(entry=WatchEntry(site="c", book_id="3"), error="boom"),
        ]

    monkeypatch.setattr(
        "novel_downloader.apps.core.watchlist.check_all", fake_check_all
    )
    bridge = ApiBridge()
    try:
        summary = bridge.check_updates()
    finally:
        bridge.shutdown()
    assert summary == {"checked": 3, "updated": 1, "failed": 1}


def test_bridge_music_download_guards_concurrency() -> None:
    bridge = ApiBridge()
    bridge._music_active = True  # noqa: SLF001 - direct state injection
    result = bridge.music_download("netease", [{"id": "1"}])
    assert result["ok"] is False


def test_bridge_music_status_and_stop_defaults() -> None:
    bridge = ApiBridge()
    status = bridge.get_music_status()
    assert status["active"] is False
    assert status["total"] == 0
    assert bridge.music_stop() == {"ok": True}


def test_bridge_music_settings_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.apps.music.settings.settings_file",
        lambda: tmp_path / "music_settings.json",
    )
    bridge = ApiBridge()
    bridge.save_music_settings({"level": "lossless", "save_lyric": False})
    stored = bridge.get_music_settings()
    assert stored["level"] == "lossless"
    assert stored["save_lyric"] is False


def test_bridge_health_start_and_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_health(platform: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": platform == "a", "elapsed_ms": 120, "error": ""}

    monkeypatch.setattr(
        "novel_downloader.apps.music.providers.health_check", fake_health
    )
    monkeypatch.setattr(
        "novel_downloader.apps.music.providers.available_labels",
        lambda: [("a", "A"), ("b", "B")],
    )
    bridge = ApiBridge()
    assert bridge.music_health_start() == {"ok": True}
    thread = bridge._health_thread
    assert thread is not None
    thread.join(timeout=10)
    rows = {row["key"]: row for row in bridge.get_music_health()}
    assert rows["a"]["ok"] is True
    assert rows["a"]["done"] is True
    assert rows["a"]["elapsed_text"] == "120ms"
    assert rows["b"]["ok"] is False


def test_bridge_open_url_ignores_unsafe_schemes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    bridge = ApiBridge()
    bridge.open_url("javascript:alert(1)")
    bridge.open_url("file:///etc/passwd")
    bridge.open_url("https://example.invalid/book")
    assert opened == ["https://example.invalid/book"]


def test_absolutize_argv0_resolves_relative_paths() -> None:
    from novel_downloader.apps.desktop import main as desktop_main_mod

    original = sys.argv[0]
    try:
        # A relative path that exists is made absolute before the chdir.
        sys.argv[0] = "setup.py" if Path("setup.py").is_file() else "pyproject.toml"
        expected = os.path.abspath(sys.argv[0])
        desktop_main_mod._absolutize_argv0()
        assert os.path.isabs(sys.argv[0])
        assert sys.argv[0] == expected

        # Absolute paths and missing files are left untouched.
        sys.argv[0] = str(Path("no_such_script.py"))
        desktop_main_mod._absolutize_argv0()
        assert sys.argv[0] == str(Path("no_such_script.py"))
    finally:
        sys.argv[0] = original


# ---------------------------------------------------------------------------
# Desktop / Start-menu shortcuts (packaged app UX)
# ---------------------------------------------------------------------------


def test_shortcut_target_requires_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert wa._shortcut_target() is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(wa.sys, "executable", "C:/apps/NovelDownloader.exe")
    assert wa._shortcut_target() == "C:/apps/NovelDownloader.exe"


def test_shortcut_exists_false_without_shell_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_shell_folder", lambda _csidl: None)
    assert wa.ApiBridge.shortcut_exists() is False


def test_create_shortcut_requires_packaged_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_shortcut_target", lambda: None)
    result = wa.ApiBridge().create_shortcut()
    assert result["ok"] is False


def test_create_shortcut_runs_powershell_with_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    exe = tmp_path / "NovelDownloader.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wa, "_shortcut_target", lambda: str(exe))
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    result = wa.ApiBridge().create_shortcut()
    assert result["ok"] is True
    assert calls and calls[0][0] == "powershell"
    assert str(exe) in calls[0][-1]
    assert "Novel Downloader.lnk" in calls[0][-1]


def test_create_shortcut_surfaces_powershell_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    exe = tmp_path / "NovelDownloader.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wa, "_shortcut_target", lambda: str(exe))

    def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    result = wa.ApiBridge().create_shortcut()
    assert result["ok"] is False
    assert "boom" in result["message"]


# ---------------------------------------------------------------------------
# Update check (GitHub releases, throttled)
# ---------------------------------------------------------------------------


def test_version_tuple_parsing() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    assert wa._version_tuple("1.2.3") == (1, 2, 3)
    assert wa._version_tuple("v2.0") == (2, 0)
    assert wa._version_tuple("1.10.0-rc1") == (1, 10, 0)
    assert wa._version_tuple("") == (0,)


def _fake_urlopen(payload: dict[str, Any] | None, error: Exception | None = None):
    class FakeResp:
        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

        def read(self) -> bytes:
            import json as _json

            return _json.dumps(payload or {}).encode("utf-8")

    def call(*_args: Any, **_kwargs: Any) -> FakeResp:
        if error is not None:
            raise error
        return FakeResp()

    return call


def test_check_update_reports_newer_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {"last_update_check": "0"})
    saved: dict[str, Any] = {}
    monkeypatch.setattr(wa, "_save_prefs", lambda v: saved.update(v))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen({"tag_name": "v9.9.9", "html_url": "https://example.invalid/r"}),
    )
    result = wa.ApiBridge().check_update()
    assert result["available"] is True
    assert result["latest"] == "9.9.9"
    assert result["url"] == "https://example.invalid/r"
    assert "last_update_check" in saved


def test_check_update_quiet_when_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader import __version__

    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    # Keep the throttle timestamp out of the real user data directory.
    monkeypatch.setattr(wa, "_save_prefs", lambda _v: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen({"tag_name": f"v{__version__}", "html_url": "u"}),
    )
    assert wa.ApiBridge().check_update()["available"] is False


def test_check_update_throttled_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(
        wa, "_load_prefs", lambda: {"last_update_check": str(time.time())}
    )
    calls: list[int] = []

    def _record(*_args: Any, **_kwargs: Any) -> None:
        calls.append(1)

    monkeypatch.setattr("urllib.request.urlopen", _record)
    result = wa.ApiBridge().check_update()
    assert result["throttled"] is True
    assert not calls


def test_check_update_survives_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    # Keep the throttle timestamp out of the real user data directory.
    monkeypatch.setattr(wa, "_save_prefs", lambda _v: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen(None, error=OSError("offline"))
    )
    result = wa.ApiBridge().check_update()
    assert result["available"] is False
    assert "error" in result


def test_check_update_tolerates_a_corrupt_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-edited prefs file must not make check_update raise."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {"last_update_check": "yesterday"})
    monkeypatch.setattr(wa, "_save_prefs", lambda _v: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen({"tag_name": "v9.9.9", "html_url": "u"}),
    )
    result = wa.ApiBridge().check_update()  # must not raise ValueError
    assert result["available"] is True


# ---------------------------------------------------------------------------
# Web front end (web_assets): escaping coverage
# ---------------------------------------------------------------------------

#: Fields rendered from bridge payloads.  Every one of them must reach the
#: DOM through ``esc()`` / ``highlight()`` (or a pre-escaped variable), never
#: as a raw template-literal interpolation.
_DYNAMIC_FIELDS = [
    "msg",
    "r.title",
    "r.author",
    "r.site",
    "r.book_id",
    "r.book_url",
    "r.latest_chapter",
    "r.update_date",
    "k.title",
    "k.error",
    "k.site",
    "k.task_id",
    "k.export_stage",
    "it.name",
    "it.path",
    "it.ext",
    "it.size_text",
    "it.mtime_text",
    "tr.name",
    "tr.artists",
    "tr.album",
    "w.title",
    "w.site",
    "w.book_id",
    "w.last_chapter",
    "w.updated_text",
    "p.key",
    "p.label",
]


@pytest.mark.parametrize("field", _DYNAMIC_FIELDS)
def test_app_html_never_interpolates_dynamic_fields_raw(field: str) -> None:
    """A bare ``${field}`` in the page would be an XSS hole."""
    # ``toast()`` escapes its message, so template literals passed to it as
    # string arguments are safe; every other interpolation site must go
    # through esc()/highlight().
    sanitized = re.sub(r"toast\(`[^`]*`", "toast(``", APP_HTML)
    assert f"${{{field}}}" not in sanitized


def test_app_html_escapes_all_html_metacharacters() -> None:
    """``esc`` must keep covering the full metacharacter set (& < > " ')."""
    assert "replace(/[&<>\"']/g" in APP_HTML


# ---------------------------------------------------------------------------
# Web front end (web_assets): shortcut / IME guards
# ---------------------------------------------------------------------------


def test_app_html_shortcuts_ignore_typing_and_ime() -> None:
    """Ctrl+F / Ctrl+D must not hijack keystrokes while the user is typing."""
    assert "if (e.isComposing) return;" in APP_HTML
    assert "typingTarget(e.target)" in APP_HTML
    assert "typingTarget(document.activeElement)" in APP_HTML
    # The typing guard runs before the Ctrl+key handling.
    guard = APP_HTML.index("typingTarget(e.target)")
    assert guard < APP_HTML.index("e.ctrlKey && !e.shiftKey")


@pytest.mark.parametrize("field", ["#q", "#m-q"])
def test_app_html_enter_search_ignores_ime_composition(field: str) -> None:
    """Confirming an IME composition must not submit the search early."""
    pattern = (
        re.escape(field) + r'"\)\.addEventListener\("keydown", e => \{ '
        r'if \(e\.key === "Enter" && !e\.isComposing\)'
    )
    assert re.search(pattern, APP_HTML), field


# ---------------------------------------------------------------------------
# Web front end (web_assets): timers / promises
# ---------------------------------------------------------------------------


def test_app_html_poll_timers_are_page_gated_and_cleared() -> None:
    """Every poll checks the active page before hitting the bridge."""
    assert 'if (currentPage !== "music") return;' in APP_HTML
    assert 'if (currentPage !== "download") return;' in APP_HTML
    assert 'if (currentPage === "music" && $("#m-health").childElementCount)' in (
        APP_HTML
    )
    # The music poll is created exactly once...
    assert "if (musicPoll) return;" in APP_HTML
    # ...and all three intervals are torn down on unload.
    for needle in (
        "clearInterval(taskTimer)",
        "clearInterval(musicPoll)",
        "clearInterval(healthPoll)",
    ):
        assert needle in APP_HTML


def test_app_html_update_check_promise_is_caught() -> None:
    """The boot update check ends its chain with .catch()."""
    assert 'api("check_update").then(' in APP_HTML
    match = re.search(r'api\("check_update"\)\.then\([\s\S]*?\}\)\.catch\(', APP_HTML)
    assert match, "the update check chain must not reject unhandled"


def test_app_html_call_helper_toasts_instead_of_rejecting() -> None:
    """``call`` resolves to undefined after toasting: no rethrown errors."""
    assert "throw err;" not in APP_HTML
    assert "return undefined;" in APP_HTML


# ---------------------------------------------------------------------------
# Web front end (web_assets): error payload surfacing
# ---------------------------------------------------------------------------


def test_app_html_search_surfaces_payload_errors() -> None:
    """A failed aggregate search must not render as "No results"."""
    assert "if (data && data.error) throw new Error(data.error);" in APP_HTML
    assert 'toast(data.error, "err");' in APP_HTML


def test_app_html_surfaces_rejected_task_errors() -> None:
    """``{"ok": false, "error": ...}`` answers are toasted, not swallowed."""
    assert 'else if (r && r.error) toast(r.error, "err");' in APP_HTML
    assert 'else if (r && r.error) toast(r.error, "warn");' in APP_HTML


def _js_handler(needle: str) -> str:
    """Return the JS statement block starting at ``needle``."""
    start = APP_HTML.index(needle)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


def test_app_html_music_download_guard_is_synchronous() -> None:
    """The download button disables before the await (double-click race)."""
    handler = _js_handler('$("#btn-m-dl").onclick')
    assert "btn.disabled = true;" in handler
    assert handler.index("btn.disabled = true;") < handler.index("await call")


def test_app_html_normalizes_the_theme_value() -> None:
    assert 'const mode = raw === "dark" ? "dark" : "light";' in APP_HTML


def test_app_html_has_no_dead_sites_csv_bootstrap() -> None:
    """The bridge never sends ``sites_csv``; the dead branch is gone."""
    assert "sites_csv" not in APP_HTML


def test_app_html_local_storage_access_is_guarded() -> None:
    """localStorage can throw (disabled, quota, corrupted JSON) — never fatal."""
    assert (
        "try { const v = localStorage.getItem(k); return v === null ? d"
        " : JSON.parse(v); } catch { return d; }" in APP_HTML
    )
    assert "try { localStorage.setItem(k, JSON.stringify(v)); } catch {}" in APP_HTML


# ---------------------------------------------------------------------------
# Window state: multi-monitor origins (negative coordinates)
# ---------------------------------------------------------------------------


def test_visible_origin_allows_negative_multi_monitor_coords() -> None:
    """A monitor left of/above the primary has negative screen coordinates."""
    # Virtual desktop: a 1920px monitor left of a 1920px primary.
    bounds = (-1920, 0, 3840, 1080)
    assert wstate.visible_origin(-1920, 0, bounds) == (-1920, 0)
    assert wstate.visible_origin(-1, 1079, bounds) == (-1, 1079)


def test_visible_origin_accepts_primary_monitor_origins() -> None:
    bounds = (0, 0, 1920, 1080)
    assert wstate.visible_origin(0, 0, bounds) == (0, 0)
    assert wstate.visible_origin(1919, 1079, bounds) == (1919, 1079)
    # Just past the edges: not on any monitor.
    assert wstate.visible_origin(1920, 0, bounds) is None
    assert wstate.visible_origin(0, 1080, bounds) is None


def test_visible_origin_drops_origins_off_the_virtual_desktop() -> None:
    """The saved monitor was unplugged: no connected screen has that origin."""
    assert wstate.visible_origin(-1920, 0, (0, 0, 1920, 1080)) is None
    assert wstate.visible_origin(5000, 200, (0, 0, 1920, 1080)) is None


def test_visible_origin_trusts_the_origin_without_bounds() -> None:
    """Unknown metrics (non-Windows) keep the stored position as-is."""
    assert wstate.visible_origin(-40, 30, None) == (-40, 30)


def test_virtual_screen_rect_reports_a_valid_rectangle() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    rect = wa._virtual_screen_rect()
    if sys.platform != "win32":
        assert rect is None
        return
    assert rect is not None
    x, y, width, height = rect
    assert width > 0 and height > 0
    # The primary monitor sits at (0, 0) and is part of the virtual desktop.
    assert x <= 0 < x + width
    assert y <= 0 < y + height


def test_run_webview_app_restores_negative_window_origins() -> None:
    """The zero clamp that broke multi-monitor restore is gone for good."""
    import novel_downloader.apps.desktop.webview_app as wa

    source = Path(wa.__file__).read_text(encoding="utf-8")
    assert "max(geometry.x, 0)" not in source
    assert "visible_origin(" in source
    assert "_virtual_screen_rect()" in source


# ---------------------------------------------------------------------------
# Desktop shortcuts: PowerShell quoting of the target path
# ---------------------------------------------------------------------------


def test_create_shortcut_doubles_single_quotes_in_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path with quotes must stay inside the PS single-quoted literal."""
    import novel_downloader.apps.desktop.webview_app as wa

    exe = tmp_path / "Ling's Novel 'App'.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(wa, "_shortcut_target", lambda: str(exe))
    scripts: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        scripts.append(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    assert wa.ApiBridge().create_shortcut()["ok"] is True
    script = scripts[0]
    # PowerShell escapes a single quote inside a single-quoted string by
    # doubling it, so the target literal must contain the doubled form...
    assert "$target = '" + str(exe).replace("'", "''") + "'" in script
    # ...and never the raw, un-escaped path.
    assert str(exe) not in script


# ---------------------------------------------------------------------------
# Bridge: music worker callback races
# ---------------------------------------------------------------------------


@dataclass
class _FakeTrack:
    name: str
    artists: str


def test_music_worker_counts_results_from_concurrent_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``download_many`` fires callbacks from a worker pool; counts must win."""
    import novel_downloader.apps.desktop.webview_app as wa

    total = 24

    def fake_download_many(
        _provider: object,
        tracks: list[_FakeTrack],
        _opts: object,
        workers: int = 3,
        on_result: Any = None,
        on_tick: Any = None,
        should_stop: Any = None,
    ) -> tuple[int, int, int, list[object], list[object]]:
        assert len(tracks) == total
        barrier = threading.Barrier(total)
        guard = threading.Lock()
        completed = {"n": 0}

        def work(i: int) -> None:
            barrier.wait()  # every callback fires at the same instant
            assert on_result is not None and on_tick is not None
            on_result(i, None, SimpleNamespace(status="ok"))
            # Mirror the real contract: the tick reports the absolute number
            # of finished tracks, which may lag the results just fired.
            with guard:
                completed["n"] += 1
                done = completed["n"]
            on_tick(done / total, done)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(total)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return total, 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _platform: None,
            "download_many": fake_download_many,
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    raw = [
        {"name": f"t{i}", "artists": "a", "junk": "filtered out"} for i in range(total)
    ]
    assert bridge.music_download("netease", raw)["ok"] is True

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and bridge.get_music_status()["active"]:
        time.sleep(0.02)
    status = bridge.get_music_status()
    assert status["active"] is False
    # Neither the counter race nor the out-of-order ticks lose progress.
    assert status["done"] == total
    assert status["fraction"] == 1.0
    assert status["failed"] is False
    assert status["summary"] == t("Downloaded {n} tracks").replace("{n}", str(total))


# ---------------------------------------------------------------------------
# Bridge: preference hardening
# ---------------------------------------------------------------------------


def test_get_initial_state_validates_the_stored_theme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only "light"/"dark" reach the page; junk falls back to "light"."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_load_prefs", lambda: {"theme": "purple"})
    state = wa.ApiBridge().get_initial_state()
    assert state["theme"] == "light"
    monkeypatch.setattr(wa, "_load_prefs", lambda: {"theme": "dark"})
    assert wa.ApiBridge().get_initial_state()["theme"] == "dark"


def test_save_prefs_swallows_serialization_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unserialisable stored value must not raise into the bridge."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_prefs_path", lambda: tmp_path / "prefs.json")

    def boom(_obj: object, **_kwargs: Any) -> str:
        raise TypeError("not serialisable")

    monkeypatch.setattr(wa.json, "dumps", boom)
    wa._save_prefs({"theme": "dark"})  # must not raise
