#!/usr/bin/env python3
"""
tests.core.test_watchlist
-------------------------

Unit tests for the persistent follow list: CRUD, atomic writes and tolerance
for missing or corrupt files, plus the shared update-check sweep (bounded
concurrency, order preservation, per-book isolation and thread-safe writes).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.core import watchlist
from novel_downloader.apps.core.watchlist import (
    CheckResult,
    WatchlistStore,
    default_watchlist_path,
)
from novel_downloader.plugins import search as search_module


@pytest.fixture
def store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hit(site: str, book_id: str, title: str, latest: str) -> dict[str, str]:
    """A minimal same-site search result for ``_match_result``."""
    return {
        "site": site,
        "book_id": book_id,
        "title": title,
        "latest_chapter": latest,
    }


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


# ---------------------------------------------------------------------------
# check_entry / check_all (shared update-check sweep)
# ---------------------------------------------------------------------------


def _seed_books(store: WatchlistStore, titles: list[str]) -> None:
    for index, title in enumerate(titles, start=1):
        store.add("site", str(index), title=title)


def _patched_search(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(search_module, "search", fake)


def test_check_all_preserves_list_order_and_reports_by_completion(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    _seed_books(store, ["Slow", "Medium", "Quick", "Instant"])
    store.update_latest("site", "3", "Chapter 3 (old)")

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        # Deliberately let the later books answer first.
        delay = {"Slow": 0.12, "Medium": 0.08, "Quick": 0.04, "Instant": 0.0}[keyword]
        await asyncio.sleep(delay)
        return [_hit("site", "", keyword, f"{keyword} chapter")]

    completion: list[str] = []

    def cb(result: CheckResult) -> None:
        completion.append(result.entry.title)

    _patched_search(monkeypatch, fake_search)
    results = asyncio.run(
        watchlist.check_all(store, timeout=1.0, max_concurrency=4, progress_cb=cb)
    )

    # Results come back in follow-list order...
    assert [result.entry.title for result in results] == [
        "Slow",
        "Medium",
        "Quick",
        "Instant",
    ]
    # ...while the progress callback fired in completion order.
    assert completion == ["Instant", "Quick", "Medium", "Slow"]
    assert all(result.error is None for result in results)
    assert all(result.changed for result in results)

    # Every new chapter was persisted.
    assert store.get("site", "1").last_chapter == "Slow chapter"
    assert store.get("site", "4").last_chapter == "Instant chapter"


def test_check_all_marks_unchanged_books(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    store.add("site", "1", title="Same", last_chapter="Chapter 1")
    store.add("site", "2", title="Other", last_chapter="Chapter 1")

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        latest = "Chapter 1" if keyword == "Same" else "Chapter 2"
        return [_hit("site", "", keyword, latest)]

    _patched_search(monkeypatch, fake_search)
    results = asyncio.run(watchlist.check_all(store, timeout=1.0))

    assert [result.changed for result in results] == [False, True]
    assert results[0].found is True
    assert store.get("site", "1").last_chapter == "Chapter 1"
    assert store.get("site", "2").last_chapter == "Chapter 2"


def test_check_all_bounds_concurrent_searches(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    _seed_books(store, ["A", "B", "C", "D", "E", "F"])
    state = {"in_flight": 0, "peak": 0}

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        # All coroutines share one event-loop thread: no lock needed here.
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.02)
        state["in_flight"] -= 1
        return []

    _patched_search(monkeypatch, fake_search)
    results = asyncio.run(watchlist.check_all(store, timeout=1.0, max_concurrency=2))

    assert state["peak"] == 2
    assert len(results) == 6


def test_check_all_single_failure_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    store.add("site", "1", title="Good A")
    store.add("site", "2", title="Broken")
    store.add("site", "3", title="Good B")

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        if keyword == "Broken":
            raise RuntimeError("site down")
        return [_hit("site", "", keyword, "Chapter 9")]

    _patched_search(monkeypatch, fake_search)
    results = asyncio.run(watchlist.check_all(store, timeout=1.0))

    assert [result.entry.title for result in results] == ["Good A", "Broken", "Good B"]
    assert results[1].error == "RuntimeError: site down"
    assert results[0].changed and results[2].changed
    assert store.get("site", "1").last_chapter == "Chapter 9"
    assert store.get("site", "3").last_chapter == "Chapter 9"


def test_check_all_empty_store(store: WatchlistStore) -> None:
    called = False

    def cb(_result: CheckResult) -> None:
        nonlocal called
        called = True

    results = asyncio.run(watchlist.check_all(store, timeout=1.0, progress_cb=cb))
    assert results == []
    assert called is False


def test_check_all_swallows_progress_callback_errors(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    _seed_books(store, ["A", "B"])

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        return [_hit("site", "", keyword, "Chapter 1")]

    def cb(_result: CheckResult) -> None:
        raise RuntimeError("observer bug")

    _patched_search(monkeypatch, fake_search)
    results = asyncio.run(watchlist.check_all(store, timeout=1.0, progress_cb=cb))

    assert [result.entry.title for result in results] == ["A", "B"]
    assert all(result.changed for result in results)


def test_check_all_slow_store_write_does_not_block_the_sweep(
    monkeypatch: pytest.MonkeyPatch, store: WatchlistStore
) -> None:
    """慢盘上的 store 写入（含 fsync）不能阻塞事件循环。

    写入必须丢到 worker 线程：否则一个卡住的 fsync 会把整个 sweep 乃至
    同一循环上的其它请求一起挂住。
    """
    _seed_books(store, ["A", "B", "C"])

    write_started = threading.Event()
    release_write = threading.Event()
    searched: list[str] = []

    async def fake_search(keyword: str, **_kwargs: Any) -> list[dict[str, str]]:
        searched.append(keyword)
        return [_hit("site", "", keyword, f"{keyword} chapter")]

    def slow_update(
        site: str,
        book_id: str,
        last_chapter: str,
        *,
        title: str | None = None,
    ) -> bool:
        write_started.set()
        # 有界等待：阻塞式实现会让测试失败而不是死锁
        release_write.wait(timeout=2.0)
        return True

    _patched_search(monkeypatch, fake_search)
    monkeypatch.setattr(store, "update_latest", slow_update)

    async def scenario() -> list[CheckResult]:
        sweep = asyncio.create_task(
            watchlist.check_all(store, timeout=1.0, max_concurrency=1)
        )
        # 等第一笔（停住的）写入开始
        await asyncio.wait_for(asyncio.to_thread(write_started.wait, 2.0), 3.0)
        # 写入停住期间，剩余搜索必须照常完成（事件循环没被卡住）
        deadline = asyncio.get_running_loop().time() + 0.5
        while len(searched) < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        release_write.set()
        return await asyncio.wait_for(sweep, timeout=3.0)

    results = asyncio.run(scenario())

    assert len(searched) == 3
    assert [result.entry.title for result in results] == ["A", "B", "C"]
    assert all(result.changed for result in results)
    assert all(result.error is None for result in results)


# ---------------------------------------------------------------------------
# Concurrent store mutations (desktop worker threads share one store)
# ---------------------------------------------------------------------------


def test_concurrent_update_latest_keeps_every_entry(store: WatchlistStore) -> None:
    books = 4
    rounds = 5
    for index in range(books):
        store.add("site", str(index), title=f"Book {index}")

    def worker(index: int) -> None:
        for round_ in range(rounds):
            store.update_latest("site", str(index), f"Chapter {index}-{round_}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(books)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = {entry.book_id: entry for entry in store.entries()}
    assert len(entries) == books
    for index in range(books):
        assert entries[str(index)].last_chapter == f"Chapter {index}-{rounds - 1}"


def test_concurrent_update_latest_same_entry_stays_valid(
    store: WatchlistStore,
) -> None:
    store.add("site", "1", title="Contended")

    def worker(tag: int) -> None:
        for _ in range(5):
            store.update_latest("site", "1", f"Chapter {tag}")

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = store.entries()
    assert len(entries) == 1
    # The survivor is one of the written chapters, never a torn/lost document.
    assert entries[0].last_chapter in {f"Chapter {tag}" for tag in range(4)}


def test_concurrent_add_keeps_every_entry(store: WatchlistStore) -> None:
    def adder(tag: int) -> None:
        store.add("site", str(tag), title=f"Book {tag}")

    threads = [threading.Thread(target=adder, args=(tag,)) for tag in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.entries()) == 10


# ---------------------------------------------------------------------------
# Storage hardening: corruption, schema migration, lossless rewrites
# ---------------------------------------------------------------------------


def _backups(store: WatchlistStore) -> list[Path]:
    """Return every quarantine copy next to ``store.path``, oldest name first."""
    return sorted(store.path.parent.glob(f"{store.path.name}.corrupt*"))


def test_invalid_utf8_is_treated_as_corrupt_and_preserved(
    store: WatchlistStore,
) -> None:
    """A byte sequence that is not UTF-8 must not crash a read or be lost."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"\xff\xfe\x00not utf-8"
    store.path.write_bytes(raw)

    assert store.entries() == []
    # Reading is side-effect free: the bad file is neither moved nor rewritten.
    assert store.path.read_bytes() == raw
    assert _backups(store) == []

    entry, created = store.add("b520", "1")
    assert created is True
    assert store.get("b520", "1") == entry
    # The original bytes survive in a quarantine copy before the rewrite.
    assert [path.read_bytes() for path in _backups(store)] == [raw]
    assert _read(store.path)["version"] == watchlist._SCHEMA_VERSION


def test_corrupt_json_is_backed_up_before_it_is_replaced(
    store: WatchlistStore,
) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")

    assert store.entries() == []
    assert _backups(store) == []

    store.add("b520", "1")

    backups = _backups(store)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_deeply_nested_json_does_not_crash_a_read(store: WatchlistStore) -> None:
    """``json.loads`` raises ``RecursionError`` on absurd nesting, not ValueError."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[" * 10_000 + "]" * 10_000, encoding="utf-8")

    assert store.entries() == []

    store.add("b520", "1")
    assert len(_backups(store)) == 1


@pytest.mark.parametrize(
    "document",
    [
        [{"site": "b520", "book_id": "1"}],
        {"books": [{"site": "b520", "book_id": "1"}]},
        {"version": 0, "books": [{"site": "b520", "book_id": "1"}]},
    ],
)
def test_legacy_documents_are_migrated_on_write(
    store: WatchlistStore, document: Any
) -> None:
    """Bare lists and version-less/0 documents are readable and migrate to v1."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")

    assert [entry.key for entry in store.entries()] == [("b520", "1")]

    store.add("b520", "1", title="Title")
    written = _read(store.path)
    assert written["version"] == watchlist._SCHEMA_VERSION
    assert written["books"] == [
        {
            "site": "b520",
            "book_id": "1",
            "title": "Title",
            "last_chapter": "",
            "updated_at": store.get("b520", "1").updated_at,
        }
    ]
    # A known, older document is migrated in place; no backup is needed.
    assert _backups(store) == []


def test_newer_schema_is_read_but_preserved_before_a_downgrade(
    store: WatchlistStore,
) -> None:
    """A file from a newer version stays readable and is never silently downgraded."""
    original = {
        "version": watchlist._SCHEMA_VERSION + 1,
        "books": [{"site": "b520", "book_id": "1", "title": "From the future"}],
        "future": {"note": "unknown to this version"},
    }
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(original), encoding="utf-8")

    entries = store.entries()
    assert [entry.title for entry in entries] == ["From the future"]

    store.add("b520", "2")
    backups = _backups(store)
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original
    assert _read(store.path)["version"] == watchlist._SCHEMA_VERSION


@pytest.mark.parametrize("version", ["1", 1.0, -1, True, {"v": 1}])
def test_unusable_schema_version_makes_the_document_unusable(
    store: WatchlistStore, version: Any
) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"version": version, "books": [{"site": "b520", "book_id": "1"}]}),
        encoding="utf-8",
    )

    assert store.entries() == []

    store.add("b520", "2")
    assert len(_backups(store)) == 1
    assert [entry.book_id for entry in store.entries()] == ["2"]


def test_rewrite_preserves_entries_it_cannot_understand(store: WatchlistStore) -> None:
    """Dropping invalid entries on read must never delete their bytes on write."""
    original = {
        "version": 1,
        "books": [
            {"site": "b520", "book_id": "1", "title": "Good"},
            {"site": "b520", "title": "No id"},
            "not-an-object",
        ],
    }
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(original), encoding="utf-8")

    assert [entry.key for entry in store.entries()] == [("b520", "1")]

    store.add("b520", "2")
    backups = _backups(store)
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original
    assert [entry.book_id for entry in store.entries()] == ["1", "2"]


def test_remove_also_preserves_invalid_entries(store: WatchlistStore) -> None:
    original = {
        "version": 1,
        "books": [
            {"site": "b520", "book_id": "1"},
            {"site": "b520", "book_id": ""},
        ],
    }
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(original), encoding="utf-8")

    assert store.remove("b520", "1") is True

    backups = _backups(store)
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original
    assert store.entries() == []


def test_an_existing_backup_is_never_overwritten(store: WatchlistStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    existing = store.path.with_name(f"{store.path.name}.corrupt")
    existing.write_bytes(b"older backup")
    store.path.write_text("{broken", encoding="utf-8")

    store.add("b520", "1")

    assert existing.read_bytes() == b"older backup"
    backups = _backups(store)
    assert len(backups) == 2
    assert any(path.read_bytes() == b"{broken" for path in backups)


def test_entry_fields_with_wrong_types_are_repaired_or_dropped(
    store: WatchlistStore,
) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "books": [
                    {
                        "site": "b520",
                        "book_id": 123,
                        "title": {"nested": 1},
                        "last_chapter": ["chapter"],
                        "updated_at": True,
                    },
                    {"site": "b520", "book_id": 2, "title": 42},
                    {"site": True, "book_id": "3"},
                    {"site": "b520", "book_id": ["4"]},
                    {"site": "b520", "book_id": float("nan")},
                ]
            }
        ),
        encoding="utf-8",
    )

    entries = store.entries()
    assert [(entry.site, entry.book_id) for entry in entries] == [
        ("b520", "123"),
        ("b520", "2"),
    ]
    assert entries[0].title == ""
    assert entries[0].last_chapter == ""
    assert entries[0].updated_at == ""
    assert entries[1].title == "42"


def test_unreadable_file_is_treated_as_empty(
    store: WatchlistStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]", encoding="utf-8")

    def deny(*_args: Any, **_kwargs: Any) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", deny)
    assert store.entries() == []


def test_write_aborts_when_the_existing_file_cannot_be_inspected(
    store: WatchlistStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never replace bytes that could not be read first (a sharing violation)."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]", encoding="utf-8")

    def deny(*_args: Any, **_kwargs: Any) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(PermissionError, match="denied"):
        store.add("b520", "1")
    assert store.path.read_text(encoding="utf-8") == "[]"


def test_write_aborts_when_the_original_cannot_be_preserved(
    store: WatchlistStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{broken", encoding="utf-8")

    def boom(_data: bytes) -> Path:
        raise OSError("read-only")

    monkeypatch.setattr(store, "_quarantine", boom)
    with pytest.raises(OSError, match="read-only"):
        store.add("b520", "1")
    assert store.path.read_text(encoding="utf-8") == "{broken"


# ---------------------------------------------------------------------------
# Concurrency across store instances (the desktop builds one per operation)
# ---------------------------------------------------------------------------


def test_all_store_instances_for_one_path_share_a_lock(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    assert WatchlistStore(path)._lock is WatchlistStore(path)._lock


def test_concurrent_add_through_separate_instances_keeps_every_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Two ``WatchlistStore`` objects aimed at the same file serialize their
    read-modify-write cycles; otherwise the later writer overwrites the other.
    """
    path = tmp_path / "watchlist.json"
    slow = WatchlistStore(path)
    fast = WatchlistStore(path)
    slow_read = threading.Event()
    release_slow = threading.Event()
    fast_done = threading.Event()
    original_read = WatchlistStore._read

    def phased_read(self: WatchlistStore) -> list[Any]:
        entries = original_read(self)
        if threading.current_thread().name == "slow-adder":
            slow_read.set()
            assert release_slow.wait(2.0)
        return entries

    monkeypatch.setattr(WatchlistStore, "_read", phased_read)

    def slow_add() -> None:
        slow.add("site", "slow")

    def fast_add() -> None:
        fast.add("site", "fast")
        fast_done.set()

    slow_thread = threading.Thread(target=slow_add, name="slow-adder")
    slow_thread.start()
    assert slow_read.wait(2.0)

    fast_thread = threading.Thread(target=fast_add, name="fast-adder")
    fast_thread.start()
    # With a shared lock this times out (the fast writer is blocked); without
    # one it completes and is later overwritten by the slow writer.
    fast_done.wait(0.5)
    release_slow.set()
    slow_thread.join(5.0)
    fast_thread.join(5.0)

    assert not slow_thread.is_alive()
    assert not fast_thread.is_alive()
    assert {entry.book_id for entry in slow.entries()} == {"slow", "fast"}
