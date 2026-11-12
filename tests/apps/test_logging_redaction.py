"""Redaction tests for the CLI and desktop logging entry points.

Both front ends used to configure plain :class:`logging.Formatter` instances,
so a URL with embedded credentials, an ``Authorization`` header or an
exception message could reach the log file (or the terminal) in clear text.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from rich.console import Console

from novel_downloader.apps.cli import ui
from novel_downloader.apps.desktop import main as desktop_main
from novel_downloader.infra.logger import RedactingFormatter
from novel_downloader.infra.paths import PACKAGE_NAME


@pytest.fixture()
def isolated_loggers() -> Iterator[None]:
    """Give each test an empty package logger."""
    package_logger = logging.getLogger(PACKAGE_NAME)
    saved_handlers = package_logger.handlers[:]
    saved_level = package_logger.level
    saved_propagate = package_logger.propagate

    for handler in package_logger.handlers[:]:
        with suppress(Exception):
            handler.close()
    package_logger.handlers.clear()
    package_logger.setLevel(logging.DEBUG)
    package_logger.propagate = False

    try:
        yield
    finally:
        for handler in package_logger.handlers[:]:
            with suppress(Exception):
                handler.close()
        package_logger.handlers.clear()
        package_logger.handlers.extend(saved_handlers)
        package_logger.setLevel(saved_level)
        package_logger.propagate = saved_propagate


@contextmanager
def _empty_root() -> Iterator[logging.Logger]:
    """Temporarily detach every root handler (pytest keeps its own there)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            if handler not in saved_handlers:
                with suppress(Exception):
                    handler.close()
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        root.setLevel(saved_level)


def _flush(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.flush()


def test_cli_file_log_is_redacted(isolated_loggers: None, tmp_path: Path) -> None:
    with _empty_root():
        logger = ui.setup_logging(log_dir=tmp_path, console=False, file=True)
        logger.info("GET https://alice:s3cret@example.test/books?token=abc123&page=2")
        logger.warning("Authorization: Bearer topsecret")
        _flush(logger)

    text = (tmp_path / f"{PACKAGE_NAME}.log").read_text(encoding="utf-8")

    assert "s3cret" not in text
    assert "abc123" not in text
    assert "topsecret" not in text
    # The useful part of the record survives.
    assert "INFO" in text
    assert "GET https://***@example.test/books?token=***&page=2" in text
    assert "Authorization: ***" in text


def test_cli_console_is_redacted(
    isolated_loggers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(
        ui, "_CONSOLE", Console(file=stream, force_terminal=False, color_system=None)
    )

    with _empty_root():
        logger = ui.setup_logging(
            log_dir=tmp_path, console=True, file=False, console_level="DEBUG"
        )
        logger.info("download https://bob:hunter2@example.test/book?api_key=key123")
        logger.info("chapter 1 saved")

    out = stream.getvalue()
    assert "hunter2" not in out
    assert "key123" not in out
    assert "download https://***@example.test/book?api_key=***" in out
    assert "chapter 1 saved" in out


def test_cli_console_exception_traceback_is_redacted(
    isolated_loggers: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rich renders ``exc_info`` itself, bypassing ``formatException``."""
    stream = io.StringIO()
    monkeypatch.setattr(
        ui, "_CONSOLE", Console(file=stream, force_terminal=False, color_system=None)
    )
    with _empty_root():
        logger = ui.setup_logging(
            log_dir=tmp_path, console=True, file=False, console_level="DEBUG"
        )

        try:
            raise ValueError("boom https://carol:pass1@example.test/x?token=deadbeef")
        except ValueError:
            logger.exception("request failed")

    out = stream.getvalue()
    assert "pass1" not in out
    assert "deadbeef" not in out
    assert "ValueError: boom https://***@example.test/x?token=***" in out
    assert "request failed" in out


def test_cli_setup_logging_is_idempotent(
    isolated_loggers: None, tmp_path: Path
) -> None:
    with _empty_root():
        logger = ui.setup_logging(log_dir=tmp_path, console=True, file=True)
        first = list(logger.handlers)

        again = ui.setup_logging(log_dir=tmp_path, console=True, file=True)
        second = list(again.handlers)

    assert again is logger
    assert len(first) == len(second) == 2, "one file + one console handler"
    assert all(
        isinstance(handler.formatter, RedactingFormatter) for handler in second
    ), "file and Rich console handlers both redact"


def test_cli_third_party_logger_reaching_root_is_redacted(
    isolated_loggers: None, tmp_path: Path
) -> None:
    stream = io.StringIO()
    root_handler = logging.StreamHandler(stream)
    root_handler.setFormatter(logging.Formatter("%(name)s|%(message)s"))
    root_handler.setLevel(logging.DEBUG)

    with _empty_root() as root:
        root.addHandler(root_handler)
        ui.setup_logging(log_dir=tmp_path, console=False, file=True)

        logging.getLogger("httpx").warning(
            "GET https://dave:letmein@example.test/a?token=xyz789&page=1"
        )

        out = stream.getvalue()
        assert "letmein" not in out
        assert "xyz789" not in out
        assert "httpx|GET https://***@example.test/a?token=***&page=1" in out
        assert root.handlers == [root_handler], "the existing root sink is reused"


def test_desktop_file_log_is_redacted_and_not_duplicated(
    isolated_loggers: None, tmp_path: Path
) -> None:
    with _empty_root() as root:
        desktop_main._setup_file_logging(tmp_path)
        desktop_main._setup_file_logging(tmp_path)

        owned = [
            handler
            for handler in root.handlers
            if getattr(handler, desktop_main._LOG_HANDLER_ATTR, False)
        ]
        assert len(owned) == 1, "repeated setup does not stack handlers"
        assert isinstance(owned[0].formatter, RedactingFormatter)

        logging.getLogger("urllib3.connectionpool").warning(
            "GET https://erin:secret9@example.test/c?api_key=zzz999"
        )
        # The desktop never configures the package logger, so its records fall
        # through to root like any other library's.
        logging.getLogger(PACKAGE_NAME).propagate = True
        logging.getLogger("novel_downloader.apps.desktop.main").info(
            "Desktop client starting (pid=%s)", 1234
        )
        owned[0].flush()

    text = (tmp_path / "logs" / desktop_main._LOG_NAME).read_text(encoding="utf-8")
    assert "secret9" not in text
    assert "zzz999" not in text
    assert "GET https://***@example.test/c?api_key=***" in text
    assert "Desktop client starting (pid=1234)" in text
