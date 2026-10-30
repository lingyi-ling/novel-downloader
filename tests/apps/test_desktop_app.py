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

import contextlib
import os
import queue
import time
from collections.abc import Callable
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
from novel_downloader.apps.desktop import preferences as prefs
from novel_downloader.apps.desktop.engine import AsyncEngine
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
# Window smoke test (guarded: needs a display)
# ---------------------------------------------------------------------------
def test_desktop_window_builds_all_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tk = pytest.importorskip("tkinter")
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))

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
            "music_health_item",
            "music_health_done",
        }:
            app._handle_event(kind, payload)
    return events


def test_follow_check_updates_entries_and_reenables_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novel_downloader.apps.desktop.ui as ui_mod

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

        monkeypatch.setattr(ui_mod, "search", fake_search)
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
