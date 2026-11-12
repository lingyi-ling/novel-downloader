"""Hardening tests for :mod:`novel_downloader.infra.logger`.

Covers credential redaction (URLs, headers, query strings, ``extra`` values and
tracebacks), resilient file-handler setup, rotation limits, permissions and
concurrent writers.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

import pytest

from novel_downloader.infra import logger as logger_mod
from novel_downloader.infra.paths import PACKAGE_NAME

ROOT = Path(__file__).resolve().parents[2]


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


def _read_log(log_dir: Path, filename: str) -> str:
    for handler in logging.getLogger(PACKAGE_NAME).handlers:
        handler.flush()
    return (log_dir / filename).read_text(encoding="utf-8")


def _file_handlers(log: logging.Logger) -> list[logging.FileHandler]:
    return [
        handler for handler in log.handlers if isinstance(handler, logging.FileHandler)
    ]


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------
def test_redact_strips_url_userinfo() -> None:
    text = "GET https://alice:s3cret@example.test/books/1"

    assert logger_mod.redact(text) == "GET https://***@example.test/books/1"


@pytest.mark.parametrize(
    "name",
    ["token", "access_token", "api_key", "apikey", "key", "sign", "cookie", "password"],
)
def test_redact_strips_sensitive_query_parameters(name: str) -> None:
    text = f"https://example.test/a?{name}=s3cret-value&safe=1"

    cleaned = logger_mod.redact(text)

    assert "s3cret-value" not in cleaned
    assert "safe=1" in cleaned
    assert f"{name}=***" in cleaned


def test_redact_strips_auth_and_cookie_headers() -> None:
    text = (
        "Authorization: Bearer abc.def-123\n"
        "Proxy-Authorization: Basic dXNlcjpwYXNz\n"
        "X-Api-Key: sk-live-42\n"
        "Cookie: SESSIONID=session-value; csrftoken=csrf-value"
    )

    cleaned = logger_mod.redact(text)

    for secret in (
        "abc.def-123",
        "dXNlcjpwYXNz",
        "sk-live-42",
        "session-value",
        "csrf-value",
    ):
        assert secret not in cleaned


def test_redact_strips_assignments_and_mapping_values() -> None:
    text = (
        "{'password': 'hunter2', 'api_key': 'sk-123', "
        "'session_id': 'sess-9', 'safe': 'visible'}"
    )

    cleaned = logger_mod.redact(text)

    for secret in ("hunter2", "sk-123", "sess-9"):
        assert secret not in cleaned
    assert "'safe': 'visible'" in cleaned


def test_redact_is_idempotent() -> None:
    text = "https://u:p@h.test/x?token=t&password=hunter2"
    once = logger_mod.redact(text)

    assert logger_mod.redact(once) == once


def test_redact_catches_prefixed_fields() -> None:
    cleaned = logger_mod.redact(
        "user_token=abc123 x-api-key=sk-9 accessToken=camel-value"
    )

    assert "abc123" not in cleaned
    assert "sk-9" not in cleaned
    assert "camel-value" not in cleaned


def test_redact_leaves_logger_names_and_ordinary_messages_alone() -> None:
    text = (
        "2026-01-01 00:00:00 [DEBUG] "
        "novel_downloader.infra.sessions.base.close: "
        "Error while closing HTTP session backend: timeout"
    )

    assert logger_mod.redact(text) == text


def test_redact_never_raises_on_hostile_objects() -> None:
    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert logger_mod.redact(Hostile())  # non-empty placeholder, no exception


# ---------------------------------------------------------------------------
# Formatter / handler redaction
# ---------------------------------------------------------------------------
def test_file_handler_redacts_message_arguments(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="redact",
        console=False,
        file=True,
        file_level="DEBUG",
    )

    log.error(
        "GET %s for %s",
        "https://alice:s3cret@example.test/x?token=tok_live_9f3&safe=1",
        "password=hunter2",
    )

    text = _read_log(tmp_path, "redact.log")
    assert "s3cret" not in text
    assert "tok_live_9f3" not in text
    assert "hunter2" not in text
    assert "safe=1" in text
    assert "[ERROR]" in text


def test_console_handler_redacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = logger_mod.setup_logging(log_dir=tmp_path, console=True, file=False)

    log.warning("proxy http://user:proxy-pass@127.0.0.1:8080 failed")

    captured = capsys.readouterr()
    assert "proxy-pass" not in captured.err
    assert "***" in captured.err


def test_traceback_is_redacted_and_locals_are_not_rendered(tmp_path: Path) -> None:
    secret = "trace-secret-77"

    def fail() -> None:
        local_only = secret  # noqa: F841 - must never be rendered
        raise RuntimeError(
            f"cannot fetch https://alice:{local_only}@example.test/x?token={local_only}"
        )

    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="trace",
        console=False,
        file=True,
        file_level="DEBUG",
    )
    try:
        fail()
    except RuntimeError:
        log.exception("request failed")

    text = _read_log(tmp_path, "trace.log")
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError" in text
    assert secret not in text


def test_extra_fields_are_not_serialized(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="extra", console=False, file=True
    )

    log.info(
        "finished",
        extra={"password": "hunter2", "headers": {"Cookie": "SESSIONID=abc"}},
    )

    text = _read_log(tmp_path, "extra.log")
    assert "hunter2" not in text
    assert "SESSIONID=abc" not in text


# ---------------------------------------------------------------------------
# Setup resilience
# ---------------------------------------------------------------------------
def test_unusable_log_path_falls_back_to_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    log = logger_mod.setup_logging(log_dir=blocker, console=True, file=True)

    assert _file_handlers(log) == []
    assert any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in log.handlers
    )
    assert capsys.readouterr().err  # diagnostics, not an exception


def test_symlinked_log_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("untouched", encoding="utf-8")
    link = tmp_path / f"{PACKAGE_NAME}.log"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this host")

    log = logger_mod.setup_logging(log_dir=tmp_path, console=False, file=True)
    log.error("token=super-secret")

    assert _file_handlers(log) == []
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_log_file_is_owner_only(tmp_path: Path) -> None:
    logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="secure", console=False, file=True
    )

    mode = stat.S_IMODE((tmp_path / "secure.log").stat().st_mode)
    assert mode == 0o600


def test_unsupported_when_and_backup_count_are_normalized(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="norm",
        console=False,
        file=True,
        when="not-a-period",
        backup_count=-5,
    )

    handler = _file_handlers(log)[0]
    assert isinstance(handler, TimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == 0


def test_log_filename_cannot_escape_the_log_dir(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="../../escape", console=False, file=True
    )

    handler = _file_handlers(log)[0]
    assert Path(handler.baseFilename).parent == tmp_path
    assert Path(handler.baseFilename).name == "escape.log"


def test_surrogate_text_does_not_break_the_file_handler(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="surrogate", console=False, file=True
    )

    log.warning("lone surrogate: \ud800")

    text = _read_log(tmp_path, "surrogate.log")
    assert "lone surrogate" in text
    assert "\\ud800" in text


def test_handler_errors_do_not_replay_sensitive_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging, "raiseExceptions", True)
    log = logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="broken", console=False, file=True
    )
    handler = _file_handlers(log)[0]

    class BrokenStream:
        encoding = "utf-8"

        def write(self, _data: str) -> int:
            raise OSError("No space left on device")

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    handler.stream = BrokenStream()  # type: ignore[assignment]
    log.error(
        "password=%s url=%s",
        "hunter2",
        "https://alice:s3cret@example.test/?token=tok_live",
    )

    err = capsys.readouterr().err
    assert "Logging error" in err
    for secret in ("hunter2", "s3cret", "tok_live"):
        assert secret not in err


# ---------------------------------------------------------------------------
# Rotation and concurrency
# ---------------------------------------------------------------------------
def test_size_rotation_keeps_complete_records(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="sized",
        console=False,
        file=True,
        max_bytes=1024,
        backup_count=2,
    )

    handler = _file_handlers(log)[0]
    assert isinstance(handler, RotatingFileHandler)
    assert not isinstance(handler, TimedRotatingFileHandler)
    assert handler.maxBytes == 1024

    for index in range(200):
        log.warning("payload-%03d-%s", index, "x" * 40)

    files = sorted(tmp_path.glob("sized.log*"))
    assert len(files) >= 2
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert content.endswith("\n")
        for line in content.splitlines():
            assert re.match(r"^\d{4}-\d\d-\d\d .*payload-\d{3}-x+$", line)


def test_time_rotation_preserves_records(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path,
        log_filename="rolled",
        console=False,
        file=True,
        when="S",
        backup_count=2,
    )

    log.warning("before-rollover")
    handler = _file_handlers(log)[0]
    handler.doRollover()
    log.warning("after-rollover")

    assert "after-rollover" in _read_log(tmp_path, "rolled.log")
    rotated = [
        path
        for path in tmp_path.glob("rolled.log.*")
        if path.is_file() and not path.name.endswith(".lock")
    ]
    assert rotated
    assert any(
        "before-rollover" in path.read_text(encoding="utf-8") for path in rotated
    )


def test_threaded_writers_do_not_interleave(tmp_path: Path) -> None:
    log = logger_mod.setup_logging(
        log_dir=tmp_path, log_filename="threads", console=False, file=True
    )

    def worker(tag: str) -> None:
        for index in range(50):
            log.info("thread-%s-%03d", tag, index)

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = _read_log(tmp_path, "threads.log")
    for tag in range(4):
        for index in range(50):
            assert text.count(f"thread-{tag}-{index:03d}") == 1


def test_processes_append_without_losing_records(tmp_path: Path) -> None:
    script = (
        "import logging, sys\n"
        "from novel_downloader.infra.logger import setup_logging\n"
        "log = setup_logging(log_dir=sys.argv[1], log_filename='multi',"
        " console=False, file=True, file_level='INFO')\n"
        "for i in range(40):\n"
        "    log.info('%s-%03d', sys.argv[2], i)\n"
        "logging.shutdown()\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), f"proc{tag}"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in range(3)
    ]
    results = [process.communicate(timeout=60) for process in processes]

    assert all(process.returncode == 0 for process in processes), results
    text = _read_log(tmp_path, "multi.log")
    for tag in range(3):
        for index in range(40):
            assert text.count(f"proc{tag}-{index:03d}") == 1
