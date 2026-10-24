"""Extended coverage for :mod:`novel_downloader.infra.logger`."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import suppress
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.infra import logger as logger_mod
from novel_downloader.infra.paths import PACKAGE_NAME


@pytest.fixture(autouse=True)
def _isolate_package_logger() -> Iterator[None]:
    """Keep the global ``novel_downloader`` logger out of other tests' way."""
    package_logger = logging.getLogger(PACKAGE_NAME)
    saved_handlers = package_logger.handlers[:]
    saved_level = package_logger.level
    saved_propagate = package_logger.propagate

    package_logger.handlers.clear()

    yield

    for handler in package_logger.handlers[:]:
        with suppress(Exception):
            handler.close()
    package_logger.handlers.clear()
    package_logger.handlers.extend(saved_handlers)
    package_logger.setLevel(saved_level)
    package_logger.propagate = saved_propagate


class CloseTrackingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - unused
        pass

    def close(self) -> None:
        self.closed = True
        super().close()


def _file_handler(log: logging.Logger) -> TimedRotatingFileHandler:
    handlers = [
        handler
        for handler in log.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]
    assert len(handlers) == 1
    return handlers[0]


def _read_log(log_dir: Path, filename: str) -> str:
    for handler in logging.getLogger(PACKAGE_NAME).handlers:
        handler.flush()
    return (log_dir / filename).read_text(encoding="utf-8")


def _point_home_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
        if home.drive:
            monkeypatch.setenv("HOMEDRIVE", home.drive)
            monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive) :])
    else:
        monkeypatch.setenv("HOME", str(home))


# ---------------------------------------------------------------------------
# Handler lifecycle
# ---------------------------------------------------------------------------
def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    for _ in range(3):
        log = logger_mod.setup_logging(log_dir=tmp_path, console=True, file=True)

        assert len(log.handlers) == 2
        assert sum(isinstance(h, logging.FileHandler) for h in log.handlers) == 1
        assert (
            sum(
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                for h in log.handlers
            )
            == 1
        )


def test_reinitialization_closes_previous_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[CloseTrackingHandler] = []

    def factory(*_args: Any, **_kwargs: Any) -> CloseTrackingHandler:
        handler = CloseTrackingHandler()
        created.append(handler)
        return handler

    monkeypatch.setattr(logger_mod, "TimedRotatingFileHandler", factory)

    logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)
    first = created[0]
    assert first.closed is False

    logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)

    assert first.closed is True
    assert created[1].closed is False


def test_logger_identity_and_propagation(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)

    assert log is logging.getLogger(PACKAGE_NAME)
    assert log.level == logging.DEBUG
    assert log.propagate is False


def test_all_handlers_can_be_disabled(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "logs"
    log = logger_mod.setup_logging(log_dir=nested, console=False, file=False)

    assert log.handlers == []
    assert not nested.exists()  # no file handler -> directory is not created


def test_records_do_not_propagate_to_root(tmp_path: Path) -> None:
    root = logging.getLogger()
    seen: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    collector = Collector()
    root.addHandler(collector)
    try:
        log = logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)
        log.warning("file-only")
    finally:
        root.removeHandler(collector)

    assert seen == []


def test_third_party_loggers_are_muted(tmp_path: Path) -> None:
    logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)

    for name in logger_mod._MUTE_LOGGERS:
        muted = logging.getLogger(name)
        assert muted.level == logging.ERROR
        assert muted.propagate is False


# ---------------------------------------------------------------------------
# File handler configuration and rotation
# ---------------------------------------------------------------------------
def test_file_handler_defaults_to_package_name(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)

    handler = _file_handler(log)
    assert Path(handler.baseFilename) == tmp_path / f"{PACKAGE_NAME}.log"


def test_file_handler_rotation_parameters(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="custom",
        console=False,
        file=True,
        backup_count=3,
        when="H",
    )

    handler = _file_handler(log)
    assert Path(handler.baseFilename) == tmp_path / "custom.log"
    assert handler.when == "H"
    assert handler.interval == 3600  # one "H" unit, normalized to seconds
    assert handler.backupCount == 3
    assert handler.utc is False
    assert handler.delay is True
    assert handler.encoding == "utf-8"


@pytest.mark.parametrize(
    ("when", "expected_interval"),
    [
        ("S", 1),
        ("M", 60),
        ("H", 3600),
        ("D", 86_400),
        ("midnight", 86_400),
    ],
)
def test_rotation_interval_is_normalized_to_seconds(
    tmp_path: Path, when: str, expected_interval: int
) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename=f"rot-{when}",
        console=False,
        file=True,
        when=when,
    )

    handler = _file_handler(log)
    assert handler.when == when.upper()
    assert handler.interval == expected_interval


def test_file_handler_writes_utf8_records(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="write",
        console=False,
        file=True,
        file_level="DEBUG",
    )

    log.debug("调试 message")

    text = _read_log(tmp_path, "write.log")
    assert "调试 message" in text
    assert "[DEBUG]" in text
    assert f"{PACKAGE_NAME}." in text


def test_log_directory_is_created_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "logs"
    assert not nested.exists()

    logger_mod.setup_logging(log_dir=nested, console=False, file=True)

    assert nested.is_dir()


def test_log_directory_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "profile"
    home.mkdir()
    _point_home_at(monkeypatch, home)

    log = logger_mod.setup_logging(log_dir="  ~/nd-logs  ", console=False, file=True)

    expected_dir = home / "nd-logs"
    assert Path(_file_handler(log).baseFilename) == expected_dir / (
        f"{PACKAGE_NAME}.log"
    )
    assert expected_dir.is_dir()


# ---------------------------------------------------------------------------
# Level filtering
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (logging.DEBUG, logging.DEBUG),
        (0, 0),
        ("DEBUG", logging.DEBUG),
        ("warn", logging.WARNING),
        ("critical", logging.CRITICAL),
        ("NOTSET", logging.NOTSET),
        ("not-a-level", logging.INFO),
        (None, logging.INFO),
        (object(), logging.INFO),
    ],
)
def test_normalize_level(value: Any, expected: int) -> None:
    assert logger_mod._normalize_level(value) == expected


def test_file_level_filters_low_severity_records(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="filtered",
        console=False,
        file=True,
        file_level="WARNING",
    )

    log.info("info-hidden")
    log.warning("warning-visible")

    text = _read_log(tmp_path, "filtered.log")
    assert "info-hidden" not in text
    assert "warning-visible" in text


def test_console_level_filters_low_severity_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        console=True,
        file=False,
        console_level="ERROR",
    )

    log.warning("console-hidden")
    log.error("console-visible")

    captured = capsys.readouterr()
    assert "console-hidden" not in captured.err
    assert "console-visible" in captured.err


def test_records_are_written_once_after_reinitialization(tmp_path: Path) -> None:
    for _ in range(2):
        log = logger_mod.setup_logging(
            log_dir=tmp_path,
            log_filename="once",
            console=False,
            file=True,
        )

    log.warning("single-line")

    text = _read_log(tmp_path, "once.log")
    assert text.count("single-line") == 1


# ---------------------------------------------------------------------------
# Sensitive information
# ---------------------------------------------------------------------------
FILE_FORMATTER_FIELDS = {"asctime", "levelname", "name", "funcName", "message"}


def test_file_formatter_only_references_non_sensitive_fields(
    tmp_path: Path,
) -> None:
    log = logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)

    formatter = _file_handler(log).formatter
    assert formatter is not None
    fmt = getattr(formatter, "_fmt", "")
    fields = set(re.findall(r"%\((\w+)\)", fmt))

    assert fields == FILE_FORMATTER_FIELDS
    sensitive = {
        "cookie",
        "cookies",
        "headers",
        "auth",
        "authorization",
        "password",
        "token",
        "session",
        "secret",
    }
    assert not fields & sensitive


def test_file_log_does_not_serialize_extra_sensitive_values(
    tmp_path: Path,
) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="sensitive",
        console=False,
        file=True,
        file_level="DEBUG",
    )

    secret = "SESSIONID=super-secret-token"
    log.info(
        "request finished",
        extra={
            "cookies": secret,
            "headers": {"Cookie": secret},
            "password": "hunter2",
            "token": "tok_live_123",
        },
    )

    text = _read_log(tmp_path, "sensitive.log")
    assert "request finished" in text
    assert "super-secret-token" not in text
    assert "hunter2" not in text
    assert "tok_live_123" not in text
