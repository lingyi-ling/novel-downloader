#!/usr/bin/env python3
"""
novel_downloader.apps.cli.ui
----------------------------

A small set of Rich-based helpers to keep CLI presentation and prompts
consistent across subcommands.

Public API:
  * emit, info, success, warn, error, status
  * confirm
  * prompt, prompt_password, prompt_choice
  * render_table
  * ProgressUI, progress_active
  * PROGRESS_DYNAMIC, PROGRESS_TEXT, PROGRESS_FALLBACK
  * setup_logging

Progress output contract
------------------------
Progress needs no flag and no configuration: :class:`ProgressUI` picks one of
three states in :meth:`ProgressUI.start` and reports it through
:attr:`ProgressUI.mode`.

``dynamic`` (:data:`PROGRESS_DYNAMIC`)
    ``sys.stdout`` *and* the stream behind :data:`_CONSOLE` (stderr) are both
    terminals. A Rich ``Progress`` bar redraws in place. While the bar owns
    the screen, :func:`progress_active` raises every **console** log handler
    to ``ERROR`` so a log line cannot tear the live region. Nothing is
    dropped: the ``DEBUG``/``INFO``/``WARNING`` records that raise would have
    swallowed are kept in a bounded buffer (:data:`_REPLAY_MAX` records, the
    overflow counted) and replayed through the *same* handler -- in that
    handler's own format -- once the bar is gone, under a one-line
    ``--- Logs during progress (N) ---`` header. ``ERROR`` and above never
    enter the buffer: the raise did not hide them, so replaying them would
    print them twice. File handlers are untouched, so every record still
    lands in the log file.

``text`` (:data:`PROGRESS_TEXT`)
    stdout or the console stream is not a TTY: redirected into a file, piped
    to ``less``/``tee``, a CI job, or a ``--json`` pipeline. No live region
    and no ANSI escape sequences are produced. One plain line is written per
    10% milestone, bracketed by a start line and a closing line -- so a small
    download that never crosses a milestone prints exactly two lines. Console
    logging is *not* arbitrated here: nothing is redrawing the screen, and a
    CI transcript is more useful with its ``WARNING`` lines in it.

``fallback`` (:data:`PROGRESS_FALLBACK`)
    Same plain-text output as ``text``. Selected when ``dynamic`` was
    expected but Rich could not build, start or render the bar (unsupported
    terminal, output redirected mid-run, a second live display already
    active, ...). The failure is swallowed: progress rendering must never
    fail the download it reports on.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, TaskID
from rich.prompt import Confirm, Prompt
from rich.status import Status

from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import PACKAGE_NAME

#: A live bar owns the screen and redraws it in place.
PROGRESS_DYNAMIC = "dynamic"
#: No TTY: plain milestone lines, no ANSI, no live region.
PROGRESS_TEXT = "text"
#: The bar failed to start/render; identical to :data:`PROGRESS_TEXT`.
PROGRESS_FALLBACK = "fallback"

#: Percent step between milestone lines in text mode. 0% is covered by the
#: start line and 100% by the closing line, hence "10" here.
_TEXT_STEP_PERCENT = 10

_MUTE_LOGGERS: set[str] = {
    "fontTools.ttLib.tables._p_o_s_t",
}
# Diagnostics (tables, spinners, warnings) go to stderr so that data printed on
# stdout -- e.g. ``search --json`` -- stays machine readable.
_CONSOLE = Console(stderr=True)


def _stream_is_tty(stream: object) -> bool:
    """Return ``stream.isatty()`` without trusting the stream to behave."""
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        # Closed/partially torn down stream: treat it as "not a terminal".
        return False


def _select_progress_mode() -> str:
    """
    Decide the progress output mode for a display that is about to start.

    Both sinks matter: the bar is drawn on :data:`_CONSOLE` (stderr), but the
    machine-readable stream the CLI contract talks about is stdout. If either
    one is redirected the caller wants an ANSI-free transcript, so ``text``.

    :return: :data:`PROGRESS_DYNAMIC` or :data:`PROGRESS_TEXT`.
    """
    if not _stream_is_tty(sys.stdout):
        return PROGRESS_TEXT
    try:
        if not _CONSOLE.is_terminal:
            return PROGRESS_TEXT
    except Exception:  # pragma: no cover - is_terminal is total in practice
        return PROGRESS_TEXT
    return PROGRESS_DYNAMIC


# ---------------------------------------------------------------------------
# console-log arbitration while a live progress region owns the screen
# ---------------------------------------------------------------------------

#: One console handler: ``(handler, original level, muting level)``.
_ConsoleTarget = tuple[logging.Handler, int, int]
#: What the outermost scope hands to the replay: the buffered records, how many
#: overflowed, and the handlers they were hidden from.
_ReplayPlan = tuple[list[logging.LogRecord], int, list[_ConsoleTarget]]

#: Guards the counter and the level switch below. Re-entrant because a bar may
#: be started from a callback that already runs inside another bar's scope.
_ARBITRATION_LOCK = threading.RLock()
#: How many ``progress_active()`` scopes are open right now.
_ARBITRATION_DEPTH = 0
#: Handlers muted by the outermost scope, restored (and replayed through) when
#: it ends.
_ARBITRATION_RESTORE: list[_ConsoleTarget] = []
#: Buffer for the records the mute hides, or ``None`` when nothing is muted.
_ARBITRATION_BUFFER: _ReplayBuffer | None = None
#: Recorder handler feeding :data:`_ARBITRATION_BUFFER`, or ``None``.
_ARBITRATION_RECORDER: _ReplayRecorder | None = None
#: Upper bound on replayed records. A bar can run for a long download, so the
#: buffer must not grow without limit; the overflow is counted, not silently
#: swallowed.
_REPLAY_MAX = 500


def _package_logger() -> logging.Logger:
    """The logger :func:`setup_logging` configures."""
    return logging.getLogger(PACKAGE_NAME)


def _is_console_handler(handler: logging.Handler) -> bool:
    """
    Whether ``handler`` writes to the terminal.

    ``logging.FileHandler`` (and subclasses such as the rotating handler from
    :func:`setup_logging`) is never a console handler; leaving its level alone
    is what keeps ``WARNING``/``ERROR`` records in the log file while a bar is
    active. ``rich.logging.RichHandler`` is a plain ``Handler`` that owns a
    :class:`~rich.console.Console`, hence the second test.
    """
    if isinstance(handler, logging.FileHandler):
        return False
    if isinstance(handler, logging.StreamHandler):
        return True
    return isinstance(getattr(handler, "console", None), Console)


class _ReplayBuffer:
    """Bounded, thread-safe store for the console records a bar hides."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._records: list[logging.LogRecord] = []
        self._dropped = 0

    def add(self, record: logging.LogRecord) -> None:
        """Keep a copy of ``record``, or count it as dropped when full."""
        # Copy now: a LogRecord is mutable and the handlers after the recorder
        # may still touch ``exc_text``/``args`` before the bar goes away.
        snapshot = logging.makeLogRecord(record.__dict__)
        with self._lock:
            if len(self._records) >= self._limit:
                self._dropped += 1
                return
            self._records.append(snapshot)

    def drain(self) -> tuple[list[logging.LogRecord], int]:
        """Take everything buffered so far, plus how many were dropped."""
        with self._lock:
            records, dropped = self._records, self._dropped
            self._records, self._dropped = [], 0
            return records, dropped


class _ReplayRecorder(logging.Handler):
    """Collects the records a muted console handler would have displayed."""

    def __init__(self, level: int, buffer: _ReplayBuffer) -> None:
        super().__init__(level)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        # ``ERROR`` and above reach the muted handler normally: buffering them
        # would print them a second time on replay.
        if record.levelno >= logging.ERROR:
            return
        try:
            self._buffer.add(record)
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)


def _mute_console_handlers() -> None:
    """
    Raise every console handler to ``ERROR`` and start recording the loss.

    A recorder is only installed when at least one handler actually gets
    stricter; when nothing would be hidden there is nothing to replay.
    """
    global _ARBITRATION_BUFFER, _ARBITRATION_RECORDER

    muted = [
        (handler, handler.level, max(handler.level, logging.ERROR))
        for handler in _package_logger().handlers
        if _is_console_handler(handler)
    ]
    for handler, _original, level in muted:
        handler.setLevel(level)
    _ARBITRATION_RESTORE.extend(muted)

    buffer_levels = [original for _, original, level in muted if level > original]
    if not buffer_levels:
        return
    buffer = _ReplayBuffer(_REPLAY_MAX)
    recorder = _ReplayRecorder(min(buffer_levels), buffer)
    _package_logger().addHandler(recorder)
    _ARBITRATION_BUFFER = buffer
    _ARBITRATION_RECORDER = recorder


def _unmute_console_handlers() -> _ReplayPlan:
    """
    Restore the recorded levels and detach the recorder.

    :return: the replay plan -- the buffered records, how many overflowed the
        buffer, and the handlers they should be replayed through. The caller
        replays *outside* the arbitration lock, because a handler may do
        arbitrary (and slow) I/O.
    """
    global _ARBITRATION_BUFFER, _ARBITRATION_RECORDER

    recorder, _ARBITRATION_RECORDER = _ARBITRATION_RECORDER, None
    buffer, _ARBITRATION_BUFFER = _ARBITRATION_BUFFER, None
    targets = list(_ARBITRATION_RESTORE)
    _ARBITRATION_RESTORE.clear()

    if recorder is not None:
        _package_logger().removeHandler(recorder)
        with suppress(Exception):
            recorder.close()
    for handler, original, _muted in targets:
        handler.setLevel(original)

    if buffer is None:
        return [], 0, targets
    records, dropped = buffer.drain()
    return records, dropped, targets


def _replay_header(count: int, dropped: int) -> str:
    """One-line banner separating replayed logs from the progress output."""
    header = t("--- Logs during progress ({count}) ---").format(count=count)
    if dropped:
        header = f"{header} " + t("({count} more dropped)").format(count=dropped)
    return header


def _handler_sink(handler: logging.Handler) -> object | None:
    """The stream/console ``handler`` writes to, if it can be identified."""
    console = getattr(handler, "console", None)
    if isinstance(console, Console):
        return console
    return getattr(handler, "stream", None)


def _write_to_handler_stream(handler: logging.Handler, text: str) -> None:
    """Write ``text`` to whatever stream ``handler`` owns."""
    with suppress(Exception):
        console = getattr(handler, "console", None)
        if isinstance(console, Console):
            console.print(escape(text), highlight=False, soft_wrap=True)
            return
        stream = getattr(handler, "stream", None)
        if stream is not None:
            stream.write(text + "\n")
            stream.flush()


def _replay_console_logs(plan: _ReplayPlan) -> None:
    """
    Re-emit the buffered records through the handlers that hid them.

    ``Handler.handle`` is used instead of ``emit`` so each handler applies its
    own formatter -- a Rich handler keeps its timestamp/level styling and a
    plain handler keeps its format string. The level was restored before this
    call (and ``handle`` never re-checks it anyway), so a record cannot be
    filtered out a second time.

    One header is written per distinct target sink. Two handlers may share a
    stream (a Rich handler and a plain one on the same console), and a header
    per record would be noise.
    """
    records, dropped, targets = plan
    if not records:
        return
    headed: set[int] = set()
    for handler, original, muted in targets:
        if muted <= original:
            # This handler was never silenced, so it saw the record live.
            continue
        visible = [record for record in records if record.levelno >= original]
        if not visible:
            continue
        sink = _handler_sink(handler)
        if id(sink) not in headed:
            headed.add(id(sink))
            _write_to_handler_stream(handler, _replay_header(len(visible), dropped))
        for record in visible:
            # A closed/broken console must not break the shutdown.
            with suppress(Exception):
                handler.handle(record)


@contextmanager
def progress_active() -> Iterator[None]:
    """
    Hand the console over to a live progress region for the scope's duration.

    Console log handlers are raised to ``ERROR`` so log output cannot tear the
    redrawn region; handlers writing to a file keep their level and therefore
    keep recording everything. What the raise hides from the console is not
    thrown away: it is buffered and replayed through the same handlers when the
    outermost scope exits, so a ``WARNING`` logged mid-download still reaches
    the terminal -- just after the bar instead of through it.

    Nesting is reference counted: the levels are muted and the recorder
    attached on the outermost entry, and released (with the replay) once, when
    the outermost scope exits -- including on the exception path. A lock makes
    the counter, the level switch and the buffer safe for worker threads that
    log while a bar is running; the replay itself runs outside that lock.

    Example::

        with progress_active():
            run_work()          # console logging is quiet here, then replayed
    """
    global _ARBITRATION_DEPTH

    with _ARBITRATION_LOCK:
        if _ARBITRATION_DEPTH == 0:
            _mute_console_handlers()
        _ARBITRATION_DEPTH += 1
    try:
        yield
    finally:
        replay: _ReplayPlan | None = None
        with _ARBITRATION_LOCK:
            if _ARBITRATION_DEPTH > 0:
                _ARBITRATION_DEPTH -= 1
            if _ARBITRATION_DEPTH == 0:
                replay = _unmute_console_handlers()
        # Outside the lock: a handler may block on I/O for a long time.
        if replay is not None:
            _replay_console_logs(replay)


def _percent(done: int, total: int) -> int:
    """Clamped integer percentage, never dividing by zero."""
    total = max(1, total)
    return min(100, max(0, done * 100 // total))


def _write_plain(markup_text: str) -> None:
    """
    Write one already-escaped line with no style and no wrapping.

    ``soft_wrap`` keeps CI log lines intact instead of folding them at the
    console width; an empty style means no ANSI escape sequences are emitted
    even if the console was built with ``force_terminal=True``.
    """
    # A closed or redirected stream at shutdown must not break the job.
    with suppress(OSError, ValueError):
        _CONSOLE.print(markup_text, highlight=False, soft_wrap=True)


def emit(text: str) -> None:
    """Write machine-readable data to stdout, untouched by rich formatting."""
    print(text)


def info(message: str) -> None:
    """Print a neutral informational message."""
    _CONSOLE.print(escape(message))


def success(message: str) -> None:
    """Print a success message in a friendly color."""
    _CONSOLE.print(escape(message), style="green")


def warn(message: str) -> None:
    """Print a warning message."""
    _CONSOLE.print(escape(message), style="yellow")


def error(message: str) -> None:
    """Print an error message."""
    _CONSOLE.print(escape(message), style="red")


def status(message: str) -> Status:
    """Context manager to show a spinner with a message."""
    return _CONSOLE.status(f"[bold cyan]{escape(message)}[/]", spinner="dots")


def confirm(message: str, *, default: bool = False) -> bool:
    """
    Ask a yes/no question.

    :param message: The question to display (without [y/N] suffix).
    :param default: Default choice (pressing Enter = Yes if True, No if False).
    :return: True if user confirms (Yes), otherwise False.

    ``KeyboardInterrupt`` is deliberately NOT caught: the caller (``cli_main``)
    turns it into the documented "Aborted." exit. Only a closed stdin
    (``EOFError``) is treated as "no".
    """
    try:
        result: bool = Confirm.ask(
            f"[bold]{escape(message)}[/bold]", default=default, console=_CONSOLE
        )
        return result
    except EOFError:
        warn(t("Cancelled."))
        return False


def prompt(message: str, *, default: str | None = None) -> str:
    """
    Prompt user for a line of text.

    :param message: Prompt message.
    :param default: Default value if the user presses Enter.
    :return: The user's input (the default when stdin is closed).

    ``KeyboardInterrupt`` propagates so the CLI can abort with exit code 1.
    """
    try:
        result: str = Prompt.ask(
            escape(message),
            default=default or "",
            show_default=False,
            console=_CONSOLE,
        )
        return result
    except EOFError:
        warn(t("Cancelled."))
        return default or ""


def prompt_password(message: str) -> str:
    """
    Prompt user for a password/secret value (no echo).

    :param message: Prompt message.
    :return: The user's input (may be empty).

    ``KeyboardInterrupt`` propagates so the CLI can abort with exit code 1.
    """
    try:
        result: str = Prompt.ask(escape(message), password=True, console=_CONSOLE)
        return result
    except EOFError:
        warn(t("Cancelled."))
        return ""


def render_table(
    title: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[str]],
) -> None:
    """
    Render a simple full-width table.

    :param title: Table title.
    :param columns: Column names.
    :param rows: Row data; each row must have the same length as `columns`.
    """
    from rich.table import Table

    # The title (e.g. "Available Books for <site>") and column headers may
    # contain user data; escape them like the cell values below.
    table = Table(title=escape(title), show_lines=True, expand=True)
    for col in columns:
        table.add_column(escape(col), overflow="fold")
    # Cell values come from remote/user data (book titles, rule names, ...);
    # escape them so a stray "[...]" is printed literally instead of being
    # parsed as rich markup.
    for row in rows:
        table.add_row(*[escape(str(x)) for x in row])
    _CONSOLE.print(table)


def prompt_choice(prompt_text: str, choices: Sequence[str]) -> str:
    """
    Prompt user to select one of several choices.

    :param prompt_text: Prompt message shown to the user.
    :param choices: Valid choices (strings). Empty string is treated as cancel.
    :return: The raw user input, lowercased and trimmed. Returns "" when
        stdin is closed (EOF); ``KeyboardInterrupt`` propagates so the CLI
        can abort with exit code 1.
    """
    try:
        resp: str = Prompt.ask(
            escape(prompt_text),
            choices=list(choices) + [""],
            case_sensitive=False,
            show_choices=False,
            default="",
            show_default=False,
            console=_CONSOLE,
        )
        return resp.strip().lower()
    except EOFError:
        # Non-interactive stdin (a pipe, a closed terminal, ...): treat as
        # "cancel" instead of letting the exception escape.
        return ""


class ProgressUI:
    """
    A progress display that never breaks the command it reports on.

    Callers keep using :meth:`start`, :meth:`update`/``update_sync`` and
    :meth:`stop`; the output mode (see the module docstring) is chosen in
    :meth:`start` and exposed by :attr:`mode`.
    """

    def __init__(self, prefix: str = t("Progress"), unit: str = t("item")):
        self._prefix = prefix
        self._unit = unit
        # Built on start(), not here: the mode is a property of the moment the
        # bar is shown, and a failed construction must degrade, not raise.
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._mode: str = PROGRESS_TEXT
        self._started = False
        #: Owns the ``progress_active()`` scope while the bar is live.
        self._stack: ExitStack | None = None
        #: Serialises milestone bookkeeping between worker threads.
        self._text_lock = threading.Lock()
        self._text_bucket = 0
        self._last_done = 0
        self._last_total = 0

    @property
    def mode(self) -> str:
        """``dynamic``, ``text`` or ``fallback`` (see the module docstring)."""
        return self._mode

    def start(self) -> None:
        """Show the bar, or fall back to milestone lines."""
        if self._started:
            return
        self._started = True
        self._mode = _select_progress_mode()
        if self._mode == PROGRESS_DYNAMIC:
            if self._start_dynamic():
                return
            # Rich could not take the screen: same output as non-TTY.
            self._mode = PROGRESS_FALLBACK
        _write_plain(self._start_line())

    def _start_dynamic(self) -> bool:
        """Try to own the screen with a rich bar; False means "use text"."""
        stack = ExitStack()
        progress: Progress | None = None
        try:
            # Mute console logging first: the bar is about to start redrawing,
            # and a log line printed in between would be torn.
            stack.enter_context(progress_active())
            progress = Progress(console=_CONSOLE)
            progress.start()
        except Exception:
            # Unsupported terminal, output redirected mid-run, a second live
            # display already active, ...: degrade instead of crashing.
            if progress is not None:
                with suppress(Exception):
                    progress.stop()
            stack.close()
            return False
        self._stack = stack
        self._progress = progress
        return True

    def stop(self) -> None:
        """Tear the bar (or emit the closing line) and release the console."""
        if not self._started:
            return
        self._started = False
        progress, self._progress = self._progress, None
        self._task_id = None
        if progress is not None:
            # A bar that already failed must not fail the shutdown as well.
            with suppress(Exception):
                progress.stop()
        self._shutdown_stack()
        # Read the counters before clearing them: the closing line reports the
        # last update of *this* run.
        if self._mode != PROGRESS_DYNAMIC and self._last_total > 0:
            _write_plain(self._final_line())
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        """
        Forget per-run bookkeeping so the instance can be ``start()``ed again.

        A stale ``_task_id`` would point into a ``Progress`` that no longer
        exists (Rich raises, and the bar degrades to text for no reason), and a
        stale milestone bucket would suppress the next run's milestone lines.
        """
        with self._text_lock:
            self._text_bucket = 0
            self._last_done = 0
            self._last_total = 0

    def _shutdown_stack(self) -> None:
        """Exit the arbitration scope; restores console log levels once."""
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()

    def _degrade(self) -> None:
        """Rendering failed mid-run: drop the bar, continue as text."""
        self._mode = PROGRESS_FALLBACK
        progress, self._progress = self._progress, None
        self._task_id = None
        if progress is not None:
            with suppress(Exception):
                progress.stop()
        self._shutdown_stack()

    async def update(self, done: int, total: int) -> None:
        if not self._started:
            return
        total = max(1, total)
        progress = self._progress
        if progress is not None:
            try:
                if self._task_id is None:
                    self._task_id = progress.add_task(
                        f"[cyan]{escape(self._prefix)}[/]", total=total
                    )
                progress.update(
                    self._task_id,
                    completed=done,
                    total=total,
                    # Rich parses markup in progress descriptions too, so a
                    # stage name containing "[...]" must be escaped.
                    description=(
                        f"{escape(self._prefix)} ({done}/{total} {escape(self._unit)})"
                    ),
                )
                self._remember(done, total)
                return
            except Exception:
                # Terminal/rendering trouble must never break the download.
                self._degrade()
        self._emit_milestone(done, total)

    # -- text mode ---------------------------------------------------------
    def _remember(self, done: int, total: int) -> None:
        with self._text_lock:
            self._last_done = done
            self._last_total = total

    def _emit_milestone(self, done: int, total: int) -> None:
        """Write one line per 10% crossed, in both text and fallback mode."""
        total = max(1, total)
        percent = _percent(done, total)
        with self._text_lock:
            self._last_done = done
            self._last_total = total
            bucket = percent // _TEXT_STEP_PERCENT
            # 0% is the start line's job and 100% the closing line's, so a
            # download that finishes without crossing a milestone prints
            # exactly two lines.
            if bucket < 1 or bucket >= 100 // _TEXT_STEP_PERCENT:
                return
            if bucket <= self._text_bucket:
                return
            self._text_bucket = bucket
            _write_plain(self._progress_line(done, total, percent))

    def _start_line(self) -> str:
        return f"{escape(self._prefix)} ..."

    def _progress_line(self, done: int, total: int, percent: int) -> str:
        return (
            f"{escape(self._prefix)}: {done}/{total} {escape(self._unit)} ({percent}%)"
        )

    def _final_line(self) -> str:
        percent = _percent(self._last_done, self._last_total)
        return self._progress_line(self._last_done, self._last_total, percent)

    def update_sync(self, done: int, total: int) -> None:
        """Sync wrapper for update()."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If running in async context, schedule without blocking
            loop.create_task(self.update(done, total))
        else:
            asyncio.run(self.update(done, total))


def _normalize_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        return logging._nameToLevel.get(level.upper(), logging.INFO)
    return logging.INFO


def setup_logging(
    log_dir: str | Path = "./logs",
    log_filename: str | None = None,
    console_level: int | str = "INFO",
    file_level: int | str = "DEBUG",
    *,
    console: bool = True,
    file: bool = True,
    backup_count: int = 7,
    when: str = "midnight",
) -> logging.Logger:
    # Tame noisy third-party loggers
    for name in _MUTE_LOGGERS:
        ml = logging.getLogger(name)
        ml.setLevel(logging.ERROR)
        ml.propagate = False

    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # otherwise may affected by PaddleOCR

    # Clear existing handlers to avoid duplicate logs
    if logger.hasHandlers():
        logger.handlers.clear()

    # File handler (rotates daily)
    if file:
        file_level = _normalize_level(file_level)

        base_dir = Path(log_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        base_name = log_filename or PACKAGE_NAME
        log_path = base_dir / f"{base_name}.log"

        fh = TimedRotatingFileHandler(
            filename=log_path,
            when=when,
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False,
            delay=True,
        )

        file_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(file_formatter)
        fh.setLevel(file_level)
        logger.addHandler(fh)

        print(t("Logging to {log_path}").format(log_path=log_path), file=sys.stderr)

    # Console handler
    if console:
        from rich.logging import RichHandler

        console_level = _normalize_level(console_level)

        ch = RichHandler(
            console=_CONSOLE,
            rich_tracebacks=True,
            markup=True,
            show_time=True,
            show_path=False,
            log_time_format="%H:%M:%S",
        )
        ch.setFormatter(logging.Formatter("%(message)s"))
        ch.setLevel(console_level)
        logger.addHandler(ch)
    return logger
