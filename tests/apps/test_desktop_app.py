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
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.common.history_files import FileItem
from novel_downloader.apps.core.models import DownloadTask, Status
from novel_downloader.apps.desktop import formatting as fmt
from novel_downloader.apps.desktop import main as desktop_main
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
        assert str(app._status_var.get())  # status bar shows something
        assert root.title() == t("Novel Downloader")
    finally:
        if app is not None:
            app._closing = True
        with contextlib.suppress(tk.TclError):
            root.destroy()
