#!/usr/bin/env python3
"""
tests.apps.test_cli_ui_progress
--------------------------------

Offline coverage for the progress/log arbitration in
:mod:`novel_downloader.apps.cli.ui`.

Three things are pinned here:

* the three-state output contract from the ``ui`` module docstring --
  ``dynamic`` (both streams are terminals), ``text`` (either stream is
  redirected) and ``fallback`` (Rich could not own the screen) -- including
  the promise that ``text``/``fallback`` output carries no ANSI escapes and
  no progress control characters;
* ``progress_active()``: console handlers are muted to ``ERROR``, nesting is
  reference counted, the levels are restored on the exception path, and file
  handlers keep recording ``WARNING``/``ERROR`` throughout. Records the mute
  hid from the console are *buffered and replayed* (never dropped) when the
  outermost scope exits;
* the per-instance ``ProgressUI`` state is reset by ``stop()`` so a reused
  instance starts a clean run, and threaded arbitration always returns to the
  initial level and detaches its recorder;
* a broken Rich bar never propagates: build, start and per-update render
  failures all degrade to plain text.

Nothing touches a real terminal: ``sys.stdout`` and ``ui._CONSOLE`` are both
pointed at in-memory buffers.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import rich.progress
from rich.console import Console
from rich.text import Text

from novel_downloader.apps.cli import ui
from novel_downloader.infra.paths import PACKAGE_NAME

#: Any ANSI escape sequence (CSI).
_ANSI = re.compile(r"\x1b\[")


class _TTYBuffer(io.StringIO):
    """In-memory stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _install_streams(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> io.StringIO:
    """
    Point ``sys.stdout`` and ``ui._CONSOLE`` at buffers.

    When ``tty`` is true both claim to be terminals (so ``dynamic`` is
    selected); otherwise both are plain, redirected streams (``text``).
    """
    buffer = _TTYBuffer() if tty else io.StringIO()
    monkeypatch.setattr(sys, "stdout", _TTYBuffer() if tty else io.StringIO())
    monkeypatch.setattr(
        ui,
        "_CONSOLE",
        Console(file=buffer, force_terminal=tty, width=120),
    )
    return buffer


@pytest.fixture(autouse=True)
def _reset_arbitration() -> Iterator[None]:
    """Keep one failing test from poisoning the module-level counter."""
    yield
    ui._ARBITRATION_DEPTH = 0
    ui._ARBITRATION_RESTORE.clear()
    ui._ARBITRATION_BUFFER = None
    ui._ARBITRATION_RECORDER = None


@pytest.fixture()
def package_logger() -> Iterator[logging.Logger]:
    """The logger ``setup_logging`` configures, emptied for one test."""
    logger = logging.getLogger(PACKAGE_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield logger
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def _run_updates(progress: ui.ProgressUI, total: int, step: int) -> None:
    for done in range(0, total + 1, step):
        asyncio.run(progress.update(done, total))


# --------------------------------------------------------------------------- #
# mode selection: dynamic / text
# --------------------------------------------------------------------------- #
def test_tty_uses_a_dynamic_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _install_streams(monkeypatch, tty=True)

    progress = ui.ProgressUI(prefix="Download progress", unit="chapters")
    progress.start()
    try:
        asyncio.run(progress.update(5, 10))
        asyncio.run(progress.update(10, 10))
    finally:
        progress.stop()

    output = buffer.getvalue()
    assert progress.mode == ui.PROGRESS_DYNAMIC
    assert _ANSI.search(output), "a live bar redraws with escape sequences"
    assert "100%" in output
    assert "chapters" in output


def test_non_tty_uses_plain_milestone_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    progress = ui.ProgressUI(prefix="Download progress", unit="chapters")
    progress.start()
    _run_updates(progress, 100, 5)
    progress.stop()

    output = buffer.getvalue()
    lines = output.splitlines()

    assert progress.mode == ui.PROGRESS_TEXT
    assert "Download progress ..." in lines, "start line must be present"
    # One line per 10% bucket, never one per item.
    for percent in range(10, 100, 10):
        assert any(f"({percent}%)" in line for line in lines), percent
    assert len(lines) == 1 + 9 + 1, "start, 10..90, and the closing line"
    assert "100/100 chapters (100%)" in output


def test_non_tty_output_has_no_ansi_or_carriage_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    _run_updates(progress, 100, 1)
    progress.stop()

    output = buffer.getvalue()
    assert not _ANSI.search(output), "CI/piped output must stay escape-free"
    assert "\r" not in output, "no in-place redraw outside a terminal"


def test_text_mode_small_download_prints_start_and_end_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    _run_updates(progress, 1, 1)  # only 100% is reached -> closing line only
    progress.stop()

    lines = buffer.getvalue().splitlines()
    assert lines[0] == "Stage ..."
    assert lines[-1] == "Stage: 1/1 chapters (100%)"
    assert len(lines) == 2, "no milestone is crossed -> exactly two lines"


def test_update_before_start_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    progress = ui.ProgressUI(prefix="Stage")
    asyncio.run(progress.update(1, 10))

    assert buffer.getvalue() == ""


def test_stop_without_start_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    ui.ProgressUI(prefix="Stage").stop()

    assert buffer.getvalue() == ""


def test_prefix_with_markup_is_rendered_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)

    progress = ui.ProgressUI(prefix="Stage [/b]", unit="chapters")
    progress.start()
    _run_updates(progress, 10, 10)
    progress.stop()

    output = buffer.getvalue()
    assert "Stage [/b]" in output
    assert "[/b]" in output.splitlines()[0]


# --------------------------------------------------------------------------- #
# fallback: rich fails to build / start / render
# --------------------------------------------------------------------------- #
def test_bar_start_failure_degrades_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=True)

    def exploding_start(self: rich.progress.Progress) -> None:
        raise RuntimeError("no live display for you")

    monkeypatch.setattr(rich.progress.Progress, "start", exploding_start)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()  # must not raise
    asyncio.run(progress.update(3, 10))
    progress.stop()

    output = buffer.getvalue()
    assert progress.mode == ui.PROGRESS_FALLBACK
    assert output.splitlines()[0] == "Stage ..."
    assert "Stage: 3/10 chapters (30%)" in output
    assert "Stage: 3/10 chapters (30%)" in output.splitlines()[-1]
    assert not _ANSI.search(output)


def test_bar_construction_failure_degrades_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=True)
    monkeypatch.setattr(ui, "Progress", _exploding_progress())

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    asyncio.run(progress.update(1, 1))
    progress.stop()

    output = buffer.getvalue()
    assert progress.mode == ui.PROGRESS_FALLBACK
    assert "Stage ..." in output
    assert "1/1 chapters (100%)" in output


def test_render_failure_mid_run_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=True)

    def exploding_update(
        self: rich.progress.Progress, *args: object, **kwargs: object
    ) -> None:
        raise RuntimeError("terminal went away")

    monkeypatch.setattr(rich.progress.Progress, "update", exploding_update)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    assert progress.mode == ui.PROGRESS_DYNAMIC
    asyncio.run(progress.update(4, 10))  # must not raise
    assert progress.mode == ui.PROGRESS_FALLBACK
    asyncio.run(progress.update(10, 10))
    progress.stop()

    output = buffer.getvalue()
    assert "Stage: 4/10 chapters (40%)" in output
    assert "Stage: 10/10 chapters (100%)" in output


def test_stop_is_idempotent_after_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = _install_streams(monkeypatch, tty=True)
    monkeypatch.setattr(rich.progress.Progress, "start", _raise_start)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    progress.stop()
    progress.stop()

    assert buffer.getvalue().count("Stage ...") == 1


def _raise_start(self: rich.progress.Progress) -> None:
    raise RuntimeError("boom")


def _exploding_progress() -> type[object]:
    class _Exploding:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("cannot build a bar here")

    return _Exploding


# --------------------------------------------------------------------------- #
# progress_active(): console-log arbitration
# --------------------------------------------------------------------------- #
def test_console_handler_is_muted_then_restored(
    package_logger: logging.Logger,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        assert handler.level == logging.ERROR

    assert handler.level == logging.INFO


def test_nested_progress_active_restores_level_only_once(
    package_logger: logging.Logger,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        with ui.progress_active():
            assert handler.level == logging.ERROR
        assert handler.level == logging.ERROR, "inner exit must not unmute"
        with ui.progress_active():
            assert handler.level == logging.ERROR
        assert handler.level == logging.ERROR

    assert handler.level == logging.INFO


def test_progress_active_restores_level_on_exception(
    package_logger: logging.Logger,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.DEBUG)
    package_logger.addHandler(handler)

    with pytest.raises(RuntimeError, match="boom"), ui.progress_active():
        assert handler.level == logging.ERROR
        raise RuntimeError("boom")

    assert handler.level == logging.DEBUG
    assert ui._ARBITRATION_DEPTH == 0


def test_file_handler_keeps_recording_warnings_during_progress(
    package_logger: logging.Logger, tmp_path: Path
) -> None:
    log_path = tmp_path / "progress.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    console_stream = io.StringIO()
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    package_logger.addHandler(file_handler)
    package_logger.addHandler(console_handler)

    try:
        with ui.progress_active():
            package_logger.warning("disk almost full")
            package_logger.error("chapter 42 failed")
    finally:
        file_handler.flush()

    written = log_path.read_text(encoding="utf-8")
    assert "disk almost full" in written
    assert "chapter 42 failed" in written
    # The console is the thing being protected: WARNING is held back while the
    # bar runs, then replayed -- it never disappears from the terminal either.
    console_out = console_stream.getvalue()
    assert "disk almost full" in console_out, "held-back warning must be replayed"
    assert "chapter 42 failed" in console_out
    assert console_out.count("chapter 42 failed") == 1, "ERROR is not duplicated"
    assert "--- Logs during progress (1) ---" in console_out
    assert console_handler.level == logging.INFO


def test_progress_active_does_not_install_a_file_handler(
    package_logger: logging.Logger,
) -> None:
    console_handler = logging.StreamHandler(io.StringIO())
    package_logger.addHandler(console_handler)
    before = list(package_logger.handlers)

    with ui.progress_active():
        pass

    assert list(package_logger.handlers) == before, "no behaviour change"


def test_muting_never_lowers_a_stricter_handler(
    package_logger: logging.Logger,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.CRITICAL)
    package_logger.addHandler(handler)

    with ui.progress_active():
        assert handler.level == logging.CRITICAL

    assert handler.level == logging.CRITICAL


def test_setup_logging_rich_handler_is_muted(
    package_logger: logging.Logger, tmp_path: Path
) -> None:
    ui.setup_logging(log_dir=tmp_path, console=True, file=False)
    rich_handler = package_logger.handlers[-1]
    rich_handler.setLevel(logging.INFO)

    with ui.progress_active():
        assert rich_handler.level == logging.ERROR

    assert rich_handler.level == logging.INFO


def test_file_only_logging_is_not_arbitrated(
    package_logger: logging.Logger, tmp_path: Path
) -> None:
    """``--json`` installs no console handler, so there is nothing to mute."""
    ui.setup_logging(log_dir=tmp_path, console=False, file=True)
    file_handler = package_logger.handlers[0]
    original_level = file_handler.level
    handlers_before = list(package_logger.handlers)

    with ui.progress_active():
        assert file_handler.level == original_level, "the file handler is untouched"
        assert list(package_logger.handlers) == handlers_before, "no recorder either"
        package_logger.warning("json mode keeps its warnings")

    assert package_logger.handlers == handlers_before
    assert original_level == file_handler.level
    file_handler.flush()
    log_path = next(tmp_path.glob("*.log"))
    assert "json mode keeps its warnings" in log_path.read_text(encoding="utf-8")


def test_progress_active_is_thread_safe() -> None:
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.INFO)
    logger = logging.getLogger(PACKAGE_NAME)
    logger.addHandler(handler)
    try:
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(50):
                    with ui.progress_active():
                        if handler.level < logging.ERROR:
                            raise AssertionError("handler was not muted")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert handler.level == logging.INFO
        assert ui._ARBITRATION_DEPTH == 0
    finally:
        logger.removeHandler(handler)


# --------------------------------------------------------------------------- #
# integration: a dynamic bar owns the console, text mode does not
# --------------------------------------------------------------------------- #
def test_dynamic_bar_mutes_console_logging_until_stop(
    monkeypatch: pytest.MonkeyPatch, package_logger: logging.Logger, tmp_path: Path
) -> None:
    _install_streams(monkeypatch, tty=True)
    log_path = tmp_path / "progress.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(io.StringIO())
    console_handler.setLevel(logging.INFO)
    package_logger.addHandler(file_handler)
    package_logger.addHandler(console_handler)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    try:
        assert progress.mode == ui.PROGRESS_DYNAMIC
        assert console_handler.level == logging.ERROR
        package_logger.warning("retrying chapter 7")
    finally:
        progress.stop()
        file_handler.flush()

    assert console_handler.level == logging.INFO
    assert "retrying chapter 7" in log_path.read_text(encoding="utf-8")


def test_text_mode_keeps_console_logging_visible(
    monkeypatch: pytest.MonkeyPatch, package_logger: logging.Logger
) -> None:
    _install_streams(monkeypatch, tty=False)
    console_stream = io.StringIO()
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    package_logger.addHandler(console_handler)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    try:
        assert progress.mode == ui.PROGRESS_TEXT
        assert console_handler.level == logging.INFO, "no live region to protect"
        package_logger.warning("keep me in the CI log")
    finally:
        progress.stop()

    assert "keep me in the CI log" in console_stream.getvalue()


# --------------------------------------------------------------------------- #
# progress_active(): held-back records are replayed, never dropped
# --------------------------------------------------------------------------- #
_HEADER = "--- Logs during progress"


def test_replay_uses_handler_formatting_so_lines_keep_their_shape(
    package_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("FMT|%(levelname)s|%(message)s"))
    package_logger.addHandler(handler)

    with ui.progress_active():
        assert "FMT|WARNING|careful" not in stream.getvalue(), "hidden while live"
        package_logger.warning("careful")

    out = stream.getvalue()
    assert "FMT|WARNING|careful" in out, "replayed through the original formatter"
    assert f"{_HEADER} (1) ---" in out


def test_replay_happens_on_the_exception_path(
    package_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with pytest.raises(RuntimeError, match="boom"), ui.progress_active():
        package_logger.warning("logged before the crash")
        raise RuntimeError("boom")

    assert "logged before the crash" in stream.getvalue()
    assert handler.level == logging.INFO
    assert ui._ARBITRATION_DEPTH == 0


def test_replay_is_silent_when_nothing_was_logged(
    package_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        package_logger.error("only an error, which passes straight through")

    out = stream.getvalue()
    assert "only an error" in out
    assert _HEADER not in out, "no header for an empty buffer"


def test_replay_waits_for_the_outermost_scope(
    package_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        with ui.progress_active():
            package_logger.warning("inner warning")
        assert "inner warning" not in stream.getvalue(), "inner exit must not replay"

    out = stream.getvalue()
    assert out.count("inner warning") == 1
    assert out.count(_HEADER) == 1, "one header per outermost scope"


def test_error_and_above_are_not_buffered_and_not_duplicated(
    package_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        package_logger.warning("held back")
        package_logger.error("live error")
        package_logger.critical("live critical")

    out = stream.getvalue()
    assert out.count("live error") == 1, "ERROR passed through once, not replayed"
    assert out.count("live critical") == 1
    assert out.count("held back") == 1
    assert f"{_HEADER} (1) ---" in out, "only the warning is in the replay"


def test_handler_that_was_never_muted_gets_no_replay(
    package_logger: logging.Logger,
) -> None:
    muted_stream = io.StringIO()
    muted = logging.StreamHandler(muted_stream)
    muted.setLevel(logging.INFO)
    strict_stream = io.StringIO()
    strict = logging.StreamHandler(strict_stream)
    strict.setLevel(logging.CRITICAL)
    package_logger.addHandler(muted)
    package_logger.addHandler(strict)

    with ui.progress_active():
        package_logger.warning("only for the INFO handler")

    assert "only for the INFO handler" in muted_stream.getvalue()
    assert strict_stream.getvalue() == "", "CRITICAL handler never showed INFO"


def test_replay_buffer_is_bounded_and_reports_the_overflow(
    package_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui, "_REPLAY_MAX", 3)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    package_logger.addHandler(handler)

    with ui.progress_active():
        for index in range(5):
            package_logger.warning("w%d", index)

    out = stream.getvalue()
    assert "w0" in out and "w2" in out, "the first records survive"
    assert "w3" not in out and "w4" not in out, "overflow is dropped by design"
    assert f"{_HEADER} (3) --- (2 more dropped)" in out


def test_replay_survives_a_broken_console_handler(
    package_logger: logging.Logger,
) -> None:
    class _BrokenHandler(logging.StreamHandler[io.StringIO]):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("console went away")

    broken = _BrokenHandler(io.StringIO())
    broken.setLevel(logging.INFO)
    package_logger.addHandler(broken)

    with ui.progress_active():
        assert broken.level == logging.ERROR, "it must look like a console handler"
        package_logger.warning("cannot be printed")
    # Reaching this line is the assertion: replay never re-raises.

    assert ui._ARBITRATION_DEPTH == 0
    assert broken.level == logging.INFO


def test_dynamic_bar_replays_through_rich_handler(
    monkeypatch: pytest.MonkeyPatch, package_logger: logging.Logger, tmp_path: Path
) -> None:
    buffer = _install_streams(monkeypatch, tty=True)
    ui.setup_logging(log_dir=tmp_path, console=True, file=False)
    rich_handler = package_logger.handlers[-1]
    rich_handler.setLevel(logging.INFO)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    try:
        assert progress.mode == ui.PROGRESS_DYNAMIC
        package_logger.warning("retrying chapter 7")
        assert "retrying chapter 7" not in buffer.getvalue(), "hidden while live"
    finally:
        progress.stop()

    out = buffer.getvalue()
    assert "retrying chapter 7" in Text.from_ansi(out).plain, (
        "the terminal still gets the warning"
    )
    assert f"{_HEADER} (1) ---" in out
    assert rich_handler.level == logging.INFO


# --------------------------------------------------------------------------- #
# mode matrix, stdout purity, instance reuse and thread-safety
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("stdout_tty", "stderr_tty", "expected"),
    [
        (True, True, ui.PROGRESS_DYNAMIC),
        (True, False, ui.PROGRESS_TEXT),
        (False, True, ui.PROGRESS_TEXT),
        (False, False, ui.PROGRESS_TEXT),
    ],
)
def test_mode_selection_matrix(
    monkeypatch: pytest.MonkeyPatch,
    stdout_tty: bool,
    stderr_tty: bool,
    expected: str,
) -> None:
    """Either stream redirected means an ANSI-free transcript."""
    monkeypatch.setattr(sys, "stdout", _TTYBuffer() if stdout_tty else io.StringIO())
    stderr_stream = _TTYBuffer() if stderr_tty else io.StringIO()
    monkeypatch.setattr(
        ui,
        "_CONSOLE",
        Console(file=stderr_stream, force_terminal=stderr_tty, width=100),
    )

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    try:
        assert ui._select_progress_mode() == expected
        assert progress.mode == expected
    finally:
        progress.stop()


@pytest.mark.parametrize("stdout_tty", [True, False])
def test_progress_never_pollutes_stdout(
    monkeypatch: pytest.MonkeyPatch, stdout_tty: bool
) -> None:
    """``--json`` writes to stdout; every progress byte is on stderr."""
    stdout = _TTYBuffer() if stdout_tty else io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        ui,
        "_CONSOLE",
        Console(file=stderr, force_terminal=stdout_tty, width=100),
    )

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    _run_updates(progress, 100, 5)
    progress.stop()
    ui.emit('{"ok": true}')

    assert stdout.getvalue() == '{"ok": true}\n', "stdout must stay parseable"
    assert progress.mode == (ui.PROGRESS_DYNAMIC if stdout_tty else ui.PROGRESS_TEXT)
    assert "Stage" in stderr.getvalue(), "progress belongs on stderr"
    if not stdout_tty:
        assert "Stage: 50/100 chapters (50%)" in stderr.getvalue()


def test_progress_ui_can_be_reused_after_stop_in_text_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)
    progress = ui.ProgressUI(prefix="Stage", unit="chapters")

    progress.start()
    asyncio.run(progress.update(5, 10))
    progress.stop()
    first_cycle = len(buffer.getvalue().splitlines())
    assert first_cycle == 3, "start, 50% milestone, closing line"

    progress.start()
    try:
        asyncio.run(progress.update(5, 10))
    finally:
        progress.stop()
    second_cycle = buffer.getvalue().splitlines()[first_cycle:]

    assert len(second_cycle) == 3, "the stale bucket must not swallow milestones"
    assert second_cycle[0] == "Stage ..."
    assert second_cycle[-1] == "Stage: 5/10 chapters (50%)"


def test_progress_ui_can_be_reused_after_stop_in_dynamic_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_streams(monkeypatch, tty=True)
    progress = ui.ProgressUI(prefix="Stage", unit="chapters")

    progress.start()
    asyncio.run(progress.update(5, 10))
    progress.stop()

    progress.start()
    try:
        assert progress.mode == ui.PROGRESS_DYNAMIC
        asyncio.run(progress.update(10, 10))
        # A stale TaskID from the dead Progress would raise here and silently
        # downgrade a real terminal to text.
        assert progress.mode == ui.PROGRESS_DYNAMIC
    finally:
        progress.stop()


def test_start_and_stop_are_idempotent_in_text_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)
    progress = ui.ProgressUI(prefix="Stage", unit="chapters")

    progress.start()
    progress.start()
    progress.stop()
    progress.stop()

    assert buffer.getvalue().splitlines() == ["Stage ..."]


def test_threaded_arbitration_returns_to_the_initial_state() -> None:
    console = io.StringIO()
    handler = logging.StreamHandler(console)
    handler.setLevel(logging.INFO)
    logger = logging.getLogger(PACKAGE_NAME)
    logger.addHandler(handler)
    try:
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(50):
                    with ui.progress_active():
                        if handler.level != logging.ERROR:
                            raise AssertionError("handler was not muted")
                        logger.warning("worker heartbeat")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert handler.level == logging.INFO, "level must be fully restored"
        assert ui._ARBITRATION_DEPTH == 0
        assert ui._ARBITRATION_BUFFER is None
        assert ui._ARBITRATION_RESTORE == []
        assert not any(
            isinstance(candidate, ui._ReplayRecorder) for candidate in logger.handlers
        ), "the recorder must be detached"
        assert "worker heartbeat" in console.getvalue()
    finally:
        logger.removeHandler(handler)


# --------------------------------------------------------------------------- #
# non-TTY output contract: how many lines for how many chapters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("chapters", "expected_lines"),
    [
        (1, 2),  # start + closing line, no milestone crossed
        (3, 4),  # start + 33% + 66% + closing line
        (100, 11),  # start + 10..90 + closing line
    ],
)
def test_text_mode_line_count_for_small_and_large_downloads(
    monkeypatch: pytest.MonkeyPatch, chapters: int, expected_lines: int
) -> None:
    buffer = _install_streams(monkeypatch, tty=False)
    progress = ui.ProgressUI(prefix="Download", unit="chapters")

    progress.start()
    for done in range(chapters + 1):
        asyncio.run(progress.update(done, chapters))
    progress.stop()

    lines = buffer.getvalue().splitlines()
    assert len(lines) == expected_lines
    assert all(line.strip() for line in lines), "no blank-line noise"
    assert lines[0] == "Download ..."
    assert lines[-1] == f"Download: {chapters}/{chapters} chapters (100%)"


def test_fallback_output_has_no_ansi_or_carriage_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = _install_streams(monkeypatch, tty=True)
    monkeypatch.setattr(rich.progress.Progress, "start", _raise_start)

    progress = ui.ProgressUI(prefix="Stage", unit="chapters")
    progress.start()
    try:
        for done in range(0, 11):
            asyncio.run(progress.update(done, 10))
    finally:
        progress.stop()

    output = buffer.getvalue()
    assert progress.mode == ui.PROGRESS_FALLBACK
    assert not _ANSI.search(output)
    assert "\r" not in output
