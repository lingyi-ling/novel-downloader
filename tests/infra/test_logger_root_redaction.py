"""Tests for root-logger redaction (:func:`install_root_redaction`).

Third-party libraries (aiohttp, httpx, urllib3, musicdl) do not log through
``novel_downloader``'s package logger.  They propagate to the root logger,
which is what these tests exercise.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import pytest

from novel_downloader.infra.logger import (
    RedactingFilter,
    RedactingFormatter,
    install_root_redaction,
)


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


def test_install_root_redaction_adds_one_fallback() -> None:
    with _empty_root() as root:
        handler = install_root_redaction()

        assert handler is not None
        assert root.handlers == [handler]
        assert handler.level == logging.WARNING, "same threshold as logging.lastResort"
        assert isinstance(handler.formatter, RedactingFormatter)


def test_install_root_redaction_is_idempotent() -> None:
    with _empty_root() as root:
        first = install_root_redaction()
        second = install_root_redaction()

        assert first is not None
        assert second is None
        assert root.handlers == [first]
        redacting = [
            filter_ for filter_ in first.filters if isinstance(filter_, RedactingFilter)
        ]
        assert len(redacting) == 1, "the filter is never stacked twice"


def test_fallback_output_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    with _empty_root():
        install_root_redaction()
        logging.getLogger("httpx").warning(
            "GET https://alice:s3cret@example.test/a?token=abc123&page=2"
        )

    out = stream.getvalue()
    assert "s3cret" not in out
    assert "abc123" not in out
    assert "https://***@example.test/a?token=***&page=2" in out


def test_existing_root_handlers_get_a_filter() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s|%(levelname)s|%(message)s"))
    handler.setLevel(logging.DEBUG)

    with _empty_root() as root:
        root.addHandler(handler)
        assert install_root_redaction() is None, "an existing handler is reused"

        logging.getLogger("urllib3.connectionpool").warning(
            "GET https://bob:hunter2@example.test/b?api_key=key123"
        )

        out = stream.getvalue()
        assert "hunter2" not in out
        assert "key123" not in out
        assert (
            "urllib3.connectionpool|WARNING|GET https://***@example.test/b?api_key=***"
            in out
        )
        assert root.handlers == [handler], "no fallback was appended"
        assert root.level == logging.WARNING, "root level is left untouched"


def test_exception_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    third_party = logging.getLogger("aiohttp.client")
    third_party.setLevel(logging.DEBUG)

    with _empty_root():
        install_root_redaction()

        try:
            raise RuntimeError("boom https://carol:pass1@example.test/?sign=deadbeef")
        except RuntimeError:
            third_party.exception("request failed")

    out = stream.getvalue()
    assert "pass1" not in out
    assert "deadbeef" not in out
    assert "RuntimeError: boom https://***@example.test/?sign=***" in out
