#!/usr/bin/env python3
"""
Offline tests for the ``browse`` command.

The searcher is replaced with a stub, so no network is touched: only the
argument handling, category-name resolution and output shape are under test.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli.commands import browse
from novel_downloader.apps.cli.commands.browse import BrowseCmd
from novel_downloader.plugins import registrar
from novel_downloader.schemas import SearchResult

_ROWS: list[SearchResult] = [
    SearchResult(
        site="fanqienovel",
        book_id="7617740782291651646",
        book_url="https://fanqienovel.com/page/7617740782291651646",
        cover_url="",
        title="骇夜：我在恐怖游戏里暴富",
        author="阿一不是阿二",
        latest_chapter="",
        update_date="2026-11-06",
        word_count="",
        priority=5,
    )
]

_GROUPS = {
    "male": [{"id": "1141", "name": "西方奇幻"}, {"id": "1139", "name": "科幻末世"}],
    "female": [{"id": "8", "name": "现代言情"}, {"id": "9", "name": "科幻末世"}],
}


class _StubSearcher:
    """Minimal searcher exposing the browse surface."""

    calls: list[tuple[Any, ...]] = []

    def __init__(self, session: Any) -> None:
        self.session = session

    async def categories(self) -> dict[str, list[dict[str, str]]]:
        _StubSearcher.calls.append(("categories",))
        return _GROUPS

    async def category_books(
        self,
        category_id: str,
        *,
        gender: str = "1",
        offset: int = 0,
        limit: int = 20,
    ) -> list[SearchResult]:
        _StubSearcher.calls.append(("category_books", category_id, gender, limit))
        return _ROWS

    async def rank(self, limit: int = 10) -> list[SearchResult]:
        _StubSearcher.calls.append(("rank", limit))
        return _ROWS


class _RanklessSearcher:
    def __init__(self, session: Any) -> None:
        self.session = session


class _BrokenCategoriesSearcher(_StubSearcher):
    """Hands the CLI a categories payload of an unexpected shape."""

    async def categories(self) -> Any:  # type: ignore[override]
        _StubSearcher.calls.append(("categories",))
        # A provider bug (list instead of the documented mapping) must be a
        # warning, not an ``AttributeError`` traceback out of the command.
        return [{"id": "1", "name": "西方奇幻"}]


class _InterruptingSearcher(_StubSearcher):
    """Simulates Ctrl+C while the fetch is in flight."""

    async def rank(self, limit: int = 10) -> list[SearchResult]:
        _StubSearcher.calls.append(("rank", limit))
        raise KeyboardInterrupt


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="novel-cli", add_help=False)
    sub = parser.add_subparsers(dest="command")
    BrowseCmd.register(sub)
    return parser


def _env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cls: type = _StubSearcher
) -> None:
    _StubSearcher.calls = []
    monkeypatch.setattr(
        browse,
        "load_or_init_config",
        lambda *_a, **_k: {"general": {"debug": {"log_dir": str(tmp_path / "logs")}}},
    )
    monkeypatch.setattr(registrar, "get_searcher_class", lambda site: cls)


def test_browse_categories_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env(monkeypatch, tmp_path)

    args = _parser().parse_args(["browse", "fanqienovel", "--categories", "--json"])
    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["male"][0] == {"id": "1141", "name": "西方奇幻"}
    assert _StubSearcher.calls == [("categories",)]


def test_browse_category_resolves_a_name_within_the_audience(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env(monkeypatch, tmp_path)

    args = _parser().parse_args(
        [
            "browse",
            "fanqienovel",
            "--category",
            "现代言情",
            "--gender",
            "female",
            "--json",
        ]
    )
    args.func(args)

    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["book_id"] == "7617740782291651646"
    assert ("category_books", "8", "2", 20) in _StubSearcher.calls


def test_browse_category_prefers_the_requested_audience_for_shared_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)

    args = _parser().parse_args(
        [
            "browse",
            "fanqienovel",
            "--category",
            "科幻末世",
            "--gender",
            "female",
            "--json",
        ]
    )
    args.func(args)

    assert ("category_books", "9", "2", 20) in _StubSearcher.calls


def test_browse_rank_is_the_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env(monkeypatch, tmp_path)

    args = _parser().parse_args(["browse", "fanqienovel", "--limit", "3", "--json"])
    args.func(args)

    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["title"] == "骇夜：我在恐怖游戏里暴富"
    assert _StubSearcher.calls == [("rank", 3)]


def test_browse_rejects_sites_without_browse_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path, cls=_RanklessSearcher)  # type: ignore[arg-type]
    errors: list[str] = []
    monkeypatch.setattr(browse.ui, "error", errors.append)

    args = _parser().parse_args(["browse", "b520"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("does not support browsing" in message for message in errors)


def test_browse_rejects_conflicting_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(browse.ui, "error", errors.append)

    args = _parser().parse_args(
        ["browse", "fanqienovel", "--categories", "--category", "1141"]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("cannot be combined" in message for message in errors)


def test_browse_unknown_category_name_reports_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch, tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(browse.ui, "error", errors.append)

    args = _parser().parse_args(["browse", "fanqienovel", "--category", "不存在的分类"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert any("was not found" in message for message in errors)


def test_browse_first_delegates_to_download_and_propagates_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``browse --first`` is ``download`` under the hood, exit code included."""
    from novel_downloader.apps.cli.commands.download import DownloadCmd

    _env(monkeypatch, tmp_path)
    seen: dict[str, Namespace] = {}

    def _run(_cls: type, args: Namespace) -> None:
        seen["args"] = args
        # Stands in for "downloaded but exported nothing".
        raise SystemExit(1)

    monkeypatch.setattr(DownloadCmd, "run", classmethod(_run))

    args = _parser().parse_args(["browse", "fanqienovel", "--first"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 1
    assert seen["args"].site == "fanqienovel"
    assert seen["args"].book_ids == [_ROWS[0]["book_id"]]
    assert seen["args"].no_export is False


def test_browse_categories_payload_of_the_wrong_shape_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed provider output must not leak an ``AttributeError``."""
    _env(monkeypatch, tmp_path, cls=_BrokenCategoriesSearcher)
    warnings: list[str] = []
    monkeypatch.setattr(browse.ui, "warn", warnings.append)

    args = _parser().parse_args(["browse", "fanqienovel", "--categories"])
    args.func(args)  # must return normally

    assert any("No browse categories" in message for message in warnings)


def test_browse_interactive_selection_requires_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Piped/CI stdin cannot answer the selection prompt; fail as usage."""
    _env(monkeypatch, tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(browse.ui, "error", errors.append)
    monkeypatch.setattr(browse, "_stdin_is_tty", lambda: False)

    args = _parser().parse_args(["browse", "fanqienovel"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("requires a terminal" in message for message in errors)


def test_browse_first_skips_the_terminal_requirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--first`` is the documented non-interactive path."""
    from novel_downloader.apps.cli.commands.download import DownloadCmd

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(browse, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(DownloadCmd, "run", classmethod(lambda _cls, _args: None))

    args = _parser().parse_args(["browse", "fanqienovel", "--first"])
    args.func(args)  # must not raise SystemExit


def test_browse_propagates_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ctrl+C is handled by ``cli_main`` (Aborted., exit 1), not swallowed."""
    _env(monkeypatch, tmp_path, cls=_InterruptingSearcher)

    args = _parser().parse_args(["browse", "fanqienovel", "--json"])
    with pytest.raises(KeyboardInterrupt):
        args.func(args)


def test_browse_namespace_exposes_every_option() -> None:
    ns: Namespace = _parser().parse_args(["browse", "fanqienovel"])
    for option in ("categories", "category", "gender", "limit", "as_json", "first"):
        assert hasattr(ns, option)
