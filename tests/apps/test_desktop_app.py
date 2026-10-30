#!/usr/bin/env python3
"""
tests.apps.test_desktop_app
---------------------------

Offline tests for the Tkinter desktop client.

The bulk of the suite covers :mod:`novel_downloader.apps.desktop.formatting`,
which is deliberately free of Tk imports, plus the threading helpers in
:mod:`novel_downloader.apps.desktop.engine` and the home-directory bootstrap in
:mod:`novel_downloader.apps.desktop.main`.  A single guarded smoke test builds
the real window when a display is available so that a missing widget or an
untranslated label is caught without requiring user interaction.

No network access and no windowed interaction happen here: the Tk window is
created, inspected via ``update_idletasks`` (which does not run the ``after``
poll callbacks) and destroyed again.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.common.history_files import FileItem
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.core.watchlist import WatchEntry, WatchlistStore
from novel_downloader.apps.desktop import formatting as fmt
from novel_downloader.apps.desktop import main as desktop_main
from novel_downloader.apps.desktop import notifications as notif
from novel_downloader.apps.desktop import preferences as prefs
from novel_downloader.apps.desktop import window_state as wstate
from novel_downloader.apps.desktop.engine import AsyncEngine
from novel_downloader.infra.i18n import t
from novel_downloader.plugins import search as search_module
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
    ui_path = Path(fmt.__file__).with_name("ui.py")
    source = ui_path.read_text(encoding="utf-8")
    assert "type(exc).__name__" not in source
    assert "format_error(exc)" in source


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
# Window smoke test (guarded: needs a display)
# ---------------------------------------------------------------------------
def test_desktop_window_builds_all_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk = pytest.importorskip("tkinter")
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    # Keep the window-state file on the private data directory as well.
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path / "music"))

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environments
        pytest.skip(f"Tk display unavailable: {exc}")

    root.withdraw()
    app: Any = None
    try:
        from novel_downloader.apps.desktop.ui import DesktopApp

        app = DesktopApp(root)
        # ``update_idletasks`` builds pending geometry but does not fire the
        # ``after`` poll callback, so no engine thread is started here.
        root.update_idletasks()

        assert app.search_tree is not None
        assert app.task_tree is not None
        assert app.history_tree is not None
        assert app.search_button is not None
        assert app.search_last_button is not None
        assert app.search_follow_button is not None
        assert app.search_follow_button.cget("text") == t("Follow")
        # Nothing is selected yet, so the follow action is unavailable.
        assert "disabled" in app.search_follow_button.state()
        assert app.retry_button is not None
        assert app.open_file_button is not None
        assert app.open_folder_button is not None
        assert app.task_filter_combo is not None
        assert app.task_keyword_entry is not None
        assert app._task_file_menu is not None
        assert str(app._status_var.get())  # status bar shows something
        assert root.title() == f"{t('Novel Downloader')} v{__version__}"
        assert str(app._env_var.get())  # API/data-directory information
        assert app.task_detail_var.get() == t("Select a task to view its details.")
        assert "disabled" in app.retry_button.state()
        assert "disabled" in app.open_file_button.state()
        assert "disabled" in app.open_folder_button.state()
        assert "disabled" in app.search_last_button.state()
        assert app.task_filter_combo.get() == t("All")
        assert app.task_keyword_var.get() == ""
        # Task-completion notification bookkeeping starts empty.
        assert app._status_label is not None
        assert app._previous_task_states == {}
        assert app._status_highlight_after is None

        # Follow ("追更") tab: widgets exist and the tab can be raised.
        assert app.follow_tree is not None
        assert app.follow_add_button is not None
        assert app.follow_remove_button is not None
        assert app.follow_check_button is not None
        assert app.follow_download_button is not None
        for button in (
            app.follow_add_button,
            app.follow_remove_button,
            app.follow_check_button,
            app.follow_download_button,
        ):
            assert button.instate(["!disabled"])
        notebook = app.follow_tab.master
        notebook.select(app.follow_tab)
        root.update_idletasks()
        assert notebook.tab(app.follow_tab, "text") == t("Follow")

        # Music tab: it can be raised, and the health-check widgets exist
        # whenever the optional music engine is installed.
        notebook.select(app.music_tab)
        root.update_idletasks()
        assert notebook.tab(app.music_tab, "text") == t("Music")
        if app._music_ready:
            engine = app._music_engine
            assert engine is not None
            assert callable(engine["health_check"])
            assert app.music_platform_combo is not None
            assert app.music_search_button is not None
            assert app.music_health_button is not None
            assert app.music_health_tree is not None
            assert "disabled" not in app.music_health_button.state()
            assert app.music_health_tree.heading("ok", "text") == t("OK")
            assert app.music_health_tree.heading("elapsed", "text") == t("Elapsed (ms)")
            assert app.music_health_tree.heading("error", "text") == t("Error")

            # The note label follows the selected platform.
            noted = [
                key
                for key in app._music_display_to_key.values()
                if engine["platform_note"](key)
            ]
            if noted:  # the registry ships notes for some platforms
                key = noted[0]
                display = next(
                    text
                    for text, candidate in app._music_display_to_key.items()
                    if candidate == key
                )
                app.music_platform_var.set(display)
                app._refresh_music_note()
                assert app.music_note_var.get() == engine["platform_note"](key)
    finally:
        if app is not None:
            app._closing = True
        with contextlib.suppress(tk.TclError):
            root.destroy()


# ---------------------------------------------------------------------------
# Follow tab (window, guarded: needs a display)
# ---------------------------------------------------------------------------
def _open_desktop_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any]:
    """Build a hidden window on a private data directory, or skip."""
    tk = pytest.importorskip("tkinter")
    import novel_downloader.apps.desktop.ui as ui_mod

    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    # Keep the window-state file on the private data directory as well.
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path / "music"))
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environments
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return ui_mod.DesktopApp(root), root


def _drain_and_dispatch(
    app: Any,
    stop: Callable[[list[tuple[str, Any]]], bool],
    timeout: float = 10.0,
) -> list[tuple[str, Any]]:
    """
    Drain the event queue like ``_poll`` does and handle the safe kinds.

    ``error`` events are recorded but not dispatched: their handler opens a
    modal message box that would block the test.
    """
    events: list[tuple[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            kind, payload = app.events.get_nowait()
        except queue.Empty:
            if stop(events):
                break
            time.sleep(0.02)
            continue
        events.append((kind, payload))
        if kind in {
            "status",
            "follow_entries",
            "follow_item",
            "follow_check_done",
            "search_follow_done",
            "search_follow_finished",
            "music_health_item",
            "music_health_done",
        }:
            app._handle_event(kind, payload)
    return events


def test_follow_check_updates_entries_and_reenables_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        store = WatchlistStore()
        store.add("site-a", "1", title="Alpha Book", last_chapter="Chapter 8")
        store.add("site-b", "2", title="Beta")
        store.add("site-c", "3", title="Gamma", last_chapter="Chapter 5")
        store.add("site-d", "4", title="Delta")

        async def fake_search(
            keyword: str, sites: list[str] | None = None, **_kwargs: Any
        ) -> list[SearchResult]:
            # Staggered delays make the completion order deterministic while
            # the books are still searched concurrently: each result is posted
            # as soon as its search finishes.
            delay = {"site-a": 0.0, "site-b": 0.02, "site-c": 0.04, "site-d": 0.06}
            if sites and sites[0] in delay:
                await asyncio.sleep(delay[sites[0]])
            if sites == ["site-a"]:
                # Different id on purpose: only the normalized title matches.
                return [
                    _search_result(
                        site="site-a",
                        book_id="999",
                        title="Alpha  BOOK!",
                        latest_chapter="Chapter 9",
                    )
                ]
            if sites == ["site-c"]:
                return [
                    _search_result(
                        site="site-c",
                        book_id="3",
                        title="Gamma",
                        latest_chapter="Chapter 5",
                    )
                ]
            if sites == ["site-d"]:
                raise ValueError("network down")
            return []

        # The shared sweep resolves the search lazily through the plugin
        # module, so the patch point is the module attribute itself.
        monkeypatch.setattr(search_module, "search", fake_search)
        app._follow_entries = store.entries()
        app._start_follow_check()

        assert "disabled" in app.follow_check_button.state()  # type: ignore[union-attr]
        events = _drain_and_dispatch(
            app,
            stop=lambda found: any(kind == "follow_check_done" for kind, _ in found),
        )

        items = [payload for kind, payload in events if kind == "follow_item"]
        assert [item[2] for item in items] == [
            "updated",
            "not_found",
            "up_to_date",
            "error",
        ]
        assert items[0] == ("site-a", "1", "updated", "Chapter 9")
        assert items[3][3] == "ValueError: network down"

        # The new chapter was persisted and the list reloaded from disk.
        assert store.get("site-a", "1") is not None
        assert store.get("site-a", "1").last_chapter == "Chapter 9"  # type: ignore[union-attr]
        assert store.get("site-c", "3").last_chapter == "Chapter 5"  # type: ignore[union-attr]
        refreshed = [payload for kind, payload in events if kind == "follow_entries"]
        assert refreshed and len(refreshed[-1]) == 4
        assert any(
            entry.last_chapter == "Chapter 9" and entry.site == "site-a"
            for entry in refreshed[-1]
        )

        assert app._follow_counts == {
            "updated": 1,
            "not_found": 1,
            "up_to_date": 1,
            "error": 1,
        }
        # The check finished: the action buttons are usable again.
        assert app.follow_check_button is not None
        assert "disabled" not in app.follow_check_button.state()
        assert app._follow_checking is False
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


def test_follow_download_queues_selected_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.ui as ui_mod

    class _StubManager:
        """Stands in for the real task manager; 'site-b' always fails."""

        def __init__(self) -> None:
            self.added: list[tuple[str, str, str]] = []

        async def add_task(
            self, *, title: str, site: str, book_id: str
        ) -> DownloadTask:
            self.added.append((title, site, book_id))
            if site == "site-b":
                raise RuntimeError("boom")
            return DownloadTask(title=title, site=site, book_id=book_id)

    stub = _StubManager()
    monkeypatch.setattr(ui_mod, "manager", stub)

    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        # Let the initial watchlist load finish so its render cannot clobber
        # the fixture state below.
        _drain_and_dispatch(
            app,
            stop=lambda found: any(kind == "follow_entries" for kind, _ in found),
        )
        entries = [
            WatchEntry(site="site-a", book_id="1", title="Book One"),
            WatchEntry(site="site-b", book_id="2", title="Book Two"),
        ]
        app._follow_entries = entries
        app._render_follow_entries(entries)
        assert app.follow_tree is not None

        # Only the selected row is queued, and only its success is reported.
        app.follow_tree.selection_set("0")
        app._download_follow_selection()
        events = _drain_and_dispatch(
            app, stop=lambda found: sum(1 for kind, _ in found if kind == "status") >= 1
        )
        assert stub.added == [("Book One", "site-a", "1")]

        # Nothing selected: the whole list is queued; failures are reported.
        stub.added.clear()
        app.follow_tree.selection_set([])
        app._download_follow_selection()
        events = _drain_and_dispatch(
            app,
            stop=lambda found: (
                sum(1 for kind, _ in found if kind in {"status", "error"}) >= 2
            ),
        )

        assert sorted(stub.added) == [
            ("Book One", "site-a", "1"),
            ("Book Two", "site-b", "2"),
        ]
        statuses = [payload for kind, payload in events if kind == "status"]
        errors = [payload for kind, payload in events if kind == "error"]
        assert any("Book One" in message for message in statuses)
        assert any(
            "Book Two" in message and "RuntimeError: boom" in message
            for message in errors
        )
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


def test_follow_add_and_remove_workers_persist_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        # Let the initial watchlist load finish before driving the workers.
        _drain_and_dispatch(
            app,
            stop=lambda found: any(kind == "follow_entries" for kind, _ in found),
        )
        store = WatchlistStore()

        # Adding (with a title) persists the entry and refreshes the list.
        app._follow_add("site-a", "1", "Alpha Book")
        _drain_and_dispatch(
            app,
            stop=lambda found: (
                any(kind == "follow_entries" for kind, _ in found)
                and len(store.entries()) == 1
            ),
        )
        entry = store.get("site-a", "1")
        assert entry is not None
        assert entry.title == "Alpha Book"

        # An empty title is stored as such (check then relies on the book id).
        app._follow_add("site-b", "2", "")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(store.entries()) < 2:
            time.sleep(0.02)
        second = store.get("site-b", "2")
        assert second is not None
        assert second.title == ""

        # The rendered list mirrors the file (one row per entry).
        _drain_and_dispatch(
            app,
            stop=lambda found: (
                any(kind == "follow_entries" for kind, _ in found)
                and app.follow_tree is not None
                and len(app.follow_tree.get_children()) == 2
            ),
        )
        assert app.follow_tree is not None
        assert len(app.follow_tree.get_children()) == 2

        # Removing the selected first row (site-a) deletes only that entry.
        app.follow_tree.selection_set("0")
        app._remove_follow_selected()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(
            entry.key == ("site-a", "1") for entry in store.entries()
        ):
            time.sleep(0.02)
        assert [entry.key for entry in store.entries()] == [("site-b", "2")]

        # Removing with nothing selected only hints at the status bar.
        app.follow_tree.selection_set([])
        app._remove_follow_selected()
        assert [entry.key for entry in store.entries()] == [("site-b", "2")]
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


def test_search_follow_button_adds_selection_to_the_watchlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search tab's follow button feeds the shared watchlist store."""
    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        # Let the initial watchlist load finish so its render cannot clobber
        # the fixture state below.
        _drain_and_dispatch(
            app,
            stop=lambda found: any(kind == "follow_entries" for kind, _ in found),
        )
        store = WatchlistStore()
        store.add("site-a", "1", title="Alpha Book")

        results = [
            _search_result(site="site-a", book_id="1", title="Alpha Book"),
            _search_result(site="site-b", book_id="2", title="Beta Book"),
            # Missing book_id: this row can never be followed.
            _search_result(site="site-c", book_id="", title="No Id"),
        ]
        app._search_results = results
        assert app.search_tree is not None
        tree = app.search_tree
        tree.delete(*tree.get_children())
        for index, result in enumerate(results):
            tree.insert("", "end", iid=str(index), values=fmt.search_row(result))

        button = app.search_follow_button
        assert button is not None

        # No selection: the button stays down.
        tree.selection_set([])
        app._update_search_follow_button()
        assert "disabled" in button.state()

        # Selecting only the invalid row keeps the button disabled, and a
        # forced click explains why instead of touching the store.
        tree.selection_set("2")
        app._update_search_follow_button()
        assert "disabled" in button.state()
        app._follow_selected_results()
        assert app._status_var.get() == t(
            "Cannot follow: site, book id and title are required."
        )
        assert [entry.key for entry in store.entries()] == [("site-a", "1")]

        # A mixed selection (valid + invalid) enables the button; the invalid
        # row is skipped and the valid one is persisted on a worker thread.
        tree.selection_set("1", "2")
        app._update_search_follow_button()
        assert "disabled" not in button.state()
        app._follow_selected_results()
        assert "disabled" in button.state()  # held down while the add runs
        _drain_and_dispatch(
            app,
            stop=lambda found: (
                any(kind == "search_follow_finished" for kind, _ in found)
                and any(kind == "follow_entries" for kind, _ in found)
            ),
        )
        entry = store.get("site-b", "2")
        assert entry is not None
        assert entry.title == "Beta Book"
        # The follow tab was refreshed through the shared store...
        assert app.follow_tree is not None
        assert len(app.follow_tree.get_children()) == 2
        # ...and the specific feedback outlives the generic count message;
        # it ends with a pointer to the Follow tab.
        assert app._status_var.get() == (
            t("Added to the follow list: {title}").format(title="Beta Book")
            + f" ({t('Follow')})"
        )
        assert app._search_follow_pending == set()
        # The batch is over: the button is usable for the still-selected rows.
        assert "disabled" not in button.state()

        # While a row's add is in flight its key is guarded against a repeated
        # click: the guard keeps the button down and swallows the re-trigger.
        app._search_follow_pending.add(("site-b", "2"))
        app._update_search_follow_button()
        assert "disabled" in button.state()
        app._follow_selected_results()
        assert app._search_follow_pending == {("site-b", "2")}
        app._search_follow_pending.clear()

        # Following a book that is already in the list changes nothing on disk
        # and says so.
        tree.selection_set("0")
        app._update_search_follow_button()
        assert "disabled" not in button.state()
        app._follow_selected_results()
        _drain_and_dispatch(
            app,
            stop=lambda found: (
                any(kind == "search_follow_finished" for kind, _ in found)
                and any(kind == "follow_entries" for kind, _ in found)
            ),
        )
        assert [entry.key for entry in store.entries()] == [
            ("site-a", "1"),
            ("site-b", "2"),
        ]
        assert store.get("site-a", "1") is not None
        assert store.get("site-a", "1").title == "Alpha Book"  # type: ignore[union-attr]
        assert app._status_var.get() == (
            t("Already in the follow list: {title}").format(title="Alpha Book")
            + f" ({t('Follow')})"
        )
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


# ---------------------------------------------------------------------------
# Search tab context menu (window, guarded: needs a display)
# ---------------------------------------------------------------------------
def test_search_context_menu_offers_row_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right-clicking a search result pops queue/follow/browser actions."""
    import tkinter as tk_mod

    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        # ``tk_popup`` would block until a menu entry is picked; record the
        # popup request instead and drive the entries via ``invoke`` below.
        popups: list[tuple[int, int]] = []
        monkeypatch.setattr(
            tk_mod.Menu,
            "tk_popup",
            lambda menu, x_root, y_root, entry="": popups.append((x_root, y_root)),
        )
        opened: list[str] = []
        monkeypatch.setattr(
            "webbrowser.open", lambda url, *args, **kwargs: opened.append(url) or True
        )

        store = WatchlistStore()
        results = [
            _search_result(site="site-a", book_id="1", title="Alpha Book"),
            _search_result(
                site="site-b",
                book_id="2",
                title="Beta Book",
                book_url="https://example.invalid/book/2",
            ),
            # Missing book_id and book_url: this row is dead weight.
            _search_result(site="site-c", book_id="", title="No Id", book_url=""),
        ]
        app._search_results = results
        assert app.search_tree is not None
        tree = app.search_tree
        tree.delete(*tree.get_children())
        for index, result in enumerate(results):
            tree.insert("", "end", iid=str(index), values=fmt.search_row(result))

        # Right-clicking a row pops the menu with the three batch actions.
        tree.selection_set("0", "1")
        app._on_search_right_click(
            SimpleNamespace(x=10, y=-1, x_root=123, y_root=45)  # type: ignore[arg-type]
        )
        assert popups == [(123, 45)]
        menu = app._search_menu
        assert menu is not None
        assert menu.index("end") == 3  # add, follow, separator, open in browser
        assert menu.entrycget(0, "label") == t("Add to download queue")
        assert menu.entrycget(1, "label") == t("Follow")
        assert menu.type(2) == "separator"
        assert menu.entrycget(3, "label") == t("Open in browser")
        for index in (0, 1, 3):
            assert menu.entrycget(index, "state") == "normal"

        # Selecting only the dead row disables every entry.
        tree.selection_set("2")
        app._on_search_right_click(
            SimpleNamespace(x=10, y=-1, x_root=1, y_root=1)  # type: ignore[arg-type]
        )
        assert app._search_menu is menu  # the lazy menu is reused
        for index in (0, 1, 3):
            assert app._search_menu.entrycget(index, "state") == "disabled"
        assert popups == [(123, 45), (1, 1)]

        # "Follow" feeds the shared watchlist store, like the toolbar button.
        tree.selection_set("1")
        app._on_search_right_click(
            SimpleNamespace(x=10, y=-1, x_root=2, y_root=2)  # type: ignore[arg-type]
        )
        assert app._search_menu.entrycget(1, "state") == "normal"
        app._search_menu.invoke(1)
        _drain_and_dispatch(
            app,
            stop=lambda found: (
                any(kind == "search_follow_finished" for kind, _ in found)
                and any(kind == "follow_entries" for kind, _ in found)
            ),
        )
        added = store.get("site-b", "2")
        assert added is not None
        assert added.title == "Beta Book"
        assert app._search_follow_pending == set()

        # "Open in browser" opens the book page of the selected rows.
        app._search_menu.invoke(3)
        assert opened == ["https://example.invalid/book/2"]
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


# ---------------------------------------------------------------------------
# Music tab (window, guarded): platform health check
# ---------------------------------------------------------------------------
def test_music_health_check_checks_every_platform_serially_and_reenables_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        if app._music_engine is None or not app._music_ready:
            pytest.skip("the music extras are not installed")

        labels = list(app._music_engine["available_labels"]())
        assert labels
        first_key, last_key = labels[0][0], labels[-1][0]
        seen: list[tuple[str, str]] = []

        def fake_health(
            platform: str,
            keyword: str = "test",
            *,
            timeout: float = 15.0,
            limit: int = 1,
        ) -> dict[str, Any]:
            # The worker is a single thread, so appends prove serial execution.
            seen.append((platform, keyword))
            if platform == first_key:
                raise RuntimeError("worker blew up")
            if platform == last_key:
                return {"ok": False, "elapsed_ms": 5, "error": "down\nnow"}
            return {"ok": True, "elapsed_ms": 10, "error": ""}

        app._music_engine = dict(app._music_engine, health_check=fake_health)

        # A check already in progress blocks a second start.
        app._music_health_running = True
        app._start_music_health_check()
        assert seen == []
        app._music_health_running = False

        app.music_query_var.set("  周杰伦  ")
        app._start_music_health_check()
        button = app.music_health_button
        assert button is not None
        assert "disabled" in button.state()  # disabled while the check runs
        events = _drain_and_dispatch(
            app,
            stop=lambda found: any(kind == "music_health_done" for kind, _ in found),
            timeout=60.0,
        )

        # Every registered platform was checked exactly once, with the query
        # keyword (trimmed) passed through to ``health_check``.
        assert sorted(platform for platform, _ in seen) == sorted(
            key for key, _ in labels
        )
        assert {keyword for _, keyword in seen} == {"周杰伦"}
        items = [payload for kind, payload in events if kind == "music_health_item"]
        assert len(items) == len(labels)

        # The table renders healthy-first; the failing platforms show a
        # one-line error summary.
        tree = app.music_health_tree
        assert tree is not None
        rows = [tuple(tree.item(iid, "values")) for iid in tree.get_children()]
        assert len(rows) == len(labels)
        failed_rows = [row for row in rows if row[1] == t("Failed")]
        assert [row[0] for row in failed_rows] == [first_key, last_key]
        assert failed_rows[0][2] == "0"
        assert failed_rows[0][3] == "RuntimeError: worker blew up"
        assert failed_rows[1][3] == "down now"
        assert all(
            row[1] == t("OK") for row in rows if row[0] not in {first_key, last_key}
        )

        # Summary and status; the button is usable again.
        summary = fmt.health_summary(app._music_health_results)
        assert app.music_health_summary_var.get() == summary
        assert app._status_var.get() == summary
        assert "disabled" not in button.state()
        assert app._music_health_running is False
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


# ---------------------------------------------------------------------------
# Task notifications (window, guarded: needs a display)
# ---------------------------------------------------------------------------
def test_task_notification_beeps_and_highlights_when_window_is_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.ui as ui_mod

    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        played: list[int] = []
        monkeypatch.setattr(ui_mod, "play_notification_sound", lambda: played.append(1))

        task = DownloadTask(title="Alpha", site="qidian", book_id="1", task_id="task-1")
        task.status = Status.RUNNING

        # First snapshot: baseline only, no notification of any kind.
        app._render_tasks(_task_snapshot(running=[task]))
        assert played == []
        assert app._previous_task_states == {"task-1": "running"}
        assert app._status_highlight_after is None

        # The fixture withdraws the window, so the app counts as background.
        assert app._window_in_background() is True

        # running -> completed while hidden: a beep and a highlighted line.
        task.status = Status.COMPLETED
        app._render_tasks(_task_snapshot(completed=[task]))
        assert played == [1]
        assert app._status_var.get() == t("Download completed: {book}").format(
            book="Alpha"
        )
        label = app._status_label
        assert label is not None
        assert str(label.cget("foreground")) == ui_mod._STATUS_HIGHLIGHT_COMPLETED
        assert app._status_highlight_after is not None

        # Re-rendering the same snapshot cannot notify twice.
        app._render_tasks(_task_snapshot(completed=[task]))
        assert played == [1]

        # Any new status message ends the highlight and restores the color.
        app._set_status(t("Ready"))
        assert str(label.cget("foreground")) == "black"
        assert app._status_highlight_after is None
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


def test_task_notification_gates_the_beep_by_window_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.ui as ui_mod

    app, root = _open_desktop_app(tmp_path, monkeypatch)
    try:
        root.update_idletasks()
        played: list[int] = []
        monkeypatch.setattr(ui_mod, "play_notification_sound", lambda: played.append(1))

        failed = DownloadTask(title="Beta", site="b520", book_id="2", task_id="task-2")
        failed.status = Status.RUNNING
        done = DownloadTask(title="Gamma", site="qidian", book_id="3", task_id="task-3")
        done.status = Status.RUNNING

        # Baseline for both tasks.
        app._render_tasks(_task_snapshot(running=[failed, done]))

        # Foreground window: the transitions update the status bar but the
        # app never beeps while the user is looking at it.
        original = app._window_in_background
        app._window_in_background = lambda: False
        try:
            failed.status = Status.FAILED
            failed.error = "HTTPError: 500"
            done.status = Status.COMPLETED
            app._render_tasks(_task_snapshot(completed=[failed, done]))
        finally:
            app._window_in_background = original

        assert played == []
        assert app._status_var.get() == " | ".join(
            [
                t("Download failed for {book}: {error}").format(
                    book="Beta", error="HTTPError: 500"
                ),
                t("Download completed: {book}").format(book="Gamma"),
            ]
        )
        # A mixed batch highlights in the failure color.
        label = app._status_label
        assert label is not None
        assert str(label.cget("foreground")) == ui_mod._STATUS_HIGHLIGHT_FAILED
        assert app._previous_task_states == {
            "task-2": "failed",
            "task-3": "completed",
        }

        # The next poll is silent: the baseline caught up.
        app._render_tasks(_task_snapshot(completed=[failed, done]))
        assert played == []
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


# ---------------------------------------------------------------------------
# Window state persistence (window, guarded: needs a display)
# ---------------------------------------------------------------------------
def _open_desktop_app_with_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Path]:
    """
    Build a hidden window whose window-state file lives in ``tmp_path``.

    Returns the app, the root and the state file path; skips when Tk cannot
    open a display.
    """
    tk = pytest.importorskip("tkinter")
    import novel_downloader.apps.desktop.ui as ui_mod

    state_dir = tmp_path / "music"
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(state_dir))
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environments
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return ui_mod.DesktopApp(root), root, state_dir / "window_state.json"


def test_desktop_window_restores_stored_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk = pytest.importorskip("tkinter")
    app, root, state_file = _open_desktop_app_with_state(tmp_path, monkeypatch)
    try:
        # Record the geometry strings the restore path applies.  The query
        # side stays live: a fully specified geometry (with position) takes
        # effect, while Tk propagation may still force a size-only request
        # up to the packed content's required size.
        applied: list[str] = []
        set_geometry = root.geometry

        def geometry_spy(value: str | None = None) -> str:
            if value is None:
                return set_geometry()
            applied.append(value)
            return set_geometry(value)

        monkeypatch.setattr(root, "geometry", geometry_spy)

        # A stored geometry is applied verbatim on launch and takes effect.
        assert wstate.save_window_geometry(
            state_file, wstate.WindowGeometry(900, 600, 22, 33)
        )
        app._restore_window_geometry()
        assert applied == ["900x600+22+33"]
        root.update_idletasks()
        current = wstate.parse_geometry(set_geometry())
        assert current is not None
        assert (current.x, current.y) == (22, 33)
        assert current.width >= 900 and current.height >= 600

        # A stored size below the minimum is clamped to 400x300 before it
        # reaches the window (the content may still demand a larger one).
        assert wstate.save_window_geometry(
            state_file, wstate.WindowGeometry(200, 100, 0, 0)
        )
        app._restore_window_geometry()
        assert applied[-1] == "400x300+0+0"
        root.update_idletasks()
        current = wstate.parse_geometry(set_geometry())
        assert current is not None
        assert (current.x, current.y) == (0, 0)
        assert current.width >= wstate.MIN_WINDOW_WIDTH
        assert current.height >= wstate.MIN_WINDOW_HEIGHT

        # A corrupted file falls back to the default geometry string.
        state_file.write_text("not json", encoding="utf-8")
        app._restore_window_geometry()
        assert applied[-1] == "1100x720"

        # A missing file falls back to the default geometry string too.
        state_file.unlink()
        app._restore_window_geometry()
        assert applied[-1] == "1100x720"
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(tk.TclError):
            root.destroy()


def test_desktop_window_saves_geometry_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("tkinter")
    app, root, state_file = _open_desktop_app_with_state(tmp_path, monkeypatch)
    try:
        root.geometry("980x640+15+25")
        root.update_idletasks()
        assert not state_file.exists()

        # Closing the window persists the geometry it had.
        app._on_close()
        assert app._closing is True
    except Exception:
        app._closing = True
        with contextlib.suppress(Exception):
            root.destroy()
        raise
    assert state_file.is_file()
    assert wstate.load_window_geometry(state_file) == wstate.WindowGeometry(
        980, 640, 15, 25
    )


def test_desktop_window_keeps_previous_state_for_degenerate_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root, state_file = _open_desktop_app_with_state(tmp_path, monkeypatch)
    try:
        assert wstate.save_window_geometry(
            state_file, wstate.WindowGeometry(980, 640, 15, 25)
        )
        # A degenerate geometry query (e.g. a window that was never mapped)
        # must not overwrite the good stored geometry.
        monkeypatch.setattr(root, "geometry", lambda value=None: "1x1+0+0")
        app._save_window_state()
        assert wstate.load_window_geometry(state_file) == wstate.WindowGeometry(
            980, 640, 15, 25
        )
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(Exception):
            root.destroy()


# ---------------------------------------------------------------------------
# Keyboard shortcuts (window, guarded: needs a display)
# ---------------------------------------------------------------------------
def test_keyboard_shortcuts_are_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk = pytest.importorskip("tkinter")
    import novel_downloader.apps.desktop.ui as ui_mod

    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    monkeypatch.setenv("NOVEL_MUSIC_HOME", str(tmp_path / "music"))

    # Spy on ``bind_all`` while keeping the real binding alive.
    registered: dict[str, Any] = {}
    original = tk.Tk.bind_all

    def spy(
        self: Any,
        sequence: str | None = None,
        func: Any = None,
        add: Any = None,
    ) -> str:
        if sequence:
            registered[sequence] = func
        return original(self, sequence, func, add)

    monkeypatch.setattr(tk.Tk, "bind_all", spy)

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environments
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = ui_mod.DesktopApp(root)
    try:
        expected = {f"<Control-{index}>" for index in range(1, 6)} | {
            "<Control-f>",
            "<Control-F>",
            "<Control-d>",
            "<Control-D>",
        }
        assert expected <= set(registered)
        assert all(callable(registered[sequence]) for sequence in expected)

        notebook = app.history_tab.master

        # Ctrl+N raises the Nth tab (Search=1 ... Follow=5).
        registered["<Control-1>"](SimpleNamespace())
        assert notebook.select() == str(app.search_tab)
        registered["<Control-3>"](SimpleNamespace())
        assert notebook.select() == str(app.history_tab)
        registered["<Control-5>"](SimpleNamespace())
        assert notebook.select() == str(app.follow_tab)

        # Ctrl+F focuses the search keyword entry.
        assert app.search_entry is not None
        focused: list[int] = []
        monkeypatch.setattr(app.search_entry, "focus_set", lambda: focused.append(1))
        registered["<Control-f>"](SimpleNamespace())
        registered["<Control-F>"](SimpleNamespace())
        assert focused == [1, 1]

        # Ctrl+D queues the search selection, like the toolbar button.
        queued: list[int] = []
        monkeypatch.setattr(app, "_add_selected_results", lambda: queued.append(1))
        registered["<Control-d>"](SimpleNamespace())
        registered["<Control-D>"](SimpleNamespace())
        assert queued == [1, 1]
    finally:
        app._closing = True
        with contextlib.suppress(Exception):
            app.engine.shutdown()
        with contextlib.suppress(tk.TclError):
            root.destroy()
