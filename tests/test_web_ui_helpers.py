#!/usr/bin/env python3
"""
tests.test_web_ui_helpers
-------------------------

Unit tests for the pure helper functions of the web UI layer: size and
duration formatting, file filtering/sorting, and navigation labels.

Importing the page modules registers the ``@ui.page`` routes but never
starts a server, so it is safe under a headless test runner.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from novel_downloader.apps.web.components.shell import _nav_label
from novel_downloader.apps.web.pages.history import (
    FileItem,
    _apply_filter,
    _apply_sort,
    _format_size,
)
from novel_downloader.apps.web.pages.progress import _format_seconds

_GIB = 1024**3

_NAV_KEYS = ("search", "download", "progress", "history")


@pytest.fixture(autouse=True)
def _pin_source_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the i18n source language so ``t()`` returns message ids verbatim."""
    monkeypatch.setenv("NOVEL_LANG", "en_US")


def _item(name: str, *, size: int = 0, mtime: float = 0.0) -> FileItem:
    """Build a :class:`FileItem`, deriving the extension from the name."""
    ext = name.rsplit(".", 1)[-1].lower()
    return FileItem(
        path=Path("downloads") / name,
        name=name,
        ext=ext,
        size=size,
        mtime=mtime,
    )


# ------------------------------------
# history._format_size
# ------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1536, "1 KB"),  # fractional KB is truncated, not rounded
        (2048, "2 KB"),
        (1024**2, "1.0 MB"),
        (1_500_000, "1.4 MB"),
        (5 * _GIB, "5.0 GB"),
    ],
)
def test_format_size(size: int, expected: str) -> None:
    assert _format_size(size) == expected


# ------------------------------------
# history._apply_filter
# ------------------------------------


def test_apply_filter_all_returns_every_item() -> None:
    items = [_item("a.txt"), _item("b.epub"), _item("c.txt")]

    result = _apply_filter(items=items, type_filter="All")

    assert result == items


def test_apply_filter_txt_keeps_only_txt_in_order() -> None:
    items = [
        _item("a.epub"),
        _item("b.txt"),
        _item("c.epub"),
        _item("d.txt"),
    ]

    result = _apply_filter(items=items, type_filter="txt")

    assert [it.name for it in result] == ["b.txt", "d.txt"]


def test_apply_filter_epub_keeps_only_epub_in_order() -> None:
    items = [_item("a.epub"), _item("b.txt"), _item("c.epub")]

    result = _apply_filter(items=items, type_filter="epub")

    assert [it.name for it in result] == ["a.epub", "c.epub"]


def test_apply_filter_with_no_matching_items() -> None:
    items = [_item("a.txt"), _item("b.txt")]

    result = _apply_filter(items=items, type_filter="epub")

    assert result == []


def test_apply_filter_with_empty_input() -> None:
    assert _apply_filter(items=[], type_filter="txt") == []
    assert _apply_filter(items=[], type_filter="All") == []


# ------------------------------------
# history._apply_sort
# ------------------------------------


def test_apply_sort_name_asc_is_case_insensitive() -> None:
    # Case-sensitive ASCII order would put "Bravo.txt" before "alpha.txt".
    items = [_item("Bravo.txt"), _item("alpha.txt"), _item("Charlie.epub")]

    result = _apply_sort(items, "name", "asc")

    assert [it.name for it in result] == ["alpha.txt", "Bravo.txt", "Charlie.epub"]


def test_apply_sort_name_desc_reverses_asc_order() -> None:
    items = [_item("Bravo.txt"), _item("alpha.txt"), _item("Charlie.epub")]

    result = _apply_sort(items, "name", "desc")

    assert [it.name for it in result] == ["Charlie.epub", "Bravo.txt", "alpha.txt"]


def test_apply_sort_mtime_asc_orders_oldest_first() -> None:
    items = [
        _item("new.txt", mtime=300.0),
        _item("old.txt", mtime=100.0),
        _item("mid.epub", mtime=200.0),
    ]

    result = _apply_sort(items, "mtime", "asc")

    assert [it.name for it in result] == ["old.txt", "mid.epub", "new.txt"]


def test_apply_sort_mtime_desc_orders_newest_first() -> None:
    items = [
        _item("new.txt", mtime=300.0),
        _item("old.txt", mtime=100.0),
        _item("mid.epub", mtime=200.0),
    ]

    result = _apply_sort(items, "mtime", "desc")

    assert [it.name for it in result] == ["new.txt", "mid.epub", "old.txt"]


def test_apply_sort_does_not_mutate_input() -> None:
    items = [_item("b.txt"), _item("a.txt")]

    _apply_sort(items, "name", "asc")

    assert [it.name for it in items] == ["b.txt", "a.txt"]


# ------------------------------------
# history.FileItem
# ------------------------------------


def test_file_item_stores_all_fields() -> None:
    path = Path("downloads") / "novel.txt"

    item = FileItem(path=path, name="novel.txt", ext="txt", size=1234, mtime=56.5)

    assert item.path == path
    assert item.name == "novel.txt"
    assert item.ext == "txt"
    assert item.size == 1234
    assert item.mtime == 56.5


def test_file_item_is_frozen() -> None:
    item = _item("frozen.txt", size=10)

    with pytest.raises(FrozenInstanceError):
        item.size = 20  # type: ignore[misc]


def test_file_item_value_equality() -> None:
    a = _item("same.txt", size=7, mtime=1.5)
    b = _item("same.txt", size=7, mtime=1.5)

    assert a == b


# ------------------------------------
# progress._format_seconds
# ------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "?"),
        (45, "0m 45s"),
        (61, "1m 1s"),
        (3600, "1h 0m"),
        (3700, "1h 1m"),
        (86400, "24h 0m"),  # exactly one day stays out of the "> 1 day" branch
        (90000, "> 1 day"),
    ],
)
def test_format_seconds(seconds: float | None, expected: str) -> None:
    assert _format_seconds(seconds) == expected


# ------------------------------------
# shell._nav_label
# ------------------------------------


@pytest.mark.parametrize("key", _NAV_KEYS)
def test_nav_label_returns_non_empty_string(key: str) -> None:
    label = _nav_label(key)

    assert isinstance(label, str)
    assert label.strip() != ""


def test_nav_label_covers_all_nav_keys() -> None:
    labels = {key: _nav_label(key) for key in _NAV_KEYS}

    assert set(labels) == set(_NAV_KEYS)
    assert all(isinstance(v, str) and v.strip() for v in labels.values())
