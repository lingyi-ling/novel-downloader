#!/usr/bin/env python3
"""tests.plugins.test_fetcher_redirect_classification
------------------------------------------------------

Deterministic request failures must be excluded from retries at *every* level.

``TooManyRedirectsError`` escaped ``BaseFetcher.fetch`` as a bare exception, so
the chapter-level ``except Exception`` in ``DownloadMixin.get_chapter`` retried
the whole redirect chain ``retry_times`` more times.  The fetcher now translates
it into :class:`RedirectLimitError` (non-retryable, like a deterministic 4xx)
and the chapter loop treats it as terminal, while genuine transport errors keep
using the retry budget.

Everything runs offline against in-memory session/fetcher doubles.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.infra.sessions.base import TooManyRedirectsError
from novel_downloader.plugins.base.errors import (
    RedirectLimitError,
    is_transient_error,
)
from novel_downloader.plugins.mixins import download as download_mod
from tests.plugins.test_base_fetcher import _make
from tests.plugins.test_download_mixin_retry import (  # noqa: F401 - pytest fixture
    _client,
    harness,
)


def _redirect_handler(url: str, kwargs: dict[str, Any]) -> BaseException:
    return TooManyRedirectsError("redirect loop")


# --------------------------------------------------------------------------- #
# fetcher level
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_does_not_retry_a_redirect_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher, session = _make(monkeypatch, _redirect_handler, retry_times=3)

    with pytest.raises(RedirectLimitError) as excinfo:
        await fetcher.fetch("https://demo/1")

    assert len(session.calls) == 1
    assert isinstance(excinfo.value.__cause__, TooManyRedirectsError)
    assert excinfo.value.retryable is False
    assert is_transient_error(excinfo.value) is False


@pytest.mark.asyncio
async def test_fetch_data_returns_none_on_a_redirect_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken cover/image redirect is a missing asset, not a failed book."""
    fetcher, session = _make(monkeypatch, _redirect_handler, retry_times=3)

    assert await fetcher.fetch_data("https://demo/cover.jpg") is None
    assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# chapter level
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_chapter_does_not_retry_a_redirect_limit_error(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def _record_sleep(**kwargs: Any) -> None:
        sleeps.append(kwargs["base"])

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _record_sleep)

    client, fetcher = await _client(tmp_path, harness, retry_times=3)

    async def _loop(*args: Any, **kwargs: Any) -> list[str]:
        fetcher.chapter_calls += 1
        raise RedirectLimitError("https://demo/c1")

    monkeypatch.setattr(fetcher, "fetch_chapter_content", _loop)

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 1, "a redirect loop cannot heal by retrying"
    assert sleeps == []
    failure = download_mod._LAST_FAILURE.get()
    assert failure is not None
    assert failure.reason == "too_many_redirects"
    assert failure.attempts == 1


@pytest.mark.asyncio
async def test_get_chapter_still_retries_a_transport_error(
    tmp_path: Path,
    harness: SimpleNamespace,  # noqa: F811 - fixture from the sibling module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic carve-out must not disable the transient retry."""
    sleeps: list[float] = []

    async def _record_sleep(**kwargs: Any) -> None:
        sleeps.append(kwargs["base"])

    monkeypatch.setattr(download_mod, "async_jitter_sleep", _record_sleep)

    client, fetcher = await _client(tmp_path, harness, retry_times=2)

    async def _reset(*args: Any, **kwargs: Any) -> list[str]:
        fetcher.chapter_calls += 1
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr(fetcher, "fetch_chapter_content", _reset)

    assert await client.get_chapter("b1", "c1") is None
    assert fetcher.chapter_calls == 3  # initial attempt + two retries
    assert len(sleeps) == 2
    failure = download_mod._LAST_FAILURE.get()
    assert failure is not None
    assert failure.reason.startswith("retry_exhausted")
    assert failure.attempts == 3
