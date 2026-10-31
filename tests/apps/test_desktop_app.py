#!/usr/bin/env python3
"""
tests.apps.test_desktop_app
---------------------------

Offline tests for the pywebview desktop client.

The bulk of the suite covers :mod:`novel_downloader.apps.desktop.formatting`,
:mod:`novel_downloader.apps.desktop.preferences`,
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
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common.history_files import FileItem
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.watchlist import WatchEntry
from novel_downloader.apps.desktop import formatting as fmt
from novel_downloader.apps.desktop import main as desktop_main
from novel_downloader.apps.desktop import notifications as notif
from novel_downloader.apps.desktop import preferences as prefs
from novel_downloader.apps.desktop import window_state as wstate
from novel_downloader.apps.desktop.engine import AsyncEngine
from novel_downloader.apps.desktop.web_assets import APP_HTML
from novel_downloader.apps.desktop.webview_app import (
    ApiBridge,
    _format_health_ms,
    _format_size,
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
@pytest.mark.parametrize(
    ("done", "total", "expected"),
    [
        (3, 10, "3/10 (30%)"),
        (1, 3, "1/3 (33%)"),
        (2, 3, "2/3 (67%)"),
        (10, 10, "10/10 (100%)"),
        (0, 10, "0/10 (0%)"),
        (1, 1000, "1/1000 (0%)"),
        # Unknown total renders as a question mark.
        (0, 0, "0/?"),
        (5, 0, "5/?"),
        # Negative values are clamped so the UI never shows a negative count.
        (-5, -1, "0/?"),
        (-3, 10, "0/10 (0%)"),
    ],
)
def test_format_task_progress(done: int, total: int, expected: str) -> None:
    assert fmt.format_task_progress(done, total) == expected


def test_task_row_renders_all_columns() -> None:
    task = DownloadTask(title="My Book", site="qidian", book_id="7")
    task.status = Status.RUNNING
    task.chapters_done = 3
    task.chapters_total = 12
    assert fmt.task_row(task) == ("Running", "My Book", "qidian", "3/12 (25%)", "")


def test_task_row_includes_error_text() -> None:
    task = DownloadTask(title="Broken", site="b520", book_id="1")
    task.status = Status.FAILED
    task.error = "network down"
    status, title, site, progress, error = fmt.task_row(task)
    assert (status, title, site, error) == ("Failed", "Broken", "b520", "network down")
    assert progress == "0/?"


def test_search_row_uses_expected_keys() -> None:
    result = _search_result(
        title="Novel",
        author="Someone",
        site="b520",
        word_count="909",
        update_date="2024-05-06",
    )
    assert fmt.search_row(result) == ("Novel", "Someone", "b520", "909", "2024-05-06")


def test_search_row_tolerates_missing_keys() -> None:
    assert fmt.search_row({}) == ("", "", "", "", "")  # type: ignore[arg-type]


def test_history_row_formats_size_and_time() -> None:
    item = FileItem(
        path=Path("downloads/a.txt"),
        name="a.txt",
        ext="txt",
        size=1536,
        mtime=1700000000.0,
    )
    name, size, modified = fmt.history_row(item)
    assert name == "a.txt"
    assert size == "1 KB"
    assert modified == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1700000000.0))


# ---------------------------------------------------------------------------
# Size / time / duration formatting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1536, "1 KB"),
        (1048575, "1023 KB"),
        (1048576, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (5 * 1024**3, "5.0 GB"),
        (1024**4, "1024.0 GB"),
    ],
)
def test_format_size(size: int, expected: str) -> None:
    assert fmt.format_size(size) == expected


def test_format_mtime_uses_local_wall_clock() -> None:
    stamp = 1700000000.0
    rendered = fmt.format_mtime(stamp)
    assert rendered == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    assert len(rendered) == 19 and rendered[4] == "-" and rendered[13] == ":"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "-"),
        (-5, "-"),
        (-1, "-"),
        (1, "0:01"),
        (59, "0:59"),
        (60, "1:00"),
        (61, "1:01"),
        (599, "9:59"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (86399, "23:59:59"),
        (360000, "100:00:00"),
    ],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert fmt.format_duration(seconds) == expected


def test_music_row_renders_track_fields() -> None:
    assert fmt.music_row(_track()) == ("Song", "Singer", "Album", "3:35")


def test_music_row_tolerates_missing_fields() -> None:
    assert fmt.music_row(SimpleNamespace()) == ("", "", "", "-")
    # Zero/absent durations render as unknown, and ``None`` artists stay empty.
    track = _track(duration_s=0, artists=None, album=None, name=None)
    assert fmt.music_row(track) == ("", "", "", "-")


# ---------------------------------------------------------------------------
# Music platform health check: pure formatting / summary / ordering
# ---------------------------------------------------------------------------
def test_truncate_error_collapses_whitespace_and_keeps_short_text() -> None:
    assert (
        fmt.truncate_error("  HTTP 500:\n upstream \t refused ")
        == "HTTP 500: upstream refused"
    )
    assert fmt.truncate_error("") == ""
    assert fmt.truncate_error(None) == ""


def test_truncate_error_cuts_long_text_with_ellipsis() -> None:
    out = fmt.truncate_error("x" * 200, limit=80)
    assert len(out) == 80
    assert out.endswith("…")
    assert out.startswith("x" * 79)
    # Short messages are never cut.
    assert fmt.truncate_error("short", limit=80) == "short"


def test_sort_health_results_puts_ok_first_then_fastest() -> None:
    results = [
        ("slow-ok", {"ok": True, "elapsed_ms": 900}),
        ("failed", {"ok": False, "elapsed_ms": 50}),
        ("fast-ok", {"ok": True, "elapsed_ms": 120}),
    ]
    assert [platform for platform, _ in fmt.sort_health_results(results)] == [
        "fast-ok",
        "slow-ok",
        "failed",
    ]


def test_sort_health_results_tolerates_missing_fields() -> None:
    # A missing elapsed_ms counts as 0 and missing ok counts as failed;
    # ties keep alphabetical platform order.
    results = [("b", {"ok": True}), ("a", {"ok": False, "elapsed_ms": 10})]
    assert [platform for platform, _ in fmt.sort_health_results(results)] == [
        "b",
        "a",
    ]


def test_health_rows_format_and_sort() -> None:
    rows = fmt.health_rows(
        {
            "netease": {"ok": True, "elapsed_ms": 1234, "error": ""},
            "kugou": {"ok": False, "elapsed_ms": 15, "error": "a\nb"},
        }
    )
    assert rows == [
        ("netease", t("OK"), "1234", ""),
        ("kugou", t("Failed"), "15", "a b"),
    ]


def test_health_rows_empty_results() -> None:
    assert fmt.health_rows({}) == []
    assert fmt.health_rows([]) == []
    assert fmt.health_summary({}) == (
        t("Health check finished: {ok} ok, {failed} failed.").format(ok=0, failed=0)
    )


def test_health_summary_all_failed_and_error_truncation() -> None:
    results = {
        "a": {"ok": False, "elapsed_ms": 1, "error": "x" * 300},
        "b": {"ok": False, "elapsed_ms": 2, "error": "boom"},
    }
    rows = fmt.health_rows(results)
    assert all(row[1] == t("Failed") for row in rows)
    # The overlong error is truncated for the table cell only.
    assert len(rows[0][3]) == 80
    assert rows[1][3] == "boom"
    assert fmt.health_summary(results) == (
        t("Health check finished: {ok} ok, {failed} failed.").format(ok=0, failed=2)
    )


def test_health_summary_counts_mixed_results() -> None:
    results = {
        "ok-fast": {"ok": True, "elapsed_ms": 10, "error": ""},
        "ok-slow": {"ok": True, "elapsed_ms": 400, "error": ""},
        "bad": {"ok": False, "elapsed_ms": 5, "error": "down"},
    }
    assert fmt.health_summary(results) == (
        t("Health check finished: {ok} ok, {failed} failed.").format(ok=2, failed=1)
    )
    assert fmt.health_counts(results) == (2, 1)


# ---------------------------------------------------------------------------
# Site parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("qidian, b520", ["qidian", "b520"]),
        ("qidian", ["qidian"]),
        ("qidian b520", ["qidian", "b520"]),
        ("qidian,b520,syosetu", ["qidian", "b520", "syosetu"]),
        ("  qidian ,  b520  ", ["qidian", "b520"]),
        ("qidian\nb520\takatsuki_novels", ["qidian", "b520", "akatsuki_novels"]),
        # Case is folded so mixed-case input still matches the registry keys.
        ("Qidian", ["qidian"]),
        ("QIDIAN, B520", ["qidian", "b520"]),
        ("Qidian,b520", ["qidian", "b520"]),
        # Differently-cased duplicates collapse into one entry.
        ("a,a,A", ["a"]),
        ("qidian,Qidian,QIDIAN", ["qidian"]),
        # CJK separators are supported.
        ("qidian，b520", ["qidian", "b520"]),
        ("qidian；b520", ["qidian", "b520"]),
        ("qidian,,b520", ["qidian", "b520"]),
        ("qidian，；b520", ["qidian", "b520"]),
    ],
)
def test_parse_sites(text: str, expected: list[str]) -> None:
    assert fmt.parse_sites(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "\t\n", ",", "，，", " ; "])
def test_parse_sites_empty_input_returns_none(text: str) -> None:
    assert fmt.parse_sites(text) is None


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


def test_music_settings_defaults_fill_missing_values(tmp_path: Path) -> None:
    settings = prefs.music_settings_from_mapping(None, tmp_path)
    assert settings.out_dir == str(tmp_path)
    assert settings.level == prefs.DEFAULT_MUSIC_LEVEL
    assert settings.embed_tags is True
    assert settings.embed_cover is True
    assert settings.save_lyric is True
    assert settings.split_folder is True
    assert settings.skip_existing is True


def test_music_settings_blank_output_dir_uses_default(tmp_path: Path) -> None:
    settings = prefs.music_settings_from_mapping({"out_dir": "   "}, tmp_path)
    assert settings.out_dir == str(tmp_path)


def test_music_settings_keep_explicit_output_dir(tmp_path: Path) -> None:
    settings = prefs.music_settings_from_mapping(
        {"out_dir": "  D:/Music/Novel  "}, tmp_path
    )
    assert settings.out_dir == "D:/Music/Novel"


@pytest.mark.parametrize("level", ["hires", "lossless", "exhigh", "higher", "standard"])
def test_music_settings_accept_supported_levels(level: str, tmp_path: Path) -> None:
    settings = prefs.music_settings_from_mapping({"level": level}, tmp_path)
    assert settings.level == level


@pytest.mark.parametrize("level", ["", "   ", "bogus", "unknown"])
def test_music_settings_normalize_unknown_levels(level: str, tmp_path: Path) -> None:
    settings = prefs.music_settings_from_mapping({"level": level}, tmp_path)
    assert settings.level == prefs.DEFAULT_MUSIC_LEVEL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (False, False),
        (True, True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("true", True),
        ("yes", True),
        ("1", True),
        ("", True),
        (None, True),
        (0, False),
        (1, True),
    ],
)
def test_music_settings_coerce_boolean_flags(
    raw: object, expected: bool, tmp_path: Path
) -> None:
    settings = prefs.music_settings_from_mapping(
        {"embed_cover": raw, "save_lyric": raw}, tmp_path
    )
    assert settings.embed_cover is expected
    assert settings.save_lyric is expected


def test_music_settings_round_trip_mapping(tmp_path: Path) -> None:
    original = prefs.music_settings_from_mapping(
        {
            "out_dir": str(tmp_path / "music"),
            "level": "lossless",
            "embed_tags": False,
            "embed_cover": False,
            "save_lyric": False,
            "split_folder": False,
            "skip_existing": False,
        },
        tmp_path,
    )
    mapped = prefs.music_settings_to_mapping(original)
    restored = prefs.music_settings_from_mapping(mapped, tmp_path)
    assert restored == original
    # Only the keys the desktop UI owns are written back.
    assert set(mapped) == {
        "out_dir",
        "level",
        "embed_tags",
        "embed_cover",
        "save_lyric",
        "split_folder",
        "skip_existing",
    }


def test_level_display_and_code_round_trip() -> None:
    for level in prefs.MUSIC_LEVELS:
        assert prefs.level_code(prefs.level_display(level)) == level
        # Raw codes are accepted too (e.g. pasted from the CLI).
        assert prefs.level_code(level) == level


def test_level_code_falls_back_to_default() -> None:
    assert prefs.level_code("unknown") == prefs.DEFAULT_MUSIC_LEVEL
    assert prefs.level_code("") == prefs.DEFAULT_MUSIC_LEVEL


def test_resolve_output_dir_expands_user_home() -> None:
    resolved = prefs.resolve_output_dir("~", "/fallback")
    assert resolved != "~"
    assert resolved.startswith(os.path.expanduser("~"))


def test_resolve_output_dir_expands_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESKTOP_TEST_DIR", str(tmp_path))
    resolved = prefs.resolve_output_dir(
        "${DESKTOP_TEST_DIR}/out" if os.name != "nt" else "%DESKTOP_TEST_DIR%/out",
        "/fallback",
    )
    assert Path(resolved) == tmp_path / "out"


def test_environment_summary_includes_api_and_data_dir(tmp_path: Path) -> None:
    summary = prefs.environment_summary("http://127.0.0.1:8081", tmp_path)
    assert "http://127.0.0.1:8081" in summary
    assert str(tmp_path) in summary


def test_remember_search_trims_both_fields() -> None:
    state = prefs.remember_search("  dune  ", "  qidian, b520  ")
    assert state.keyword == "dune"
    assert state.sites == "qidian, b520"
    assert state.has_previous is True


@pytest.mark.parametrize("keyword", ["", "   ", "\t"])
def test_remember_search_rejects_blank_keywords(keyword: str) -> None:
    assert prefs.remember_search(keyword, "qidian").has_previous is False


# ---------------------------------------------------------------------------
# Task output actions / detail text
# ---------------------------------------------------------------------------
def test_task_export_paths_prefers_epub_then_txt() -> None:
    task = _task(
        Status.COMPLETED,
        {
            "txt": Path("downloads/book.txt"),
            "epub": Path("downloads/book.epub"),
            "pdf": Path("downloads/book.pdf"),
        },
    )
    assert fmt.task_export_paths(task) == (
        Path("downloads/book.epub"),
        Path("downloads/book.txt"),
        Path("downloads/book.pdf"),
    )


def test_task_export_paths_deduplicates_and_skips_none() -> None:
    task = _task(Status.COMPLETED)
    task.exported_paths = {
        "epub": Path("downloads/book.epub"),
        "txt": Path("downloads/book.epub"),
        "pdf": None,
    }
    assert fmt.task_export_paths(task) == (Path("downloads/book.epub"),)


@pytest.mark.parametrize("status", [Status.COMPLETED, Status.FAILED])
def test_task_output_actions_enabled_for_finished_tasks(status: Status) -> None:
    task = _task(status, {"epub": Path("downloads/book.epub")})
    actions = fmt.task_output_actions(task)
    assert actions.can_open_file is True
    assert actions.can_open_folder is True
    assert actions.file_path == Path("downloads/book.epub")
    assert actions.folder_path == Path("downloads")


@pytest.mark.parametrize(
    "status",
    [Status.QUEUED, Status.RUNNING, Status.PROCESSING, Status.EXPORTING],
)
def test_task_output_actions_disabled_before_completion(status: Status) -> None:
    task = _task(status, {"epub": Path("downloads/book.epub")})
    actions = fmt.task_output_actions(task)
    assert actions.can_open_file is False
    assert actions.can_open_folder is False
    assert actions.file_path is None
    assert actions.folder_path is None


def test_task_output_actions_disabled_without_exported_paths() -> None:
    task = _task(Status.COMPLETED)
    actions = fmt.task_output_actions(task)
    assert actions.can_open_file is False
    assert actions.can_open_folder is False


def test_task_output_actions_handle_missing_selection() -> None:
    actions = fmt.task_output_actions(None)
    assert actions.can_open_file is False
    assert actions.can_open_folder is False


def test_task_output_actions_handle_duck_typed_task() -> None:
    task = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        exported_paths={"txt": Path("downloads/other.txt")},
    )
    actions = fmt.task_output_actions(task)
    assert actions.file_path == Path("downloads/other.txt")
    assert actions.folder_path == Path("downloads")


def test_task_detail_text_includes_full_error() -> None:
    long_error = " | ".join(["HTTP 500: upstream refused the request"] * 8)
    task = _task(Status.FAILED, error=long_error)
    detail = fmt.task_detail_text(task)
    assert long_error in detail
    assert t("Error") in detail
    assert t("Status") in detail
    assert t("Failed") in detail


def test_task_detail_text_lists_exported_files() -> None:
    task = _task(Status.COMPLETED, {"epub": Path("downloads/a.epub")})
    detail = fmt.task_detail_text(task)
    assert t("Exported files") in detail
    assert "downloads" in detail


def test_task_detail_text_for_no_selection() -> None:
    assert fmt.task_detail_text(None) == t("Select a task to view its details.")


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
def test_task_export_choices_prefer_epub_then_txt_with_labels() -> None:
    task = _task(
        Status.COMPLETED,
        {
            "txt": Path("downloads/book.txt"),
            "epub": Path("downloads/book.epub"),
            "pdf": Path("downloads/book.pdf"),
        },
    )
    choices = fmt.task_export_choices(task)
    assert [choice.key for choice in choices] == ["epub", "txt", "pdf"]
    assert [choice.path for choice in choices] == [
        Path("downloads/book.epub"),
        Path("downloads/book.txt"),
        Path("downloads/book.pdf"),
    ]
    assert choices[0].label == "EPUB: book.epub"


def test_task_export_choices_deduplicate_keeping_the_first_key() -> None:
    task = _task(Status.COMPLETED)
    task.exported_paths = {
        "epub": Path("downloads/book.epub"),
        "txt": Path("downloads/book.epub"),
        "pdf": None,
    }
    choices = fmt.task_export_choices(task)
    assert len(choices) == 1
    assert choices[0].key == "epub"
    assert choices[0].path == Path("downloads/book.epub")
    assert fmt.task_export_paths(task) == (Path("downloads/book.epub"),)


def test_task_export_choices_empty_without_exports() -> None:
    assert fmt.task_export_choices(_task(Status.COMPLETED)) == ()
    assert fmt.task_export_choices(None) == ()


# ---------------------------------------------------------------------------
# Context-menu availability
# ---------------------------------------------------------------------------
def test_task_menu_state_for_completed_task_with_multiple_files() -> None:
    task = _task(
        Status.COMPLETED,
        {
            "epub": Path("downloads/book.epub"),
            "txt": Path("downloads/book.txt"),
        },
    )
    state = fmt.task_menu_state(task)
    assert state.can_open_file is True
    assert state.can_open_folder is True
    assert state.can_copy_path is True
    assert state.can_show_details is True
    assert state.can_retry is False
    assert state.can_copy_error is False
    assert [choice.key for choice in state.choices] == ["epub", "txt"]


def test_task_menu_state_offers_retry_and_error_copy_for_failed_task() -> None:
    task = _task(Status.FAILED, error=" network down ")
    state = fmt.task_menu_state(task)
    assert state.can_retry is True
    assert state.can_copy_error is True
    assert state.can_show_details is True
    assert state.can_copy_path is False


def test_task_menu_state_respects_retry_blocked() -> None:
    task = _task(Status.FAILED, error="boom")
    assert fmt.task_menu_state(task).can_retry is True
    assert fmt.task_menu_state(task, retry_blocked=True).can_retry is False


def test_task_menu_state_for_cancelled_task_is_retryable() -> None:
    assert fmt.task_menu_state(_task(Status.CANCELLED)).can_retry is True


def test_task_menu_state_for_running_task_disables_output_actions() -> None:
    task = _task(Status.RUNNING, {"epub": Path("downloads/book.epub")})
    state = fmt.task_menu_state(task)
    assert state.can_open_file is False
    assert state.can_open_folder is False
    assert state.can_copy_path is True  # the path can still be copied
    assert state.can_retry is False


def test_task_menu_state_for_no_selection() -> None:
    state = fmt.task_menu_state(None)
    assert state == fmt.TaskMenuState(
        can_retry=False,
        can_open_file=False,
        can_open_folder=False,
        can_copy_path=False,
        can_copy_error=False,
        can_show_details=False,
        choices=(),
    )


# ---------------------------------------------------------------------------
# Status / keyword filtering
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.QUEUED, fmt.FILTER_ACTIVE),
        (Status.RUNNING, fmt.FILTER_ACTIVE),
        (Status.PROCESSING, fmt.FILTER_ACTIVE),
        (Status.EXPORTING, fmt.FILTER_ACTIVE),
        (Status.COMPLETED, fmt.FILTER_COMPLETED),
        (Status.FAILED, fmt.FILTER_FAILED),
        # Cancelled tasks stay reachable from the failed filter.
        (Status.CANCELLED, fmt.FILTER_FAILED),
        ("running", fmt.FILTER_ACTIVE),
        ("unknown", "other"),
        (None, "other"),
    ],
)
def test_task_status_group(status: object, expected: str) -> None:
    assert fmt.task_status_group(status) == expected


@pytest.mark.parametrize(
    ("status", "filter_code", "expected"),
    [
        (Status.RUNNING, fmt.FILTER_ALL, True),
        (Status.RUNNING, fmt.FILTER_ACTIVE, True),
        (Status.RUNNING, fmt.FILTER_COMPLETED, False),
        (Status.RUNNING, fmt.FILTER_FAILED, False),
        (Status.COMPLETED, fmt.FILTER_COMPLETED, True),
        (Status.COMPLETED, fmt.FILTER_ACTIVE, False),
        (Status.FAILED, fmt.FILTER_FAILED, True),
        (Status.CANCELLED, fmt.FILTER_FAILED, True),
        (Status.CANCELLED, fmt.FILTER_COMPLETED, False),
    ],
)
def test_task_matches_filter_by_status(
    status: Status, filter_code: str, expected: bool
) -> None:
    task = _task(status)
    assert fmt.task_matches_filter(task, status=filter_code) is expected


def test_task_matches_filter_by_title_is_case_insensitive() -> None:
    task = DownloadTask(title="The Great Novel", site="qidian", book_id="1")
    assert fmt.task_matches_filter(task, keyword="great") is True
    assert fmt.task_matches_filter(task, keyword="  GREAT  ") is True
    assert fmt.task_matches_filter(task, keyword="novel") is True
    assert fmt.task_matches_filter(task, keyword="") is True
    assert fmt.task_matches_filter(task, keyword="   ") is True
    # Only the title is searched, not the site or book id.
    assert fmt.task_matches_filter(task, keyword="qidian") is False
    assert fmt.task_matches_filter(task, keyword="missing") is False


def test_filter_tasks_combines_filters_and_preserves_order() -> None:
    running = DownloadTask(title="Alpha", site="qidian", book_id="1")
    running.status = Status.RUNNING
    failed_match = DownloadTask(title="Alpha Failure", site="qidian", book_id="2")
    failed_match.status = Status.FAILED
    failed_other = DownloadTask(title="Beta Failure", site="qidian", book_id="3")
    failed_other.status = Status.CANCELLED
    completed = DownloadTask(title="Alpha Done", site="qidian", book_id="4")
    completed.status = Status.COMPLETED
    tasks = [running, failed_match, failed_other, completed]

    assert fmt.filter_tasks(tasks, status=fmt.FILTER_FAILED, keyword="alpha") == [
        failed_match
    ]
    assert fmt.filter_tasks(tasks, keyword="ALPHA") == [
        running,
        failed_match,
        completed,
    ]
    assert fmt.filter_tasks(tasks, status=fmt.FILTER_ACTIVE) == [running]
    assert fmt.filter_tasks(tasks, status=fmt.FILTER_ALL) == tasks


def test_task_filter_labels_and_codes_round_trip() -> None:
    for code in fmt.TASK_FILTER_CODES:
        label = fmt.task_filter_label(code)
        assert label
        assert fmt.task_filter_code(label) == code
        assert fmt.task_filter_code(code) == code
        assert fmt.task_filter_code(code.upper()) == code
    assert fmt.task_filter_code("bogus") == fmt.FILTER_ALL
    assert fmt.task_filter_code(None) == fmt.FILTER_ALL


def test_task_filter_labels_use_translations() -> None:
    assert fmt.task_filter_label(fmt.FILTER_ALL) == t("All")
    assert fmt.task_filter_label(fmt.FILTER_ACTIVE) == t("Active")
    assert fmt.task_filter_label(fmt.FILTER_COMPLETED) == t("Completed")
    assert fmt.task_filter_label(fmt.FILTER_FAILED) == t("Failed")


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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.QUEUED, False),
        (Status.RUNNING, False),
        (Status.PROCESSING, False),
        (Status.EXPORTING, False),
        (Status.COMPLETED, True),
        (Status.CANCELLED, True),
        (Status.FAILED, True),
        # Raw strings are accepted, with trimming and case folding.
        ("completed", True),
        ("  FAILED  ", True),
        ("Cancelled", True),
        ("running", False),
        ("bogus", False),
        (None, False),
    ],
)
def test_is_terminal_status(status: object, expected: bool) -> None:
    assert notif.is_terminal_status(status) is expected


def test_first_observation_is_only_a_baseline() -> None:
    """Tasks seen for the first time never notify (their history is unknown)."""
    tasks = [
        _status_task(Status.COMPLETED, task_id="done"),
        _status_task(Status.FAILED, task_id="broken", error="boom"),
    ]
    assert notif.detect_task_notifications({}, tasks) == []


def test_running_to_completed_produces_completed_event() -> None:
    task = _status_task(Status.COMPLETED)
    events = notif.detect_task_notifications({task.task_id: "running"}, [task])
    assert events == [
        notif.TaskNotification(
            task_id="task-1",
            title="Alpha",
            outcome=notif.TaskOutcome.COMPLETED,
            error="",
        )
    ]


def test_running_to_failed_produces_failed_event_with_error() -> None:
    task = _status_task(Status.FAILED, task_id="task-2", error="HTTPError: 500")
    events = notif.detect_task_notifications({"task-2": "running"}, [task])
    assert events == [
        notif.TaskNotification(
            task_id="task-2",
            title="Alpha",
            outcome=notif.TaskOutcome.FAILED,
            error="HTTPError: 500",
        )
    ]


def test_cancellation_is_terminal_but_never_notifies() -> None:
    task = _status_task(Status.CANCELLED, error="Cancelled by user")
    assert notif.detect_task_notifications({"task-1": "running"}, [task]) == []
    # ...and a cancelled task is terminal, so a later poll cannot notify either.
    assert notif.detect_task_notifications({"task-1": "cancelled"}, [task]) == []


@pytest.mark.parametrize("before", ["completed", "failed", "cancelled"])
def test_terminal_to_terminal_does_not_notify_twice(before: str) -> None:
    completed = _status_task(Status.COMPLETED)
    failed = _status_task(Status.FAILED, task_id="task-2", error="boom")
    previous = {"task-1": before, "task-2": before}
    assert notif.detect_task_notifications(previous, [completed, failed]) == []


@pytest.mark.parametrize("before", ["queued", "running", "processing", "exporting"])
def test_every_non_terminal_baseline_notifies_on_completion(before: str) -> None:
    task = _status_task(Status.COMPLETED)
    assert len(notif.detect_task_notifications({"task-1": before}, [task])) == 1


def test_unknown_previous_status_still_notifies_on_completion() -> None:
    """Only a *missing* entry is a baseline; an unknown value is non-terminal."""
    task = _status_task(Status.COMPLETED)
    assert len(notif.detect_task_notifications({"task-1": "mystery"}, [task])) == 1


def test_active_statuses_never_notify() -> None:
    previous = {"task-1": "running"}
    tasks = [
        _status_task(Status.QUEUED),
        _status_task(Status.RUNNING, task_id="task-2"),
        _status_task(Status.PROCESSING, task_id="task-3"),
        _status_task(Status.EXPORTING, task_id="task-4"),
    ]
    assert notif.detect_task_notifications(previous, tasks) == []


def test_poll_cycle_notifies_once_per_transition() -> None:
    """The full loop: baseline -> transition -> one event -> silence."""
    task = _status_task(Status.RUNNING)

    # Poll 1: the first sight of the task only builds the baseline.
    previous = notif.snapshot_task_states([task])
    assert previous == {"task-1": "running"}
    assert notif.detect_task_notifications(previous, [task]) == []

    # Poll 2: the task finished -> exactly one notification.
    task.status = Status.COMPLETED
    events = notif.detect_task_notifications(previous, [task])
    assert len(events) == 1

    # Poll 3+: the baseline caught up; the same snapshot is silent forever.
    previous = notif.snapshot_task_states([task])
    assert notif.detect_task_notifications(previous, [task]) == []


def test_snapshot_deduplicates_tasks_by_id_keeping_the_first() -> None:
    first = _status_task(Status.COMPLETED, title="First")
    same_id = _status_task(Status.COMPLETED, title="Second")
    flattened = notif.flatten_task_snapshot(_task_snapshot(completed=[first, same_id]))
    assert flattened == [first]

    # Detection sees one task, so one transition cannot double-notify.
    events = notif.detect_task_notifications({"task-1": "queued"}, flattened)
    assert len(events) == 1
    assert events[0].title == "First"


def test_flatten_task_snapshot_covers_all_sections_in_order() -> None:
    running = _status_task(Status.RUNNING)
    pending = _status_task(Status.QUEUED, task_id="task-2")
    completed = _status_task(Status.COMPLETED, task_id="task-3")
    flattened = notif.flatten_task_snapshot(
        _task_snapshot(running=[running], pending=[pending], completed=[completed])
    )
    assert flattened == [running, pending, completed]


def test_snapshot_task_states_uses_raw_status_values() -> None:
    tasks = [_status_task(Status.RUNNING), _status_task(Status.FAILED, task_id="t2")]
    assert notif.snapshot_task_states(tasks) == {"task-1": "running", "t2": "failed"}


def test_task_event_status_message_for_a_completed_task() -> None:
    event = notif.TaskNotification(
        task_id="1", title="Alpha", outcome=notif.TaskOutcome.COMPLETED
    )
    assert notif.task_event_status_message([event]) == t(
        "Download completed: {book}"
    ).format(book="Alpha")


def test_task_event_status_message_for_a_failed_task() -> None:
    event = notif.TaskNotification(
        task_id="1", title="Alpha", outcome=notif.TaskOutcome.FAILED, error="boom"
    )
    assert notif.task_event_status_message([event]) == t(
        "Download failed for {book}: {error}"
    ).format(book="Alpha", error="boom")

    # A missing error falls back to the localized status label.
    blank = notif.TaskNotification(
        task_id="1", title="Alpha", outcome=notif.TaskOutcome.FAILED, error="  "
    )
    assert notif.task_event_status_message([blank]) == t(
        "Download failed for {book}: {error}"
    ).format(book="Alpha", error=t("Failed"))


def test_task_event_status_message_joins_a_batch() -> None:
    events = [
        notif.TaskNotification(
            task_id="1", title="Alpha", outcome=notif.TaskOutcome.COMPLETED
        ),
        notif.TaskNotification(
            task_id="2", title="Beta", outcome=notif.TaskOutcome.FAILED, error="boom"
        ),
    ]
    assert notif.task_event_status_message(events) == " | ".join(
        [
            t("Download completed: {book}").format(book="Alpha"),
            t("Download failed for {book}: {error}").format(book="Beta", error="boom"),
        ]
    )
    assert notif.task_event_status_message([]) == ""


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
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hello, World!", "helloworld"),
        ("  spaced  out  ", "spacedout"),
        # Case and punctuation differences collapse to the same key.
        ("Hello,  WORLD!", "helloworld"),
        ("斗破苍穹：第二卷", "斗破苍穹第二卷"),
        ("O'Neill—凡人修仙传（精校）", "oneill凡人修仙传精校"),
        ("ABC", "abc"),
        ("", ""),
    ],
)
def test_normalize_title(raw: object, expected: str) -> None:
    assert fmt.normalize_title(raw) == expected


def test_normalize_title_handles_none() -> None:
    assert fmt.normalize_title(None) == ""


def test_normalize_title_matches_punctuation_and_case_variants() -> None:
    assert fmt.normalize_title("Dune Messiah（精校版）") == fmt.normalize_title(
        "dune messiah 精校版"
    )


# ---------------------------------------------------------------------------
# Follow list: matching a search result to a followed book
# ---------------------------------------------------------------------------
def test_match_watch_result_prefers_exact_site_and_book_id() -> None:
    result = _search_result(site="b520", book_id="9", title="Other Name")
    # The title differs, but the site/book id identity wins.
    assert fmt.match_watch_result("b520", "9", "Wanted Title", [result]) is result


def test_match_watch_result_falls_back_to_normalized_title() -> None:
    # Different id, same site: only the normalized title can identify the book.
    result = _search_result(site="b520", book_id="777", title="  Hello,  WORLD! ")
    assert fmt.match_watch_result("b520", "1", "hello world", [result]) is result


def test_match_watch_result_title_fallback_ignores_other_sites() -> None:
    result = _search_result(site="qidian", book_id="1", title="Same Title")
    assert fmt.match_watch_result("b520", "1", "Same Title", [result]) is None


@pytest.mark.parametrize("title", ["", "   "])
def test_match_watch_result_empty_title_disables_fallback(title: str) -> None:
    result = _search_result(site="b520", book_id="777", title="Whatever")
    assert fmt.match_watch_result("b520", "1", title, [result]) is None


def test_match_watch_result_without_any_results() -> None:
    assert fmt.match_watch_result("b520", "1", "Title", []) is None


def test_match_watch_result_without_a_match() -> None:
    result = _search_result(site="b520", book_id="9", title="Different")
    assert fmt.match_watch_result("b520", "1", "Title", [result]) is None


def test_match_watch_result_returns_first_of_duplicate_titles() -> None:
    first = _search_result(site="b520", book_id="7", title="Same")
    second = _search_result(site="b520", book_id="8", title="Same")
    assert fmt.match_watch_result("b520", "1", "Same", [first, second]) is first


def test_match_watch_result_tolerates_empty_identity() -> None:
    result = _search_result(site="b520", book_id="777", title="Same")
    assert fmt.match_watch_result("", "", "Same", [result]) is None


# ---------------------------------------------------------------------------
# Follow list: row formatting
# ---------------------------------------------------------------------------
def test_follow_row_renders_all_columns() -> None:
    entry = WatchEntry(
        site="b520",
        book_id="8_8187",
        title="凡人修仙传",
        last_chapter="第10章",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    site, book_id, title, last_chapter, updated = fmt.follow_row(entry)
    assert (site, book_id, title, last_chapter) == (
        "b520",
        "8_8187",
        "凡人修仙传",
        "第10章",
    )
    expected = (
        datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    assert updated == expected


def test_follow_row_tolerates_missing_fields() -> None:
    assert fmt.follow_row(SimpleNamespace()) == ("", "", "", "", "")


def test_follow_row_keeps_unparsable_timestamps_visible() -> None:
    entry = SimpleNamespace(
        site="s", book_id="1", title="t", last_chapter="c", updated_at="yesterday"
    )
    assert fmt.follow_row(entry)[4] == "yesterday"


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
@pytest.mark.parametrize(
    ("site", "book_id", "title", "expected"),
    [
        # A complete row is followable.
        ("b520", "8_8187", "凡人修仙传", True),
        # Whitespace around a field is trimmed, not rejected.
        ("  b520  ", " 8_8187 ", " 凡人修仙传 ", True),
        # Any missing field disables the action.
        ("", "1", "Title", False),
        ("b520", "", "Title", False),
        ("b520", "1", "", False),
        # Whitespace-only fields count as missing.
        ("   ", "1", "Title", False),
        ("b520", "   ", "Title", False),
        ("b520", "1", "   ", False),
        # None degrades to a missing field instead of raising.
        (None, "1", "Title", False),
        ("b520", None, "Title", False),
        ("b520", "1", None, False),
        (None, None, None, False),
    ],
)
def test_can_follow_search_result_requires_all_fields(
    site: object, book_id: object, title: object, expected: bool
) -> None:
    assert fmt.can_follow_search_result(site, book_id, title) is expected


def test_follow_feedback_message_for_a_new_entry() -> None:
    message = fmt.follow_feedback_message(
        fmt.FOLLOW_ADDED, site="b520", book_id="8_8187", title=" 凡人修仙传 "
    )
    assert message == t("Added to the follow list: {title}").format(title="凡人修仙传")


def test_follow_feedback_message_for_an_existing_entry() -> None:
    message = fmt.follow_feedback_message(
        fmt.FOLLOW_ALREADY, site="b520", book_id="8_8187", title="凡人修仙传"
    )
    assert message == t("Already in the follow list: {title}").format(
        title="凡人修仙传"
    )


def test_follow_feedback_message_for_an_invalid_row() -> None:
    assert fmt.follow_feedback_message(fmt.FOLLOW_INVALID) == t(
        "Cannot follow: site, book id and title are required."
    )
    # The identity fields are ignored for the invalid outcome.
    assert fmt.follow_feedback_message(
        fmt.FOLLOW_INVALID, site="b520", book_id="1", title="T"
    ) == t("Cannot follow: site, book id and title are required.")


def test_follow_feedback_message_falls_back_to_site_and_book_id() -> None:
    # A blank title labels the book as "site/book_id" instead.
    message = fmt.follow_feedback_message(
        fmt.FOLLOW_ADDED, site="b520", book_id="42", title="  "
    )
    assert message == t("Added to the follow list: {title}").format(title="b520/42")
    # Missing site and book id degrade to an empty label.
    assert fmt.follow_feedback_message(fmt.FOLLOW_ALREADY, title=None) == t(
        "Already in the follow list: {title}"
    ).format(title="")


# ---------------------------------------------------------------------------
# Search results context menu (pure availability logic)
# ---------------------------------------------------------------------------
def test_search_menu_state_without_selection() -> None:
    # No selected rows: nothing may be done at all.
    assert fmt.search_menu_state([]) == fmt.SearchMenuState(
        can_add_to_queue=False, can_follow=False, can_open_url=False
    )


def test_search_menu_state_for_a_complete_selection() -> None:
    assert fmt.search_menu_state([_search_result()]) == fmt.SearchMenuState(
        can_add_to_queue=True, can_follow=True, can_open_url=True
    )


def test_search_menu_state_tracks_each_entry_independently() -> None:
    # Missing site and book id: cannot queue and cannot follow, but the
    # book page can still be opened in a browser.
    row = _search_result(site="", book_id="", title="", book_url="https://a/b")
    state = fmt.search_menu_state([row])
    assert state == fmt.SearchMenuState(
        can_add_to_queue=False, can_follow=False, can_open_url=True
    )

    # A blank-but-present book_url behaves exactly like a missing one, while
    # the identity fields still allow queueing and following.
    row = _search_result(site="b520", book_id="1", title="T", book_url="   ")
    state = fmt.search_menu_state([row])
    assert state == fmt.SearchMenuState(
        can_add_to_queue=True, can_follow=True, can_open_url=False
    )


def test_search_menu_state_any_valid_row_enables_an_entry() -> None:
    # The menu acts on the whole selection: an entry stays available while
    # any selected row could use it, and each entry is judged separately.
    results = [
        _search_result(site="", book_id="", title="", book_url=""),
        _search_result(site="b520", book_id="7", title="No Url", book_url=""),
        _search_result(site="b520", book_id="8", title="", book_url="https://a/b"),
    ]
    state = fmt.search_menu_state(results)
    assert state == fmt.SearchMenuState(
        can_add_to_queue=True, can_follow=True, can_open_url=True
    )


# ---------------------------------------------------------------------------
# Window state: geometry parsing / serialization (pure)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1100x720", wstate.WindowGeometry(width=1100, height=720)),
        ("1100x720+50+30", wstate.WindowGeometry(1100, 720, 50, 30)),
        # Negative offsets keep their Tk meaning (right/bottom screen edge).
        ("900x600-40-20", wstate.WindowGeometry(900, 600, -40, -20)),
        ("900x600+40-20", wstate.WindowGeometry(900, 600, 40, -20)),
        ("900x600-40+20", wstate.WindowGeometry(900, 600, -40, 20)),
        ("0x0", wstate.WindowGeometry(0, 0)),
        # A leading "=" and surrounding whitespace are tolerated.
        ("=1100x720+1+2", wstate.WindowGeometry(1100, 720, 1, 2)),
        ("  1100x720+1+2  ", wstate.WindowGeometry(1100, 720, 1, 2)),
    ],
)
def test_parse_geometry_accepts_tk_geometry_strings(
    raw: str, expected: wstate.WindowGeometry
) -> None:
    assert wstate.parse_geometry(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "1100",
        "x720",
        "1100xx720",
        "1100 x 720",
        "1100x720+",
        "1100x720++1+1",
        "1100x720+1",
        "1100x720+1+2+3",
    ],
)
def test_parse_geometry_rejects_invalid_strings(raw: str) -> None:
    assert wstate.parse_geometry(raw) is None
    assert wstate.parse_geometry(None) is None  # type: ignore[arg-type]


def test_serialize_geometry_matches_the_tk_format() -> None:
    assert wstate.serialize_geometry(wstate.WindowGeometry(1100, 720)) == "1100x720"
    assert (
        wstate.serialize_geometry(wstate.WindowGeometry(1100, 720, 50, 30))
        == "1100x720+50+30"
    )
    assert (
        wstate.serialize_geometry(wstate.WindowGeometry(900, 600, -40, -20))
        == "900x600-40-20"
    )


@pytest.mark.parametrize(
    "geometry",
    [
        wstate.WindowGeometry(1100, 720),
        wstate.WindowGeometry(1100, 720, 50, 30),
        wstate.WindowGeometry(900, 600, -40, -20),
        wstate.WindowGeometry(1, 1, 0, 0),
    ],
)
def test_geometry_parse_serialize_round_trip(geometry: wstate.WindowGeometry) -> None:
    assert wstate.parse_geometry(wstate.serialize_geometry(geometry)) == geometry


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


def test_window_geometry_mapping_round_trip() -> None:
    for geometry in (
        wstate.WindowGeometry(1100, 720),
        wstate.WindowGeometry(1100, 720, 50, 30),
        wstate.WindowGeometry(900, 600, -40, -20),
    ):
        mapping = wstate.window_geometry_to_mapping(geometry)
        assert wstate.window_geometry_from_mapping(mapping) == geometry


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
    """Every data-i18n / data-i18n-ph key in the HTML resolves via t()."""
    catalog = _i18n_catalog()
    keys = set(re.findall(r'data-i18n(?:-ph)?="([^"]+)"', APP_HTML))
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


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (3 * 1024 * 1024, "3.0 MB")],
)
def test_format_size_units(size: int, expected: str) -> None:
    assert _format_size(size) == expected


def test_format_health_ms_units() -> None:
    assert _format_health_ms(850) == "850ms"
    assert _format_health_ms(1500) == "1.5s"


# ---------------------------------------------------------------------------
# Webview bridge (webview_app): ApiBridge behaviour
# ---------------------------------------------------------------------------


def test_bridge_initial_state_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "novel_downloader.apps.desktop.webview_app._load_prefs",
        lambda: {"theme": "dark"},
    )
    state = ApiBridge().get_initial_state()
    assert state["theme"] == "dark"
    assert "search" in state["sites"] and "download" in state["sites"]
    assert state["i18n"]["Search"] == t("Search")


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


def test_bridge_prefs_round_trip(tmp_path: Path) -> None:
    import novel_downloader.apps.desktop.webview_app as wa

    patcher = pytest.MonkeyPatch()
    try:
        patcher.setattr(wa, "_prefs_path", lambda: tmp_path / "prefs.json")
        wa._save_prefs({"theme": "dark"})
        assert wa._load_prefs() == {"theme": "dark"}
        wa._save_prefs({"lang": "zh"})
        assert wa._load_prefs() == {"theme": "dark", "lang": "zh"}
    finally:
        patcher.undo()


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
