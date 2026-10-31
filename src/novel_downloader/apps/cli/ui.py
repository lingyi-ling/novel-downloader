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
  * ProgressUI
  * setup_logging
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Sequence
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.status import Status

from novel_downloader.infra.i18n import t
from novel_downloader.infra.paths import PACKAGE_NAME

_MUTE_LOGGERS: set[str] = {
    "fontTools.ttLib.tables._p_o_s_t",
}
# Diagnostics (tables, spinners, warnings) go to stderr so that data printed on
# stdout -- e.g. ``search --json`` -- stays machine readable.
_CONSOLE = Console(stderr=True)


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
    def __init__(self, prefix: str = t("Progress"), unit: str = t("item")):
        from rich.progress import Progress, TaskID

        self._progress = Progress(console=_CONSOLE)
        self._task_id: TaskID | None = None
        self._prefix = prefix
        self._unit = unit

    def start(self) -> None:
        self._progress.start()

    def stop(self) -> None:
        self._progress.stop()

    async def update(self, done: int, total: int) -> None:
        if self._task_id is None:
            self._task_id = self._progress.add_task(
                f"[cyan]{escape(self._prefix)}[/]", total=max(1, total)
            )
        self._progress.update(
            self._task_id,
            completed=done,
            total=max(1, total),
            # Rich parses markup in progress descriptions too, so a stage
            # name containing "[...]" must be escaped.
            description=f"{escape(self._prefix)} ({done}/{total} {self._unit})",
        )

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
