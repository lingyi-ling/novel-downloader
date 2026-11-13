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

import ast
import asyncio
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
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


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only")
def test_play_notification_sound_uses_the_system_beep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    fake = SimpleNamespace(MessageBeep=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    notif.play_notification_sound()
    assert calls == [()]


@pytest.mark.skipif(sys.platform != "win32", reason="winsound is Windows-only")
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


def test_clamp_geometry_fits_a_window_saved_on_a_larger_screen() -> None:
    """A 1550-wide window must not reopen clipped on a 1536-wide panel."""
    geometry = wstate.WindowGeometry(1550, 838, 26, 26)

    fitted = wstate.clamp_geometry(geometry, (0, 0, 1536, 864))

    assert (fitted.width, fitted.height) == (1536, 838)
    assert (fitted.x, fitted.y) == (0, 26)


def test_clamp_geometry_keeps_negative_multi_monitor_origins() -> None:
    geometry = wstate.WindowGeometry(1200, 800, -1200, 0)

    fitted = wstate.clamp_geometry(geometry, (-1200, 0, 3120, 1080))

    assert (fitted.x, fitted.y) == (-1200, 0)


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
        # same-book merge: the search filter row exposes the toggle and the
        # result cards render the source capsules (best source marked).
        ('id="q-group"',),
        ('class="merge-toggle"',),
        ("Merge same book",),
        ("function sourceCaps(",),
        ("cap-best",),
        ("cap-more",),
        ("src-caps",),
        # narrow windows: the details panel becomes a dismissible drawer
        ('id="detail-backdrop"',),
        ("data-detail-close",),
        ("closeDetail()",),
        # music: playlist export entry point and its result bar
        ('id="btn-m-export"',),
        ('id="m-export-note"',),
        ('id="m-export-path"',),
        # music: availability guidance and the account credential card
        ('id="m-availability"',),
        ('id="set-music-cred-platform"',),
        ('id="set-music-cred-value"',),
        ('id="set-music-cred-status"',),
        ('id="btn-music-cred-save"',),
        ('id="btn-music-cred-clear"',),
        ("data-music-cred",),
    ],
)
def test_app_html_contains_core_structures(needle: str) -> None:
    """The single-file front end keeps its pages, bridge calls and theming."""
    assert needle in APP_HTML


def test_app_html_mergetoggle_defaults_on_and_remembers_choice() -> None:
    """Search merges same-book rows out of the box; a stored choice wins."""
    label = next(
        line
        for line in APP_HTML.splitlines()
        if "merge-toggle" in line and "q-group" in line
    )
    assert 'id="q-group"' in label
    assert "checked" in label
    assert 'store.get("merge_same_book", true)' in APP_HTML
    assert 'store.set("merge_same_book"' in APP_HTML


def test_app_html_detail_is_reachable_below_1180px() -> None:
    """The old `display: none` hide is replaced by an accessible drawer."""
    assert "max-width: 1180px" in APP_HTML
    assert ".split > #detail { display: none !important; }" not in APP_HTML
    assert ".split > #detail.open" in APP_HTML
    # Esc dismisses the drawer and focus is returned to the card.
    assert 'e.key === "Escape"' in APP_HTML
    assert "detailReturnFocus" in APP_HTML
    # The reduced-motion rule still neutralises the slide animation.
    assert "@media (prefers-reduced-motion: reduce)" in APP_HTML


def _theme_tokens(selector: str) -> dict[str, str]:
    """Parse the custom properties of one ``selector { ... }`` block."""
    start = APP_HTML.index(selector)
    body_start = APP_HTML.index("{", start)
    body = APP_HTML[body_start + 1 : APP_HTML.index("}", body_start)]
    return {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", body)
    }


def _css_color(value: str) -> tuple[float, float, float, float]:
    """Parse ``#rrggbb`` or ``rgba(r, g, b, a)`` into 0–255 channels + alpha."""
    text = value.strip()
    if text.startswith("#"):
        digits = text[1:]
        return (
            int(digits[0:2], 16),
            int(digits[2:4], 16),
            int(digits[4:6], 16),
            1.0,
        )
    match = re.match(r"rgba?\(([^)]+)\)", text)
    assert match, text
    channels = match.group(1).replace("/", " ").replace(",", " ").split()
    parts = [float(value) for value in channels]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def _composite(
    fg: tuple[float, float, float, float],
    bg: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        fg[0] * fg[3] + bg[0] * (1 - fg[3]),
        fg[1] * fg[3] + bg[1] * (1 - fg[3]),
        fg[2] * fg[3] + bg[2] * (1 - fg[3]),
        1.0,
    )


def _relative_luminance(color: tuple[float, float, float, float]) -> float:
    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color[0])
        + 0.7152 * channel(color[1])
        + 0.0722 * channel(color[2])
    )


def _contrast_ratio(
    fg: tuple[float, float, float, float],
    bg: tuple[float, float, float, float],
) -> float:
    light, dark = _relative_luminance(fg), _relative_luminance(bg)
    return (max(light, dark) + 0.05) / (min(light, dark) + 0.05)


def test_app_html_status_tokens_meet_wcag_contrast() -> None:
    """Status text is 10.5–13 px, so every tone needs 4.5:1 against its tint.

    The tint is alpha-composited over the card colour instead of being compared
    with white: ``.pill.completed`` used to measure 3.3:1 this way.
    """
    pairs = [
        ("accent", "accent-soft"),
        ("green", "green-bg"),
        ("red", "red-bg"),
        ("blue", "blue-bg"),
        ("purple", "purple-bg"),
        ("yellow", "yellow-bg"),
        ("gray", "gray-bg"),
    ]
    for theme, selector in (
        ("light", ":root {"),
        ("dark", ':root[data-theme="dark"] {'),
    ):
        tokens = _theme_tokens(selector)
        card = _css_color(tokens["--card"])
        for fg_name, bg_name in pairs:
            background = _composite(_css_color(tokens[f"--{bg_name}"]), card)
            ratio = _contrast_ratio(_css_color(tokens[f"--{fg_name}"]), background)
            assert ratio >= 4.5, f"{theme} --{fg_name} is {ratio:.2f}:1"


def test_app_html_narrow_search_row_can_shrink() -> None:
    """A long platform <select> must not push the search button out of the
    content column at narrow widths (measured at 760 px in headless Chromium)."""
    assert ".search-row select.picker { min-width: 0; }" in APP_HTML
    assert ".search-row .field { flex: 1; min-width: 0; }" in APP_HTML


def test_app_html_mergetoggle_is_registered_as_i18n_key() -> None:
    """The merge toggle label goes through the catalog like every other label."""
    label = next(
        line
        for line in APP_HTML.splitlines()
        if "merge-toggle" in line and "q-group" in line
    )
    assert 'data-i18n="Merge same book"' in label
    assert "Merge same book" in _i18n_catalog()


def test_app_html_is_self_contained() -> None:
    """No external stylesheets or scripts: everything is inlined."""
    assert not re.search(r"<script[^>]+src=", APP_HTML)
    assert not re.search(r"<link[^>]+stylesheet", APP_HTML)


def test_app_html_declares_six_navigation_items() -> None:
    keys = re.findall(r'class="nav-item[^"]*" data-page="([a-z]+)"', APP_HTML)
    assert keys == ["search", "download", "history", "music", "follow", "settings"]


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
        "Some platforms need extra setup",
        "Go to settings",
        "Paste the token JSON",
        "Configured",
        "Not configured",
        "Platform credentials saved",
        "Platform credentials cleared",
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


def test_result_dict_keeps_merged_source_capsules() -> None:
    # Grouped entries (the "Merge same book" toggle) reach the front end with
    # their capsule list intact, next to the best flag.
    merged = {**_search_result(), "sources": ["beta", "alpha"]}
    data = _result_dict(merged, 0)
    assert data["sources"] == ["beta", "alpha"]
    assert data["best"] is True
    flat = _result_dict(_search_result(), 1)
    assert "sources" not in flat


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


def test_bridge_search_merges_when_group_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["keyword"] = keyword
        calls.update(kwargs)
        return [
            {
                "site": "beta",
                "book_id": "7",
                "book_url": "https://example.invalid/7",
                "cover_url": "",
                "title": "三体",
                "author": "刘慈欣",
                "latest_chapter": "",
                "update_date": "",
                "word_count": "300万字",
                "priority": 1,
                "sources": ["beta", "alpha"],
            }
        ]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    bridge = ApiBridge()
    try:
        payload = bridge.search("三体", "", True)
    finally:
        bridge.shutdown()

    # The third positional argument (the front end's toggle state) selects the
    # merged mode; the capsules and the best flag reach the page together.
    assert calls["keyword"] == "三体"
    assert calls["group"] is True
    assert payload["results"][0]["sources"] == ["beta", "alpha"]
    assert payload["results"][0]["best"] is True


def test_bridge_search_stays_flat_without_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.update(kwargs)
        return [
            {
                "site": "beta",
                "book_id": "7",
                "book_url": "https://example.invalid/7",
                "cover_url": "",
                "title": "三体",
                "author": "刘慈欣",
                "latest_chapter": "",
                "update_date": "",
                "word_count": "300万字",
                "priority": 1,
            }
        ]

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    bridge = ApiBridge()
    try:
        payload = bridge.search("三体")
    finally:
        bridge.shutdown()

    assert calls["group"] is False
    assert "sources" not in payload["results"][0]


def test_bridge_search_passes_optional_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page's min/max/date values reach the search layer as typed kwargs."""
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls["keyword"] = keyword
        calls.update(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    bridge = ApiBridge()
    try:
        payload = bridge.search("三体", "", False, 4, "10000", 200000, "2024-01-01")
    finally:
        bridge.shutdown()

    assert payload["results"] == []
    assert "error" not in payload
    assert calls["min_words"] == 10000
    assert calls["max_words"] == 200000
    assert calls["updated_after"] == date(2024, 1, 1)


def test_bridge_search_omits_blank_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank inputs mean "no filter" and are not forwarded at all."""
    calls: dict[str, Any] = {}

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.update(kwargs)
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)
    bridge = ApiBridge()
    try:
        bridge.search("三体", "", False, 4, "", "   ", "")
    finally:
        bridge.shutdown()

    assert "min_words" not in calls
    assert "max_words" not in calls
    assert "updated_after" not in calls


def test_bridge_progressive_search_forwards_filters_to_stream_and_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the preview chunks and the final ranking carry the same filters."""
    row = {
        "site": "alpha",
        "book_id": "a",
        "book_url": "https://example.invalid/a",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "30万字",
        "priority": 1,
    }
    stream_calls: dict[str, Any] = {}
    final_calls: dict[str, Any] = {}

    async def fake_stream(keyword: str, **kwargs: Any) -> Any:
        stream_calls.update(kwargs)
        yield [row]

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        final_calls.update(kwargs)
        return [row]

    monkeypatch.setattr("novel_downloader.plugins.search.search_stream", fake_stream)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    bridge = ApiBridge()
    try:
        job = bridge.search_async("三体", "", False, 4, "10000", "200000", "2024-01-01")
        deadline = time.time() + 5
        while True:
            state = bridge.search_progress(job["search_id"])
            if state["done"] or time.time() > deadline:
                break
            time.sleep(0.02)
    finally:
        bridge.shutdown()

    assert state["done"] is True
    for calls in (stream_calls, final_calls):
        assert calls["min_words"] == 10000
        assert calls["max_words"] == 200000
        assert calls["updated_after"] == date(2024, 1, 1)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"min_words": "-1"}, "min_words"),
        ({"min_words": "ten thousand"}, "min_words"),
        ({"max_words": 1_000_000_001}, "max_words"),
        ({"min_words": 100, "max_words": 10}, "max_words"),
        ({"updated_after": "2024/01/01"}, "updated_after"),
        ({"updated_after": "2024-02-30"}, "updated_after"),
    ],
)
def test_bridge_rejects_invalid_search_filters(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    field: str,
) -> None:
    """An invalid filter answers a structured error instead of searching."""

    async def fail_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("the search layer must not run for invalid filters")

    monkeypatch.setattr("novel_downloader.plugins.search.search", fail_search)
    bridge = ApiBridge()
    try:
        payload = bridge.search("三体", **kwargs)
    finally:
        bridge.shutdown()

    assert payload["results"] == []
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_search_filter"
    assert payload["error_field"] == field
    assert payload["error"]


def test_bridge_progressive_search_rejects_invalid_filters_before_starting() -> None:
    """``search_async`` returns the same structured error with ``done=True``."""
    bridge = ApiBridge()
    try:
        payload = bridge.search_async("三体", "", False, 4, "not-a-number")
    finally:
        bridge.shutdown()

    assert payload["search_id"] == ""
    assert payload["results"] == []
    assert payload["done"] is True
    assert payload["error_field"] == "min_words"
    assert payload["error_code"] == "invalid_search_filter"


def test_saved_search_filters_round_trip_through_the_prefs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The last valid filter set is stored in a normalised JSON shape."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    bridge = wa.ApiBridge()
    try:
        result = bridge.save_search_filters("10000", 200000, "2024-01-01")
        assert result["ok"] is True
        assert result["error"] is None
        expected = {
            "min_words": 10000,
            "max_words": 200000,
            "updated_after": "2024-01-01",
        }
        assert result["filters"] == expected
        assert json.loads(prefs.read_text(encoding="utf-8")) == {
            "search_filters": expected
        }

        # A fresh bridge (the next launch) restores the same values.
        restored = wa.ApiBridge().get_initial_state()["search_filters"]
    finally:
        bridge.shutdown()

    assert restored == expected


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"min_words": "-1"}, "min_words"),
        ({"max_words": "ten thousand"}, "max_words"),
        ({"min_words": 100, "max_words": 10}, "max_words"),
        ({"updated_after": "2024/01/01"}, "updated_after"),
        ({"updated_after": "2024-02-30"}, "updated_after"),
    ],
)
def test_save_search_filters_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kwargs: dict[str, Any],
    field: str,
) -> None:
    """The save path reuses the search contract and never writes bad values."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    payload = wa.ApiBridge().save_search_filters(**kwargs)

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_search_filter"
    assert payload["error_field"] == field
    assert payload["error"]
    assert not prefs.exists(), "a rejected filter must not reach the file"


def test_clear_search_filters_removes_the_saved_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Clearing — explicitly or by searching blank — forgets the filters."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    bridge = wa.ApiBridge()
    try:
        assert bridge.save_search_filters("10000", "", "")["ok"] is True
        assert json.loads(prefs.read_text(encoding="utf-8"))["search_filters"] == {
            "min_words": 10000
        }

        assert bridge.clear_search_filters()["ok"] is True
        assert "search_filters" not in json.loads(prefs.read_text(encoding="utf-8"))
        assert bridge.get_initial_state()["search_filters"] == {}

        # Saving an all-blank set is the same as clearing.
        assert bridge.save_search_filters("", "", "")["ok"] is True
        assert "search_filters" not in json.loads(prefs.read_text(encoding="utf-8"))
    finally:
        bridge.shutdown()


def test_corrupt_prefs_fall_back_to_no_saved_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unreadable prefs are ignored (and preserved) instead of raising."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text("{ this is not json", encoding="utf-8")

    bridge = wa.ApiBridge()
    try:
        assert bridge.get_initial_state()["search_filters"] == {}
        assert bridge.save_search_filters("1000")["ok"] is True
    finally:
        bridge.shutdown()

    backup = prefs.with_name(prefs.name + ".broken")
    assert backup.read_text(encoding="utf-8") == "{ this is not json"
    assert json.loads(prefs.read_text(encoding="utf-8")) == {
        "search_filters": {"min_words": 1000}
    }


def test_invalid_stored_search_filters_are_ignored_field_by_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hand-edited values are dropped without losing the valid neighbours."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        json.dumps(
            {
                "search_filters": {
                    "min_words": "many",
                    "max_words": 5000,
                    "updated_after": "2024-02-30",
                    "evil": "payload",
                }
            }
        ),
        encoding="utf-8",
    )
    bridge = wa.ApiBridge()
    try:
        assert bridge.get_initial_state()["search_filters"] == {"max_words": 5000}

        # An impossible min/max pair is discarded as a unit, the date survives.
        prefs.write_text(
            json.dumps(
                {
                    "search_filters": {
                        "min_words": 100,
                        "max_words": 10,
                        "updated_after": "2024-01-01",
                    }
                }
            ),
            encoding="utf-8",
        )
        assert bridge.get_initial_state()["search_filters"] == {
            "updated_after": "2024-01-01"
        }

        # A non-object value (older/broken file) is ignored as a whole.
        prefs.write_text(json.dumps({"search_filters": ["10000"]}), encoding="utf-8")
        assert bridge.get_initial_state()["search_filters"] == {}
    finally:
        bridge.shutdown()


def test_bridge_progressive_search_reports_rows_then_the_ranked_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page gets rows while sites answer, then the ranked final list."""
    row_a = {
        "site": "alpha",
        "book_id": "a",
        "book_url": "https://example.invalid/a",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 2,
    }
    row_b = dict(row_a, site="beta", book_id="b")

    async def fake_stream(keyword: str, **kwargs: Any) -> Any:
        assert keyword == "三体"
        yield [row_a]
        yield [row_b]

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        # Final ranking puts beta first; the page must end up with this order.
        return [row_b, row_a]

    monkeypatch.setattr("novel_downloader.plugins.search.search_stream", fake_stream)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    bridge = ApiBridge()
    try:
        job = bridge.search_async("三体")
        assert job["search_id"]
        assert job["done"] is False

        deadline = time.time() + 5
        while True:
            state = bridge.search_progress(job["search_id"])
            if state["done"] or time.time() > deadline:
                break
            time.sleep(0.02)

        assert state["done"] is True
        assert state["error"] == ""
        assert [item["book_id"] for item in state["results"]] == ["b", "a"]
        assert state["results"][0]["best"] is True
        # The finished entry is dropped, so a late poll cannot leak state.
        assert bridge.search_progress(job["search_id"])["results"] == []
    finally:
        bridge.shutdown()


def test_bridge_progressive_search_reports_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_stream(keyword: str, **kwargs: Any) -> Any:
        raise RuntimeError("engine down")
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr("novel_downloader.plugins.search.search_stream", failing_stream)

    bridge = ApiBridge()
    try:
        job = bridge.search_async("三体")
        deadline = time.time() + 5
        while True:
            state = bridge.search_progress(job["search_id"])
            if state["done"] or time.time() > deadline:
                break
            time.sleep(0.02)

        assert state["done"] is True
        assert "engine down" in state["error"]
    finally:
        bridge.shutdown()


def test_bridge_progressive_search_ignores_an_empty_keyword() -> None:
    bridge = ApiBridge()
    try:
        payload = bridge.search_async("   ")
    finally:
        bridge.shutdown()

    assert payload["search_id"] == ""
    assert payload["done"] is True


def test_bridge_progressive_search_reports_per_site_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed/failed sites and the row count reach the page while it polls."""
    row_a = {
        "site": "alpha",
        "book_id": "a",
        "book_url": "https://example.invalid/a",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 2,
    }
    row_b = dict(row_a, site="beta", book_id="b")

    async def fake_stream(keyword: str, **kwargs: Any) -> Any:
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(
                SimpleNamespace(
                    site="alpha", kind="completed", result_count=1, message=""
                )
            )
        yield [row_a]
        # Leave a window in which the page can poll the half-finished state.
        await asyncio.sleep(0.25)
        if on_event is not None:
            on_event(
                SimpleNamespace(
                    site="beta", kind="failed", result_count=None, message="boom"
                )
            )
        yield [row_b]

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [row_a, row_b]

    monkeypatch.setattr("novel_downloader.plugins.search.search_stream", fake_stream)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    bridge = ApiBridge()
    try:
        job = bridge.search_async("三体")
        snapshots: list[dict[str, Any]] = []
        deadline = time.time() + 5
        while True:
            state = bridge.search_progress(job["search_id"])
            snapshots.append(state)
            if state["done"] or time.time() > deadline:
                break
            time.sleep(0.02)

        assert any(
            snap["sites_completed"] == 1 and snap["sites_failed"] == 0
            for snap in snapshots
        ), snapshots
        assert any(snap["sites_failed"] == 1 for snap in snapshots), snapshots
        assert any(not snap["done"] and snap["elapsed"] > 0 for snap in snapshots), (
            snapshots
        )
        assert state["sites_completed"] == 1
        assert state["sites_failed"] == 1
        assert state["sites_done"] == 2
        assert state["result_count"] == 2
        assert state["cancelled"] is False
        assert state["error"] == ""
        assert state["elapsed"] > 0
    finally:
        bridge.shutdown()


def test_bridge_cancel_search_drops_late_chunks_and_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling stops the backend stream instead of only hiding its rows."""
    row_a = {
        "site": "alpha",
        "book_id": "a",
        "book_url": "https://example.invalid/a",
        "cover_url": "",
        "title": "三体",
        "author": "刘慈欣",
        "latest_chapter": "",
        "update_date": "",
        "word_count": "",
        "priority": 2,
    }
    row_b = dict(row_a, site="beta", book_id="b")
    closed = threading.Event()
    late_chunk = threading.Event()

    async def fake_stream(keyword: str, **kwargs: Any) -> Any:
        try:
            yield [row_a]
            await asyncio.sleep(0.5)
            late_chunk.set()
            yield [row_b]
        finally:
            closed.set()

    async def fake_search(keyword: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("a cancelled search must not run its final ranking")

    monkeypatch.setattr("novel_downloader.plugins.search.search_stream", fake_stream)
    monkeypatch.setattr("novel_downloader.plugins.search.search", fake_search)

    bridge = ApiBridge()
    try:
        job = bridge.search_async("三体")
        deadline = time.time() + 5
        while True:
            state = bridge.search_progress(job["search_id"])
            if state["results"] or time.time() > deadline:
                break
            time.sleep(0.01)
        assert [item["book_id"] for item in state["results"]] == ["a"]

        cancelled = bridge.cancel_search(job["search_id"])
        assert cancelled == {
            "ok": True,
            "search_id": job["search_id"],
            "cancelled": True,
        }
        assert closed.wait(2.0), "the stream must be closed on cancellation"

        final = bridge.search_progress(job["search_id"])
        deadline = time.time() + 5
        while not final["done"] and time.time() < deadline:
            time.sleep(0.02)
            final = bridge.search_progress(job["search_id"])

        assert final["done"] is True
        assert final["cancelled"] is True
        assert final["error"] == ""
        assert [item["book_id"] for item in final["results"]] == ["a"]
        # The generator has already closed, so the chunk after its sleep can
        # never be produced (and a non-cancelled run would have set this).
        assert late_chunk.is_set() is False
        # A second cancel is a harmless no-op (the entry is gone after the
        # final poll above).
        assert bridge.cancel_search(job["search_id"])["cancelled"] is False
    finally:
        bridge.shutdown()


def test_bridge_cancel_search_unknown_id_is_a_noop() -> None:
    bridge = ApiBridge()
    try:
        assert bridge.cancel_search("does-not-exist") == {
            "ok": True,
            "search_id": "does-not-exist",
            "cancelled": False,
        }
    finally:
        bridge.shutdown()


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


def test_bridge_check_updates_publishes_per_book_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress polling needs the books that changed or failed, not just counts."""
    from novel_downloader.apps.core import watchlist as wl
    from novel_downloader.apps.core.watchlist import CheckResult

    monkeypatch.setattr(wl, "default_watchlist_path", lambda: tmp_path / "watch.json")

    async def fake_check_all(_store: Any, **_kwargs: Any) -> list[Any]:
        return [
            CheckResult(
                entry=WatchEntry(site="a", book_id="1", title="Alpha"),
                latest="Chapter 9",
                changed=True,
            ),
            CheckResult(entry=WatchEntry(site="b", book_id="2", title="Beta")),
            CheckResult(
                entry=WatchEntry(site="c", book_id="3", title="Gamma"),
                error="TimeoutError: boom",
            ),
        ]

    monkeypatch.setattr(wl, "check_all", fake_check_all)
    bridge = ApiBridge()
    try:
        summary = bridge.check_updates()
        state = bridge.get_follow_check_state()
    finally:
        bridge.shutdown()
    assert summary == {"checked": 3, "updated": 1, "failed": 1}
    assert state["running"] is False
    assert (state["total"], state["done"]) == (3, 3)
    assert state["updates"] == [
        {"site": "a", "book_id": "1", "title": "Alpha", "latest": "Chapter 9"}
    ]
    assert state["failures"] == [
        {"site": "c", "book_id": "3", "title": "Gamma", "error": "TimeoutError: boom"}
    ]


def test_bridge_check_updates_refuses_a_parallel_sweep() -> None:
    """A second sweep would interleave the progress counters."""
    bridge = ApiBridge()
    try:
        bridge._follow_check["running"] = True  # noqa: SLF001 - state injection
        result = bridge.check_updates()
    finally:
        bridge.shutdown()
    assert result["checked"] == 0
    assert result["error"] == "A check is already running"


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
    assert status["paths"] == []
    assert status["results"] == []
    assert bridge.music_stop() == {"ok": True}


def test_bridge_music_results_report_only_existing_lyrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``lyric_path`` lands in the per-track result only when the ``.lrc`` exists."""
    import novel_downloader.apps.desktop.webview_app as wa

    audio = tmp_path / "Track - Artist.mp3"
    audio.write_bytes(b"\xff\xfb")
    with_lyrics = tmp_path / "Other - Artist.mp3"
    with_lyrics.write_bytes(b"\xff\xfb")
    lrc = with_lyrics.with_suffix(".lrc")
    lrc.write_text("[00:00.00]hi\n", encoding="utf-8")

    @dataclass
    class _FakeTrack:
        name: str = ""
        artists: str = ""
        path: str = ""

    def fake_download_mixed(
        _create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        on_result = kwargs["on_result"]
        on_result(0, tracks[0], SimpleNamespace(status="ok", path=str(audio)))
        on_result(1, tracks[1], SimpleNamespace(status="ok", path=str(with_lyrics)))
        return 2, 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _platform: None,
            "download_mixed": fake_download_mixed,
            # A concrete platform skips the aggregate routing.
            "is_all_platforms": lambda _platform: False,
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    rows = [{"name": "Track"}, {"name": "Other"}]
    assert bridge.music_download("netease", rows)["ok"] is True
    status = _wait_for_music_idle(bridge)

    # The legacy field is unchanged and still lists both audio files.
    assert status["paths"] == [str(audio), str(with_lyrics)]
    assert status["results"][0] == {"path": str(audio)}
    # Existence filtering: a missing ``.lrc`` must not reach the page.
    assert "lyric_path" not in status["results"][0]
    assert status["results"][1] == {
        "path": str(with_lyrics),
        "lyric_path": str(lrc),
        "lyric_paths": [{"lang": "原文", "path": str(lrc)}],
    }
    # The batch summary counts tracks whose lyrics landed.
    assert status["summary"] == (
        t("Downloaded {n} tracks").replace("{n}", "2")
        + " · "
        + t("{n} with lyrics").replace("{n}", "1")
    )


def test_bridge_music_results_report_original_and_translated_lyrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``lyric_paths`` lists every existing lyric file, original first."""
    import novel_downloader.apps.desktop.webview_app as wa

    plain = tmp_path / "Plain - Artist.mp3"
    plain.write_bytes(b"\xff\xfb")
    original_only = tmp_path / "Original - Artist.mp3"
    original_only.write_bytes(b"\xff\xfb")
    original_lrc = original_only.with_suffix(".lrc")
    original_lrc.write_text("[00:00.00]original\n", encoding="utf-8")
    both = tmp_path / "Both - Artist.mp3"
    both.write_bytes(b"\xff\xfb")
    both_lrc = both.with_suffix(".lrc")
    both_lrc.write_text("[00:00.00]original\n", encoding="utf-8")
    both_zh_lrc = both.with_suffix(".zh.lrc")
    both_zh_lrc.write_text("[00:00.00]译文\n", encoding="utf-8")
    translation_only = tmp_path / "Translated - Artist.mp3"
    translation_only.write_bytes(b"\xff\xfb")
    translation_lrc = translation_only.with_suffix(".zh.lrc")
    translation_lrc.write_text("[00:00.00]译文\n", encoding="utf-8")

    @dataclass
    class _FakeTrack:
        name: str = ""
        artists: str = ""
        path: str = ""

    audio_paths = [plain, original_only, both, translation_only]

    def fake_download_mixed(
        _create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        on_result = kwargs["on_result"]
        for index, audio in enumerate(audio_paths):
            on_result(
                index, tracks[index], SimpleNamespace(status="ok", path=str(audio))
            )
        return len(audio_paths), 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _platform: None,
            "download_mixed": fake_download_mixed,
            "is_all_platforms": lambda _platform: False,
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    rows = [{"name": str(path.stem)} for path in audio_paths]
    assert bridge.music_download("netease", rows)["ok"] is True
    status = _wait_for_music_idle(bridge)

    assert status["paths"] == [str(path) for path in audio_paths]
    # Neither lyric exists: the old field is absent and the new list is empty.
    assert status["results"][0] == {"path": str(plain)}
    # Original only: one entry, and the legacy field still points at it.
    assert status["results"][1] == {
        "path": str(original_only),
        "lyric_path": str(original_lrc),
        "lyric_paths": [{"lang": "原文", "path": str(original_lrc)}],
    }
    # Original + translation: sorted original first, translation second.
    assert status["results"][2] == {
        "path": str(both),
        "lyric_path": str(both_lrc),
        "lyric_paths": [
            {"lang": "原文", "path": str(both_lrc)},
            {"lang": "译文", "path": str(both_zh_lrc)},
        ],
    }
    # Translation only: exposed through ``lyric_paths``; ``lyric_path`` keeps
    # its historical "original .lrc" meaning and is therefore absent.
    assert status["results"][3] == {
        "path": str(translation_only),
        "lyric_paths": [{"lang": "译文", "path": str(translation_lrc)}],
    }
    assert "lyric_path" not in status["results"][3]
    # Only tracks with the original file count as "lyrics saved" in the summary.
    assert status["summary"] == (
        t("Downloaded {n} tracks").replace("{n}", "4")
        + " · "
        + t("{n} with lyrics").replace("{n}", "2")
    )


def test_bridge_music_export_writes_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    @dataclass
    class _FakeTrack:
        name: str = ""
        artists: str = ""
        path: str = ""

    written: list[str] = []

    def fake_export(tracks: list[Any], path: Path) -> str:
        written.extend(tr.path for tr in tracks)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#EXTM3U\n", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {"Track": _FakeTrack, "export_m3u": fake_export},
    )
    target = tmp_path / "playlist.m3u8"
    result = wa.ApiBridge().music_export(
        "netease",
        [{"name": "A", "path": "C:/music/a.mp3"}, {"name": "B", "path": ""}],
        str(target),
    )
    assert result["ok"] is True
    assert result["path"] == str(target)
    assert result["tracks_written"] == 1
    assert result["skipped"] == 1
    assert written == ["C:/music/a.mp3"]
    assert target.read_text("utf-8") == "#EXTM3U\n"


def test_bridge_music_export_without_local_paths_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    @dataclass
    class _FakeTrack:
        name: str = ""
        path: str = ""

    monkeypatch.setattr(wa, "_music_funcs", lambda: {"Track": _FakeTrack})
    result = wa.ApiBridge().music_export("netease", [{"name": "B", "path": ""}])
    assert result["ok"] is False
    assert result["error"]
    assert result["skipped"] == 1


def _fake_music_export(
    monkeypatch: pytest.MonkeyPatch, exporter: Any | None = None
) -> Any:
    """Install a fake ``music`` extra set and return the bridge."""
    import novel_downloader.apps.desktop.webview_app as wa

    @dataclass
    class _FakeTrack:
        name: str = ""
        path: str = ""

    def default_export(tracks: list[Any], path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#EXTM3U\n", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "export_m3u": exporter or default_export,
        },
    )
    return wa.ApiBridge()


def test_bridge_music_export_requires_permission_to_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing playlist is reported, not silently replaced."""
    bridge = _fake_music_export(monkeypatch)
    target = tmp_path / "list.m3u8"
    target.write_text("old\n", encoding="utf-8")

    refused = bridge.music_export("netease", [{"path": "C:/a.mp3"}], str(target))
    assert refused["ok"] is False
    assert refused["needs_overwrite"] is True
    assert refused["path"] == str(target)
    assert str(target) in refused["error"]
    assert target.read_text("utf-8") == "old\n"

    accepted = bridge.music_export("netease", [{"path": "C:/a.mp3"}], str(target), True)
    assert accepted["ok"] is True
    assert accepted["path"] == str(target)
    assert target.read_text("utf-8") == "#EXTM3U\n"


def test_bridge_music_export_treats_non_playlist_path_as_directory() -> None:
    """A typo such as ``songs.txt`` must not become the playlist file name."""
    import novel_downloader.apps.desktop.webview_app as wa

    resolved = wa._playlist_target(str(Path("C:/exports/songs.txt")))
    assert resolved.parent == Path("C:/exports/songs.txt")
    assert resolved.suffix == ".m3u8"
    # Supported suffixes are honoured verbatim, case-insensitively.
    assert wa._playlist_target("C:/exports/list.M3U") == Path("C:/exports/list.M3U")


def test_bridge_music_export_failure_names_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write error must tell the user which path failed."""

    def broken_export(tracks: list[Any], path: Path) -> str:
        raise OSError("disk full")

    bridge = _fake_music_export(monkeypatch, broken_export)
    target = tmp_path / "list.m3u8"
    result = bridge.music_export("netease", [{"path": "C:/a.mp3"}], str(target))
    assert result["ok"] is False
    assert result["path"] == str(target)
    assert str(target) in result["error"]
    assert "disk full" in result["error"]


def _wait_for_music_idle(bridge: Any, timeout: float = 10.0) -> dict[str, Any]:
    """Block until the polled music job stops, then return its final status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and bridge.get_music_status()["active"]:
        time.sleep(0.02)
    return bridge.get_music_status()


def test_bridge_music_note_for_all_platforms_is_a_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")
    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "is_all_platforms": aggregate.is_all_platforms,
            "platform_note": lambda _key: "",
        },
    )
    bridge = wa.ApiBridge()
    assert bridge.music_note("all") == t(
        "Searches every platform at once and merges the results"
    )
    # A concrete platform still uses the provider's own note.
    assert bridge.music_note("netease") == ""


def test_bridge_music_search_all_uses_the_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``platform="all"`` must go through aggregate search, not ``create``."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")
    base = pytest.importorskip("novel_downloader.apps.music.providers.base")
    calls: dict[str, Any] = {}
    cached: dict[str, Any] = {}

    track = base.Track(provider="netease", id="1", name="Song", artists="Artist")
    track.sources = ["netease", "qq"]

    def fake_search_all(keyword: str, platforms: Any = None, limit: Any = None) -> Any:
        calls.update(keyword=keyword, platforms=platforms, limit=limit)
        return aggregate.AggregateResult(
            tracks=[track],
            platforms=[aggregate.PlatformOutcome("netease", True, count=1)],
            total=1,
        )

    class _Cache:
        def put(self, tracks: Any) -> str:
            cached["tracks"] = list(tracks)
            return "sid-all"

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "is_all_platforms": aggregate.is_all_platforms,
            "search_all": fake_search_all,
            "CACHE": _Cache(),
            "ALL_PLATFORM_KEY": aggregate.ALL_PLATFORM_KEY,
            "create": lambda _key: pytest.fail("create() must not run for all"),
        },
    )

    result = wa.ApiBridge().music_search("all", "  Song  ", 30)

    assert calls == {"keyword": "Song", "platforms": None, "limit": 30}
    assert result["platform"] == "all"
    assert result["search_id"] == "sid-all"
    assert result["total"] == 1
    assert result["platforms"] == [
        {"platform": "netease", "ok": True, "count": 1, "error": ""}
    ]
    assert result["tracks"][0]["sources"] == ["netease", "qq"]
    # Provider-private data cannot cross the bridge; the cache carries it.
    assert "extra" not in result["tracks"][0]
    assert cached["tracks"] == [track]


def test_bridge_music_search_single_platform_returns_search_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providers answer ``(tracks, total)``; the tuple must be unpacked."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")
    base = pytest.importorskip("novel_downloader.apps.music.providers.base")
    track = base.Track(provider="netease", id="7", name="Solo", artists="Artist")

    class _Provider:
        def search(self, keyword: str, limit: int = 30) -> Any:
            assert (keyword, limit) == ("Solo", 5)
            return [track], 1

    class _Cache:
        def put(self, tracks: Any) -> str:
            assert list(tracks) == [track]
            return "sid-one"

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "is_all_platforms": aggregate.is_all_platforms,
            "create": lambda key: _Provider(),
            "CACHE": _Cache(),
        },
    )
    result = wa.ApiBridge().music_search("netease", "Solo", 5)
    assert result["search_id"] == "sid-one"
    assert [row["name"] for row in result["tracks"]] == ["Solo"]


def test_bridge_music_search_async_streams_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each platform's arrival is visible before the aggregate search ends."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")
    base = pytest.importorskip("novel_downloader.apps.music.providers.base")

    first = base.Track(provider="netease", id="1", name="Song", artists="Artist")
    first.sources = ["netease"]
    second = base.Track(provider="qq", id="2", name="Song", artists="Artist")
    second.sources = ["netease", "qq"]
    release = threading.Event()

    def fake_search_all(
        keyword: str,
        platforms: Any = None,
        limit: Any = None,
        **kwargs: Any,
    ) -> Any:
        on_update = kwargs["on_update"]
        assert (keyword, platforms, limit) == ("Song", None, 30)
        assert kwargs["timeout"] == wa.SEARCH_SITE_TIMEOUT
        on_update(
            aggregate.AggregateResult(
                tracks=[first],
                platforms=[aggregate.PlatformOutcome("netease", True, count=1)],
                total=1,
            )
        )
        # Keep the search open until the test has observed the partial state.
        assert release.wait(5.0)
        final = aggregate.AggregateResult(
            tracks=[first, second],
            platforms=[
                aggregate.PlatformOutcome("netease", True, count=1),
                aggregate.PlatformOutcome("qq", False, error="timeout"),
            ],
            total=2,
        )
        on_update(final)
        return final

    class _Cache:
        def put(self, tracks: Iterable[Any]) -> str:
            return "sid-final"

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "is_all_platforms": aggregate.is_all_platforms,
            "search_all": fake_search_all,
            "CACHE": _Cache(),
        },
    )

    bridge = wa.ApiBridge()
    started = bridge.music_search_async("all", "Song", 30)
    assert started["done"] is False
    assert started["search_id"]

    partial = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        snapshot = bridge.music_search_progress(started["search_id"])
        if snapshot["tracks"] and not snapshot["done"]:
            partial = snapshot
            break
        time.sleep(0.01)
    assert partial is not None, "the first platform never became visible"
    assert partial["partial"] is True
    assert partial["result_id"] == ""
    assert [row["name"] for row in partial["tracks"]] == ["Song"]
    assert [row["platform"] for row in partial["platforms"]] == ["netease"]

    release.set()
    final = bridge.music_search_progress(started["search_id"])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not final["done"]:
        time.sleep(0.01)
        final = bridge.music_search_progress(started["search_id"])
    assert final["done"] is True
    assert final["error"] == ""
    assert final["result_id"] == "sid-final"
    assert [row["name"] for row in final["tracks"]] == ["Song", "Song"]
    assert [item["platform"] for item in final["platforms"]] == ["netease", "qq"]
    assert final["platforms"][1]["ok"] is False
    assert final["elapsed"] >= 0.0


def test_bridge_music_search_async_single_platform_completes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concrete platform answers in one call; polling still works."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")
    base = pytest.importorskip("novel_downloader.apps.music.providers.base")
    track = base.Track(provider="netease", id="7", name="Solo", artists="Artist")

    class _Provider:
        def search(self, keyword: str, limit: int = 30) -> Any:
            assert (keyword, limit) == ("Solo", 5)
            return [track], 1

    class _Cache:
        def put(self, tracks: Any) -> str:
            assert list(tracks) == [track]
            return "sid-one"

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "is_all_platforms": aggregate.is_all_platforms,
            "create": lambda _key: _Provider(),
            "CACHE": _Cache(),
        },
    )

    bridge = wa.ApiBridge()
    result = bridge.music_search_async("netease", "Solo", 5)
    assert result["done"] is True
    assert result["result_id"] == "sid-one"
    assert [row["name"] for row in result["tracks"]] == ["Solo"]

    snapshot = bridge.music_search_progress(result["search_id"])
    assert snapshot["done"] is True
    assert snapshot["result_id"] == "sid-one"
    assert snapshot["tracks"] == result["tracks"]


def test_bridge_music_search_progress_unknown_id_is_done() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    snapshot = wa.ApiBridge().music_search_progress("does-not-exist")
    assert snapshot["done"] is True
    assert snapshot["tracks"] == []
    assert snapshot["platforms"] == []
    assert snapshot["result_id"] == ""
    assert snapshot["error"] == ""
    assert snapshot["elapsed"] == 0.0


def test_bridge_music_status_reports_failed_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures carry their row index and reason; a fallback clears the row."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")

    @dataclass
    class _FakeTrack:
        provider: str = ""
        id: str = ""
        name: str = ""

    def fake_mixed(
        _create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        on_result = kwargs["on_result"]
        on_result(0, tracks[0], SimpleNamespace(status="ok", path="a.mp3", reason=""))
        on_result(
            1,
            tracks[1],
            SimpleNamespace(status="fail", path="", reason="HTTP 403"),
        )
        return 1, 0, 1, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _key: None,
            "download_mixed": fake_mixed,
            "is_all_platforms": aggregate.is_all_platforms,
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    assert (
        bridge.music_download("netease", [{"name": "good"}, {"name": "bad"}])["ok"]
        is True
    )
    status = _wait_for_music_idle(bridge)
    assert status["failed"] is True
    assert status["failed_items"] == [{"index": 1, "name": "bad", "reason": "HTTP 403"}]

    # The aggregate fallback re-reports the same row as successful: the entry
    # must disappear instead of pointing at a track that did download.
    def fake_fallback(
        _create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        on_result = kwargs["on_result"]
        on_result(0, tracks[0], SimpleNamespace(status="fail", reason="first"))
        on_result(0, tracks[0], SimpleNamespace(status="ok", path="b.mp3"))
        return 1, 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _key: None,
            "download_mixed": fake_fallback,
            "is_all_platforms": aggregate.is_all_platforms,
        },
    )
    assert bridge.music_download("netease", [{"name": "recovered"}])["ok"] is True
    status = _wait_for_music_idle(bridge)
    assert status["failed_items"] == []


def test_bridge_music_retry_resubmits_only_the_failed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed row retried alone succeeds and clears the failure panel.

    The page keeps the search id and maps ``failed_items[*].index`` back to
    its row through ``music.batch``; this pins the bridge half of that
    contract: the retried batch is the single failed row and its status is
    aligned at index 0.
    """
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")

    @dataclass
    class _FakeTrack:
        provider: str = ""
        id: str = ""
        name: str = ""

    submitted: list[list[str]] = []

    def fake_mixed(
        _create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        names = [getattr(track, "name", "") for track in tracks]
        submitted.append(names)
        on_result = kwargs["on_result"]
        if len(submitted) == 1:
            on_result(0, tracks[0], SimpleNamespace(status="ok", path="a.mp3"))
            on_result(1, tracks[1], SimpleNamespace(status="fail", reason="HTTP 403"))
            return 1, 0, 1, [], []
        # The retry submits the failed row alone; it now succeeds.
        on_result(0, tracks[0], SimpleNamespace(status="ok", path="b.mp3"))
        return 1, 0, 0, [], []

    class _Cache:
        def get(self, search_id: str) -> list[object] | None:
            return [object()] if search_id == "sid" else None

        def resolve(self, _search_id: str, tracks: Any) -> list[Any]:
            return list(tracks)

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _key: None,
            "download_mixed": fake_mixed,
            "is_all_platforms": aggregate.is_all_platforms,
            "CACHE": _Cache(),
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    rows = [{"name": "good"}, {"name": "bad"}]
    assert bridge.music_download("netease", rows, None, "sid")["ok"] is True
    first = _wait_for_music_idle(bridge)
    assert first["failed_items"] == [{"index": 1, "name": "bad", "reason": "HTTP 403"}]

    failed = first["failed_items"][0]
    assert (
        bridge.music_download("netease", [{"name": failed["name"]}], None, "sid")["ok"]
        is True
    )
    second = _wait_for_music_idle(bridge)

    assert submitted == [["good", "bad"], ["bad"]], "only the failed row is retried"
    assert second["failed"] is False
    assert second["failed_items"] == []
    assert second["paths"] == ["b.mp3"]


def test_bridge_open_music_dir_creates_and_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The music button opens the stored output directory, creating it."""
    import novel_downloader.apps.desktop.webview_app as wa

    target = tmp_path / "music-out"
    monkeypatch.setattr(wa, "_music_output_dir", lambda: target)
    opened: list[str] = []
    monkeypatch.setattr(
        wa.ApiBridge, "open_path", lambda _self, path: opened.append(path) or True
    )

    result = wa.ApiBridge().open_music_dir()

    assert result["ok"] is True
    assert result["error"] is None
    assert target.is_dir()
    assert opened == [str(target.resolve())]


def test_bridge_open_music_dir_reports_a_failed_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file manager that cannot open must reach the user as a toast."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_music_output_dir", lambda: tmp_path)
    monkeypatch.setattr(wa.ApiBridge, "open_path", lambda _self, _path: False)

    result = wa.ApiBridge().open_music_dir()

    assert result["ok"] is False
    assert result["path"] == str(tmp_path.resolve())
    assert result["error"]


def test_bridge_open_music_dir_reports_a_failed_mkdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unwritable path must not raise across the bridge."""
    import novel_downloader.apps.desktop.webview_app as wa

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(wa, "_music_output_dir", lambda: blocker / "child")

    result = wa.ApiBridge().open_music_dir()

    assert result["ok"] is False
    assert result["error"]


def _stub_downloads_dir(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Point ``open_downloads_dir`` at ``target`` through the real setting."""
    import novel_downloader.infra.config as config

    monkeypatch.setattr(
        config, "get_config_value", lambda *_a, **_k: str(target), raising=True
    )


def test_bridge_open_downloads_dir_reports_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The history "Open folder" button gets a result it can act on."""
    import novel_downloader.apps.desktop.webview_app as wa

    target = tmp_path / "out"
    _stub_downloads_dir(monkeypatch, target)
    opened: list[str] = []
    monkeypatch.setattr(
        wa.ApiBridge, "open_path", lambda _self, path: opened.append(path) or True
    )

    result = wa.ApiBridge().open_downloads_dir()

    assert result == {"ok": True, "path": str(target.resolve()), "error": None}
    assert target.is_dir(), "the export folder is created on demand"
    assert opened == [str(target.resolve())]


def test_bridge_open_downloads_dir_reports_a_failed_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file manager that refuses must not look like a successful click."""
    import novel_downloader.apps.desktop.webview_app as wa

    target = tmp_path / "out"
    _stub_downloads_dir(monkeypatch, target)
    monkeypatch.setattr(wa.ApiBridge, "open_path", lambda _self, _path: False)

    result = wa.ApiBridge().open_downloads_dir()

    assert result["ok"] is False
    assert result["path"] == str(target.resolve())
    assert result["error"]


def test_bridge_open_downloads_dir_reports_a_failed_mkdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unwritable export path is reported, not raised."""
    import novel_downloader.apps.desktop.webview_app as wa

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    _stub_downloads_dir(monkeypatch, blocker / "child")
    monkeypatch.setattr(wa.ApiBridge, "open_path", lambda _self, _path: True)

    result = wa.ApiBridge().open_downloads_dir()

    assert result["ok"] is False
    assert result["error"]


def test_bridge_open_downloads_dir_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken config must not break the button (falls back to ./downloads)."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("settings.toml is unreadable")

    monkeypatch.setattr(config, "get_config_value", boom, raising=True)
    monkeypatch.chdir(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(
        wa.ApiBridge, "open_path", lambda _self, path: opened.append(path) or True
    )

    result = wa.ApiBridge().open_downloads_dir()

    assert result["ok"] is True
    assert (tmp_path / "downloads").is_dir()
    assert opened and opened[0].endswith("downloads")


def test_music_output_dir_prefers_the_stored_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The resolver mirrors the engine: settings out_dir, else the default."""
    import novel_downloader.apps.desktop.webview_app as wa

    default = tmp_path / "default"
    monkeypatch.setattr(
        "novel_downloader.apps.music.paths.default_download_dir", lambda: default
    )
    monkeypatch.setattr(
        "novel_downloader.apps.music.settings.load_settings", lambda: {}
    )
    assert wa._music_output_dir() == default

    stored = tmp_path / "picked"
    monkeypatch.setattr(
        "novel_downloader.apps.music.settings.load_settings",
        lambda: {"out_dir": str(stored)},
    )
    assert wa._music_output_dir() == stored


def test_music_output_dir_survives_a_broken_settings_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt settings file falls back to the platform default."""
    import novel_downloader.apps.desktop.webview_app as wa

    default = tmp_path / "default"
    monkeypatch.setattr(
        "novel_downloader.apps.music.paths.default_download_dir", lambda: default
    )

    def boom() -> dict[str, Any]:
        raise ValueError("broken json")

    monkeypatch.setattr("novel_downloader.apps.music.settings.load_settings", boom)

    assert wa._music_output_dir() == default


def test_bridge_music_download_uses_cached_tracks_for_all_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching ``search_id`` hands ``download_mixed`` the original objects."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")

    @dataclass
    class _FakeTrack:
        provider: str = ""
        id: str = ""
        name: str = ""

    original = [_FakeTrack(provider="netease", id="1", name="cached")]
    submitted = [{"provider": "netease", "id": "1", "name": "wire"}]
    seen: dict[str, Any] = {}

    class _Cache:
        def get(self, search_id: str) -> Any:
            return original if search_id == "sid" else None

        def resolve(self, search_id: str, tracks: Any) -> Any:
            seen["resolve"] = (search_id, list(tracks))
            return list(original)

    def fake_mixed(
        create_provider: Any,
        tracks: Any,
        _opts: Any,
        **kwargs: Any,
    ) -> tuple[int, int, int, list[object], list[object]]:
        seen["tracks"] = list(tracks)
        seen["create_provider"] = create_provider
        seen["platform"] = kwargs.get("platform")
        return 1, 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda key: ("created", key),
            "download_mixed": fake_mixed,
            "is_all_platforms": aggregate.is_all_platforms,
            "CACHE": _Cache(),
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    assert bridge.music_download("all", submitted, None, "sid")["ok"] is True
    assert _wait_for_music_idle(bridge)["active"] is False

    assert seen["resolve"][0] == "sid"
    assert seen["tracks"] == original
    assert seen["platform"] is None
    # "all" resolves each track's own provider.
    assert seen["create_provider"]("qq") == ("created", "qq")


def test_bridge_music_download_single_platform_pins_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")

    @dataclass
    class _FakeTrack:
        name: str = ""

    seen: dict[str, Any] = {}

    def fake_mixed(
        create_provider: Any, tracks: Any, _opts: Any, **kwargs: Any
    ) -> tuple[int, int, int, list[object], list[object]]:
        seen["create_provider"] = create_provider
        seen["platform"] = kwargs.get("platform")
        return len(tracks), 0, 0, [], []

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda key: ("created", key),
            "download_mixed": fake_mixed,
            "is_all_platforms": aggregate.is_all_platforms,
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    assert bridge.music_download("netease", [{"name": "x"}])["ok"] is True
    _wait_for_music_idle(bridge)

    assert seen["platform"] == "netease"
    # Every track goes to the platform picked in the UI, whatever it claims.
    assert seen["create_provider"]("qq") == ("created", "netease")


def test_bridge_music_download_reports_an_expired_search_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale ``search_id`` must surface as a failure, not a silent no-op."""
    import novel_downloader.apps.desktop.webview_app as wa

    aggregate = pytest.importorskip("novel_downloader.apps.music.aggregate")

    @dataclass
    class _FakeTrack:
        name: str = ""

    class _Cache:
        def get(self, _search_id: str) -> None:
            return None

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "Track": _FakeTrack,
            "create": lambda _key: None,
            "download_mixed": lambda *_a, **_k: pytest.fail("must not download"),
            "is_all_platforms": aggregate.is_all_platforms,
            "CACHE": _Cache(),
        },
    )
    monkeypatch.setattr(wa.ApiBridge, "_music_options", staticmethod(lambda _l: None))

    bridge = wa.ApiBridge()
    assert bridge.music_download("all", [{"name": "x"}], None, "gone")["ok"] is True
    status = _wait_for_music_idle(bridge)
    assert status["failed"] is True
    assert (
        t("The search results expired — search again before downloading")
        in (status["summary"])
    )


def test_bridge_music_export_accepts_the_all_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    @dataclass
    class _FakeTrack:
        name: str = ""
        path: str = ""

    written: list[str] = []

    def fake_export(tracks: list[Any], path: Path) -> str:
        written.extend(tr.path for tr in tracks)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#EXTM3U\n", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(
        wa, "_music_funcs", lambda: {"Track": _FakeTrack, "export_m3u": fake_export}
    )
    target = tmp_path / "all.m3u8"
    result = wa.ApiBridge().music_export(
        "all", [{"name": "A", "path": "C:/music/a.mp3"}], str(target)
    )
    assert result["ok"] is True
    assert result["tracks_written"] == 1
    assert written == ["C:/music/a.mp3"]
    assert target.is_file()


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


def _music_status_rows(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dependency_available: Any = True,
    tool_available: Any = True,
    configured: Any = False,
    registered: Iterable[str] = ("tidal", "apple", "soundcloud", "youtube"),
) -> dict[str, dict[str, Any]]:
    """Build ``key -> status row`` with the heavy-platform probes faked."""
    import novel_downloader.apps.desktop.webview_app as wa

    if not callable(dependency_available):
        value = dependency_available
        dependency_available = lambda _name, value=value: value  # noqa: E731
    if not callable(tool_available):
        value = tool_available
        tool_available = lambda _name, value=value: value  # noqa: E731
    if not callable(configured):
        value = configured
        configured = lambda _key, value=value: value  # noqa: E731

    keys = list(registered)
    monkeypatch.setattr(wa, "_music_dependency_available", dependency_available)
    monkeypatch.setattr(wa, "_music_tool_available", tool_available)
    monkeypatch.setattr(wa, "_music_credential_configured", configured)
    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {"available_labels": lambda: [(key, key) for key in keys]},
    )
    return {row["key"]: row for row in wa._music_platform_status()}


def test_music_status_maps_missing_dependency_to_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hidden heavy platform still gets a row naming the missing package."""
    rows = _music_status_rows(
        monkeypatch,
        dependency_available=lambda name: name != "pywidevine",
    )

    for key in ("apple", "soundcloud"):
        assert rows[key]["state"] == "missing_dependency"
        assert "pywidevine" in rows[key]["reason"]
        assert (
            t('Install the music extras with: pip install "novel-downloader[music]"')
            in rows[key]["reason"]
        )
        assert rows[key]["available"] is False

    # TIDAL/YouTube keep their own dependency state instead of a generic error.
    assert rows["tidal"]["state"] == "needs_credentials"
    assert rows["youtube"]["state"] == "ready"


def test_music_status_distinguishes_tidal_token_from_optional_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIDAL blocks search; Apple/SoundCloud only limit full downloads."""
    rows = _music_status_rows(monkeypatch, configured=False)

    assert rows["tidal"]["state"] == "needs_credentials"
    assert rows["tidal"]["needs_credentials"] is True
    assert (
        t(
            "TIDAL needs an account token before it can search; paste the token JSON "
            "under Settings → Music accounts."
        )
        in rows["tidal"]["reason"]
    )

    for key, needle in (
        ("apple", "media-user-token"),
        ("soundcloud", "oauth_token"),
    ):
        assert rows[key]["state"] == "limited"
        assert rows[key]["needs_credentials"] is True
        assert needle in rows[key]["reason"]

    assert rows["youtube"]["state"] == "ready"
    assert rows["youtube"]["can_configure"] is False


def test_music_status_surfaces_a_missing_external_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N_m3u8DL-RE gets an install hint, not a generic search failure."""
    rows = _music_status_rows(
        monkeypatch,
        tool_available=lambda name: name != "N_m3u8DL-RE",
        configured=True,
    )

    for key in ("tidal", "soundcloud"):
        assert rows[key]["state"] == "missing_tool"
        assert "N_m3u8DL-RE" in rows[key]["reason"]
        assert t("Install N_m3u8DL-RE and add it to your PATH.") in rows[key]["reason"]
        # Search still works; only the HLS/dash download path is affected.
        assert rows[key]["available"] is True
    assert rows["apple"]["state"] == "ready"


def test_music_status_reports_a_platform_the_registry_did_not_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _music_status_rows(monkeypatch, registered=())
    assert rows["tidal"]["state"] == "unavailable"
    assert t("This platform is unavailable in this build.") in rows["tidal"]["reason"]


def test_bridge_music_platform_status_returns_ok_false_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status call obeys the bridge contract instead of raising."""
    import novel_downloader.apps.desktop.webview_app as wa

    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(wa, "_music_platform_status", boom)
    assert wa.ApiBridge().get_music_platform_status() == {
        "ok": False,
        "error": "RuntimeError: boom",
    }


def test_bridge_music_credentials_returns_ok_false_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken credential store is reported, not raised across the bridge."""
    import novel_downloader.apps.desktop.webview_app as wa

    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(wa, "_music_credential_rows", boom)
    assert wa.ApiBridge().get_music_credentials() == {
        "ok": False,
        "error": "RuntimeError: boom",
    }


def test_bridge_music_credentials_save_is_not_echoed_or_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Saving stores the token but never returns or logs its value."""
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.music.providers import creds

    monkeypatch.setattr(creds, "_FILE", str(tmp_path / "platform_creds.json"))
    bridge = wa.ApiBridge()
    secret = "tidal-super-secret-token"

    with caplog.at_level(logging.DEBUG):
        saved = bridge.save_music_credentials(
            "tidal", json.dumps({"access_token": secret})
        )
        rows = bridge.get_music_credentials()
        cleared = bridge.clear_music_credentials("tidal")

    assert saved == {"ok": True, "changed": True, "configured": True}
    assert secret not in json.dumps(rows)
    assert secret not in caplog.text
    assert {row["key"]: row["configured"] for row in rows["platforms"]} == {
        "tidal": True,
        "apple": False,
        "soundcloud": False,
    }
    assert cleared == {"ok": True, "configured": False}
    rows = {row["key"]: row for row in bridge.get_music_credentials()["platforms"]}
    assert rows["tidal"]["configured"] is False


def test_bridge_music_credentials_blank_keeps_and_explicit_clear_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank means "no change"; only the clear button removes a token."""
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.music.providers import creds

    monkeypatch.setattr(creds, "_FILE", str(tmp_path / "platform_creds.json"))
    bridge = wa.ApiBridge()
    bridge.save_music_credentials("apple", '{"media-user-token": "secret"}')

    blank = bridge.save_music_credentials("apple", "   ")
    assert blank == {"ok": True, "changed": False, "configured": True}
    rows = {row["key"]: row for row in bridge.get_music_credentials()["platforms"]}
    assert rows["apple"]["configured"] is True

    bridge.clear_music_credentials("apple")
    rows = {row["key"]: row for row in bridge.get_music_credentials()["platforms"]}
    assert rows["apple"]["configured"] is False


def test_bridge_music_credentials_reject_bad_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.music.providers import creds

    monkeypatch.setattr(creds, "_FILE", str(tmp_path / "platform_creds.json"))
    bridge = wa.ApiBridge()

    invalid = bridge.save_music_credentials("tidal", "{not json")
    assert invalid["ok"] is False
    assert invalid["error"] == t("Platform credentials must be valid JSON.")

    empty = bridge.save_music_credentials("tidal", "{}")
    assert empty["ok"] is False
    assert empty["error"] == t("Platform credentials must be a non-empty JSON object.")

    unknown = bridge.save_music_credentials("not-a-platform", '{"a": 1}')
    assert unknown["ok"] is False
    assert (
        t("Unknown music platform: {platform}").replace("{platform}", "not-a-platform")
        in unknown["error"]
    )

    assert bridge.clear_music_credentials("not-a-platform")["ok"] is False


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


def test_absolutize_argv0_resolves_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.desktop import main as desktop_main_mod

    # Pin the working directory: an earlier test in the same session may have
    # chdir-ed away, and this test needs a known relative path to exist.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "script.py").write_text("", encoding="utf-8")

    original = sys.argv[0]
    try:
        # A relative path that exists is made absolute before the chdir.
        sys.argv[0] = "script.py"
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


@pytest.mark.skipif(sys.platform != "win32", reason="shortcut paths are Windows-only")
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
    assert "书音.lnk" in calls[0][-1]


@pytest.mark.skipif(sys.platform != "win32", reason="shortcut paths are Windows-only")
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
    "tr.lyric_path",
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
# Web front end (web_assets): keyboard accessibility and focus management
# ---------------------------------------------------------------------------


def test_app_html_has_a_skip_link_and_main_landmark() -> None:
    """The first tab stop skips the six nav buttons and lands on <main>."""
    assert 'class="skip-link"' in APP_HTML
    assert 'href="#main-content"' in APP_HTML
    assert re.search(r'<main[^>]*id="main-content"[^>]*tabindex="-1"', APP_HTML)
    # It must precede the sidebar, or it stops being the first focusable node.
    assert APP_HTML.index('class="skip-link"') < APP_HTML.index('class="sidebar"')
    assert 'role="tabpanel"' in APP_HTML
    # The label is translated like every other visible string.
    assert "Skip to content" in _i18n_catalog()


def test_app_html_nav_is_a_tablist_with_selected_state() -> None:
    """aria-selected tracks the active page and arrow keys move between tabs."""
    assert 'role="tablist"' in APP_HTML
    for page in ("search", "download", "history", "music", "follow", "settings"):
        tab = re.search(
            rf'id="tab-{page}"[^>]*role="tab"[^>]*aria-selected="[^"]+"'
            rf'[^>]*aria-controls="page-{page}"',
            APP_HTML,
        )
        assert tab, page
        assert re.search(
            rf'id="page-{page}"[^>]*role="tabpanel"[^>]*aria-labelledby="tab-{page}"',
            APP_HTML,
        ), page
    # One tab selected at a time, matching the initial page.
    assert re.search(r'id="tab-search"[^>]*aria-selected="true"', APP_HTML)
    nav = _js_block("function syncNav()", "function showPage(")
    assert 'b.setAttribute("aria-selected", on ? "true" : "false")' in nav
    nav_click = '$("#nav").addEventListener("click"'
    show = _js_block("function showPage(name, opts)", nav_click)
    assert "syncNav();" in show
    assert "opts.focusField === false" in show
    arrows = _js_block(
        '$("#nav-tabs").addEventListener("keydown"',
        "/* ================================================================ theme */",
    )
    for key in ("ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in arrows, key
    assert "showPage(item.dataset.page, { focusField: false });" in arrows
    assert "item.focus({ preventScroll: true });" in arrows


def test_app_html_aria_keys_resolve_via_catalogue() -> None:
    """aria-label-only keys are looked up like data-i18n / -ph / -title."""
    catalog = _i18n_catalog()
    keys = set(re.findall(r'data-i18n-aria="([^"]+)"', APP_HTML))
    assert keys
    missing = sorted(key for key in keys if key not in catalog)
    assert not missing, missing
    assert 'document.querySelectorAll("[data-i18n-aria]")' in APP_HTML


def test_app_html_modal_traps_focus_and_returns_it() -> None:
    """The add-book dialog is modal for the keyboard, not only for the mouse."""
    focus = _js_block("function focusables(root)", "function api(method")
    assert "getClientRects().length > 0" in focus
    assert "function trapTab(root, e)" in focus
    modal = _js_block("function openModal(fields, onOk)", "function typingTarget(el)")
    assert 'role="dialog"' in modal
    assert 'aria-modal="true"' in modal
    assert 'aria-labelledby="modal-title"' in modal
    assert 'id="modal-title"' in modal
    # Every field gets an accessible name, not only a placeholder.
    assert 'aria-label="${esc(t(f.ph))}"' in modal
    assert "const returnTo = document.activeElement;" in modal
    assert "document.contains(returnTo)" in modal
    assert '$("#btn-f-add")' in modal
    assert "trapTab(mask, e)" in modal
    assert 'e.key === "Escape" && !e.isComposing' in modal
    assert "firstInput.focus();" in modal
    # A second close (Esc after Cancel) must not refocus twice.
    assert "if (closed) return;" in modal


def test_app_html_detail_drawer_is_a_dialog_only_when_narrow() -> None:
    """The same node is a column on wide windows and a modal sheet on narrow."""
    assert "function setDetailDialog(open)" in APP_HTML
    assert 'box.setAttribute("role", "dialog");' in APP_HTML
    assert 'box.setAttribute("aria-modal", "true");' in APP_HTML
    assert 'box.setAttribute("aria-labelledby", "detail-title");' in APP_HTML
    assert "setDetailDialog(true);" in _js_block(
        "function openDetail()", "function closeDetail()"
    )
    assert "setDetailDialog(false);" in _js_block(
        "function closeDetail()", '$("#detail").addEventListener("keydown"'
    )
    assert "narrowDetail.matches && detailIsOpen()" in APP_HTML
    assert 'trapTab($("#detail"), e)' in APP_HTML
    # Closing still returns focus to the row that opened the drawer.
    assert "detailReturnFocus" in APP_HTML


def test_app_html_result_card_keyboard_does_not_swallow_nested_buttons() -> None:
    """Enter on a card opens details; Enter on its Download button downloads."""
    handler = _js_block(
        '$("#results").addEventListener("keydown"',
        '$("#results").addEventListener("click"',
    )
    assert "e.target !== card" in handler
    assert 'e.key !== "Enter" && e.key !== " "' in handler
    assert "selectResult(+card.dataset.sel);" in handler
    # Selection is exposed, not only painted.
    select = _js_block("function selectResult(i)", "function renderDetail")
    assert 'el.setAttribute("aria-current", "true")' in select
    assert 'el.removeAttribute("aria-current")' in select
    assert 'tabindex="0"' in APP_HTML


def test_app_html_search_escape_is_explicit_and_local() -> None:
    """Esc inside the query box cancels a run, otherwise clears the text."""
    q_esc = _js_block(
        '$("#q").addEventListener("keydown", e => {\n  if (e.key !== "Escape"',
        '// "Merge same book" defaults to on',
    )
    assert "e.isComposing" in q_esc
    assert "e.preventDefault();" in q_esc
    assert "if (searching) { cancelCurrentSearch(); return; }" in q_esc
    assert '$("#q").value = "";' in q_esc
    # The shortcut handler bails out for inputs, so this is the only definition.
    guard = APP_HTML.index(
        "typingTarget(e.target) || typingTarget(document.activeElement)"
    )
    assert guard < APP_HTML.index("if (e.ctrlKey && !e.shiftKey && !e.altKey)")
    music_esc = _js_block(
        '$("#m-q").addEventListener("keydown", e => {\n  if (e.key !== "Escape"',
        '$("#btn-m-dl").onclick',
    )
    assert '$("#m-q").value = "";' in music_esc
    assert "e.isComposing" in music_esc


def test_app_html_toast_is_announced_without_stealing_focus() -> None:
    """Toasts live in a polite live region and never move the focus."""
    assert re.search(r'id="toasts"[^>]*role="status"[^>]*aria-live="polite"', APP_HTML)
    toaster = _js_block("function toast(msg, kind", "function fmtSize(n)")
    assert 'tabindex="-1"' in toaster
    assert 'aria-label="${esc(t("Close"))}"' in toaster
    assert ".focus(" not in toaster


def test_app_html_polling_redraws_keep_keyboard_focus() -> None:
    """A 1.2 s redraw must not drop focus back to <body>."""
    assert "function captureFocus(root, attrs)" in APP_HTML
    assert "function restoreFocus(root, snap)" in APP_HTML
    tasks = _js_block("function renderTasks(snap)", "function taskCard(k)")
    assert "captureFocus(body," in tasks
    assert "restoreFocus(body, focusSnapshot);" in tasks
    tracks = _js_block(
        "function renderTracks()", '$("#m-tracks").addEventListener("change"'
    )
    assert 'captureFocus(box, ["data-key", "data-lyric-id"])' in tracks
    assert "restoreFocus(box, focusKey);" in tracks
    # Music rows carry an identity key and a name for the checkbox.
    assert 'data-key="${esc(musicTrackKey(tr))}"' in APP_HTML
    assert 'aria-label="${esc(tr.name)}"' in APP_HTML
    # A focused lyric button survives the redraw too (it has its own key).
    assert 'data-lyric-id="${i}"' in APP_HTML


def test_app_html_labels_unlabelled_controls() -> None:
    """Inputs/selects get names from the translated catalogue."""
    assert 'data-i18n-aria="Results"' in APP_HTML
    assert 'data-i18n-title="Platform"' in APP_HTML
    assert 'id="m-level-lbl"' in APP_HTML
    assert 'aria-labelledby="m-level-lbl"' in APP_HTML
    assert 'id="set-music-cred-platform-lbl"' in APP_HTML
    assert 'aria-labelledby="set-music-cred-platform-lbl"' in APP_HTML
    assert 'aria-labelledby="m-export-title"' in APP_HTML
    # Placeholders are mirrored into aria-label, which is the accessible name.
    assert 'el.setAttribute("aria-label", text);' in APP_HTML
    # Status and progress are exposed as such.
    assert 'id="search-progress" role="status" aria-live="polite"' in APP_HTML
    assert 'id="m-search-note" role="status" aria-live="polite"' in APP_HTML
    assert 'id="m-progress" role="progressbar"' in APP_HTML
    assert "aria-valuenow" in APP_HTML


def test_app_html_gender_filter_exposes_pressed_state() -> None:
    """The male/female pair is a pressed-state toggle, not two plain buttons."""
    assert 'id="cat-male" data-i18n="Male" aria-pressed="true"' in APP_HTML
    assert 'id="cat-female" data-i18n="Female" aria-pressed="false"' in APP_HTML
    switch = _js_block("function switchCat(gender)", '$("#cat-male").onclick')
    male = 'setAttribute("aria-pressed", gender === "male" ? "true" : "false")'
    female = 'setAttribute("aria-pressed", gender === "female" ? "true" : "false")'
    assert male in switch
    assert female in switch


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
    assert "if (st.error) throw new Error(st.error);" in APP_HTML
    assert 'toast(state.error, "err", 7000);' in APP_HTML


def test_app_html_search_streams_results_while_sites_answer() -> None:
    """Search polls the progressive bridge instead of one blocking call."""
    assert 'api("search_async"' in APP_HTML
    assert 'api("search_progress"' in APP_HTML
    # The polling loop must stop on the final poll rather than spin forever.
    assert "if (st.done) break;" in APP_HTML


def _js_block(start_needle: str, end_needle: str) -> str:
    """Return the JS source between two markers (a whole function body)."""
    start = APP_HTML.index(start_needle)
    return APP_HTML[start : APP_HTML.index(end_needle, start)]


def test_app_html_renders_large_result_sets_in_chunks() -> None:
    """Aggregate searches can return hundreds of rows: only one batch is built."""
    assert "const RESULT_CHUNK = 60;" in APP_HTML
    renderer = _js_block(
        "function renderResults(results, kw)", "function appendResultChunk()"
    )
    assert "results.slice(0, renderedResultCount)" in renderer
    assert "moreResultsHtml(results.length, renderedResultCount)" in renderer
    # Appended cards keep the global index into lastResults.
    append = _js_block(
        "function appendResultChunk()", '$("#results").addEventListener("keydown"'
    )
    assert "lastResults.slice(start, end)" in append
    assert "start + i" in append
    assert "renderedResultCount = end;" in append
    # The delegated click handler must claim the button before card handling.
    assert 'if (e.target.closest("#results-more")) {' in APP_HTML


def test_app_html_result_chunks_keep_selection_and_scroll() -> None:
    """data-sel stays the lastResults index and re-renders keep the scroll box."""
    renderer = _js_block(
        "function renderResults(results, kw)", "function appendResultChunk()"
    )
    assert 'const scroller = box.closest(".scroll") || box;' in renderer
    assert "const scrollTop = scroller.scrollTop;" in renderer
    assert "scroller.scrollTop = scrollTop;" in renderer
    # A streaming re-render never collapses rows the user already expanded.
    assert "Math.max(RESULT_CHUNK, renderedResultCount)" in renderer
    assert 'data-sel="${i}"' in APP_HTML


def test_app_html_search_supersedes_stale_polls() -> None:
    """A new query stops the old loop; duplicate Enter/click stays a no-op."""
    searcher = _js_block("async function doSearch()", "function resultCardHtml")
    assert "const token = ++searchSeq;" in searcher
    assert "if (searching && kw === activeSearchKeyword) {" in searcher
    assert "if (filterKey === activeSearchFilterKey) return;" in searcher
    # The loop checks the token after every await so it cannot paint stale rows.
    assert searcher.count("if (token !== searchSeq) return;") >= 3
    # Only the current owner releases the controls.
    assert "if (token === searchSeq) endSearchUi();" in searcher
    assert "function cancelActiveSearch()" in APP_HTML
    assert "cancelActiveSearch();" in _js_block(
        '$("#btn-rank").onclick', "let catGroups"
    )


def test_app_html_search_progress_and_cancel_contract() -> None:
    """The progress line and the cancel button are part of the page contract."""
    assert 'id="search-progress"' in APP_HTML
    assert 'id="search-progress-text"' in APP_HTML
    assert 'id="btn-search-cancel"' in APP_HTML
    progress = _js_block(
        "function searchProgressLine(st, secs)", "function showSearchProgress"
    )
    assert "st.sites_done" in progress
    assert "st.sites_failed" in progress
    assert "st.result_count" in progress
    assert 't("{n} sources")' in progress
    searcher = _js_block("async function doSearch()", "function resultCardHtml")
    assert "const previousId = activeSearchId;" in searcher
    assert 'api("cancel_search"' in searcher
    assert '$("#btn-search-cancel").hidden = false;' in searcher
    assert "showSearchProgress(searchProgressLine(st, secs), st.done);" in searcher
    controls = _js_block("function endSearchUi()", "function cancelActiveSearch()")
    assert '$("#btn-search-cancel").hidden = true;' in controls
    assert '$("#btn-search-cancel").onclick = cancelCurrentSearch;' in APP_HTML
    # Escape is the keyboard shortcut for the same cancellation (the else
    # branch clears the query; see the accessibility section above).
    assert "if (searching) { cancelCurrentSearch(); return; }" in APP_HTML
    cancel = _js_block("function cancelCurrentSearch()", "function resetResultList()")
    assert 't("Search cancelled")' in cancel
    assert "showSearchProgress(frozen ? frozen" in cancel


def test_app_html_search_filters_are_optional_and_collapsed() -> None:
    """The extra controls are additive: the old search bar keeps working."""
    assert 'id="btn-search-filters"' in APP_HTML
    assert 'aria-controls="search-filter-panel"' in APP_HTML
    assert 'aria-expanded="false"' in APP_HTML
    assert re.search(r'id="search-filter-panel"[^>]*hidden', APP_HTML)
    for element in ("q-min-words", "q-max-words"):
        assert f'id="{element}" type="number"' in APP_HTML
        assert 'min="0" step="1"' in APP_HTML
    assert 'id="q-updated-after" type="date"' in APP_HTML
    assert ".search-filters[hidden] { display: none; }" in APP_HTML
    filters_css = _js_block(".search-filters {", ".search-applied {")
    assert "flex-wrap: wrap;" in filters_css
    assert "min-width: 0;" in filters_css


def test_app_html_do_search_sends_and_reports_filters() -> None:
    """The three optional values travel positionally after the group flag."""
    searcher = _js_block("async function doSearch()", "function resultCardHtml")
    assert "const filters = readSearchFilters();" in searcher
    assert "if (!filters) return;" in searcher
    assert "setAppliedFilters(filters);" in searcher
    assert (
        'api("search_async", kw, $("#q-sites").value.trim(), '
        '$("#q-group").checked, filters.min_words, filters.max_words, '
        "filters.updated_after)" in searcher
    )
    # A structured bridge rejection is shown as its own message.
    assert "if (job && job.error) throw new Error(job.error);" in searcher


def test_app_html_search_filter_validation_and_clear_contract() -> None:
    """Invalid values are caught in the page; clearing wipes all three fields."""
    assert 't("Min words must be a whole number between 0 and {max}")' in APP_HTML
    assert 't("Max words must be a whole number between 0 and {max}")' in APP_HTML
    assert 't("Min words cannot be greater than max words")' in APP_HTML
    assert 't("Use a YYYY-MM-DD date")' in APP_HTML
    assert "function validDateText(text)" in APP_HTML
    assert 'id="search-filter-error"' in APP_HTML
    assert 'role="alert"' in APP_HTML
    assert 'id="search-applied"' in APP_HTML
    assert 'id="search-applied" role="status" aria-live="polite"' in APP_HTML
    assert 'id="btn-clear-filters"' in APP_HTML
    assert 't("Filters applied: {filters}")' in APP_HTML
    clear = _js_block("function clearSearchFilters()", "async function doSearch()")
    for selector in ('"#q-min-words"', '"#q-max-words"', '"#q-updated-after"'):
        assert selector in clear
    assert "setAppliedFilters(null);" in clear
    assert '$("#btn-clear-filters").onclick' in APP_HTML


def test_app_html_search_filters_are_keyboard_and_narrow_screen_friendly() -> None:
    """The toggle/panel are ordinary focusable controls and wrap on narrow windows."""
    assert '$("#search-filter-panel").addEventListener("keydown"' in APP_HTML
    assert "setFilterPanel(false);" in APP_HTML
    assert 'e.key === "Enter" && !e.isComposing' in APP_HTML
    assert "flex: 1 1 150px" in APP_HTML


def test_app_html_search_filter_labels_are_in_the_catalog() -> None:
    """New labels/placeholder/feedback go through the injected catalogue."""
    catalog = _i18n_catalog()
    for key in (
        "Filters",
        "Min words",
        "Max words",
        "Updated after",
        "Clear filters",
        "e.g. 100000",
        "Filters applied: {filters}",
    ):
        assert key in catalog


def test_app_html_restores_saved_search_filters_without_searching() -> None:
    """Boot fills the panel from prefs, but never starts a search itself."""
    restore = _js_block(
        "function applyStoredSearchFilters(filters)", "async function doSearch()"
    )
    for selector in ('"#q-min-words"', '"#q-max-words"', '"#q-updated-after"'):
        assert selector in restore
    assert "Number.isInteger(filters.min_words)" in restore
    assert "Number.isInteger(filters.max_words)" in restore
    assert "validDateText(date)" in restore
    # Opening the restored panel must not steal focus from the search box.
    assert "setFilterPanel(true, false);" in restore
    assert "doSearch" not in restore
    assert "applyStoredSearchFilters(state.search_filters);" in APP_HTML


def test_app_html_persists_and_clears_search_filters() -> None:
    """A valid search saves the filters; Clear filters forgets them."""
    searcher = _js_block("async function doSearch()", "function resultCardHtml")
    assert (
        'api("save_search_filters", filters.min_words, filters.max_words, '
        "filters.updated_after)" in searcher
    )
    clear = _js_block("function clearSearchFilters()", "async function doSearch()")
    assert 'api("clear_search_filters").catch(() => {});' in clear


def test_app_html_settings_can_clear_saved_search_filters() -> None:
    """The settings page owns the explicit "forget my filters" action."""
    assert 'id="btn-set-clear-search-filters"' in APP_HTML
    handler = _js_block(
        '$("#btn-set-clear-search-filters").onclick', '$("#btn-set-shortcut").onclick'
    )
    assert 'call("clear_search_filters")' in handler
    assert "clearSearchFilters();" in handler
    assert 't("Saved search filters cleared")' in handler
    catalog = _i18n_catalog()
    for key in (
        "Search filters",
        "The last used filters are restored when the app starts.",
        "Clear saved filters",
        "Saved search filters cleared",
        "Could not clear the saved search filters",
    ):
        assert key in catalog


def test_app_html_playlist_export_confirms_overwrite() -> None:
    """The bridge is asked first; stale targets need an explicit second click."""
    assert '"music_export", $("#m-platform").value, ready, "", overwrite' in APP_HTML
    assert "r.needs_overwrite" in APP_HTML
    assert 't("Click to overwrite")' in APP_HTML
    assert "exportPlaylist(true)" in APP_HTML


def test_app_html_surfaces_rejected_task_errors() -> None:
    """``{"ok": false, "error": ...}`` answers are toasted, not swallowed."""
    assert 'else if (r && r.error) toast(r.error, "err");' in APP_HTML
    assert 'else if (r && r.error) toast(r.error, "warn");' in APP_HTML


def _js_handler(needle: str) -> str:
    """Return the JS statement block starting at ``needle``."""
    start = APP_HTML.index(needle)
    end = APP_HTML.index("\n\n", start)
    return APP_HTML[start:end]


# ---------------------------------------------------------------------------
# Web front end (web_assets): download / history / follow feedback
# ---------------------------------------------------------------------------


def test_app_html_finished_bucket_is_not_labelled_completed() -> None:
    """The third bucket holds failed and cancelled rows too."""
    assert '["done", t("Finished"), done]' in APP_HTML
    assert '["done", t("Completed")' not in APP_HTML


def test_app_html_draws_a_task_in_one_bucket_only() -> None:
    """A cancelled run can sit in ``running`` *and* ``completed`` for a poll."""
    renderer = _js_handler("function renderTasks(snap)")
    assert "const settled = new Set(done.map(k => k.task_id));" in renderer
    assert "settled.has(k.task_id) || seen.has(k.task_id)" in renderer
    # The terminal record wins, so the card stays under "Finished".
    assert '["done", t("Finished"), done]' in renderer
    # The empty state must look at the de-duplicated buckets, not the raw ones.
    assert "if (liveCount === 0 && groups[2][2].length === 0)" in renderer


def test_app_html_task_card_shows_chapter_count_and_percent() -> None:
    """Per-task progress is drawn as ``done/total · pct%``, not only a bar."""
    card = _js_handler("function taskCard(k)")
    assert "const chapterText = total > 0 ? `${done}/${total} · ${pct}%`" in card
    # The indeterminate bar is only for a task whose chapter list is unknown.
    assert "const indet = active && total <= 0;" in card
    assert 'meta.push(`<span class="mono">${chapterText}</span>`)' in card


def test_app_html_failed_task_keeps_its_error_actionable() -> None:
    """A failed row offers the whole message and the export folder."""
    card = _js_handler("function taskCard(k)")
    assert 'class="task-error" title="${esc(k.error)}"' in card
    assert 'data-copy-error="${esc(k.task_id)}"' in card
    assert "data-open-dir" in card
    assert 't("Copy error")' in card
    handler = _js_handler('$("#tasks-body").addEventListener("click"')
    assert 'await copyText(taskErrors.get(ce.dataset.copyError) || "")' in handler
    assert 'await call("open_downloads_dir")' in handler
    # The reporting helper and its "open failed" message are reused, not
    # duplicated with a silent api() call.
    assert 't("Could not open the download folder: {error}")' in handler


def test_app_html_follow_check_reports_progress_and_empty_outcome() -> None:
    """The sweep shows live progress and says when nothing was new."""
    assert 'id="f-status"' in APP_HTML
    assert 'api("get_follow_check_state")' in APP_HTML
    assert 't("Checking for updates…")' in APP_HTML
    assert 't("Checking {done}/{total}…")' in APP_HTML
    assert 't("Checked {n} books · No new chapters")' in APP_HTML
    handler = _js_handler('$("#btn-f-check").onclick')
    assert "startFollowProgress();" in handler
    assert "await renderFollowReport(r);" in handler


def test_app_html_cancel_and_retry_report_rejected_requests() -> None:
    """``{ok: false}`` from cancel/retry must explain the dead-looking click."""
    handler = _js_handler('$("#tasks-body").addEventListener("click"')
    stale_cancel = (
        "if (res && res.ok === false) "
        'toast(t("This task has already finished"), "warn");'
    )
    assert stale_cancel in handler
    assert (
        'if (res && res.ok === false) toast(t("Could not retry this task"), "err");'
        in handler
    )
    # The card list is re-polled either way: the manager owns the truth.
    assert handler.count("refreshTasks();") == 2
    # Open-file goes through the reporting helper, not a bare api() call.
    assert "openPath(f.dataset.openFile);" in handler


def test_app_html_open_path_failures_are_never_swallowed() -> None:
    """``open_path`` answers ``false`` for a vanished file; say so."""
    assert "async function openPath(path)" in APP_HTML
    helper = _js_handler("async function openPath(path)")
    assert "if (ok === false)" in helper
    assert 't("Could not open {path}")' in helper
    assert 'throw new Error("bridge not ready")' not in helper
    # Every open click funnels through the helper: no silent .catch(() => {}).
    assert APP_HTML.count('api("open_path"') == 1
    assert APP_HTML.count("openPath(") >= 5


@pytest.mark.parametrize(
    "needle",
    [
        '$("#tasks-body").addEventListener("click"',
        '$("#hist-body").addEventListener("click"',
        '$("#m-export-open").onclick',
        '$("#m-tracks").addEventListener("click"',
    ],
)
def test_app_html_open_buttons_use_the_reporting_helper(needle: str) -> None:
    """Task exports, history rows and the playlist path all report failures."""
    assert "openPath(" in _js_handler(needle)


def test_app_html_music_rows_offer_saved_lyrics() -> None:
    """A downloaded track with a ``.lrc`` gets a keyboard-reachable button."""
    # The row only grows the button when the bridge reported an existing file.
    assert 'data-lyric="${esc(tr.lyric_path)}"' in APP_HTML
    assert 'id="m-lyric-${i}"' in APP_HTML
    assert 'data-lyric-id="${i}"' in APP_HTML
    assert '<svg class="icon"><use href="#i-lyrics"/></svg>' in APP_HTML
    assert 't("Lyrics for {name}")' in APP_HTML
    # A native button is tabbable and activates on Enter/Space; it opens the
    # file through the reporting helper (failure becomes a toast).
    handler = _js_handler('$("#m-tracks").addEventListener("click"')
    assert "openPath(btn.dataset.lyric);" in handler
    # Polling mirrors both paths and lyric paths; rows are repainted once the
    # batch settles so the buttons appear without a second search.
    poll = _js_handler("function pollMusic()")
    assert "item.lyric_path" in poll
    assert "if (hasLyrics) renderTracks();" in poll


def test_app_html_music_rows_offer_translated_lyrics() -> None:
    """A translated ``.zh.lrc`` gets its own button next to the original."""
    # The bridge's stable language slots are labelled through the i18n table,
    # so saved rows follow a later language switch.
    assert 'if (slot === "原文") return t("Original lyrics");' in APP_HTML
    assert 'if (slot === "译文") return t("Translated lyrics");' in APP_HTML
    # One native button per saved file, carrying the path the click handler
    # already sends through openPath(). The two buttons share a labelled group.
    helper = _js_handler("function lyricButtons(tr, i)")
    assert 'data-lyric="${esc(entry.path)}"' in helper
    assert 'data-lyric-lang="${esc(entry.lang)}"' in helper
    assert 'role="group"' in helper
    assert 'type="button"' in helper
    assert 't("Lyrics for {name}")' in helper
    # Legacy payloads (lyric_path without lyric_paths) keep the old button.
    assert 'id="m-lyric-${i}"' in helper
    assert 'data-lyric="${esc(tr.lyric_path)}"' in helper
    # The click handler is shared, so translations use the reporting helper too.
    handler = _js_handler('$("#m-tracks").addEventListener("click"')
    assert "openPath(btn.dataset.lyric);" in handler
    # Polling mirrors the list and repaints the rows when any lyric lands.
    poll = _js_handler("function pollMusic()")
    assert "item.lyric_paths" in poll
    assert "tr.lyric_paths = item.lyric_paths;" in poll
    assert "if (hasLyrics) renderTracks();" in poll


def test_app_html_open_download_folder_reports_failure() -> None:
    """The history "Open folder" button must not be a silent no-op."""
    handler = _js_handler('$("#btn-open-dl").onclick')
    assert 'await call("open_downloads_dir")' in handler
    assert "if (r && r.ok === false)" in handler
    assert 't("Could not open the download folder: {error}")' in handler
    assert 'r.error || t("Unknown error")' in handler


def test_app_html_load_failure_panel_offers_a_retry() -> None:
    """A failed list load is neither an empty state nor a dead end."""
    assert "function renderLoadFailure(body, message)" in APP_HTML
    helper = _js_handler("function renderLoadFailure(body, message)")
    assert 't("Could not load this list")' in helper
    assert "data-reload" in helper
    assert "esc(message)" in helper, "the backend message must be escaped"
    # Both list pages route their catch block through it…
    assert APP_HTML.count("renderLoadFailure(body, String(e && e.message || e));") == 2
    # …and both delegate the retry click back to their loader.
    assert (
        'if (e.target.closest("[data-reload]")) { refreshHistory(); return; }'
        in APP_HTML
    )
    assert (
        'if (e.target.closest("[data-reload]")) { refreshFollow(); return; }'
        in APP_HTML
    )


def test_app_html_load_failure_panel_does_not_blow_up_the_button_icon() -> None:
    """``.empty svg.icon`` is 34px; the retry button needs its own size."""
    assert ".empty .btn svg.icon { width: 14px; height: 14px" in APP_HTML


def test_app_html_history_subline_failure_keeps_the_list() -> None:
    """``get_data_dir`` is decoration: failing it must not hide the files."""
    assert (
        'try { dir = (await api("get_data_dir")) || ""; } catch { dir = ""; }'
        in APP_HTML
    )
    sentence = (
        't("{n} files in {dir}").replace("{n}", items.length).replace("{dir}", dir)'
    )
    assert sentence in APP_HTML
    # The sub-line is cleared when the list itself could not load.
    assert '$("#hist-sub").textContent = "";' in APP_HTML


def test_app_html_unfollow_failure_is_reported() -> None:
    """``remove_watch`` answering ``{ok: false}`` left the row with no word."""
    handler = _js_handler('$("#follow-body").addEventListener("click"')
    assert 'await call("remove_watch", u.dataset.unfollow, u.dataset.bid);' in handler
    failed_remove = (
        "if (r && r.ok === false) "
        'toast(t("Could not remove this book from the follow list"), "err", 6000);'
    )
    assert failed_remove in handler
    # A rejected queue request is surfaced as well, not only ``r.error``.
    assert (
        'else if (r && r.ok === false) toast(t("Could not queue this book"), "err");'
        in handler
    )
    # Dataset keys of the two row buttons exist in the template.
    follow_row = (
        'data-fdl="${esc(w.site)}" data-bid="${esc(w.book_id)}" '
        'data-title="${esc(w.title)}"'
    )
    assert follow_row in APP_HTML
    assert 'data-unfollow="${esc(w.site)}" data-bid="${esc(w.book_id)}"' in APP_HTML


def test_app_html_music_download_guard_is_synchronous() -> None:
    """The download button disables before the await (double-click race)."""
    handler = _js_handler('$("#btn-m-dl").onclick')
    assert "btn.disabled = true;" in handler
    assert handler.index("btn.disabled = true;") < handler.index("await call")


def test_app_html_music_platform_defaults_to_all() -> None:
    """The first option is the aggregate search and it is selected by default."""
    assert '<option value="all">${esc(t("All platforms"))}</option>' in APP_HTML
    assert 'sel.value = "all";' in APP_HTML
    # The aggregate option is prepended, so it stays first after a refresh.
    assert 'sel.innerHTML = `<option value="all">' in APP_HTML


def test_app_html_music_search_and_download_carry_the_search_id() -> None:
    """The search id is what lets the bridge recover the original tracks."""
    assert 'api("music_search_async", platform, kw, 30)' in APP_HTML
    assert 'api("music_search_progress", job.search_id || job.result_id)' in APP_HTML
    # Downloads only start once the final, cached result id has arrived.
    assert 'music.searchId = state.result_id || "";' in APP_HTML
    assert (
        'call("music_download", $("#m-platform").value, tracks, null, '
        "music.searchId || null)" in APP_HTML
    )


def test_app_html_music_search_is_progressive_and_supersedable() -> None:
    """Poll every 250 ms, redraw partials, and let a newer search win."""
    assert "let musicSearchToken = 0;" in APP_HTML
    assert "const token = ++musicSearchToken;" in APP_HTML
    assert "if (token !== musicSearchToken) return;" in APP_HTML
    assert "await new Promise(resolve => setTimeout(resolve, 250));" in APP_HTML
    assert "function applyMusicTracks(tracks)" in APP_HTML
    assert 'id="m-search-note"' in APP_HTML
    assert "function musicSearchNote(text, warn)" in APP_HTML
    assert 't("Received results from {n} platforms…")' in APP_HTML
    # Aggregate platform failures are inline page copy, never a toast.
    assert 't("{n} platforms did not respond")' in APP_HTML
    assert 'toast(t("{n} platforms did not respond")' not in APP_HTML
    # The download button stays disabled until the final result id arrives.
    assert '$("#btn-m-dl").disabled = true;' in APP_HTML
    assert (
        '$("#btn-m-dl").disabled = music.downloading || !music.searchId '
        "|| !music.selected.size;" in APP_HTML
    )


def test_app_html_music_keeps_manual_selection_across_partials() -> None:
    """Partial snapshots are re-sorted; manual picks follow their identity."""
    match = re.search(r"function applyMusicTracks\(tracks\) \{[\s\S]*?\n\}", APP_HTML)
    assert match, "applyMusicTracks() is missing from the front end"
    handler = match.group(0)
    assert "music.userTouched" in handler
    assert "musicTrackKey(music.tracks[i])" in handler
    assert "keep.has(musicTrackKey(music.tracks[i]))" in handler
    assert "music.userTouched = true;" in APP_HTML


def test_app_html_music_download_failures_offer_reason_and_retry() -> None:
    """Failed rows stay on the page with a retry that reuses the search id."""
    assert 'id="m-failures"' in APP_HTML
    assert 'id="m-fail-title"' in APP_HTML
    assert 'id="m-fail-list"' in APP_HTML
    assert 'id="m-fail-close"' in APP_HTML
    assert 'id="btn-m-retry"' in APP_HTML
    assert 'data-i18n="Retry failed"' in APP_HTML
    assert "const MUSIC_FAIL_ROWS = 8;" in APP_HTML
    assert "music.failedItems.slice(0, MUSIC_FAIL_ROWS)" in APP_HTML
    assert "function showMusicFailures(items)" in APP_HTML
    assert "function hideMusicFailures()" in APP_HTML
    handler = _js_handler('$("#btn-m-retry").onclick')
    assert (
        'call("music_download", $("#m-platform").value, tracks, null, '
        "music.searchId || null)" in handler
    )
    # The poll turns the bridge's failed_items into the panel.
    assert (
        "showMusicFailures(st && Array.isArray(st.failed_items) ? "
        "st.failed_items : []);" in APP_HTML
    )


def test_app_html_music_retry_maps_batch_indexes_back_to_rows() -> None:
    """``failed_items[*].index`` is a position in the submitted batch."""
    handler = _js_handler('$("#btn-m-retry").onclick')
    # The bridge reports positions inside the last batch; the page translates
    # them through ``music.batch`` before resubmitting.
    assert "music.batch[it.index]" in handler
    assert "music.tracks[i] ?" not in handler
    assert "const indexes = [...new Set(rows)].sort((a, b) => a - b);" in handler
    # A retry with nothing left to map must close the panel, not submit all.
    assert "if (!rows.length) { hideMusicFailures(); return; }" in handler


def test_app_html_music_search_drops_stale_batch_indexes() -> None:
    """A new result list invalidates the indexes of the previous batch."""
    assert "music.batch = [];\n  hideMusicFailures();" in APP_HTML


def test_app_html_music_has_an_open_folder_button() -> None:
    """The music page can reveal the folder its files land in."""
    assert 'id="btn-m-open"' in APP_HTML
    assert 'data-i18n="Open music folder"' in APP_HTML
    handler = _js_handler('$("#btn-m-open").onclick')
    assert 'await call("open_music_dir");' in handler
    assert 't("Could not open the music folder: {error}")' in handler
    assert "if (!r || !r.ok)" in handler


def test_app_html_music_rows_prefer_sources_over_provider() -> None:
    """Aggregate rows list their source platforms; single rows fall back."""
    match = re.search(r"function sourceChips\(tr\) \{[\s\S]*?\n\}", APP_HTML)
    assert match, "sourceChips() is missing from the front end"
    handler = match.group(0)
    assert "tr.sources" in handler
    assert "tr.provider" in handler
    assert 'class="src-tag"' in handler
    assert "${sourceChips(tr)}" in APP_HTML


def test_app_html_music_all_platform_note_is_inline_not_an_error() -> None:
    """The "all platforms" hint is page copy: it must not raise a toast."""
    assert 'note = await call("music_note", $("#m-platform").value) || "";' in APP_HTML
    assert '$("#m-note").textContent = note;' in APP_HTML


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
    # Geometry is fitted to the current monitors instead of being clamped at
    # zero; ``clamp_geometry`` keeps legitimate negative origins.
    assert "clamp_geometry(geometry, _virtual_screen_rect())" in source
    assert "_virtual_screen_rect()" in source


# ---------------------------------------------------------------------------
# Desktop shortcuts: PowerShell quoting of the target path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32", reason="PowerShell quoting is Windows-only"
)
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
    """``download_mixed`` fires callbacks from a worker pool; counts must win."""
    import novel_downloader.apps.desktop.webview_app as wa

    total = 24

    def fake_download_mixed(
        _create_provider: object,
        tracks: list[_FakeTrack],
        _opts: object,
        workers: int = 3,
        on_result: Any = None,
        on_tick: Any = None,
        should_stop: Any = None,
        platform: str | None = None,
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
            "download_mixed": fake_download_mixed,
            "is_all_platforms": lambda platform: (
                not str(platform or "").strip()
                or str(platform).strip().lower() == "all"
            ),
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


def test_result_buttons_read_the_dataset_keys_they_set() -> None:
    """A card puts the site in ``data-add`` / ``data-follow``.

    Reading ``dataset.site`` instead passed an empty string to ``add_task``,
    so every Download / Follow click answered "Site and book ID are required"
    even though the row rendered perfectly.
    """
    assert "add.dataset.add" in APP_HTML
    assert "add.dataset.bid" in APP_HTML
    assert "fol.dataset.follow" in APP_HTML
    assert "fol.dataset.bid" in APP_HTML
    assert "dataset.site" not in APP_HTML


# ---------------------------------------------------------------------------
# Front end <-> bridge contract parity
# ---------------------------------------------------------------------------

#: Every ``api("x")`` / ``call("x")`` target found in the single-file front end.
_JS_BRIDGE_CALLS = sorted(
    set(re.findall(r'(?:api|call)\("([A-Za-z_][A-Za-z0-9_]*)"', APP_HTML))
)


def test_every_front_end_call_targets_a_public_bridge_method() -> None:
    """A renamed/removed bridge method must fail here, not in a click handler."""
    assert _JS_BRIDGE_CALLS, "the front end declares no bridge calls"
    missing = sorted(
        name
        for name in _JS_BRIDGE_CALLS
        if not callable(getattr(ApiBridge, name, None))
    )
    assert not missing, f"front end calls unknown bridge methods: {missing}"


def _bridge_literal_keys(methods: Iterable[str]) -> set[str]:
    """String keys of every dict literal inside the named ApiBridge methods."""
    import novel_downloader.apps.desktop.webview_app as wa

    tree = ast.parse(Path(wa.__file__).read_text(encoding="utf-8"))
    bridge = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApiBridge"
    )
    functions = {
        node.name: node
        for node in bridge.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    keys: set[str] = set()
    for name in methods:
        function = functions[name]
        for node in ast.walk(function):
            if not isinstance(node, ast.Dict):
                continue
            keys.update(
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
    return keys


#: ``method -> (helper methods building its payload, keys the front end reads)``.
#: The helpers matter because several replies are assembled in a private
#: ``_*_payload`` function (``update_state`` and friends).
_PAYLOAD_CONTRACT: dict[str, tuple[tuple[str, ...], set[str]]] = {
    "get_initial_state": (
        ("get_initial_state",),
        {
            "version",
            "theme",
            "theme_stored",
            "data_dir",
            "output_dir",
            "is_frozen",
            "shortcut_exists",
            "auto_check_updates",
            "auto_download_updates",
            "auto_install_on_exit",
            "language",
            "languages",
            "i18n",
        },
    ),
    "get_settings": (
        ("get_settings",),
        {
            "version",
            "config_path",
            "output_dir",
            "proxy",
            "proxy_user",
            "proxy_has_password",
            "auto_check_updates",
            "auto_download_updates",
            "auto_install_on_exit",
            "theme",
            "language",
            "languages",
            "shortcut_supported",
            "shortcut_exists",
            "data_dir",
        },
    ),
    "save_settings": (
        ("save_settings", "_save_settings"),
        {"ok", "errors", "saved", "message", "settings"},
    ),
    "save_prefs": (("save_prefs",), {"ok", "error", "ignored"}),
    "create_shortcut": (("create_shortcut",), {"ok", "message"}),
    "check_update": (("check_update",), {"available", "current", "latest", "url"}),
    "check_update_async": (
        ("check_update_async", "_update_payload", "_empty_update_payload"),
        {
            "available",
            "latest",
            "notes_url",
            "url",
            "asset_name",
            "asset_size",
            "downloaded",
            "downloaded_size",
            "pending",
            "error",
            "started",
            "reason",
        },
    ),
    "update_state": (
        ("update_state", "_update_payload", "_empty_update_payload"),
        {
            "current",
            "available",
            "latest",
            "notes_url",
            "url",
            "asset_name",
            "asset_size",
            "downloaded",
            "downloaded_size",
            "platform",
            "checked_at",
            "error",
            "pending",
            "auto_check",
            "auto_install",
        },
    ),
    "update_download_state": (
        (
            "update_download_state",
            "download_update_async",
            "_download_update",
            "_download_error",
            "__init__",
        ),
        {"active", "ok", "received", "total", "error"},
    ),
    "download_update_async": (("download_update_async",), {"started", "error"}),
    "download_update": (
        ("download_update", "_download_update", "_download_error"),
        {"ok", "path", "error"},
    ),
    "install_update": (
        ("install_update", "_install_update", "_launch_update"),
        {"ok", "error"},
    ),
    "search_async": (("search_async",), {"search_id", "results", "done"}),
    "search_progress": (
        ("search_progress",),
        {
            "results",
            "done",
            "cancelled",
            "error",
            "sites_done",
            "sites_completed",
            "sites_failed",
            "result_count",
            "elapsed",
        },
    ),
    "cancel_search": (("cancel_search",), {"ok", "search_id", "cancelled"}),
    "add_task": (("add_task",), {"ok", "task_id", "error"}),
    "add_watch": (("add_watch",), {"ok", "message", "created"}),
    "rank_list": (("rank_list",), {"results", "error"}),
    "category_list": (("category_list",), {"groups", "error"}),
    "category_books": (("category_books",), {"results", "error"}),
    "get_tasks": (("get_tasks",), {"running", "queued", "completed"}),
    "cancel_task": (("cancel_task",), {"ok"}),
    "retry_task": (("retry_task",), {"ok"}),
    "open_downloads_dir": (("open_downloads_dir",), {"ok", "path", "error"}),
    "open_music_dir": (("open_music_dir",), {"ok", "path", "error"}),
    "get_history": (
        ("get_history",),
        {"name", "ext", "path", "size", "size_text", "mtime", "mtime_text"},
    ),
    "get_music_settings": (
        ("get_music_settings",),
        {
            "level",
            "embed_tags",
            "embed_cover",
            "save_lyric",
            "split_folder",
            "skip_existing",
        },
    ),
    "get_music_platform_status": (
        ("get_music_platform_status",),
        {"ok", "platforms", "error"},
    ),
    "get_music_credentials": (
        ("get_music_credentials",),
        {"ok", "platforms", "error"},
    ),
    "save_music_credentials": (
        ("save_music_credentials",),
        {"ok", "error", "changed", "configured"},
    ),
    "clear_music_credentials": (
        ("clear_music_credentials",),
        {"ok", "error", "configured"},
    ),
    "music_search_async": (
        ("music_search_async",),
        {"search_id", "done", "tracks", "platforms", "result_id", "error"},
    ),
    "music_search_progress": (
        ("music_search_progress",),
        {"tracks", "platforms", "done", "error", "result_id"},
    ),
    "music_download": (("music_download",), {"ok", "error"}),
    "music_export": (("music_export",), {"ok", "path", "skipped", "error"}),
    "get_music_status": (
        ("get_music_status",),
        {
            "active",
            "done",
            "total",
            "fraction",
            "summary",
            "failed",
            "paths",
            "results",
            "failed_items",
        },
    ),
    "get_watchlist": (
        ("get_watchlist",),
        {"site", "book_id", "title", "last_chapter", "updated_at", "updated_text"},
    ),
    "check_updates": (("check_updates",), {"checked", "updated", "failed", "error"}),
    "remove_watch": (("remove_watch",), {"ok"}),
    "music_health_start": (("music_health_start",), {"ok", "error"}),
    "get_music_health": (
        ("get_music_health",),
        {"done", "ok", "error", "label", "elapsed_text"},
    ),
}


def test_bridge_payloads_expose_the_keys_the_front_end_reads() -> None:
    """Payload construction and page reads stay in step (the ``dataset.site`` class)."""
    failures: list[str] = []
    for method, (sources, required) in _PAYLOAD_CONTRACT.items():
        available = _bridge_literal_keys(sources)
        missing = sorted(required - available)
        if missing:
            failures.append(f"{method}: missing {missing}")
    assert not failures, "; ".join(failures)


def _kebab_case(name: str) -> str:
    """``openUrl`` -> ``open-url`` (the attribute spelling of a dataset key)."""
    return re.sub(r"([A-Z])", lambda match: "-" + match.group(1).lower(), name)


def test_every_dataset_read_has_a_matching_data_attribute() -> None:
    """``dataset.foo`` is only valid when some element carries ``data-foo``."""
    attributes = {
        match.group(1) for match in re.finditer(r"data-([A-Za-z0-9-]+)\s*=", APP_HTML)
    }
    reads = {
        match.group(1) for match in re.finditer(r"\bdataset\.([A-Za-z0-9_]+)", APP_HTML)
    }
    assert reads, "the front end must read dataset values"
    missing = sorted(name for name in reads if _kebab_case(name) not in attributes)
    assert not missing, f"dataset reads without a data-* attribute: {missing}"


def test_app_html_category_failures_are_not_rendered_as_empty_lists() -> None:
    """``category_*`` answers an ``error`` key alongside empty rows."""
    guard = "if (data && data.error) throw new Error(data.error);"
    # The rankings handler already did this; the two category handlers now do
    # too instead of painting an empty chip list / "No results".
    assert guard in _js_handler('$("#btn-cats").onclick')
    assert guard in _js_handler('$("#cat-chips").addEventListener("click"')
    # Both handlers run inside a try/catch that prints the message.
    assert '$("#cat-hint").textContent = String(e && e.message || e);' in APP_HTML


def test_app_html_follow_check_reports_a_failed_run() -> None:
    """``check_updates`` errors must not read as "Checked 0 books"."""
    handler = _js_handler('$("#btn-f-check").onclick')
    assert "if (r && r.error) {" in handler
    assert 'toast(r.error, "err", 7000);' in handler
    assert handler.index("if (r && r.error)") < handler.index('toast(t("Checked')


def test_app_html_music_health_refusal_is_reported() -> None:
    """A refused health check (missing extras) used to leave an empty panel."""
    handler = _js_handler('$("#btn-m-health").onclick')
    assert 'if (r && r.error) toast(r.error, "warn", 6000);' in handler


def test_app_html_music_availability_links_to_the_matching_credential_field() -> None:
    """An unavailable heavy platform explains itself and focuses its token field."""
    status = _js_handler("async function refreshMusicAvailability()")
    assert 'call("get_music_platform_status")' in status
    assert 'data-music-cred="' in status
    assert 't("Go to settings")' in status

    click = _js_handler('$("#m-availability").addEventListener("click"')
    assert "openMusicCredential(btn.dataset.musicCred)" in click

    opener = _js_handler("function openMusicCredential(key)")
    assert 'showPage("settings")' in opener
    assert "loadMusicCredentialSettings(key)" in opener
    assert "input.focus()" in opener


def test_app_html_music_credentials_refresh_the_music_page() -> None:
    """Saving or clearing a token immediately repaints the music status panel."""
    save = _js_handler('$("#btn-music-cred-save").onclick')
    assert 'call("save_music_credentials", row.key, value)' in save
    assert "await refreshMusicAvailability();" in save
    assert "await updateMusicNote();" in save

    clear = _js_handler('$("#btn-music-cred-clear").onclick')
    assert 'call("clear_music_credentials", row.key)' in clear
    assert "await refreshMusicAvailability();" in clear

    # A blank field is a no-op, never an implicit clear.
    assert "if (!value) {" in save
    assert 't("Enter the token JSON before saving")' in save


# ---------------------------------------------------------------------------
# Bridge methods not covered above: history, folders, music health
# ---------------------------------------------------------------------------


def test_data_dir_is_the_history_scan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history sub-line must name the folder the files were read from."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_pkg

    target = tmp_path / "books"
    monkeypatch.setattr(
        config_pkg,
        "get_config_value",
        lambda keys, default: (
            str(target) if keys == ["general", "output_dir"] else default
        ),
    )
    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    bridge = wa.ApiBridge()
    expected = str(target.resolve())
    assert bridge.get_data_dir() == expected
    # The bootstrap payload's ``data_dir`` uses the same meaning.
    assert bridge.get_initial_state()["data_dir"] == expected


def test_bridge_get_history_formats_each_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_history`` hands the page name/size/time text, newest first."""
    import novel_downloader.apps.desktop.webview_app as wa
    from novel_downloader.apps.common import history_files

    older = history_files.FileItem(
        path=tmp_path / "older.txt",
        name="older.txt",
        ext="txt",
        size=1024,
        mtime=1_700_000_000.0,
    )
    newer = history_files.FileItem(
        path=tmp_path / "newer.epub",
        name="newer.epub",
        ext="epub",
        size=2048,
        mtime=1_700_000_100.0,
    )
    monkeypatch.setattr(history_files, "_scan_files", lambda: [older, newer])

    rows = wa.ApiBridge().get_history()

    assert [row["name"] for row in rows] == ["newer.epub", "older.txt"]
    assert rows[0]["size_text"] == "2.0 KB"
    assert rows[1]["size_text"] == "1.0 KB"
    assert rows[0]["mtime_text"] == wa._format_time(newer.mtime)
    assert rows[0]["path"] == str(newer.path)


def test_bridge_open_downloads_dir_creates_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is created on demand and reported as ``{ok, path, error}``."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_pkg

    target = tmp_path / "exports"
    monkeypatch.setattr(
        config_pkg, "get_config_value", lambda keys, default: str(target)
    )
    opened: list[str] = []
    monkeypatch.setattr(
        wa.ApiBridge,
        "open_path",
        lambda _self, path: opened.append(path) or True,
    )

    result = wa.ApiBridge().open_downloads_dir()

    assert target.is_dir()
    assert result == {"ok": True, "path": str(target.resolve()), "error": None}
    assert opened == [str(target.resolve())]


def test_bridge_open_downloads_dir_reports_a_shell_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed open answers ``ok: False`` with a message, never ``None``."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_pkg

    target = tmp_path / "exports"
    monkeypatch.setattr(
        config_pkg, "get_config_value", lambda keys, default: str(target)
    )
    monkeypatch.setattr(wa.ApiBridge, "open_path", lambda _self, _path: False)

    result = wa.ApiBridge().open_downloads_dir()

    assert result["ok"] is False
    assert result["path"] == str(target.resolve())
    assert result["error"] == t("The system file manager could not open it")


def test_bridge_music_platforms_and_health_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms carry their note; health rows carry done/ok/latency text."""
    import novel_downloader.apps.desktop.webview_app as wa

    def fake_health(key: str, timeout: float) -> dict[str, Any]:
        assert timeout == wa.HEALTH_CHECK_TIMEOUT
        if key == "qq":
            return {"ok": False, "elapsed_ms": 34, "error": "timeout"}
        return {"ok": True, "elapsed_ms": 12, "error": ""}

    monkeypatch.setattr(
        wa,
        "_music_funcs",
        lambda: {
            "available_labels": lambda: [("netease", "NetEase"), ("qq", "QQ")],
            "platform_note": lambda key: f"note-{key}",
            "health_check": fake_health,
        },
    )

    bridge = wa.ApiBridge()
    assert bridge.get_music_platforms() == [
        {"key": "netease", "label": "NetEase", "note": "note-netease"},
        {"key": "qq", "label": "QQ", "note": "note-qq"},
    ]
    assert bridge.music_health_start() == {"ok": True}

    deadline = time.monotonic() + 5.0
    rows = bridge.get_music_health()
    while time.monotonic() < deadline and not all(row["done"] for row in rows):
        time.sleep(0.01)
        rows = bridge.get_music_health()

    assert [row["key"] for row in rows] == ["netease", "qq"]
    assert rows[0]["ok"] is True and rows[0]["elapsed_text"] == "12ms"
    assert rows[1]["ok"] is False and rows[1]["elapsed_text"] == "34ms"
    assert rows[1]["error"] == "timeout"


def test_bridge_music_health_start_requires_the_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing without the extras now carries the reason the page toasts."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(wa, "_music_funcs", lambda: None)
    assert wa.ApiBridge().music_health_start() == {
        "ok": False,
        "error": t("Music extras are not installed"),
    }


def test_bridge_create_shortcut_requires_the_packaged_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpackaged run answers ``{ok: false, message}`` instead of raising."""
    import novel_downloader.apps.desktop.webview_app as wa

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    result = wa.ApiBridge().create_shortcut()
    assert result["ok"] is False
    assert result["message"] == t("Shortcuts require the packaged app")


# ---------------------------------------------------------------------------
# Settings page: preference contract, config writing and first-run recovery
# ---------------------------------------------------------------------------


def _isolate_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    """Point the preference and config files at ``tmp_path``."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs = tmp_path / "profile" / "desktop_prefs.json"
    config = tmp_path / "home" / "settings.toml"
    monkeypatch.setattr(wa, "_prefs_path", lambda: prefs)
    monkeypatch.setattr(wa, "_settings_config_path", lambda: config)
    return prefs, config


def _config_reader(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    """Make ``get_config_value`` read the isolated TOML file (or a default)."""
    import novel_downloader.infra.config as config_mod

    def read(keys: list[str], default: Any) -> Any:
        if len(keys) == 2 and config.is_file():
            import tomllib

            try:
                data = tomllib.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return default
            node = data.get(keys[0])
            if isinstance(node, dict) and keys[1] in node:
                return node[keys[1]]
        return default

    monkeypatch.setattr(config_mod, "get_config_value", read, raising=True)


def test_save_prefs_round_trips_every_initial_state_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``save_prefs`` writes exactly the switches ``get_initial_state`` publishes."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    bridge = wa.ApiBridge()
    switches = {
        "theme": "dark",
        "auto_check_updates": False,
        "auto_download_updates": False,
        "auto_install_on_exit": False,
    }

    result = bridge.save_prefs(switches)

    assert result["ok"] is True
    assert result["ignored"] == []
    state = bridge.get_initial_state()
    assert state["theme"] == "dark"
    assert state["theme_stored"] is True
    for key in (
        "auto_check_updates",
        "auto_download_updates",
        "auto_install_on_exit",
    ):
        assert state[key] is False
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert stored == switches


def test_save_prefs_ignores_unknown_and_internal_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page bug must not be able to inject arbitrary preference keys."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    result = wa.ApiBridge().save_prefs(
        {"theme": "dark", "evil": "payload", "last_update_check": "0"}
    )

    assert result["ok"] is True
    assert result["ignored"] == ["evil", "last_update_check"]
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"theme": "dark"}


def test_save_prefs_rejects_an_unknown_theme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    result = wa.ApiBridge().save_prefs({"theme": "purple"})

    assert result["ok"] is False
    assert result["errors"] == {"theme": t("Unknown theme")}
    assert not prefs.exists(), "a rejected value must not reach the file"


def test_save_prefs_reports_a_read_only_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The theme must not silently revert when the profile is unwritable."""
    import novel_downloader.apps.desktop.webview_app as wa

    blocker = tmp_path / "profile-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(wa, "_prefs_path", lambda: blocker / "prefs.json")

    result = wa.ApiBridge().save_prefs({"theme": "dark"})

    assert result["ok"] is False
    assert result["error"] == t("Could not save the settings")


def test_save_prefs_is_atomic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed replace leaves no truncated file and reports failure."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)

    def boom(_src: Any, _dst: Any) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(wa.os, "replace", boom)

    assert wa._save_prefs({"theme": "dark"}) is False
    assert not prefs.exists()


def test_patch_toml_text_preserves_comments_and_section_bounds() -> None:
    """Only the touched key lines change; neighbouring comments survive."""
    import novel_downloader.apps.desktop.webview_app as wa

    source = (
        "# top of the file\n"
        "[general]\n"
        'raw_data_dir = "./raw_data"  # keep me\n'
        'output_dir = "./downloads"  # books land here\n'
        "\n"
        "[general.output]\n"
        'formats = ["txt"]\n'
    )
    updated = wa._patch_toml_text(
        source, "general", {"output_dir": "D:/books", "proxy": ""}
    )

    import tomllib

    data = tomllib.loads(updated)
    assert data["general"]["output_dir"] == "D:/books"
    assert data["general"]["proxy"] == ""
    assert data["general"]["raw_data_dir"] == "./raw_data"
    assert "# keep me" in updated
    assert "# books land here" in updated
    # The new key belongs to [general], not to [general.output].
    assert updated.index('proxy = ""') < updated.index("[general.output]")


def test_update_config_section_round_trips_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing config is seeded from the sample and patched without losing it."""
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    target = tmp_path / "books"

    wa._update_config_section(
        "general", {"output_dir": str(target), "proxy": "http://127.0.0.1:7890"}
    )

    text = config.read_text(encoding="utf-8")
    assert "# 全局通用设置" in text, "the shipped sample's comments are kept"
    data = tomllib.loads(text)
    assert data["general"]["output_dir"] == str(target)
    assert data["general"]["proxy"] == "http://127.0.0.1:7890"
    assert data["general"]["output"]["formats"], "later sections are untouched"


def test_update_config_section_round_trips_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    config = config.with_suffix(".json")
    monkeypatch.setattr(wa, "_settings_config_path", lambda: config)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"general": {"output_dir": "./old"}, "other": 1}),
        encoding="utf-8",
    )

    wa._update_config_section("general", {"output_dir": "D:/new"})

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["output_dir"] == "D:/new"
    assert data["other"] == 1


def test_save_settings_creates_a_missing_output_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saving a new folder creates it and proves a write before storing it."""
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    target = tmp_path / "new" / "books"

    result = wa.ApiBridge().save_settings({"output_dir": str(target)})

    assert result["ok"] is True, result["errors"]
    assert target.is_dir(), "a missing output folder is created on save"
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["output_dir"] == str(target.resolve())
    assert result["settings"]["output_dir"]["state"] == "ready"


def test_save_settings_rejects_a_file_as_the_output_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    blocker = tmp_path / "book.txt"
    blocker.write_text("x", encoding="utf-8")

    result = wa.ApiBridge().save_settings({"output_dir": str(blocker)})

    assert result["ok"] is False
    assert result["errors"]["output_dir"] == t("That path is a file, not a folder")
    assert not config.exists(), "a failed validation writes nothing"


def test_save_settings_surfaces_a_config_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, _config = _isolate_settings(monkeypatch, tmp_path)

    def boom(_section: str, _values: Any) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(wa, "_update_config_section", boom)
    result = wa.ApiBridge().save_settings({"output_dir": str(tmp_path / "books")})

    assert result["ok"] is False
    assert "disk is full" in result["errors"]["output_dir"]
    assert result["message"] == t("Some settings were not saved")


def test_save_settings_validates_and_normalises_the_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    bridge = wa.ApiBridge()

    for bad, hint in (
        ("127.0.0.1", "port"),
        ("ftp://127.0.0.1:21", "scheme"),
        ("http://host:notaport", "port"),
    ):
        rejected = bridge.save_settings({"proxy": bad})
        assert rejected["ok"] is False, bad
        assert hint in rejected["errors"]["proxy"].lower(), bad
    assert not config.exists(), "invalid input must not create a config file"

    accepted = bridge.save_settings({"proxy": "127.0.0.1:7890"})
    assert accepted["ok"] is True, accepted["errors"]
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["proxy"] == "http://127.0.0.1:7890"


def test_get_settings_never_returns_the_proxy_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The page only learns that a password is stored, never its value."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_mod

    _isolate_settings(monkeypatch, tmp_path)
    values = {
        "proxy": "http://127.0.0.1:7890",
        "proxy_user": "alice",
        "proxy_pass": "s3cret",
    }
    monkeypatch.setattr(
        config_mod,
        "get_config_value",
        lambda keys, default: values.get(keys[-1], default),
        raising=True,
    )

    payload = wa.ApiBridge().get_settings()

    assert payload["proxy_has_password"] is True
    assert payload["proxy_user"] == "alice"
    assert "s3cret" not in json.dumps(payload)


def test_save_settings_switches_language_and_returns_the_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A language change applies at once and publishes the new catalogue."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.i18n as i18n

    _isolate_settings(monkeypatch, tmp_path)
    active = {"code": "zh_CN"}
    calls: list[str] = []

    def fake_set(lang: str) -> str:
        calls.append(lang)
        active["code"] = lang
        return lang

    monkeypatch.setattr(i18n, "get_language", lambda: active["code"])
    monkeypatch.setattr(i18n, "set_language", fake_set)

    result = wa.ApiBridge().save_settings({"language": "en"})

    assert result["ok"] is True, result["errors"]
    assert calls == ["en_US"]
    assert result["i18n"], "the page needs the new catalogue to re-render"
    assert result["settings"]["language"] == "en_US"


def test_save_settings_rejects_an_unknown_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _isolate_settings(monkeypatch, tmp_path)
    result = wa.ApiBridge().save_settings({"language": "fr"})

    assert result["ok"] is False
    assert result["errors"]["language"] == t("Unknown language")
    # An empty select (catalogue failed to load) means "unchanged", not invalid.
    assert wa.ApiBridge().save_settings({"language": ""})["ok"] is True


def test_initial_state_reports_an_output_path_that_is_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first-run warning needs a vocabulary for "this is not a folder"."""
    import novel_downloader.apps.desktop.webview_app as wa

    blocker = tmp_path / "book.txt"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(wa, "_load_prefs", lambda: {})
    monkeypatch.setattr(wa.ApiBridge, "get_data_dir", lambda _self: str(blocker))

    state = wa.ApiBridge().get_initial_state()

    assert state["output_dir"]["state"] == "not_a_dir"
    assert state["output_dir"]["path"] == str(blocker)


def test_settings_page_wires_every_switch_to_the_bridge() -> None:
    """The save handler must send all three update switches and the theme."""
    start = APP_HTML.index('$("#btn-set-save").onclick')
    section_rule = (
        "/* ================================================================ updates */"
    )
    end = APP_HTML.index(section_rule, start)
    handler = APP_HTML[start:end]
    for key in (
        "output_dir",
        "proxy",
        "proxy_user",
        "auto_check_updates",
        "auto_download_updates",
        "auto_install_on_exit",
        "theme",
        "language",
    ):
        assert key in handler, f"{key} is not sent by the settings page"
    assert "save_settings" in handler
    assert 'id="page-settings"' in APP_HTML
    assert 'id="btn-set-save"' in APP_HTML
    assert 'id="set-out-status"' in APP_HTML


def test_boot_warns_about_an_unusable_output_folder() -> None:
    """A blocked folder is a first-run dead end, not a silent download failure."""
    boot = _js_handler("function boot()")
    assert "state.output_dir" in boot
    assert "not_writable" in boot
    assert "The output folder isn't writable" in boot
    assert 'showPage("settings")' in boot


# ---------------------------------------------------------------------------
# Settings hardening: hostile paths, proxies and preference-file failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "\x00", "a\x00b", "~/books", "%TEMP%\\books", "relative/books", "C:books"],
)
def test_expand_output_dir_never_raises_on_unusable_text(raw: str) -> None:
    """Every string the JS bridge can deliver resolves to *some* Path."""
    import novel_downloader.apps.desktop.webview_app as wa

    expanded = wa._expand_output_dir(raw)
    assert isinstance(expanded, Path)


def test_expand_output_dir_anchors_home_and_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NDDL_BOOKS", str(tmp_path / "env-books"))

    assert wa._expand_output_dir("~/books") == (home / "books").resolve()
    assert wa._expand_output_dir("$NDDL_BOOKS") == (tmp_path / "env-books").resolve()
    assert (
        wa._expand_output_dir("sub/books/") == (Path.cwd() / "sub" / "books").resolve()
    )


def test_initial_state_survives_an_invalid_configured_output_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NUL byte in settings.toml must not crash the bootstrap payload."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_config_value",
        lambda keys, default: "\x00bad" if keys[-1] == "output_dir" else default,
        raising=True,
    )

    bridge = wa.ApiBridge()
    state = bridge.get_initial_state()

    assert isinstance(bridge.get_data_dir(), str)
    assert state["output_dir"]["state"] == "error"


def test_probe_directory_reports_permission_errors_as_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("access is denied")

    monkeypatch.setattr(wa.tempfile, "mkstemp", deny)

    problem = wa._probe_directory(tmp_path / "books")

    assert isinstance(problem, str)
    assert "access is denied" in problem


def test_save_settings_reports_a_null_byte_path_structurally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)

    result = wa.ApiBridge().save_settings({"output_dir": "books\x00nul"})

    assert result["ok"] is False
    assert set(result["errors"]) == {"output_dir"}
    assert not config.exists()


@pytest.mark.parametrize("raw", ["", "   "])
def test_save_settings_rejects_an_empty_output_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _isolate_settings(monkeypatch, tmp_path)
    result = wa.ApiBridge().save_settings({"output_dir": raw})

    assert result["ok"] is False
    assert result["errors"]["output_dir"] == t("Enter a folder path")


def test_save_settings_validates_every_field_before_creating_a_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejected proxy must not leave an empty folder behind."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    target = tmp_path / "never-created"

    result = wa.ApiBridge().save_settings(
        {"output_dir": str(target), "proxy": "ftp://127.0.0.1:21"}
    )

    assert result["ok"] is False
    assert "proxy" in result["errors"]
    assert not target.exists(), "validation must precede the directory probe"
    assert not config.exists()


def test_save_settings_anchors_a_relative_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    result = wa.ApiBridge().save_settings({"output_dir": "books"})

    assert result["ok"] is True, result["errors"]
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["output_dir"] == str((work / "books").resolve())


def test_save_settings_expands_a_home_relative_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    result = wa.ApiBridge().save_settings({"output_dir": "~/books"})

    assert result["ok"] is True, result["errors"]
    assert (home / "books").is_dir()
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["output_dir"] == str((home / "books").resolve())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("socks5://10.0.0.1:1080", "socks5://10.0.0.1:1080"),
        ("socks5h://10.0.0.1:1080", "socks5h://10.0.0.1:1080"),
        ("[::1]:7890", "http://[::1]:7890"),
        ("http://[::1]:7890", "http://[::1]:7890"),
        ("http://127.0.0.1:7890/", "http://127.0.0.1:7890/"),
    ],
)
def test_normalize_proxy_is_idempotent(raw: str, expected: str) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    once, error = wa._normalize_proxy(raw)
    assert error is None
    assert once == expected

    again, error_again = wa._normalize_proxy(once)
    assert (again, error_again) == (once, None)


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:0",
        "127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://:7890",
        "http://127.0.0.1:",
        "http://::1:7890",
        "socks://127.0.0.1:1080",
    ],
)
def test_normalize_proxy_rejects_unusable_endpoints(raw: str) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _normalized, error = wa._normalize_proxy(raw)
    assert error is not None


@pytest.mark.parametrize(
    "raw",
    [
        "http://alice:s3cret@127.0.0.1:7890",
        "alice:s3cret@127.0.0.1:7890",
        "http://alice@127.0.0.1:7890",
    ],
)
def test_save_settings_rejects_credentials_in_the_proxy_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    """Secrets belong in proxy_pass, not in a URL that is echoed back."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    _config_reader(monkeypatch, config)

    result = wa.ApiBridge().save_settings({"proxy": raw, "proxy_password": "s3cret"})

    assert result["ok"] is False
    assert result["errors"]["proxy"] == t(
        "Put the proxy username and password in the fields below, not in the address"
    )
    assert "s3cret" not in json.dumps(result)
    assert not config.exists()


def test_get_settings_hides_credentials_stored_in_the_proxy_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legacy ``user:pass@host`` value must not round-trip to the page."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_mod

    _isolate_settings(monkeypatch, tmp_path)
    values = {
        "proxy": "http://alice:s3cret@127.0.0.1:7890",
        "proxy_user": "",
        "proxy_pass": "",
    }
    monkeypatch.setattr(
        config_mod,
        "get_config_value",
        lambda keys, default: values.get(keys[-1], default),
        raising=True,
    )

    payload = wa.ApiBridge().get_settings()

    assert payload["proxy"] == "http://127.0.0.1:7890"
    assert payload["proxy_user"] == "alice"
    assert payload["proxy_has_password"] is True
    assert "s3cret" not in json.dumps(payload)


def test_get_settings_hides_credentials_in_a_malformed_proxy_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even a URL ``urlsplit`` rejects must not echo its password."""
    import novel_downloader.apps.desktop.webview_app as wa
    import novel_downloader.infra.config as config_mod

    _isolate_settings(monkeypatch, tmp_path)
    values = {
        "proxy": "http://alice:s3cret@[::1",
        "proxy_user": "",
        "proxy_pass": "",
    }
    monkeypatch.setattr(
        config_mod,
        "get_config_value",
        lambda keys, default: values.get(keys[-1], default),
        raising=True,
    )

    payload = wa.ApiBridge().get_settings()

    assert "s3cret" not in payload["proxy"]
    assert "s3cret" not in json.dumps(payload)


def test_save_settings_migrates_inline_proxy_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saving an untouched page must keep the stored secret usable."""
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "[general]\n"
        'output_dir = "./downloads"\n'
        'proxy = "http://alice:s3cret@127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    _config_reader(monkeypatch, config)
    bridge = wa.ApiBridge()
    shown = bridge.get_settings()

    result = bridge.save_settings(
        {"proxy": shown["proxy"], "proxy_user": shown["proxy_user"]}
    )

    assert result["ok"] is True, result["errors"]
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["general"]["proxy"] == "http://127.0.0.1:7890"
    assert data["general"]["proxy_user"] == "alice"
    assert data["general"]["proxy_pass"] == "s3cret"


def test_save_prefs_preserves_keys_written_by_other_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whitelist filters *incoming* keys; stored ones survive a save."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        json.dumps({"last_update_check": "1700000000", "theme": "light"}),
        encoding="utf-8",
    )

    result = wa.ApiBridge().save_prefs({"theme": "dark", "evil": "payload"})

    assert result["ok"] is True
    assert result["ignored"] == ["evil"]
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert stored == {"last_update_check": "1700000000", "theme": "dark"}


def test_save_prefs_rejects_a_non_object_payload() -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    result = wa.ApiBridge().save_prefs(["theme"])  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error"] == t("Expected on/off")
    assert result["ignored"] == []


def test_save_prefs_keeps_the_previous_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed replace must leave the old bytes and no temp litter."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        json.dumps({"theme": "light", "last_update_check": "1"}),
        encoding="utf-8",
    )
    before = prefs.read_bytes()

    def boom(_src: Any, _dst: Any) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(wa.os, "replace", boom)

    assert wa._save_prefs({"theme": "dark"}) is False
    assert prefs.read_bytes() == before
    assert [p for p in prefs.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_save_prefs_serialises_concurrent_writers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The read-merge-write cycle must not drop a concurrent key."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    failures: list[int] = []

    def writer(index: int) -> None:
        if wa._save_prefs({f"k{index}": index}) is False:
            failures.append(index)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert stored == {f"k{i}": i for i in range(12)}


def test_save_prefs_keeps_a_corrupt_file_as_a_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replacing an unreadable file must not silently destroy its bytes."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text("{not json", encoding="utf-8")

    assert wa._save_prefs({"theme": "dark"}) is True
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"theme": "dark"}
    backup = prefs.with_name(prefs.name + ".broken")
    assert backup.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize(
    ("suffix", "text"),
    [(".toml", "[general\noutput_dir = './x'\n"), (".json", "{ oops")],
)
def test_save_settings_does_not_clobber_a_corrupt_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str, text: str
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    config = config.with_suffix(suffix)
    monkeypatch.setattr(wa, "_settings_config_path", lambda: config)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")
    before = config.read_bytes()

    result = wa.ApiBridge().save_settings({"output_dir": str(tmp_path / "books")})

    assert result["ok"] is False
    assert "output_dir" in result["errors"]
    assert config.read_bytes() == before


def test_patch_toml_text_inserts_a_newline_after_a_bare_header() -> None:
    """A file ending at ``[general]`` must stay parseable after an insert."""
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    updated = wa._patch_toml_text("[general]", "general", {"proxy": ""})

    assert tomllib.loads(updated)["general"]["proxy"] == ""


def test_save_settings_reports_a_file_used_as_a_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``mkdir(parents=True)`` under a file must be reported, not raised."""
    import novel_downloader.apps.desktop.webview_app as wa

    _isolate_settings(monkeypatch, tmp_path)
    blocker = tmp_path / "book.txt"
    blocker.write_text("x", encoding="utf-8")

    result = wa.ApiBridge().save_settings({"output_dir": str(blocker / "books")})

    assert result["ok"] is False
    assert "output_dir" in result["errors"]
    assert blocker.is_file(), "validation must not remove the blocking file"


def test_save_settings_never_logs_the_proxy_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing write must not spill the secret into the log or the reply."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[general\n", encoding="utf-8")
    _config_reader(monkeypatch, config)
    caplog.set_level("DEBUG")

    result = wa.ApiBridge().save_settings(
        {"proxy": "127.0.0.1:7890", "proxy_password": "s3cret"}
    )

    assert result["ok"] is False
    assert "s3cret" not in caplog.text
    assert "s3cret" not in json.dumps(result)


def test_update_config_section_serialises_concurrent_writers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two section patches must merge instead of replacing one another."""
    import tomllib

    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    expected = {f"key{index}": f"value{index}" for index in range(8)}

    def writer(key: str, value: str) -> None:
        wa._update_config_section("general", {key: value})

    threads = [
        threading.Thread(target=writer, args=(key, value))
        for key, value in expected.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    general = tomllib.loads(config.read_text(encoding="utf-8"))["general"]
    assert {key: general.get(key) for key in expected} == expected


# ---------------------------------------------------------------------------
# Cross-process settings locks
# ---------------------------------------------------------------------------

#: Workers are module-level so the Windows ``spawn`` start method can pickle
#: them by qualified name.  They patch the path lookup in their own process
#: (monkeypatch state is not inherited across ``spawn``).


def _mp_save_prefs_worker(
    prefs_path: str, keys: list[str], start: Any, errors: Any
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    target = Path(prefs_path)
    wa._prefs_path = lambda: target  # type: ignore[assignment]
    try:
        if not start.wait(60):
            errors.put("start event timed out")
            return
        for key in keys:
            if not wa._save_prefs({key: True}):
                errors.put(f"save failed for {key}")
                return
    except Exception as exc:  # noqa: BLE001 - reported through the queue
        errors.put(f"{type(exc).__name__}: {exc}")


def _mp_update_config_worker(
    config_path: str, items: list[tuple[str, str]], start: Any, errors: Any
) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    target = Path(config_path)
    wa._settings_config_path = lambda: target  # type: ignore[assignment]
    try:
        if not start.wait(60):
            errors.put("start event timed out")
            return
        for key, value in items:
            wa._update_config_section("general", {key: value})
    except Exception as exc:  # noqa: BLE001 - reported through the queue
        errors.put(f"{type(exc).__name__}: {exc}")


def _drain_errors(errors: Any) -> list[str]:
    """Return every message a worker posted (non-blocking)."""
    import queue

    messages: list[str] = []
    while True:
        try:
            messages.append(str(errors.get_nowait()))
        except queue.Empty:
            return messages


def _run_workers(procs: list[Any], start: Any, errors: Any) -> list[str]:
    """Start ``procs`` together, wait for them, and return worker errors."""
    try:
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(120)
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
    assert all(proc.exitcode == 0 for proc in procs), [proc.exitcode for proc in procs]
    return _drain_errors(errors)


def test_prefs_saves_from_two_processes_do_not_lose_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Instance A's theme and instance B's switch must both survive."""
    import multiprocessing

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    errors = context.Queue()
    keys_a = [f"proc_a_{index}" for index in range(20)]
    keys_b = [f"proc_b_{index}" for index in range(20)]
    procs = [
        context.Process(
            target=_mp_save_prefs_worker, args=(str(prefs), keys_a, start, errors)
        ),
        context.Process(
            target=_mp_save_prefs_worker, args=(str(prefs), keys_b, start, errors)
        ),
    ]

    assert _run_workers(procs, start, errors) == []

    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert all(stored.get(key) is True for key in keys_a + keys_b)


def test_config_writes_from_two_processes_do_not_lose_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two simultaneous ``[general]`` patches must merge, not replace."""
    import multiprocessing

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    errors = context.Queue()
    items_a = [(f"proc_a_{index}", f"value_a_{index}") for index in range(15)]
    items_b = [(f"proc_b_{index}", f"value_b_{index}") for index in range(15)]
    procs = [
        context.Process(
            target=_mp_update_config_worker, args=(str(config), items_a, start, errors)
        ),
        context.Process(
            target=_mp_update_config_worker, args=(str(config), items_b, start, errors)
        ),
    ]

    assert _run_workers(procs, start, errors) == []

    import tomllib

    general = tomllib.loads(config.read_text(encoding="utf-8"))["general"]
    expected = dict(items_a + items_b)
    assert {key: general.get(key) for key in expected} == expected


def test_save_prefs_times_out_without_touching_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A held lock must fail fast and leave the previous preferences intact."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('{"theme": "light"}', encoding="utf-8")
    monkeypatch.setattr(wa, "_LOCK_TIMEOUT", 0.05)

    with wa._cross_process_lock(prefs, timeout=10):
        assert wa._save_prefs({"theme": "dark"}) is False

    assert json.loads(prefs.read_text(encoding="utf-8")) == {"theme": "light"}


def test_update_config_section_times_out_without_truncating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out config patch raises instead of claiming success."""
    import novel_downloader.apps.desktop.webview_app as wa

    _prefs, config = _isolate_settings(monkeypatch, tmp_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    original = '[general]\noutput_dir = "./old"\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(wa, "_LOCK_TIMEOUT", 0.05)

    with (
        wa._cross_process_lock(config, timeout=10),
        pytest.raises(wa._LockTimeoutError),
    ):
        wa._update_config_section("general", {"output_dir": "D:/new"})

    assert config.read_text(encoding="utf-8") == original


def test_settings_lock_files_live_next_to_the_data_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lock files stay in the profile folder, never in the working directory."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, config = _isolate_settings(monkeypatch, tmp_path)
    assert wa._save_prefs({"theme": "dark"}) is True
    wa._update_config_section("general", {"output_dir": "D:/books"})

    prefs_lock = wa._lock_path(prefs)
    config_lock = wa._lock_path(config)
    for lock, target in ((prefs_lock, prefs), (config_lock, config)):
        assert lock.is_file()
        assert lock.parent == target.parent
        assert lock.name == target.name + ".lock"
    assert not any(tmp_path.glob("*.lock"))

    if os.name == "posix":
        for lock in (prefs_lock, config_lock):
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_corrupt_lock_file_is_reusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Garbage or a directory at the lock path must not break saving."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    lock = wa._lock_path(prefs)
    lock.write_bytes(b"\x00not a lock\xff")

    assert wa._save_prefs({"theme": "dark"}) is True
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"theme": "dark"}

    # A directory where the lock file belongs is also survivable: the helper
    # degrades to the in-process lock instead of failing the save.
    lock.unlink()
    lock.mkdir()
    assert wa._save_prefs({"auto_check_updates": False}) is True
    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert stored["auto_check_updates"] is False


def test_bridge_saves_report_a_lock_timeout_structurally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both bridge entry points answer ``{ok: false}`` instead of blocking."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, config = _isolate_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(wa, "_LOCK_TIMEOUT", 0.05)
    bridge = wa.ApiBridge()

    with wa._cross_process_lock(prefs, timeout=10):
        prefs_result = bridge.save_prefs({"theme": "dark"})

    _config_reader(monkeypatch, config)
    with wa._cross_process_lock(config, timeout=10):
        settings_result = bridge.save_settings({"output_dir": str(tmp_path / "books")})

    assert prefs_result["ok"] is False
    assert prefs_result["error"] == t("Could not save the settings")
    assert settings_result["ok"] is False
    assert "output_dir" in settings_result["errors"]
    assert not config.exists(), "the timed-out save must not create the file"


@pytest.mark.skipif(os.name != "posix", reason="symlinks and 0600 are POSIX-only")
def test_lock_file_is_never_opened_through_a_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A planted symlink is replaced, not followed (the target stays intact)."""
    import novel_downloader.apps.desktop.webview_app as wa

    prefs, _config = _isolate_settings(monkeypatch, tmp_path)
    prefs.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    lock = wa._lock_path(prefs)
    lock.symlink_to(victim)

    assert wa._save_prefs({"theme": "dark"}) is True
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert lock.is_file() and not lock.is_symlink()
