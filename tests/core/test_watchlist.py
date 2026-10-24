#!/usr/bin/env python3
"""
tests.core.test_watchlist
-------------------------

Unit tests for the persistent follow list: CRUD, atomic writes and tolerance
for missing or corrupt files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_downloader.apps.core import watchlist
from novel_downloader.apps.core.watchlist import (
    WatchlistStore,
    default_watchlist_path,
)


@pytest.fixture
def store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_add_get_list_and_remove(store: WatchlistStore) -> None:
    entry, created = store.add("b520", "8_8187", title="Book A")
    assert created is True
    assert entry.site == "b520"
    assert entry.book_id == "8_8187"
    assert entry.title == "Book A"
    assert entry.last_chapter == ""
    assert entry.updated_at

    assert store.get("b520", "8_8187") == entry
    assert [item.key for item in store.entries()] == [("b520", "8_8187")]

    # Adding again updates instead of duplicating.
    updated, created = store.add("b520", "8_8187", title="Book A (new)")
    assert created is False
    assert updated.title == "Book A (new)"
    assert len(store.entries()) == 1

    assert store.remove("b520", "8_8187") is True
    assert store.entries() == []
    # Removal is idempotent.
    assert store.remove("b520", "8_8187") is False


def test_add_preserves_existing_optional_fields(store: WatchlistStore) -> None:
    store.add("b520", "1", title="Title", last_chapter="Chapter 1")

    entry, _ = store.add("b520", "1")
    assert entry.title == "Title"
    assert entry.last_chapter == "Chapter 1"


def test_add_rejects_empty_identity(store: WatchlistStore) -> None:
    with pytest.raises(ValueError):
        store.add("  ", "1")


def test_update_latest_only_writes_on_change(store: WatchlistStore) -> None:
    store.add("b520", "1", title="Title", last_chapter="Chapter 1")

    assert store.update_latest("b520", "1", "Chapter 1") is False
    assert store.update_latest("b520", "1", "") is False
    assert store.update_latest("b520", "unknown", "Chapter 2") is False

    assert store.update_latest("b520", "1", "Chapter 2") is True
    entry = store.get("b520", "1")
    assert entry is not None
    assert entry.last_chapter == "Chapter 2"


def test_update_latest_fills_missing_title(store: WatchlistStore) -> None:
    store.add("b520", "1")
    assert store.update_latest("b520", "1", "Chapter 2", title="Found title")
    entry = store.get("b520", "1")
    assert entry is not None
    assert entry.title == "Found title"


def test_file_format_and_no_temporary_files_left(store: WatchlistStore) -> None:
    store.add("b520", "1", title="T")
    document = _read(store.path)
    assert document["version"] == watchlist._SCHEMA_VERSION
    assert isinstance(document["books"], list)
    assert document["books"][0]["site"] == "b520"

    leftovers = [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_failure_keeps_previous_document(
    store: WatchlistStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add("b520", "1", title="First")
    before = store.path.read_text(encoding="utf-8")

    def boom(_src: str, _dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(watchlist.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.add("b520", "2", title="Second")

    assert store.path.read_text(encoding="utf-8") == before
    leftovers = [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_missing_file_is_empty(store: WatchlistStore) -> None:
    assert store.entries() == []


def test_corrupt_file_is_tolerated_and_replaced(store: WatchlistStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")

    assert store.entries() == []

    entry, created = store.add("b520", "1")
    assert created is True
    assert entry.book_id == "1"
    document = _read(store.path)
    assert document["books"][0]["site"] == "b520"


def test_unusable_and_duplicate_entries_are_skipped(store: WatchlistStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "books": [
                    {"site": "b520", "book_id": "1"},
                    {"site": "b520", "book_id": "1", "title": "duplicate"},
                    "not-an-object",
                    {"site": "", "book_id": "2"},
                    {"site": "b520"},
                ]
            }
        ),
        encoding="utf-8",
    )

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].key == ("b520", "1")
    assert entries[0].title == ""


def test_bare_list_document_is_accepted(store: WatchlistStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps([{"site": "b520", "book_id": "1", "title": "T"}]),
        encoding="utf-8",
    )

    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].title == "T"


def test_default_path_honours_novel_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path))
    assert default_watchlist_path() == tmp_path / "watchlist.json"
    assert WatchlistStore().path == tmp_path / "watchlist.json"
