#!/usr/bin/env python3
"""Offline tests for :class:`novel_downloader.plugins.common.client.CommonClient`.

Only the orchestration layer is exercised: login persistence and per-format
export dispatch/error isolation.  The real exporter mixins are replaced by
in-memory callables, so nothing is written to disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    ClientConfig,
    ExporterConfig,
    FetcherConfig,
    ParserConfig,
)


class _Fetcher:
    def __init__(self) -> None:
        self.load_state_result = False
        self.login_result = True
        self.load_state_calls = 0
        self.login_calls: list[dict[str, Any]] = []
        self.save_state_calls = 0

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @property
    def is_logged_in(self) -> bool:
        return True

    @property
    def login_fields(self) -> list[Any]:
        return [{"name": "cookies"}]

    async def load_state(self) -> bool:
        self.load_state_calls += 1
        return self.load_state_result

    async def login(
        self,
        username: str = "",
        password: str = "",
        cookies: dict[str, str] | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> bool:
        self.login_calls.append(
            {"username": username, "password": password, "cookies": cookies}
        )
        return self.login_result

    async def save_state(self) -> bool:
        self.save_state_calls += 1
        return True


class _Parser:
    def parse_book_info(self, raw_pages: list[str], **kwargs: Any) -> Any:
        return None

    def parse_chapter_content(
        self, raw_pages: list[str], chapter_id: str, **kwargs: Any
    ) -> Any:
        return None


class _LoginUI:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data if data is not None else {"cookies": "sid=1"}
        self.prompt_calls = 0
        self.prefill: dict[str, str] | None = None
        self.failed = 0
        self.succeeded = 0

    async def prompt(
        self, fields: list[Any], prefill: dict[str, str] | None = None
    ) -> dict[str, str]:
        self.prompt_calls += 1
        self.prefill = prefill
        return self.data

    def on_login_failed(self) -> None:
        self.failed += 1

    def on_login_success(self) -> None:
        self.succeeded += 1


class _ExportUI:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def on_start(self, book: BookConfig, fmt: str) -> None:
        self.events.append(("start", fmt))

    def on_success(self, book: BookConfig, fmt: str, path: Path) -> None:
        self.events.append(("success", fmt, path))

    def on_unsupported(self, book: BookConfig, fmt: str) -> None:
        self.events.append(("unsupported", fmt))

    def on_error(self, book: BookConfig, fmt: str, exc: BaseException) -> None:
        self.events.append(("error", fmt, type(exc).__name__))


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[_Fetcher, _Parser]:
    fetcher = _Fetcher()
    parser = _Parser()

    monkeypatch.setattr(registrar, "get_fetcher", lambda site, cfg: fetcher)
    monkeypatch.setattr(registrar, "get_parser", lambda site, cfg: parser)
    return fetcher, parser


def _client(tmp_path: Path, **overrides: Any) -> CommonClient:
    kwargs: dict[str, Any] = {
        "raw_data_dir": str(tmp_path / "raw"),
        "cache_dir": str(tmp_path / "cache"),
        "output_dir": str(tmp_path / "out"),
        "request_interval": 0.0,
        "retry_times": 0,
    }
    kwargs.update(overrides)
    return CommonClient("demo", ClientConfig(**kwargs))


# --------------------------------------------------------------------------- #
# login()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_succeeds_from_saved_state_without_prompt(
    tmp_path: Path, wired: tuple[_Fetcher, _Parser]
) -> None:
    fetcher, _ = wired
    fetcher.load_state_result = True
    client = _client(tmp_path)
    await client.init(FetcherConfig(), ParserConfig())
    ui = _LoginUI()

    assert await client.login(ui=ui) is True

    assert fetcher.load_state_calls == 1
    assert ui.prompt_calls == 0
    assert fetcher.login_calls == []


@pytest.mark.asyncio
async def test_login_prompts_and_persists_on_success(
    tmp_path: Path, wired: tuple[_Fetcher, _Parser]
) -> None:
    fetcher, _ = wired
    client = _client(tmp_path)
    await client.init(FetcherConfig(), ParserConfig())
    ui = _LoginUI({"cookies": "sid=1"})
    login_cfg = {"cookies": "old"}

    assert await client.login(ui=ui, login_cfg=login_cfg) is True

    assert ui.prompt_calls == 1
    assert ui.prefill == login_cfg
    assert fetcher.login_calls == [{"username": "", "password": "", "cookies": "sid=1"}]
    assert fetcher.save_state_calls == 1
    assert ui.succeeded == 1
    assert ui.failed == 0


@pytest.mark.asyncio
async def test_login_failure_reports_ui_and_skips_persistence(
    tmp_path: Path, wired: tuple[_Fetcher, _Parser]
) -> None:
    fetcher, _ = wired
    fetcher.login_result = False
    client = _client(tmp_path)
    await client.init(FetcherConfig(), ParserConfig())
    ui = _LoginUI()

    assert await client.login(ui=ui) is False

    assert ui.failed == 1
    assert ui.succeeded == 0
    assert fetcher.save_state_calls == 0


# --------------------------------------------------------------------------- #
# export_book(): per-format isolation
# --------------------------------------------------------------------------- #
def test_export_book_success_reports_each_path(tmp_path: Path) -> None:
    client = _client(tmp_path)
    book = BookConfig(book_id="b1")
    first = tmp_path / "a.epub"
    second = tmp_path / "b.epub"
    calls: list[tuple[Any, ...]] = []

    def _export(
        book: BookConfig,
        cfg: ExporterConfig,
        *,
        stage: str | None,
        **kw: Any,
    ) -> list[Path]:
        calls.append((book, cfg, stage, kw))
        return [first, second]

    client._export_book_epub = _export  # type: ignore[method-assign]
    ui = _ExportUI()

    result = client.export_book(book, formats=["epub"], stage="raw", ui=ui)

    assert result == {"epub": [first, second]}
    assert calls[0][0] is book
    assert calls[0][2] == "raw"
    assert ui.events == [
        ("start", "epub"),
        ("success", "epub", first),
        ("success", "epub", second),
    ]


def test_export_book_unsupported_format_is_reported(tmp_path: Path) -> None:
    client = _client(tmp_path)
    ui = _ExportUI()

    result = client.export_book(BookConfig(book_id="b1"), formats=["pdf"], ui=ui)

    assert result == {"pdf": []}
    assert ui.events == [("unsupported", "pdf")]


def test_export_book_failure_does_not_abort_other_formats(tmp_path: Path) -> None:
    client = _client(tmp_path)
    good = tmp_path / "a.txt"
    attempted: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> list[Path]:
        attempted.append("epub")
        raise RuntimeError("write failed")

    def _ok(*args: Any, **kwargs: Any) -> list[Path]:
        attempted.append("txt")
        return [good]

    client._export_book_epub = _boom  # type: ignore[method-assign]
    client._export_book_txt = _ok  # type: ignore[method-assign]
    ui = _ExportUI()

    result = client.export_book(
        BookConfig(book_id="b1"), formats=["epub", "txt"], ui=ui
    )

    assert attempted == ["epub", "txt"]
    assert result == {"epub": [], "txt": [good]}
    assert ui.events == [
        ("start", "epub"),
        ("error", "epub", "RuntimeError"),
        ("start", "txt"),
        ("success", "txt", good),
    ]


def test_export_book_honours_split_mode(tmp_path: Path) -> None:
    client = _client(tmp_path)
    called: list[str] = []

    def _volume(*args: Any, **kwargs: Any) -> list[Path]:
        called.append("volume")
        return []

    client._export_volume_epub = _volume  # type: ignore[method-assign]

    result = client.export_book(
        BookConfig(book_id="b1"),
        ExporterConfig(split_mode="volume"),
        formats=["epub"],
    )

    assert called == ["volume"]
    assert result == {"epub": []}


def test_export_delegates_to_export_book(tmp_path: Path) -> None:
    client = _client(tmp_path)
    seen: list[str] = []

    def _ok(*args: Any, **kwargs: Any) -> list[Path]:
        seen.append("epub")
        return []

    client._export_book_epub = _ok  # type: ignore[method-assign]

    assert client.export(BookConfig(book_id="b1"), formats=["epub"]) == {"epub": []}
    assert seen == ["epub"]


# --------------------------------------------------------------------------- #
# export_chapter(): per-format isolation
# --------------------------------------------------------------------------- #
def test_export_chapter_defaults_to_txt(tmp_path: Path) -> None:
    client = _client(tmp_path)
    expected = tmp_path / "c1.txt"
    calls: list[tuple[Any, ...]] = []

    def _export(
        book_id: str,
        chapter_id: str,
        cfg: ExporterConfig,
        *,
        stage: str | None,
        **kwargs: Any,
    ) -> Path:
        calls.append((book_id, chapter_id, stage))
        return expected

    client._export_chapter_txt = _export  # type: ignore[method-assign]

    result = client.export_chapter("b1", "c1", stage="raw")

    assert result == {"txt": expected}
    assert calls == [("b1", "c1", "raw")]


def test_export_chapter_unsupported_format_returns_none(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.export_chapter("b1", "c1", formats=["pdf"]) == {"pdf": None}


def test_export_chapter_failure_is_isolated(tmp_path: Path) -> None:
    client = _client(tmp_path)
    expected = tmp_path / "c1.html"

    def _boom(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("write failed")

    def _ok(*args: Any, **kwargs: Any) -> Path:
        return expected

    client._export_chapter_txt = _boom  # type: ignore[method-assign]
    client._export_chapter_html = _ok  # type: ignore[method-assign]

    result = client.export_chapter("b1", "c1", formats=["txt", "html"])

    assert result == {"txt": None, "html": expected}
