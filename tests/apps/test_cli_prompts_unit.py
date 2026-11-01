#!/usr/bin/env python3
"""Offline unit tests for the interactive CLI prompt helpers.

``ui.prompt_choice`` and ``ui.prompt`` are scripted through a queue so the
pagination and selection logic of :mod:`novel_downloader.apps.cli.prompts`
is fully deterministic; nothing is read from the real user profile.

``select_books`` reads free-form input via ``ui.prompt`` (a constrained
``prompt_choice`` would reject the advertised "1,3,5" multi-select), so its
answers flow through the same queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli import prompts
from novel_downloader.schemas import SearchResult


@pytest.fixture()
def answers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    queue: list[str] = []
    monkeypatch.setattr(
        "novel_downloader.apps.cli.prompts.ui.prompt_choice",
        lambda *a, **k: queue.pop(0),
    )
    monkeypatch.setattr(
        "novel_downloader.apps.cli.prompts.ui.prompt",
        lambda *a, **k: queue.pop(0),
    )
    return queue


def _search_result(i: int) -> SearchResult:
    return {
        "site": f"site{i}",
        "book_id": f"b{i}",
        "book_url": f"https://x/{i}",
        "cover_url": "",
        "title": f"Title {i}",
        "author": f"Author {i}",
        "latest_chapter": f"Ch {i}",
        "update_date": "2024-01-01",
        "word_count": "1M",
        "priority": i,
    }


# --------------------------------------------------------------------------- #
# select_main_action
# --------------------------------------------------------------------------- #


def test_select_main_action_returns_choice(answers: list[str]) -> None:
    answers.append("2")
    assert prompts.select_main_action() == "2"


# --------------------------------------------------------------------------- #
# select_site
# --------------------------------------------------------------------------- #


def test_select_site_missing_dir_returns_none(tmp_path: Path) -> None:
    assert prompts.select_site(tmp_path / "nope") is None


def test_select_site_no_subdirs_returns_none(tmp_path: Path) -> None:
    assert prompts.select_site(tmp_path) is None


def test_select_site_picks_directory(tmp_path: Path, answers: list[str]) -> None:
    (tmp_path / "siteb").mkdir()
    (tmp_path / "sitea").mkdir()
    answers.append("1")
    # Sorted directory listing: sitea first.
    assert prompts.select_site(tmp_path) == "sitea"


def test_select_site_pagination(tmp_path: Path, answers: list[str]) -> None:
    for i in range(12):
        (tmp_path / f"site{i:02d}").mkdir()

    # Page 1 -> next -> page 2 -> pick global index 11 (first of page 2).
    answers.extend(["n", "11"])
    assert prompts.select_site(tmp_path, per_page=10) == "site10"


def test_select_site_cancel(tmp_path: Path, answers: list[str]) -> None:
    (tmp_path / "s").mkdir()
    answers.append("")
    assert prompts.select_site(tmp_path) is None


def test_select_site_previous_page(tmp_path: Path, answers: list[str]) -> None:
    for i in range(12):
        (tmp_path / f"site{i:02d}").mkdir()
    # Page 2 -> previous -> page 1 -> last entry of page 1 (site09).
    answers.extend(["n", "p", "10"])
    assert prompts.select_site(tmp_path, per_page=10) == "site09"


# --------------------------------------------------------------------------- #
# select_books
# --------------------------------------------------------------------------- #


def _make_books(tmp_path: Path, site: str, n: int) -> None:
    for i in range(n):
        book = tmp_path / site / f"book{i}"
        book.mkdir(parents=True)
        (book / "book_info.raw.json").write_text(
            json.dumps({"book_name": f"Name {i}", "author": f"Author {i}"}),
            encoding="utf-8",
        )


def test_select_books_missing_site(tmp_path: Path) -> None:
    assert prompts.select_books(tmp_path, "ghost") == []


def test_select_books_empty_site(tmp_path: Path) -> None:
    (tmp_path / "site").mkdir()
    assert prompts.select_books(tmp_path, "site") == []


def test_select_books_select_all(tmp_path: Path, answers: list[str]) -> None:
    _make_books(tmp_path, "site", 3)
    answers.append("a")
    assert prompts.select_books(tmp_path, "site") == ["book0", "book1", "book2"]


def test_select_books_multi_index(tmp_path: Path, answers: list[str]) -> None:
    _make_books(tmp_path, "site", 5)
    answers.append("1,3")
    assert prompts.select_books(tmp_path, "site") == ["book0", "book2"]


def test_select_books_reads_metadata(tmp_path: Path, answers: list[str]) -> None:
    _make_books(tmp_path, "site", 1)
    answers.append("1")
    # Function returns ids; metadata reading is exercised implicitly and must
    # not raise on valid JSON.
    assert prompts.select_books(tmp_path, "site") == ["book0"]


def test_select_books_corrupt_metadata_warns_but_continues(
    tmp_path: Path, answers: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    book = tmp_path / "site" / "book0"
    book.mkdir(parents=True)
    (book / "book_info.raw.json").write_text("{broken", encoding="utf-8")
    answers.append("1")
    assert prompts.select_books(tmp_path, "site") == ["book0"]
    assert "Failed to read metadata" in capsys.readouterr().err


def test_select_books_invalid_numbers_reprompts(
    tmp_path: Path, answers: list[str]
) -> None:
    _make_books(tmp_path, "site", 3)
    answers.extend(["x,y", "2"])
    assert prompts.select_books(tmp_path, "site") == ["book1"]


def test_select_books_out_of_range_reprompts(
    tmp_path: Path, answers: list[str]
) -> None:
    _make_books(tmp_path, "site", 2)
    answers.extend(["99", "1"])
    assert prompts.select_books(tmp_path, "site") == ["book0"]


def test_select_books_cancel(tmp_path: Path, answers: list[str]) -> None:
    _make_books(tmp_path, "site", 1)
    answers.append("")
    assert prompts.select_books(tmp_path, "site") == []


def test_select_books_pagination(tmp_path: Path, answers: list[str]) -> None:
    _make_books(tmp_path, "site", 11)
    answers.extend(["n", "11"])
    # Directories are sorted lexicographically: book0, book1, book10, book2,
    # ... — so global index 11 is "book9", not "book10".
    assert prompts.select_books(tmp_path, "site", per_page=10) == ["book9"]


# --------------------------------------------------------------------------- #
# select_search_result
# --------------------------------------------------------------------------- #


def test_select_search_result_empty_returns_none() -> None:
    assert prompts.select_search_result([]) is None


def test_select_search_result_picks_global_index(
    answers: list[str],
) -> None:
    results = [_search_result(i) for i in range(3)]
    answers.append("2")
    chosen = prompts.select_search_result(results)
    assert chosen is not None
    assert chosen["book_id"] == "b1"


def test_select_search_result_pagination(answers: list[str]) -> None:
    results = [_search_result(i) for i in range(25)]
    answers.extend(["n", "13"])
    chosen = prompts.select_search_result(results, per_page=10)
    assert chosen is not None
    assert chosen["book_id"] == "b12"


def test_select_search_result_cancel(answers: list[str]) -> None:
    results = [_search_result(i) for i in range(2)]
    answers.append("")
    assert prompts.select_search_result(results) is None


def test_select_search_result_unknown_site_falls_back_to_key(
    answers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _fake_table(*args: Any, **kwargs: Any) -> None:
        seen["rows"] = args[2] if len(args) > 2 else kwargs.get("rows")

    monkeypatch.setattr(
        "novel_downloader.apps.cli.prompts.ui.render_table", _fake_table
    )
    results = [_search_result(0)]
    answers.append("1")
    prompts.select_search_result(results)
    assert seen["rows"][0][5] == "site0"


# --------------------------------------------------------------------------- #
# select_search_result: merged (grouped) rows render source capsules
# --------------------------------------------------------------------------- #


def _capture_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _fake_table(*args: Any, **kwargs: Any) -> None:
        seen["rows"] = args[2] if len(args) > 2 else kwargs.get("rows")

    monkeypatch.setattr(
        "novel_downloader.apps.cli.prompts.ui.render_table", _fake_table
    )
    return seen


def test_select_search_result_merged_row_lists_capsules_best_first(
    answers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_rows(monkeypatch)
    merged = dict(_search_result(0))
    merged["sources"] = ["site0", "site2", "site1"]
    answers.append("1")

    chosen = prompts.select_search_result([merged])

    assert chosen is not None
    # Best source first (starred), the remaining capsules in rank order.
    assert seen["rows"][0][5] == "*site0 · site2 · site1"


def test_select_search_result_flat_row_without_sources_stays_single(
    answers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_rows(monkeypatch)
    answers.append("1")

    prompts.select_search_result([_search_result(0)])

    assert seen["rows"][0][5] == "site0"


def test_select_search_result_merged_row_maps_known_site_labels(
    answers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_downloader.apps.constants import SEARCH_SUPPORT_SITES

    seen = _capture_rows(monkeypatch)
    merged = dict(_search_result(0))
    merged["site"] = "n23qb"
    merged["sources"] = ["n23qb", "alicesw"]
    answers.append("1")

    prompts.select_search_result([merged])

    # Capsules use the same display-name mapping as flat rows.
    best = SEARCH_SUPPORT_SITES["n23qb"]
    other = SEARCH_SUPPORT_SITES["alicesw"]
    assert seen["rows"][0][5] == f"*{best} · {other}"
