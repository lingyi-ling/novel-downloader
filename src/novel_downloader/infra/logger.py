#!/usr/bin/env python3
"""
novel_downloader.infra.logger
-----------------------------

Provides a configurable logging setup for Python applications.
"""

from __future__ import annotations

__all__ = ["setup_logging", "FILE_LOG_FORMAT", "CONSOLE_LOG_FORMAT"]

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from novel_downloader.infra.paths import PACKAGE_NAME

#: Canonical file-record format shared by every entry point that adopts this
#: module. Kept as a module constant so the CLI/API/desktop front ends can
#: converge on one format instead of re-declaring their own.
FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s: %(message)s"

#: Canonical console-record format (short timestamp, no function name).
CONSOLE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_MUTE_LOGGERS: set[str] = {
    "fontTools.ttLib.tables._p_o_s_t",
}


def _normalize_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        # Level names may arrive padded (``general.debug.log_level`` values
        # are used verbatim from the config file).
        return logging._nameToLevel.get(level.strip().upper(), logging.INFO)
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
    """
    Create and configure a package logger with optional console and file handlers.

    :param log_dir: Directory where log files will be saved.
    :param log_filename: Base log file name (without date suffix).
    :param console_level: Minimum level for the console handler (string or int).
    :param file_level: Minimum level for the file handler (string or int).
    :param console: Add a console handler.
    :param file: Add a file handler.
    :param backup_count: How many rotated files to keep.
    :param when: Rotation interval for TimedRotatingFileHandler (e.g., "midnight").
    :return: The configured logger.
    """
    # Tame noisy third-party loggers
    for name in _MUTE_LOGGERS:
        ml = logging.getLogger(name)
        ml.setLevel(logging.ERROR)
        ml.propagate = False

    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # otherwise may affected by PaddleOCR

    # Replace handlers from a previous call. Close them first: a file handler
    # keeps the descriptor (and a lock on Windows) until closed, so merely
    # clearing the list would leak handles on repeated initialization.
    for handler in logger.handlers[:]:
        close_handler = getattr(handler, "close", None)
        if close_handler is not None:
            close_handler()
    logger.handlers.clear()

    # File handler (rotates daily)
    if file:
        file_level = _normalize_level(file_level)

        # ``~`` is commonly used in ``debug.log_dir``; strip surrounding
        # whitespace and resolve it here so the public helper behaves like the
        # config loader (which expands too).
        base_dir = Path(str(log_dir).strip()).expanduser()
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
            fmt=FILE_LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(file_formatter)
        fh.setLevel(file_level)
        logger.addHandler(fh)

        # Diagnostics belong on stderr so stdout stays machine readable
        # (``doctor --json`` / ``storage --json`` pipelines).
        print(f"Logging to {log_path}", file=sys.stderr)

    # Console handler
    if console:
        console_level = _normalize_level(console_level)

        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            fmt=CONSOLE_LOG_FORMAT,
            datefmt="%H:%M:%S",
        )
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(console_level)
        logger.addHandler(console_handler)

    return logger
